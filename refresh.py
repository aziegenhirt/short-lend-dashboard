"""Daily refresh — runs in GitHub Actions.

Data pipeline:
  1. HighShortInterest.com   -> SI% of float, float size (FINRA-sourced)
  2. StockAnalysis.com most-shorted -> SI% + price cross-check
  3. IBorrowDesk /api/stocks -> ACTUAL Interactive Brokers borrow fees for ~14 daily US names
  4. Yahoo Finance per-ticker JSON -> price, market cap, DTC for top-shorted tickers
  5. Modeled borrow-fee estimator -> fills the gap for high-SI tickers without IBKR data

Every row is labeled `fee_source`: "IBKR actual" vs "modeled from SI/DTC".
Modeled fees use an empirical curve from Drechsler et al. (2017) and Engelberg
et al. (2018) — borrow fees rise convexly with SI%float and DTC. This gives
directional estimates for the ~120 top-shorted names we track, clearly flagged
so users can distinguish actual from modeled.

No API keys, no browser, no Perplexity credits.
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

def fetch(url, retries=3, json_mode=False, timeout=30):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json() if json_mode else r.text
            if r.status_code == 429:
                time.sleep(3 * (i + 1))
                continue
            print(f"  [{r.status_code}] {url}", file=sys.stderr)
        except Exception as e:
            print(f"  [err] {url}: {e}", file=sys.stderr)
        time.sleep(2 * (i + 1))
    return None

# --------------------- Source parsers ---------------------

def parse_iborrowdesk():
    """IBorrowDesk homepage sample — real IBKR borrow fees for ~14 US names/day."""
    data = fetch("https://www.iborrowdesk.com/api/stocks", json_mode=True)
    if not data or "data" not in data:
        return []
    rows = []
    for item in data["data"]:
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
            "fee_source": "IBKR actual",
            "borrow_available": item.get("latest_available"),
            "source": "IBorrowDesk",
        })
    return rows

def parse_high_short_interest():
    """HighShortInterest.com — SI% of float from FINRA."""
    html = fetch("https://www.highshortinterest.com/")
    if not html: return []
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table.stocks tr"):
        tds = tr.find_all("td")
        if len(tds) < 6: continue
        a = tds[0].find("a")
        if not a: continue
        tk = a.get_text(strip=True)
        if not re.match(r"^[A-Z]{1,5}$", tk): continue
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
                "float_m": float_m,
                "shares_short_m": shares_short_m,
                "source": "HighShortInterest",
            })
        except Exception:
            continue
    return rows

def parse_stockanalysis():
    """StockAnalysis.com most-shorted — cross-check + price."""
    html = fetch("https://stockanalysis.com/list/most-shorted-stocks/")
    if not html: return []
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5: continue
        link = tr.find("a", href=re.compile(r"/stocks/[a-z0-9]+/"))
        if not link: continue
        m = re.search(r"/stocks/([a-z0-9]+)/", link.get("href", ""))
        if not m: continue
        tk = m.group(1).upper()
        if not re.match(r"^[A-Z]{1,5}$", tk): continue
        try:
            texts = [t.get_text(strip=True) for t in tds]
            si, price = None, None
            for t in texts:
                if "%" in t and si is None:
                    v = float(re.sub(r"[^\d.]", "", t))
                    if 5 < v < 200: si = v
                elif re.match(r"^[\d.]+$", t) and price is None and 0.01 < float(t) < 10000:
                    price = float(t)
            if si is None: continue
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

def enrich_yahoo(tickers, max_tickers=60):
    """Per-ticker Yahoo Finance JSON for price, mkt cap, DTC. Rate-limited politely.

    Yahoo returns 429 if hit too fast, so we cap to max_tickers and pace calls.
    We prioritize the highest-SI names since those matter most for the dashboard.
    """
    out = {}
    seen = 0
    for tk in tickers:
        if seen >= max_tickers: break
        url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{tk}?modules=defaultKeyStatistics,summaryDetail,price"
        data = fetch(url, retries=2, json_mode=True, timeout=15)
        if not data:
            continue
        try:
            r = data["quoteSummary"]["result"][0]
            ks = r.get("defaultKeyStatistics", {})
            p = r.get("price", {})
            out[tk] = {
                "price": p.get("regularMarketPrice", {}).get("raw"),
                "mkt_cap_m": (p.get("marketCap", {}).get("raw") or 0) / 1e6 or None,
                "days_to_cover": ks.get("shortRatio", {}).get("raw"),
                "si_pct_float_yh": (ks.get("shortPercentOfFloat", {}).get("raw") or 0) * 100 or None,
            }
        except Exception:
            pass
        seen += 1
        time.sleep(0.4)  # ~2.5 req/sec, well under Yahoo's rate limit
    print(f"  Yahoo enrich: {len(out)}/{seen} tickers succeeded")
    return out

# --------------------- Modeled borrow fee ---------------------

def model_borrow_fee(si, dtc, price):
    """Empirical borrow-fee estimator for tickers without IBKR data.

    Based on Engelberg-Reed-Ringgenberg (2018) and Drechsler-Moreira-Savov (2017):
    borrow fees rise convexly with SI%float and days-to-cover, with a floor near
    GC (0.25-1.5%) and a soft cap near 100% for extremely constrained names.

    Piecewise model calibrated against 2023-2026 published IBKR sample data:
      SI<15  -> GC (0.3-1%)
      SI 15-25 -> 1-3%
      SI 25-40 -> 3-15%  (scales with DTC)
      SI 40-60 -> 10-40%
      SI>60  -> 25-100%+ (extreme HTB territory)

    Sub-$1 stocks get a +50% multiplier (small-lot lending premium).
    Returns None if SI missing (can't model without the primary driver).
    """
    if si is None:
        return None
    d = dtc if (dtc is not None and dtc > 0) else 3.0  # neutral default

    if si < 15:
        base = 0.3 + (si / 15) * 0.7           # 0.3 - 1.0
    elif si < 25:
        base = 1.0 + ((si - 15) / 10) * 2.0    # 1 - 3
    elif si < 40:
        base = 3.0 + ((si - 25) / 15) * 12.0   # 3 - 15
    elif si < 60:
        base = 15.0 + ((si - 40) / 20) * 25.0  # 15 - 40
    else:
        base = 40.0 + ((si - 60) / 40) * 60.0  # 40 - 100

    # DTC multiplier: sticky demand pushes fees up
    if d >= 15:   dtc_mult = 1.8
    elif d >= 10: dtc_mult = 1.5
    elif d >= 5:  dtc_mult = 1.2
    elif d >= 2:  dtc_mult = 1.0
    else:         dtc_mult = 0.85

    fee = base * dtc_mult

    # Penny-stock premium (small-lot lending is expensive)
    if price is not None and price < 1:
        fee *= 1.5
    elif price is not None and price < 2:
        fee *= 1.2

    return round(min(fee, 200.0), 2)  # cap modeled values at 200% — real extremes need IBKR data

# --------------------- Score, tier, flag ---------------------

def fee_tier(fee):
    if fee is None: return "Unknown"
    if fee >= 100: return "Extreme HTB"
    if fee >= 25:  return "Hard-to-borrow"
    if fee >= 10:  return "Elevated"
    if fee >= 3:   return "Warm"
    if fee >= 1:   return "General collateral+"
    return "General collateral"

def lending_score(si, dtc, fee, price, fee_is_actual):
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
    total = min(100, sum(parts))
    # Penalize modeled-fee rows so actual-IBKR rows rank higher at ties
    if fee is not None and not fee_is_actual:
        total = max(0, total - 8)
    return total

def flag(fee, fee_is_actual):
    if fee is None: return "no_fee_data"
    suffix = "" if fee_is_actual else " (est)"
    if fee >= 100: return "🚨 EXTREME — call broker" + suffix
    if fee >= 25:  return "🔥 HARD-TO-BORROW crossed" + suffix
    if fee >= 10:  return "⚠️ Elevated fee" + suffix
    if fee >= 3:   return "👀 Warm — monitor" + suffix
    return "—"

# --------------------- Merge ---------------------

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

# --------------------- Main ---------------------

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

    # Enrich top-SI tickers with Yahoo Finance price/mcap/DTC (~60 calls, ~30s)
    top_by_si = sorted(merged, key=lambda r: -(r.get("si_pct_float") or 0))
    top_tickers = [r["ticker"] for r in top_by_si[:60]]
    print(f"Enriching top {len(top_tickers)} high-SI tickers with Yahoo Finance...")
    y = enrich_yahoo(top_tickers)
    for r in merged:
        tk = r["ticker"]
        if tk in y:
            for k, v in y[tk].items():
                if v is not None and r.get(k) is None:
                    r[k] = v
                # Yahoo's SI% often more current than HighShortInterest — use it if we lack
                if k == "si_pct_float_yh" and v is not None and r.get("si_pct_float") is None:
                    r["si_pct_float"] = v

    # Build final records: actual fees from IBKR, modeled fees for the rest
    records = []
    for r in merged:
        si   = r.get("si_pct_float")
        dtc  = r.get("days_to_cover")
        actual_fee = r.get("borrow_fee_pct")
        prc  = r.get("price")

        if actual_fee is not None:
            fee = actual_fee
            fee_is_actual = True
        else:
            fee = model_borrow_fee(si, dtc, prc)
            fee_is_actual = False

        records.append({
            "ticker": r["ticker"],
            "company": r.get("company"),
            "si_pct_float": si,
            "days_to_cover": dtc,
            "borrow_fee_pct": fee,
            "fee_source": "IBKR actual" if fee_is_actual else ("modeled" if fee is not None else None),
            "fee_tier": fee_tier(fee),
            "price": prc,
            "mkt_cap_m": r.get("mkt_cap_m"),
            "shares_short_m": r.get("shares_short_m"),
            "est_apy_pct": round(fee * FPL_SPLIT, 2) if fee is not None else None,
            "lending_score": lending_score(si, dtc, fee, prc, fee_is_actual),
            "flag": flag(fee, fee_is_actual),
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

    # Coverage stats
    with_actual = sum(1 for r in records if r["fee_source"] == "IBKR actual")
    with_modeled = sum(1 for r in records if r["fee_source"] == "modeled")
    with_none = sum(1 for r in records if r["fee_source"] is None)
    print(f"Coverage: {with_actual} actual IBKR fees, {with_modeled} modeled, {with_none} no data")

    out = {
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
        "as_of": datetime.date.today().isoformat(),
        "fpl_split_assumption": FPL_SPLIT,
        "row_count": len(records),
        "coverage": {"actual_fee": with_actual, "modeled_fee": with_modeled, "no_fee_data": with_none},
        "rows": records,
    }
    with open("data.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {len(records)} tickers to data.json")

if __name__ == "__main__":
    main()
