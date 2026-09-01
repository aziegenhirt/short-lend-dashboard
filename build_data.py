"""Build consolidated dataset for the lending-probability dashboard.

Merges short interest / CTB / utilization signals from multiple public sources
and computes:
  - lending_score (0-100): probability a broker's Fully-Paid Lending program
    will actually place & pay on the shares
  - est_apy_pct: expected annual yield to lender assuming 50% split of the
    prevailing borrow fee (typical Fidelity/Schwab FPL economics)
  - flag: daily monitor flag when borrow fee crosses critical thresholds
"""
import json, datetime

# === Raw merged data (sourced from MarketWatch, Tapeboard, TheDesperateTrader,
# TheTrading.tools, IBorrowDesk, Yahoo Finance, all pulled Sep 1, 2026) ===
# Fields: ticker, company, si_pct_float, days_to_cover, borrow_fee_pct,
# price, mkt_cap_m, shares_short_m, source_notes
RAW = [
    # Extreme-fee hard-to-borrow names (IBorrowDesk / Tapeboard borrow-fee lists)
    ("BTCT","BTC Digital Ltd",             None, None, 953.1, None,   None, None, "IBKR HTB"),
    ("TC",  "Token Cat Ltd",               None, None, 943.0, None,   None, None, "IBKR HTB"),
    ("NCPL","Netcapital Inc",              None, None, 924.2, None,   None, None, "IBKR HTB"),
    ("BRAI","Braiin Ltd",                  None, None, 895.0, None,   None, None, "IBKR HTB"),
    ("PFSA","Profusa Inc",                 None, None, 876.7, None,   None, None, "IBKR HTB"),
    ("NCRA","Nocera Inc",                  None, None, 815.9, None,   None, None, "IBKR HTB"),
    ("VBIO","Valion Bio Inc",              None, None, 753.7, None,   None, None, "IBKR HTB"),
    ("LABT","Lakewood-Amedex Biothera",    None, None, 690.6, None,   None, None, "IBKR HTB"),
    ("AMIX","Autonomix Medical Inc",       None, None, 665.6, None,   None, None, "IBKR HTB"),
    ("WHLR","Wheeler Real Estate Inv",     None, None, 662.4, None,   None, None, "IBKR HTB"),
    ("WLDS","Wearable Devices Ltd",        None, None, 659.9, None,   None, None, "IBKR HTB"),
    ("ATPC","Agape ATP Corp",              None, None, 645.7, None,   None, None, "IBKR HTB"),
    ("SDOT","Sadot Group Inc",             None, None, 617.7, None,   None, None, "IBKR HTB"),
    ("SGLY","Singularity Future Tech",     None, None, 590.0, None,   None, None, "IBKR HTB"),
    ("MB",  "Masterbeef Group",            None, None, 558.0, None,   None, None, "IBKR HTB"),
    ("JZ",  "Jianzhi Education Tech",      None, None, 546.9, None,   None, None, "IBKR HTB"),
    ("VNRX","VolitionRx Ltd",              None, None, 311.9, None,   None, None, "IBKR HTB"),
    ("GCTK","GlucoTrack Inc",              None, None, 252.9, None,   None, None, "Tapeboard 8/18"),
    ("RGC", "Regencell Bioscience",        None, None, 246.2, None,   None, None, "Tapeboard 8/18"),
    ("INM", "InMed Pharmaceuticals",       None, None, 180.2, None,   None, None, "Tapeboard 8/18"),
    ("DRMA","Dermata Therapeutics",        None, None, 103.9, None,   None, None, "Tapeboard 8/18"),
    ("GOVX","GeoVax Labs Inc",             None, None,  92.3, None,   None, None, "Tapeboard 8/18"),
    ("NXTC","NextCure Inc",                 6.3,  1.0,  90.1,  5.91,   None, None, "Tapeboard"),
    ("PARA","Paramount Global",            None, None,  33.1, None,   None, None, "Tapeboard 8/18"),
    ("EBET","EBET Inc",                    None, None,  27.2, None,   None, None, "Tapeboard 8/18"),
    ("BBIG","Vinco Ventures",              None, None,  22.7, None,   None, None, "Tapeboard 8/18"),
    ("KITT","Nauticus Robotics",           None, None,  21.0, None,   None, None, "Tapeboard 8/18"),
    ("ZVSA","ZyVersa Therapeutics",        None, None,  15.9, None,   None, None, "Tapeboard 8/18"),
    ("GRRR","Gorilla Technology",         18.5,  4.0,  27.3, 14.03,   None, None, "Tapeboard live"),
    ("RGCT","Regencell Bio",               1.6,  4.0, 272.4,  5.36,   None, None, "Tapeboard live"),
    ("LCID","Lucid Group Inc",            36.58, 2.9,  14.5,  5.01,   None,  56.3, "MarketWatch + Tapeboard"),
    ("CGC", "Canopy Growth Corp",         None, None,  10.7, None,   None, None, "Tapeboard 8/18"),
    ("RCEL","Recel Ltd",                   8.6, 11.1,   9.4, 10.52,   None, None, "Tapeboard live"),
    ("CYDY","CytoDyn Inc",                 2.7,  9.9,   8.1,  0.24,   None, None, "Tapeboard live"),
    ("BYND","Beyond Meat Inc",            30.3,  2.3,  20.0,  None,   None,  5.1, "Tapeboard"),
    ("NRDY","Nerdy Inc",                  None, None,  10.6, None,   None, None, "Tapeboard 8/18"),
    ("BIRD","Allbirds Inc",               None, None,   9.9, None,   None, None, "Tapeboard 8/18"),
    ("RUM", "Rumble Inc",                 23.7, 12.9,   4.9, 10.28,   None, None, "Tapeboard"),
    ("OPAD","Offerpad Solutions",         None, None,  11.5, None,   None, None, "Tapeboard 8/18"),
    ("WKHS","Workhorse Group",            34.03, None,  27.1, 3.07,   None,  0.88, "MarketWatch + Tapeboard"),

    # Highest SI% of float (MarketWatch / TheDesperateTrader / TheTrading.tools)
    ("GPUS","Hyperscale Data Inc",       109.5,  1.4,  None,  0.28,   None, 51.2, "TDT — synthetic-like"),
    ("BAOS","Baosheng Media Group",      105.73, None, None,  0.35,   None,  1.06, "MarketWatch"),
    ("ZCMD","Zhongchao Inc",              97.08, None, None,  0.92,   None,  0.35, "MarketWatch"),
    ("WOLF","Wolfspeed Inc",              92.7,  5.4,  None,  None,   497,  None, "TheTrading.tools"),
    ("MRLN","Merlin Inc",                 86.97, None, None,  3.28,   None,  8.65, "MarketWatch"),
    ("GRPN","Groupon Inc",                83.7,  7.3,   1.5, 19.18,   None, 12.68, "TDT + Tapeboard"),
    ("TTAN","ServiceTitan Inc",           82.4,  4.3,  None, 98.04,   None, 10.0, "TDT"),
    ("PROK","ProKidney Corp",             80.5, 18.7,  None,  1.83,   None, 21.5, "TDT + MW"),
    ("SATL","Satellogic Inc",             72.1,  2.6,  None,  None,   None, 20.0, "TDT"),
    ("FGI", "FGI Industries Ltd",         72.31, None, None,  7.37,   None,  0.38, "MarketWatch"),
    ("AVTX","Avalo Therapeutics",         70.7, 20.2,  None,  None,   301,  None, "TheTrading.tools"),
    ("HTZ", "Hertz Global Holdings",      64.0,  1.2,  None,  2.27,  1800, 110.0, "TDT"),
    ("WYFI","WhiteFiber Inc",             63.76, None, None, 17.75,   None,  6.04, "MarketWatch"),
    ("ECHO","EchoStar Corp",              61.7,  6.5,  None,  None,   None, 29.0, "TDT"),
    ("EOSE","Eos Energy Enterprises",     60.3,  4.1,  None,  3.22,  5900,112.8, "TDT + tools"),
    ("ONDS","Ondas Holdings",             59.8,  2.7,  None,  7.66,  5300,230.5, "MW + tools"),
    ("KPTI","Karyopharm Therapeutics",    52.55, 4.5,  None,  1.87,   None,  9.64, "MW + TDT"),
    ("NUAI","New Era Energy & Digital",   50.2,  5.6,  None,  4.66,   431, 26.8, "TheTrading.tools + MW"),
    ("GCT", "GigaCloud Technology",       50.0,  4.6,  None, 48.53,   None,  4.0, "TDT"),
    ("CRML","Critical Metals Corp",       49.7,  3.6,  None,  7.21,   None, 22.3, "TDT"),
    ("PLCE","Children's Place Inc",       46.30, None, None,  2.47,   None,  3.41, "MarketWatch"),
    ("HQ",  "Horizon Quantum Holdings",   45.23, None, None, 15.18,   None,  1.06, "MarketWatch"),
    ("DFSC","DEFSEC Technologies",        44.71, None, None,  1.72,   None,  1.04, "MarketWatch"),
    ("METC","Ramaco Resources",           42.9,  5.4,  None, 14.24,   None, 13.8, "TDT + MW"),
    ("DUOL","Duolingo Inc",               42.6,  4.5,  None,  None,   None,  7.1, "TDT"),
    ("LUNR","Intuitive Machines",         42.2,  2.9,  None,  None,   None, 36.0, "TDT"),
    ("IBRX","ImmunityBio Inc",            42.1, 15.4,  None,  8.05,   None,127.5, "TDT + MW"),
    ("HPK", "HighPeak Energy Inc",        41.79, None, None,  8.10,   None,  9.30, "MarketWatch"),
    ("LENZ","LENZ Therapeutics",          40.3, 12.5,  None,  5.21,   666, 11.2, "TDT + tools"),
    ("HYMC","Hycroft Mining Holding",     40.3,  7.9,  None, 23.43,   None, 12.8, "TDT"),
    ("BETR","Better Home & Finance",      40.55, None, None, 13.85,   None,  2.85, "MarketWatch"),
    ("ACDC","ProFrac Holding Corp",       39.4,  2.1,  None,  None,   None,  4.9, "TDT"),
    ("SLS", "SELLAS Life Sciences",       39.6, 10.4,  None,  None,   768, None, "TheTrading.tools"),
    ("ARQQ","Arqit Quantum Inc",          38.90, None, None, 20.04,   None,  2.72, "MarketWatch"),
    ("BWIN","Baldwin Insurance Group",    38.6, 10.2,  None, 30.85,   None, 12.3, "TDT"),
    ("QXO", "QXO Inc",                    38.2,  4.1,  None,  None,   None, 81.0, "TDT"),
    ("IBTA","Ibotta Inc",                 38.10, 6.8,  None, 37.06,   None,  3.22, "TDT + MW"),
    ("CAPR","Capricor Therapeutics",      38.8,  1.6,  None,  9.91,  1300, 17.8, "TDT + tools"),
    ("NTLA","Intellia Therapeutics",      38.8, 13.2,  None, 12.87,  1800, 44.9, "TDT + tools + MW"),
    ("GOSS","Gossamer Bio",               37.5,  7.4,  None,  None,   664, None, "TheTrading.tools"),
    ("NTST","NetSTREIT Corp",             37.4, 27.9,  None,  None,  1500, 31.0, "TDT + tools"),
    ("UWMC","UWM Holdings",               37.2,  2.7,  None,  None,  1600, None, "TheTrading.tools"),
    ("SVRA","Savara Inc",                 37.46, None, None,  5.26,  1300, 38.5, "MW + tools"),
    ("VELO","Velo3D Inc",                 37.10, None, None, 11.87,   None,  5.52, "MarketWatch"),
    ("RXRX","Recursion Pharmaceuticals",  37.05, 13.4, None,  3.34,  2600,177.9, "MW + tools + TDT"),
    ("SHOE","Shoe Station Group",         37.1, 13.7,  None, 13.70,   None,  6.6, "TDT + MW"),
    ("RH",  "RH",                         37.0,  7.4,  None,149.15,   None,  5.0, "TDT"),
    ("SOUN","SoundHound AI",              42.0,  4.6,  None,  7.11,  4600,162.6, "TDT + tools + MW"),
    ("SERV","Serve Robotics",             36.5,  4.8,  None,  5.03,   987, 27.0, "TDT + tools"),
    ("PRME","Prime Medicine Inc",         36.72, None, None,  3.61,   None, 35.0, "MarketWatch"),
    ("ATLC","Atlanticus Holdings",        36.7,  2.7,  None,  None,   None,  0.47, "TDT"),
    ("CHYM","Chime Financial",            36.4,  1.6,  None, 33.11,   None, 19.0, "TDT"),
    ("PCT", "PureCycle Technologies",     36.4, 11.1,  None,  6.53,   None, 55.7, "TDT"),
    ("BTDR","Bitdeer Technologies",       39.06, 3.7,  None, 10.85,  3300, 56.8, "MW + tools + TDT"),
    ("HNGE","Hinge Health",               36.2,  2.7,  None, 92.56,   None,  5.6, "TDT"),
    ("JACK","Jack in the Box",            36.20, 5.4,  None, 16.51,   415,  6.1, "TDT + MW + tools"),
    ("XRX", "Xerox Holdings",             35.34, None, None,  3.06,   None, 39.15, "MarketWatch"),
    ("SPRY","ARS Pharmaceuticals",        35.20, None, None,  5.89,   None, 22.95, "MarketWatch"),
    ("AI",  "C3.ai Inc",                  35.18, 11.5, None, 10.53,  1800, 48.1, "MW + tools + TDT"),
    ("TNGX","Tango Therapeutics",         34.79, 20.9, None, 23.34,  1700, 43.2, "MW + tools"),
    ("IEP", "Icahn Enterprises LP",       34.77, None, None,  6.78,   None, 17.88, "MarketWatch"),
    ("BELFB","Bel Fuse Inc",              34.4,  3.6,  None,  None,   None,  0.75, "TDT"),
    ("BRNX","BrenX Ltd",                  36.31, 1.2,  None,  3.89,   None,  0.25, "MW + TDT"),
    ("ZBIO","Zenas BioPharma",            34.16, None, None, 31.27,   None, 11.66, "MarketWatch"),
    ("NAVI","Navient Corp",               33.6,  7.0,  None,  9.51,   None, 10.9, "TDT"),
    ("NNE", "Nano Nuclear Energy",        33.23, 7.1,  None, 17.97,   None, 14.6, "TDT + MW"),
    ("FRGT","Freight Technologies",       33.79, None,179.6,  1.45,   None,  0.19, "MW + IBKR HTB"),
    ("BOXL","Boxlight Corp",              33.95, None,428.8,  5.95,   None,  0.19, "MW + IBKR HTB"),
    ("CSIQ","Canadian Solar Inc",         33.25, None, None, 13.14,   None, 15.82, "MarketWatch"),
    ("INDI","indie Semiconductor",        33.5,  8.0,  None,  3.96,   928, 68.0, "TDT + tools"),
    ("IREN","IREN Ltd",                   32.6,  2.2,  None,  None, 17200, None, "TheTrading.tools"),
    ("OCGN","Ocugen Inc",                 32.6, 23.9,  None,  None,   515,102.0, "TDT + tools"),
    ("SOC", "Sable Offshore Corp",        31.9,  5.7,  None,  None,  1800, None, "TheTrading.tools"),
    ("LEU", "Centrus Energy",             31.8,  9.3,  None,  None,  5500, None, "TheTrading.tools"),
    ("REPL","Replimune Group",            31.7,  4.0,  None,  None,   620, None, "TheTrading.tools"),
    ("DDD", "3D Systems Corp",            33.9,  8.2,  None,  None,   413, None, "TheTrading.tools"),
    ("FCEL","FuelCell Energy",            35.2,  2.2,  None,  None,   474, None, "TheTrading.tools"),
    ("GPRO","GoPro Inc",                  32.4,  3.3,  None,  0.876,  None, 24.9, "TDT"),
    ("TBCH","Turtle Beach Corp",          32.88, None, None, 12.45,   None,  4.38, "MarketWatch"),
    ("NVAX","Novavax Inc",                31.6, 14.0,  None,  None,   None, 47.0, "TDT"),
    ("QUBT","Quantum Computing Inc",      31.95, None, None,  8.15,   None, 64.09, "MarketWatch"),
    ("ASTS","AST SpaceMobile",            31.76, None, None, 58.05,   None, 57.48, "MarketWatch"),
    ("TE",  "T1 Energy Inc",              31.5,  1.6,  None,  None,   None, 67.0, "TDT"),
    ("MARA","MARA Holdings",              28.6,  3.1,  None,  None,   None,110.0, "TDT"),
    ("CLSK","CleanSpark Inc",             29.3,  3.6,  None,  None,   None, 74.0, "TDT"),
    ("SMR", "NuScale Power",              29.2,  2.3,  None,  None,   None, 68.0, "TDT"),
    ("BBAI","BigBear.ai Holdings",        33.5,  6.4,  None,  None,  2700,146.0, "TDT + tools"),
]

