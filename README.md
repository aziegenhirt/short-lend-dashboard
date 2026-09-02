# Short Interest & Lending Probability Tracker

A self-updating dashboard tracking high short-interest equities, cost-to-borrow
(CTB) rates, and Fully-Paid-Lending yield estimates.

**Live demo:** enable GitHub Pages on this repo (Settings → Pages → Source: `main` branch, root folder).

## What it does

- Consolidates short-interest and borrow-fee data from **IBorrowDesk (IBKR)**,
  **MarketWatch**, and **TheDesperateTrader**
- Computes a 0-100 **Lending Score** (probability of being placed in Fidelity FPL /
  Schwab SLFP / IBKR SYEP)
- Estimates **annualized lender APY** assuming a 50% split of the prevailing borrow fee
- Flags tickers crossing critical CTB thresholds (Extreme ≥100%, Hard-to-borrow ≥25%,
  Elevated ≥10%, Warm ≥3%)

## Auto-refresh

`.github/workflows/refresh.yml` runs every weekday at **5:15pm ET** (after market close):

1. Fetches fresh data from all sources
2. Recomputes scores and APY estimates
3. Commits the updated `data.json` back to the repo
4. GitHub Pages serves the updated dashboard automatically

**Cost: $0.** GitHub Actions gives 2,000 free minutes/month on private repos, and
unlimited minutes on public repos. This job takes ~30 seconds per run.

## Manual refresh

Click **Actions → Daily Data Refresh → Run workflow** in GitHub.

## Local development

```bash
pip install requests beautifulsoup4
python refresh.py     # writes data.json
python -m http.server # serve index.html at http://localhost:8000
```

## Files

- `index.html` — the dashboard (self-contained, no build step)
- `data.json` — current data (regenerated daily)
- `refresh.py` — the daily fetch + score script
- `build_data.py` — original seed script (baseline dataset)
- `.github/workflows/refresh.yml` — the cron schedule

## Methodology

**Lending Score (0-100):** weighted composite of borrow fee (40%), SI %float (25%),
days-to-cover (15%), price eligibility (10%), data completeness (10%).

**Est. Lender APY:** `borrow_fee × 0.50`. Actual FPL splits vary daily and by
broker — use as a directional indicator.

## Disclaimer

Educational tool only. Not investment advice. FINRA short-interest data is
bi-weekly; CTB rates move intraday.
