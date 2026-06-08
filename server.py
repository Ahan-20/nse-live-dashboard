#!/usr/bin/env python3
"""
NSE live-data proxy + static server for the scanner dashboard.

Why this exists:
  - NSE has no public API and blocks non-browser requests.
  - A plain .html file can't fetch nseindia.com directly (CORS).
This server fetches NSE's own JSON endpoints with a browser-like
session (cookies + headers), and serves both the dashboard and a
clean /api/dashboard feed on ONE origin, so the page just fetches
same-origin JSON.

Run:   python3 server.py
Open:  http://localhost:8787
Data is live only during NSE hours (Mon-Fri, 09:15-15:30 IST).
Outside hours NSE returns the last close, so the page still fills in.
"""

import os
import json
import time
import threading
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import backtest as bt

PORT = int(os.environ.get("PORT", 8787))   # Railway injects PORT
HOST = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
NSE = "https://www.nseindia.com"
HERE = Path(__file__).resolve().parent

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE + "/market-data/live-equity-market",
}

# Sectors we want, in display order, mapped to pretty labels.
SECTOR_DISPLAY = {
    "NIFTY BANK": "Nifty Bank",
    "NIFTY IT": "Nifty IT",
    "NIFTY AUTO": "Nifty Auto",
    "NIFTY PHARMA": "Nifty Pharma",
    "NIFTY METAL": "Nifty Metal",
    "NIFTY FMCG": "Nifty FMCG",
    "NIFTY ENERGY": "Nifty Energy",
    "NIFTY REALTY": "Nifty Realty",
    "NIFTY MEDIA": "Nifty Media",
    "NIFTY PSU BANK": "Nifty PSU Bank",
}
TICKER_WANT = [
    ("NIFTY 50", "NIFTY 50"),
    ("NIFTY BANK", "BANK NIFTY"),
    ("NIFTY IT", "NIFTY IT"),
    ("NIFTY MIDCAP 100", "NIFTY MID"),
    ("NIFTY METAL", "NIFTY METAL"),
    ("INDIA VIX", "INDIA VIX"),
]

# ── NSE session ──────────────────────────────────────────────────────
_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
_lock = threading.Lock()
_bootstrapped_at = 0.0


def _bootstrap():
    """Visit homepage pages to collect the cookies NSE's API requires."""
    for url in (NSE + "/", NSE + "/market-data/live-equity-market"):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            _opener.open(req, timeout=12).read()
        except Exception:
            pass