# Bucket borrow-fee tiers based on Interactive Brokers standard tiers
def fee_tier(fee):
    if fee is None: return "Unknown"
    if fee >= 100: return "Extreme HTB"
    if fee >= 25:  return "Hard-to-borrow"
    if fee >= 10:  return "Elevated"
    if fee >= 3:   return "Warm"
    if fee >= 1:   return "General collateral+"
    return "General collateral"

# Fully-Paid Lending (FPL) split assumption — retail lender share of fee
# Fidelity FPL: variable, historically ~35-50%. Schwab FPL: ~50%. IBKR SYEP: ~50%.
FPL_SPLIT = 0.50

def lending_score(si, dtc, fee, price):
    """0-100 probability that a retail broker actually places & pays on shares.

    Weighted composite:
      - Borrow fee (40%): higher fee = more demand = more likely to be lent
      - SI% of float (25%): concentration of short demand
      - Days-to-cover (15%): stickiness of demand
      - Price sanity (10%): sub-$1 stocks often excluded from FPL programs
      - Data completeness (10%): more corroborating signals = higher confidence
    """
    parts = []

    # Fee component (0-40)
    if fee is not None:
        if fee >= 100: parts.append(40)
        elif fee >= 25: parts.append(35)
        elif fee >= 10: parts.append(28)
        elif fee >= 5:  parts.append(20)
        elif fee >= 1:  parts.append(12)
        else: parts.append(5)
    else:
        parts.append(0)

    # SI% component (0-25)
    if si is not None:
        if si >= 50: parts.append(25)
        elif si >= 30: parts.append(20)
        elif si >= 15: parts.append(13)
        elif si >= 5:  parts.append(7)
        else: parts.append(3)
    else:
        parts.append(0)

    # DTC component (0-15)
    if dtc is not None:
        if dtc >= 10: parts.append(15)
        elif dtc >= 5: parts.append(11)
        elif dtc >= 2: parts.append(7)
        else: parts.append(3)
    else:
        parts.append(0)

    # Price sanity (0-10) — most brokers exclude sub-$1
    if price is not None:
        if price >= 5: parts.append(10)
        elif price >= 2: parts.append(7)
        elif price >= 1: parts.append(4)
        else: parts.append(0)   # sub-$1 rarely eligible
    else:
        parts.append(5)

    # Completeness (0-10)
    completeness = sum(x is not None for x in (si, dtc, fee, price))
    parts.append(int(completeness * 2.5))

    return min(100, sum(parts))

