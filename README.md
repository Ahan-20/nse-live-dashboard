# NSE Live Scanner Dashboard

Live NSE market dashboard — sectoral indices, top gainers/losers, and a
movers feed — pulled from NSE's own JSON endpoints.

- **`server.py`** — pure-stdlib Python proxy + static server. Fetches NSE
  with a browser-like session (cookies + headers) and serves the page,
  a same-origin `/api/dashboard` feed (which sidesteps CORS), and
  `/api/backtest?symbol=XXX`.
- **`index.html`** — the dashboard; fetches `/api/dashboard` every 10s, plus
  a **Backtest** tab.
- **`backtest.py`** — 200 EMA pullback strategy backtester (5y daily, ATR
  stop + 2R target). Reads `prices.db` locally, else the shipped `prices_web.db`.
- **`ingest.py`** — builds the price DB from NSE daily bhavcopy archives
  (no API key). `prices_web.db` = top-500 liquid names (shipped, ~58MB);
  `prices.db` = all ~3000 symbols (local only, gitignored).
- **`Dockerfile`** — for Railway / any container host. No dependencies.

## Backtest

```bash
python3 ingest.py 2021-06-08 2026-06-08   # build prices.db (one-time, ~4 min)
python3 backtest.py RELIANCE              # run a backtest from the CLI
```
The strategy: trend filter on a rising/falling 200 EMA; enter on a
pullback-and-reclaim of the 20 EMA; stop = ATR×1.5, target = 2R, risking 1%
of equity per trade. Daily candles (free 5-year intraday data doesn't exist).
Backtest results are not a prediction of future returns.

## Run locally

```bash
python3 server.py        # → http://localhost:8787
```

## Notes

- Live data only during NSE hours (Mon–Fri, 09:15–15:30 IST); outside that
  NSE returns the last close, so the page still fills in.
- These are unofficial NSE endpoints (the same ones nseindia.com uses).
  Personal use; they can change without notice.
- NSE spells the losers param `loosers` — the correctly-spelled one
  returns junk. Not a typo in the code.
