"""Daily refresh script — runs in GitHub Actions.

Fetches current short-interest and borrow-fee data from public sources,
recomputes lending scores and APY estimates, and writes data.json.

Zero dependencies beyond `requests` and `beautifulsoup4` (installed in the
workflow). No Perplexity credits consumed.
"""
import json, datetime, re, sys, time, os
import requests
from bs4 import BeautifulSoup

# Minimum acceptable freshly-fetched row count. If we get fewer than this,
# something is wrong upstream and we KEEP the existing data.json untouched.
MIN_FRESH_ROWS = 40

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

def fetch(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text
            print(f"  [{r.status_code}] {url}", file=sys.stderr)
        except Exception as e:
            print(f"  [err] {url}: {e}", file=sys.stderr)
        time.sleep(2 * (i + 1))
    return None

# --------------------- Source parsers ---------------------
# Each returns a list of dicts with any of:
# ticker, company, si_pct_float, days_to_cover, borrow_fee_pct, price, mkt_cap_m, shares_short_m

def parse_iborrowdesk():
    """IBorrowDesk homepage — top IBKR borrow fees.
    Page structure: each stock is a row with symbol, company, fee%, available shares."""
    html = fetch("https://www.iborrowdesk.com/")
    if not html: return []
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    # Try multiple selectors — the page has changed layout historically
    for tr in soup.select("tr, .stock-row, .row"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "div", "span"], recursive=False)]
        if len(tds) < 2:
            tds = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "div", "span"])]
        if len(tds) < 3: continue
        m = re.match(r"^([A-Z]{1,6})$", tds[0])
        if not m: continue
        # Find a percent-like value anywhere in this row
        fee = None
        for cell in tds[1:]:
            fm = re.search(r"([\d]+\.?\d*)\s*%", cell)
            if fm:
                v = float(fm.group(1))
                if 0.1 < v < 10000:
                    fee = v
                    break
        if fee is None: continue
        company = tds[1] if len(tds) > 1 and not re.match(r"[\d.]+%", tds[1]) else None
        rows.append({
            "ticker": m.group(1),
            "company": company[:60] if company else None,
            "borrow_fee_pct": fee,
            "source": "IBorrowDesk (IBKR)",
        })
    return rows[:80]

def parse_marketwatch():
    """MarketWatch most-shorted screener."""
    html = fetch("https://www.marketwatch.com/tools/screener/short-interest")
    if not html: return []
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) < 8: continue
        tk = tds[0].strip()
        if not re.match(r"^[A-Z]{1,5}$", tk): continue
        try:
            price = float(re.sub(r"[^\d.]", "", tds[2])) if tds[2] else None
            short_shares = int(re.sub(r"[^\d]", "", tds[6])) if tds[6] else None
            float_shares = int(re.sub(r"[^\d]", "", tds[7])) if tds[7] else None
            si_pct = float(re.sub(r"[^\d.]", "", tds[8])) if len(tds) > 8 and tds[8] else None
            rows.append({
                "ticker": tk,
                "company": tds[1][:60],
                "price": price,
                "shares_short_m": short_shares / 1e6 if short_shares else None,
                "si_pct_float": si_pct,
                "source": "MarketWatch",
            })
        except Exception:
            continue
    return rows[:80]

def parse_desperate_trader():
    """TheDesperateTrader — SI% of float leaderboard."""
    html = fetch("https://thedesperatetrader.com/betting-against")
    if not html: return []
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(tds) < 5: continue
        # First col: "TICKER Company"
        parts = tds[0].split(" ", 1)
        if len(parts) < 2 or not re.match(r"^[A-Z]{1,5}$", parts[0]): continue
        tk = parts[0]
        try:
            si = float(re.sub(r"[^\d.]", "", tds[1]))
            ss_txt = tds[2].replace(",", "")
            ss_m = None
            if "M" in ss_txt: ss_m = float(re.sub(r"[^\d.]", "", ss_txt))
            elif "K" in ss_txt: ss_m = float(re.sub(r"[^\d.]", "", ss_txt)) / 1000
            dtc = float(re.sub(r"[^\d.]", "", tds[3])) if tds[3] else None
            price = float(re.sub(r"[^\d.]", "", tds[5])) if len(tds) > 5 and "$" in tds[5] else None
            rows.append({
                "ticker": tk, "company": parts[1][:60],
                "si_pct_float": si, "shares_short_m": ss_m,
                "days_to_cover": dtc, "price": price,
                "source": "TheDesperateTrader",
            })
        except Exception:
            continue
    return rows[:60]

# --------------------- Merge & score ---------------------