def flag(fee, score):
    # Daily monitor flags on critical thresholds
    if fee is None:
        return "no_fee_data"
    if fee >= 100: return "🚨 EXTREME — call broker"
    if fee >= 25:  return "🔥 HARD-TO-BORROW crossed"
    if fee >= 10:  return "⚠️ Elevated fee"
    if fee >= 3:   return "👀 Warm — monitor"
    return "—"

records = []
for r in RAW:
    tk, name, si, dtc, fee, price, mcap, ss, note = r
    est_apy = round(fee * FPL_SPLIT, 2) if fee is not None else None
    records.append({
        "ticker": tk,
        "company": name,
        "si_pct_float": si,
        "days_to_cover": dtc,
        "borrow_fee_pct": fee,
        "fee_tier": fee_tier(fee),
        "price": price,
        "mkt_cap_m": mcap,
        "shares_short_m": ss,
        "est_apy_pct": est_apy,
        "lending_score": lending_score(si, dtc, fee, price),
        "flag": flag(fee, lending_score(si, dtc, fee, price)),
        "sources": note,
    })

# Dedup: keep highest lending_score per ticker
best = {}
for r in records:
    tk = r["ticker"]
    if tk not in best or r["lending_score"] > best[tk]["lending_score"]:
        best[tk] = r
records = sorted(best.values(), key=lambda x: (-x["lending_score"], -(x["borrow_fee_pct"] or 0)))

out = {
    "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    "as_of": "2026-09-01",
    "fpl_split_assumption": FPL_SPLIT,
    "row_count": len(records),
    "rows": records,
}
with open("/home/user/workspace/short-lend-dashboard/data.json","w") as f:
    json.dump(out, f, indent=2)

print(f"Wrote {len(records)} tickers")
print("Top 10 by lending score:")
for r in records[:10]:
    print(f"  {r['ticker']:6} score={r['lending_score']:3}  fee={r['borrow_fee_pct']}  si={r['si_pct_float']}  apy={r['est_apy_pct']}  {r['flag']}")