def nse_get(path):
    """GET a NSE /api path as JSON, refreshing cookies on failure."""
    global _bootstrapped_at
    with _lock:
        if time.time() - _bootstrapped_at > 600 or not list(_jar):
            _bootstrap()
            _bootstrapped_at = time.time()
    url = NSE + path
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        return json.loads(_opener.open(req, timeout=12).read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        with _lock:                       # one retry with fresh cookies
            _bootstrap()
            _bootstrapped_at = time.time()
        req = urllib.request.Request(url, headers=HEADERS)
        return json.loads(_opener.open(req, timeout=12).read().decode("utf-8"))


# ── formatting helpers ───────────────────────────────────────────────
def fnum(x):
    try:
        return f"{float(x):,.2f}"
    except (TypeError, ValueError):
        return "—"


def fpct(x):
    try:
        return f"{float(x):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def signal_for(chg):
    if chg >= 1.0:
        return "LONG"
    if chg <= -1.0:
        return "SHORT"
    return "WATCH"


def hhmm(timestamp):
    # NSE timestamps look like "08-Jun-2026 15:30:00"
    try:
        return timestamp.split(" ")[1][:5]
    except Exception:
        return ""


# ── dashboard builder ────────────────────────────────────────────────
def build_dashboard():
    out = {"status": "live", "asOf": "", "indices": [], "ticker": [],
           "gainers": [], "losers": [], "movers": [], "nifty": None, "bank": None}

    all_idx = nse_get("/api/allIndices")
    rows = all_idx.get("data", [])
    by_name = {r.get("index"): r for r in rows}
    out["asOf"] = hhmm(all_idx.get("timestamp", ""))

    # Market open / closed
    try:
        ms = nse_get("/api/marketStatus").get("marketState", [])
        cap = next((m for m in ms if m.get("market") == "Capital Market"), None)
        if cap and str(cap.get("marketStatus", "")).lower() != "open":
            out["status"] = "closed"
    except Exception:
        pass

    # Sectors
    for nse_name, disp in SECTOR_DISPLAY.items():
        r = by_name.get(nse_name)
        if not r:
            continue
        chg = round(float(r.get("percentChange", 0)), 2)
        out["indices"].append({"name": disp, "chg": chg, "signal": signal_for(chg)})

    # Ticker
    for nse_name, disp in TICKER_WANT:
        r = by_name.get(nse_name)
        if not r:
            continue
        chg = float(r.get("percentChange", 0))
        out["ticker"].append({
            "name": disp, "val": fnum(r.get("last")),
            "chg": fpct(chg), "up": chg >= 0,
        })

    # Header chips
    n = by_name.get("NIFTY 50")
    if n:
        c = float(n.get("percentChange", 0))
        out["nifty"] = {"val": fnum(n.get("last")), "chg": fpct(c), "up": c >= 0}
    b = by_name.get("NIFTY BANK")
    if b:
        c = float(b.get("percentChange", 0))
        out["bank"] = {"val": fnum(b.get("last")), "chg": fpct(c), "up": c >= 0}

    # Gainers / losers via the (ungated) live-analysis-variations endpoint.
    # It buckets movers by index; we use the NIFTY (50) bucket.
    def variations(kind):
        d = nse_get("/api/live-analysis-variations?index=" + kind)
        return (d.get("NIFTY") or {}).get("data", [])

    def pack(s):
        ch = float(s.get("perChange", 0))
        return {"name": s.get("symbol"), "price": fnum(s.get("ltp")),
                "chg": f"{ch:+.2f}", "sig": signal_for(ch)}

    # NB: NSE's losers param is the misspelled "loosers"; "losers" returns junk.
    g = sorted(variations("gainers"), key=lambda s: float(s.get("perChange", 0)), reverse=True)
    l = sorted(variations("loosers"), key=lambda s: float(s.get("perChange", 0)))
    out["gainers"] = [pack(s) for s in g[:7]]
    out["losers"] = [pack(s) for s in l[:7]]

    # "Live movers" feed = strongest few each way (real %, not invented signals)
    t = out["asOf"]
    for s in g[:3] + l[:3]:
        ch = float(s.get("perChange", 0))
        up = ch >= 0
        out["movers"].append({
            "time": t,
            "stock": s.get("symbol"),
            "sig": ("▲ LONG" if up else "▼ SHORT"),
            "type": ("conf" if up else "short"),
            "tf": "1D",
            "price": fnum(s.get("ltp")),
            "reason": f"Live {ch:+.2f}% on the day · NIFTY 50",
        })
    return out


# ── cache so refreshes don't hammer NSE ──────────────────────────────
_cache = {"t": 0.0, "data": None}


def cached_dashboard():
    if _cache["data"] and time.time() - _cache["t"] < 8:
        return _cache["data"]
    try:
        d = build_dashboard()
        _cache["data"], _cache["t"] = d, time.time()
        return d
    except Exception as e:
        if _cache["data"]:
            stale = dict(_cache["data"])
            stale["status"] = "stale"
            stale["error"] = str(e)
            return stale
        return {"status": "error", "error": str(e), "indices": [], "ticker": [],
                "gainers": [], "losers": [], "movers": []}


# ── backtest cache (results are deterministic per symbol) ────────────
_bt_cache = {}


def cached_backtest(symbol):
    if symbol in _bt_cache:
        return _bt_cache[symbol]
    rep = bt.run_backtest(symbol)
    _bt_cache[symbol] = rep
    return rep


# ── HTTP server ──────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/dashboard"):
            body = json.dumps(cached_dashboard()).encode("utf-8")
            return self._send(200, body, "application/json")
        if self.path.startswith("/api/backtest"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sym = (q.get("symbol", [""])[0] or "").strip().upper()
            if not sym:
                return self._send(400, b'{"ok":false,"error":"missing symbol"}',
                                  "application/json")
            try:
                rep = cached_backtest(sym)
            except Exception as e:
                rep = {"ok": False, "error": str(e)}
            return self._send(200, json.dumps(rep).encode("utf-8"), "application/json")
        if self.path in ("/", "/index.html"):
            try:
                body = (HERE / "index.html").read_bytes()
                return self._send(200, body, "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(404, b"index.html not found", "text/plain")
        return self._send(404, b"not found", "text/plain")

    def log_message(self, *args):
        pass  # quiet


if __name__ == "__main__":
    print(f"NSE live dashboard  →  http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.\n")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
