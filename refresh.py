"""Daily refresh script — runs in GitHub Actions.

Fetches short-interest and borrow-fee data from three public sources that work
headlessly (no browser required, no API keys):

  1. IBorrowDesk /api/stocks          — Interactive Brokers borrow fees
  2. HighShortInterest.com            — FINRA short-interest %float
  3. StockAnalysis.com most-shorted   — cross-check on SI% and price

Merges by ticker, recomputes lending scores + APY estimates, writes data.json.
Includes a fail-safe: refuses to overwrite data.json if the fresh dataset is
suspiciously small (protects against a source outage wiping the dashboard).
"""
import json, datetime, re, sys, time, os
import requests
from bs4 import BeautifulSoup

MIN_FRESH_ROWS = 40
FPL_SPLIT = 0.50

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def fetch(url, retries=3, json_mode=False):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.json() if json_mode else r.text
            print(f"  [{r.status_code}] {url}", file=sys.stderr)
        except Exception as e:
            print(f"  [err] {url}: {e}", file=sys.stderr)
        time.sleep(2 * (i + 1))
    return None

# --------------------- Source parsers ---------------------

def parse_iborrowdesk():
    """IBorrowDesk JSON API — IBKR borrow fees, cleanest source."""
    data = fetch("https://www.iborrowdesk.com/api/stocks", json_mode=True)
    if not data or "data" not in data:
        return []
    rows = []
    for item in data["data"]:
        # US-listed only (skip .HK, .JP, etc.)
        if item.get("country") != "usa":
            continue
        sym = item.get("symbol", "")
        if not re.match(r"^[A-Z]{1,5}$", sym):
            continue
        fee = item.get("latest_fee")
        if fee is None:
            continue
        rows.append({
            "ticker": sym,
            "company": (item.get("name") or "").title()[:60],
            "borrow_fee_pct": float(fee),
            "source": "IBorrowDesk (IBKR)",
        })
    return rows

def parse_high_short_interest():
    """HighShortInterest.com — table of SI% of float."""
    html = fetch("https://www.highshortinterest.com/")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table.stocks tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        # First cell has an <a>TICKER</a>
        a = tds[0].find("a")
        if not a:
            continue
        tk = a.get_text(strip=True)
        if not re.match(r"^[A-Z]{1,5}$", tk):
            continue
        try:
            company = tds[1].get_text(strip=True)
            si_txt = tds[3].get_text(strip=True).replace("%", "")
            si = float(si_txt) if si_txt else None

            def parse_shares(txt):
                txt = txt.replace(",", "").strip()
                m = re.match(r"([\d.]+)\s*([MK]?)", txt)
                if not m: return None
                v = float(m.group(1))
                return v if m.group(2) == "M" else (v / 1000 if m.group(2) == "K" else v / 1e6)

            float_m = parse_shares(tds[4].get_text(strip=True))
            shares_short_m = (si / 100 * float_m) if (si and float_m) else None

            rows.append({
                "ticker": tk,
                "company": company[:60],
                "si_pct_float": si,
                "shares_short_m": shares_short_m,
                "source": "HighShortInterest",
            })
        except Exception:
            continue
    return rows

def parse_stockanalysis():
    """StockAnalysis.com — cross-check on SI% and price."""
    html = fetch("https://stockanalysis.com/list/most-shorted-stocks/")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    # Table cells arrive in groups of 6: rank, company, SI%, price, chg%, volume
    # But we need the ticker, which is in the row header/link nearby. Look at rows:
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        # Find ticker in a link or bold text within the row
        link = tr.find("a", href=re.compile(r"/stocks/[a-z0-9]+/"))
        if not link:
            continue
        m = re.search(r"/stocks/([a-z0-9]+)/", link.get("href", ""))
        if not m:
            continue
        tk = m.group(1).upper()
        if not re.match(r"^[A-Z]{1,5}$", tk):
            continue
        try:
            texts = [t.get_text(strip=True) for t in tds]
            si = None
            price = None
            for t in texts:
                if "%" in t and si is None:
                    v = float(re.sub(r"[^\d.]", "", t))
                    if 5 < v < 200:  # plausible SI%
                        si = v
                elif re.match(r"^[\d.]+$", t) and price is None and 0.01 < float(t) < 10000:
                    price = float(t)
            if si is None:
                continue
            company = next((t.get_text(strip=True) for t in tds
                           if len(t.get_text(strip=True)) > 5 and "%" not in t.get_text() and
                           not re.match(r"^[\d.,]+$", t.get_text(strip=True))), "")
            rows.append({
                "ticker": tk,
                "company": company[:60] if company else None,
                "si_pct_float": si,
                "price": price,
                "source": "StockAnalysis",
            })
        except Exception:
            continue
    return rows

# --------------------- Score + merge ---------------------

def fee_tier(fee):
    if fee is None: return "Unknown"
    if fee >= 100: return "Extreme HTB"
    if fee >= 25:  return "Hard-to-borrow"
    if fee >= 10:  return "Elevated"
    if fee >= 3:   return "Warm"
    if fee >= 1:   return "General collateral+"
    return "General collateral"

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
    merged, sources = {}, {}
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
    for name, fn in [
        ("IBorrowDesk",        parse_iborrowdesk),
        ("HighShortInterest",  parse_high_short_interest),
        ("StockAnalysis",      parse_stockanalysis),
    ]:
        try:
            rows = fn()
            print(f"  {name}: {len(rows)} rows")
            all_rows.extend(rows)
        except Exception as e:
            print(f"  {name}: FAILED — {e}", file=sys.stderr)

    if len(all_rows) < MIN_FRESH_ROWS:
        print(f"ERROR: only {len(all_rows)} rows fetched (min {MIN_FRESH_ROWS}) — "
              f"preserving last-known-good data.json", file=sys.stderr)
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

    # Sanity check vs previous baseline
    if os.path.exists("data.json"):
        try:
            with open("data.json") as f:
                prev = json.load(f)
            prev_n = prev.get("row_count", 0)
            if prev_n and len(records) < prev_n * 0.4:
                print(f"ERROR: new dataset has {len(records)} rows vs {prev_n} baseline — "
                      f"preserving previous data.json", file=sys.stderr)
                sys.exit(1)
        except Exception:
            pass

    out = {
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
        "as_of": datetime.date.today().isoformat(),
        "fpl_split_assumption": FPL_SPLIT,
        "row_count": len(records),
        "rows": records,
    }
    with open("data.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {len(records)} tickers to data.json")

if __name__ == "__main__":
    main()
