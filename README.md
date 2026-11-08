# Momentum Screener (Nifty 500)

A static web page that screens the Nifty 500 for quality momentum stocks. No broker login.
A GitHub Action pulls fresh data every weekday after market close and commits it to `data/stocks.json`;
the page reads that file and applies your filters in the browser.

## Default filters

| Filter | Rule |
|---|---|
| Market cap | > ₹500 Cr |
| ROCE / ROE | > 15% |
| Debt to equity | < 1 |
| Profit growth 3Y | > 15% |
| Sales growth 3Y | > 10% |
| P/E | < 60 |
| Down from 52w high | < 20% |
| Return 3M / 6M | > 8% / > 15% |
| RSI (14, Wilder) | > 55 |
| Volume | > 1.5 × previous 20-session average |

Every filter can be switched off, and its operator and value changed. Changes are saved in your browser,
and **Copy share link** puts your exact filter set in the URL.

## Setup (about 5 minutes)

1. Create a new GitHub repo and upload everything in this folder (keep the `.github` folder).
2. **Settings → Pages**: Source = *Deploy from a branch*, Branch = `main`, folder `/ (root)`. Save.
3. **Actions tab**: enable workflows if prompted, open **Refresh screener data**, click **Run workflow**.
   The first run fetches fundamentals for all 500 stocks and takes roughly 20–25 minutes; daily runs after that take a few minutes.
4. Open `https://<your-username>.github.io/<repo-name>/`.

Until the first run finishes, the page shows a notice instead of data.

## Refresh on demand

The page has a **Refresh now** button. Viewers see when data was last updated; only the repo owner can start a refresh:

1. Create a [fine-grained GitHub token](https://github.com/settings/personal-access-tokens/new) with access to **only this repository** and permissions **Actions: Read and write**, **Contents: Read-only**.
2. On the page, click **Refresh now** (or ⚙) and paste the token. It is saved only in that browser.
3. Click **Refresh now** whenever you want fresh data. The page shows progress and loads the new data when the job finishes (about 2–5 minutes).

Refreshing during market hours gives intraday prices; the page flags this because today's volume is still partial.

## Data sources

- **Universe**: Nifty 500 constituent list from NSE / niftyindices.com
- **Prices, volume, returns, RSI, 52w high**: Yahoo Finance (via `yfinance`), adjusted daily data
- **Market cap, P/E, ROCE, ROE, 3Y growth, debt/equity**: Screener.in public company pages
  (consolidated, falling back to standalone). Debt/equity = Borrowings ÷ (Equity capital + Reserves), latest balance sheet.

Fundamentals are cached in `data/fundamentals.json`; each run refreshes up to 120 of the oldest entries,
so every stock is refreshed roughly weekly. Market cap and P/E are scaled to the latest close.

## Run locally

```bash
pip install -r requirements.txt
python scripts/fetch_data.py      # refresh data
python -m http.server 8000        # then open http://localhost:8000
```

Opening `index.html` directly from disk won't work because browsers block reading local JSON files.

## Troubleshooting

- **Workflow fails on `git push`**: Settings → Actions → General → Workflow permissions → *Read and write permissions*.
- **Schedule stopped running**: GitHub pauses scheduled workflows in repos with no activity for 60 days. Re-enable it from the Actions tab.
- **Banks show missing ROCE or D/E**: Screener.in reports bank financials differently. Untick *Exclude stocks with missing data* to let them through on the filters that do have values.
- **Screener.in returns 429 (rate limited)**: the script backs off and retries; lower `FUND_MAX_REFRESH` in `scripts/fetch_data.py` if it keeps happening.
- **Refresh now says GitHub refused the request**: the token has expired or lacks *Actions: Read and write*. Create a new one and paste it via ⚙.

For research only. Not investment advice.
