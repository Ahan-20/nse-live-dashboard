# NSE Live Scanner Dashboard

Live NSE market dashboard — sectoral indices, top gainers/losers, and a
movers feed — pulled from NSE's own JSON endpoints.

- **`server.py`** — pure-stdlib Python proxy + static server. Fetches NSE
  with a browser-like session (cookies + headers) and serves the page and
  a same-origin `/api/dashboard` feed (which sidesteps CORS).
- **`index.html`** — the dashboard; fetches `/api/dashboard` every 10s.
- **`Dockerfile`** — for Railway / any container host. No dependencies.

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