def fee_tier(fee):
    if fee is None: return "Unknown"
    if fee >= 100: return "Extreme HTB"
    if fee >= 25:  return "Hard-to-borrow"
    if fee >= 10:  return "Elevated"
    if fee >= 3:   return "Warm"
    if fee >= 1:   return "General collateral+"
    return "General collateral"

FPL_SPLIT = 0.50

def lending_score(si, dtc, fee, price):
    parts = []
    if fee is not None:
        parts.append(40 if fee >= 100 else 35 if fee >= 25 else 28 if fee >= 10
                    else 20 if fee >= 5 else 12 if fee >= 1 else 5)
    else: parts.append(0)
    if si is not None:
        parts.append(25 if si >= 50 else 20 if si >= 30 else 13 if si >= 15
                    else 7 if si >= 5 else 3)
    else: parts.append(0)
    if dtc is not None:
        parts.append(15 if dtc >= 10 else 11 if dtc >= 5 else 7 if dtc >= 2 else 3)
    else: parts.append(0)
    if price is not None:
        parts.append(10 if price >= 5 else 7 if price >= 2 else 4 if price >= 1 else 0)
    else: parts.append(5)
    completeness = sum(x is not None for x in (si, dtc, fee, price))
    parts.append(int(completeness * 2.5))
    return min(100, sum(parts))

def flag(fee):
    if fee is None: return "no_fee_data"
    if fee >= 100: return "🚨 EXTREME — call broker"
    if fee >= 25:  return "🔥 HARD-TO-BORROW crossed"
    if fee >= 10:  return "⚠️ Elevated fee"
    if fee >= 3:   return "👀 Warm — monitor"
    return "—"

def merge(all_rows):
    """Merge by ticker — union of fields, preferring non-null values."""
    merged = {}
    sources = {}
    for r in all_rows:
        tk = r["ticker"]
        src = r.pop("source", "")
        if tk not in merged:
            merged[tk] = {k: v for k, v in r.items() if v is not None}
            sources[tk] = {src}
        else:
            for k, v in r.items():
                if v is not None and merged[tk].get(k) is None:
                    merged[tk][k] = v
            sources[tk].add(src)
    for tk in merged:
        merged[tk]["sources"] = " + ".join(sorted(sources[tk]))
    return list(merged.values())

def main():
    print("Fetching sources...")
    all_rows = []
    for name, fn in [("IBorrowDesk", parse_iborrowdesk),
                      ("MarketWatch", parse_marketwatch),
                      ("TheDesperateTrader", parse_desperate_trader)]:
        try:
            rows = fn()
            print(f"  {name}: {len(rows)} rows")
            all_rows.extend(rows)
        except Exception as e:
            print(f"  {name}: FAILED — {e}", file=sys.stderr)

    if len(all_rows) < MIN_FRESH_ROWS:
        print(f"ERROR: only {len(all_rows)} rows fetched (min {MIN_FRESH_ROWS}) — "
              f"aborting to preserve last-known-good data.json", file=sys.stderr)
        sys.exit(1)

    merged = merge(all_rows)
    records = []
    for r in merged:
        si   = r.get("si_pct_float")
        dtc  = r.get("days_to_cover")
        fee  = r.get("borrow_fee_pct")
        prc  = r.get("price")
        records.append({
            "ticker": r["ticker"],
            "company": r.get("company"),
            "si_pct_float": si,
            "days_to_cover": dtc,
            "borrow_fee_pct": fee,
            "fee_tier": fee_tier(fee),
            "price": prc,
            "mkt_cap_m": r.get("mkt_cap_m"),
            "shares_short_m": r.get("shares_short_m"),
            "est_apy_pct": round(fee * FPL_SPLIT, 2) if fee is not None else None,
            "lending_score": lending_score(si, dtc, fee, prc),
            "flag": flag(fee),
            "sources": r.get("sources", ""),
        })
    records.sort(key=lambda x: (-x["lending_score"], -(x["borrow_fee_pct"] or 0)))

    out = {
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
        "as_of": datetime.date.today().isoformat(),
        "fpl_split_assumption": FPL_SPLIT,
        "row_count": len(records),
        "rows": records,
    }
    # Sanity check against previous baseline — refuse to shrink dramatically
    if os.path.exists("data.json"):
        try:
            with open("data.json") as f:
                prev = json.load(f)
            prev_n = prev.get("row_count", 0)
            if prev_n and len(records) < prev_n * 0.4:
                print(f"ERROR: new dataset has {len(records)} rows vs {prev_n} baseline — "
                      f"refusing to overwrite. Sources may be blocked.", file=sys.stderr)
                sys.exit(1)
        except Exception:
            pass

    with open("data.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {len(records)} tickers to data.json")

if __name__ == "__main__":
    main()
