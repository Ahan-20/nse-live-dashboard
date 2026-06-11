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
import datetime
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


# ── BSE (SENSEX is a BSE index, not on NSE) ──────────────────────────
_bse_opener = urllib.request.build_opener()
BSE_HEADERS = {"User-Agent": HEADERS["User-Agent"], "Accept": "application/json",
               "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}


def fetch_sensex():
    """Live BSE SENSEX (scripcode 1) -> {val, chg, up}."""
    url = ("https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w"
           "?Debtflag=&scripcode=1&seriesid=")
    req = urllib.request.Request(url, headers=BSE_HEADERS)
    d = json.loads(_bse_opener.open(req, timeout=12).read().decode("utf-8"))
    cr = d.get("CurrRate", {})
    ltp = float(str(cr.get("LTP", "0")).replace(",", ""))
    pchg = float(str(cr.get("PcChg", "0")).replace(",", "") or 0)
    return {"val": f"{ltp:,.2f}", "chg": f"{pchg:+.2f}%", "up": pchg >= 0}


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

    # SENSEX (BSE) — broad-market gauge. Failure here must not break the NSE feed.
    try:
        sx = fetch_sensex()
        out["sensex"] = sx
        out["ticker"].insert(0, {"name": "SENSEX", "val": sx["val"],
                                 "chg": sx["chg"], "up": sx["up"]})
    except Exception:
        out["sensex"] = None

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


NIFTY50 = ["ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
           "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA",
           "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH",
           "HDFCBANK", "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC",
           "INFY", "INDIGO", "JSWSTEEL", "JIOFIN", "KOTAKBANK", "LT", "M&M",
           "MARUTI", "MAXHEALTH", "NTPC", "NESTLEIND", "ONGC", "POWERGRID",
           "RELIANCE", "SBILIFE", "SHRIRAMFIN", "SBIN", "SUNPHARMA", "TCS",
           "TATACONSUM", "TMPV", "TATASTEEL", "TECHM", "TITAN", "TRENT",
           "ULTRACEMCO", "WIPRO"]

# ── Trade Finder: sector + gainer/loser + 200 EMA confluence ─────────
try:
    with open(os.path.join(HERE, "sector_map.json")) as _f:
        SECTOR_MAP = json.load(_f)
except Exception:
    SECTOR_MAP = {}

_ema_cache = {}


def tf_emas(symbol):
    """200-EMA on multiple timeframes from the daily DB, plus the last close on
    each timeframe (for cross detection). Cached (daily-static).
    Returns {daily, weekly, daily_close, weekly_close} or None."""
    if symbol in _ema_cache:
        return _ema_cache[symbol]
    res = None
    try:
        bars = bt.load(symbol)
        if len(bars) >= 200:
            closes = [b["c"] for b in bars]
            # weekly: last close of each ISO week
            wk, order = {}, []
            for b in bars:
                y, w, _ = datetime.date.fromisoformat(b["dt"]).isocalendar()
                k = (y, w)
                if k not in wk:
                    order.append(k)
                wk[k] = b["c"]
            wcloses = [wk[k] for k in order]
            res = {
                "daily": bt.ema(closes, 200)[-1],
                "daily_close": closes[-1],
                "weekly": bt.ema(wcloses, 200)[-1] if len(wcloses) >= 200 else None,
                "weekly_close": wcloses[-1] if wcloses else None,
            }
    except Exception:
        res = None
    _ema_cache[symbol] = res
    return res


def cross_state(prev, now, ema):
    """How `now` sits vs the 200 EMA, and whether it just crossed from `prev`.
    Returns (label, dir, dist%) — dir is 'up'|'dn'|None."""
    if not ema or not prev or not now:
        return ("—", None, None)
    dist = (now - ema) / ema * 100
    if prev <= ema < now:
        return ("Cross ↑", "up", dist)
    if prev >= ema > now:
        return ("Cross ↓", "dn", dist)
    return ("Near" if abs(dist) < 1 else ("Above" if dist > 0 else "Below"), None, dist)


_MON3 = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _nse_date_iso(ts):
    try:
        d, mon, y = ts.split(" ")[0].split("-")
        return "%04d-%02d-%02d" % (int(y), _MON3[mon], int(d))
    except Exception:
        return None


def _movers(kind, bucket):
    d = nse_get("/api/live-analysis-variations?index=" + kind)
    return (d.get(bucket) or {}).get("data", [])


def build_tradefinder(universe):
    bucket = "NIFTY" if universe == "nifty50" else "FOSec"
    all_idx = nse_get("/api/allIndices")
    by_name = {r.get("index"): float(r.get("percentChange", 0))
               for r in all_idx.get("data", [])}

    def sdir(disp):
        return by_name.get(disp.upper())

    live_date = _nse_date_iso(all_idx.get("timestamp", "") or "")

    stocks = {}
    for s in _movers("gainers", bucket):
        stocks[s.get("symbol")] = s
    for s in _movers("loosers", bucket):
        stocks.setdefault(s.get("symbol"), s)

    rows = []
    for sym, s in stocks.items():
        if not sym:
            continue
        ch = float(s.get("perChange", 0))
        ltp = float(s.get("ltp", 0) or 0)
        secs = SECTOR_MAP.get(sym, [])
        secdirs = [(d, sdir(d)) for d in secs if sdir(d) is not None]
        rep = max(secdirs, key=lambda x: abs(x[1])) if secdirs else None
        sec_up = rep[1] > 0 if rep else None

        # Live yesterday-close (prev_price) and today's price (ltp) drive the DAILY
        # cross (precise for TODAY). Weekly cross uses the last weekly close vs ltp
        # (this week). DB supplies only the slow 200-EMA values per timeframe.
        tf = tf_emas(sym)
        prevp = float(s.get("prev_price", 0) or 0)
        d_lbl, d_dir, d_dist = ("—", None, None)
        w_lbl, w_dir, w_dist = ("—", None, None)
        if tf and ltp:
            d_lbl, d_dir, d_dist = cross_state(prevp, ltp, tf["daily"])
            w_lbl, w_dir, w_dist = cross_state(tf["weekly_close"], ltp, tf["weekly"])

        cross_dir = d_dir            # "today" cross == the DAILY 200 EMA cross
        # Strong signal: fresh DAILY cross + stock move + sector all aligned.
        if d_dir == "up":
            setup = "LONG" if (ch > 0 and sec_up is True) else "CROSS-UP"
        elif d_dir == "dn":
            setup = "SHORT" if (ch < 0 and sec_up is False) else "CROSS-DN"
        else:
            setup = ""
        rows.append({
            "symbol": sym, "chg": round(ch, 2), "ltp": round(ltp, 2),
            "side": "gainer" if ch >= 0 else "loser",
            "sector": rep[0] if rep else "—",
            "sector_chg": round(rep[1], 2) if rep else None,
            "daily": {"label": d_lbl, "dir": d_dir, "dist": round(d_dist, 2) if d_dist is not None else None},
            "weekly": {"label": w_lbl, "dir": w_dir, "dist": round(w_dist, 2) if w_dist is not None else None},
            "cross_today": d_dir is not None, "cross_dir": cross_dir,
            "cross_any": (d_dir is not None) or (w_dir is not None),
            "setup": setup,
        })
    rank = {"LONG": 4, "SHORT": 4, "CROSS-UP": 3, "CROSS-DN": 3, "": 0}
    rows.sort(key=lambda r: (rank.get(r["setup"], 0), r["weekly"]["dir"] is not None,
                             abs(r["chg"])), reverse=True)
    return {"ok": True, "universe": universe, "asOf": (all_idx.get("timestamp", "") or "")[-8:],
            "data_date": live_date, "rows": rows}


_tf_cache = {"t": 0.0, "data": {}}


def cached_tradefinder(universe):
    now = time.time()
    if universe in _tf_cache["data"] and now - _tf_cache["t"] < 30:
        return _tf_cache["data"][universe]
    d = build_tradefinder(universe)
    _tf_cache["data"][universe] = d
    _tf_cache["t"] = now
    return d


# ── backtest cache (deterministic per symbol + parameter set) ────────
_bt_cache = {}


def _num(q, key, default, lo, hi):
    try:
        return max(lo, min(hi, float(q.get(key, [default])[0])))
    except (ValueError, TypeError):
        return default


def cached_backtest(symbol, q):
    mode = "ema200" if q.get("strategy", [""])[0] == "pullback" else "ema200cross"
    exit_mode = q.get("exit", ["cross"])[0]
    if exit_mode not in ("cross", "sltp", "either"):
        exit_mode = "cross"
    direction = q.get("direction", ["both"])[0]
    if direction not in ("both", "long", "short"):
        direction = "both"
    period = q.get("period", ["5y"])[0]
    lookback = {"3d": 3, "7d": 7, "15d": 15, "30d": 30, "60d": 60,
                "90d": 90, "5y": None}.get(period, None)
    params = dict(mode=mode, exit_mode=exit_mode, direction=direction,
                  lookback_days=lookback,
                  capital=_num(q, "capital", 100000, 1000, 1e9),
                  risk_pct=_num(q, "risk", 1.0, 0.1, 100) / 100.0,
                  sl_atr=_num(q, "sl", 1.5, 0.1, 20),
                  rr=_num(q, "rr", 2.0, 0.1, 50))
    key = (symbol,) + tuple(sorted(params.items()))
    if key not in _bt_cache:
        rep = bt.run_backtest(symbol, **params)
        if rep.get("ok"):
            rep["config"] = {
                "strategy": "200 EMA Cross" if mode == "ema200cross" else "20 EMA Pullback",
                "exit": {"cross": "Opposite 200 EMA cross", "sltp": "ATR stop + target",
                         "either": "Stop/target or cross"}[exit_mode],
                "direction": direction, "capital": params["capital"],
                "risk_pct": round(params["risk_pct"] * 100, 2),
                "sl_atr": params["sl_atr"], "rr": params["rr"],
                "period": period.upper()}
        _bt_cache[key] = rep
    return _bt_cache[key]


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
                rep = cached_backtest(sym, q)
            except Exception as e:
                rep = {"ok": False, "error": str(e)}
            return self._send(200, json.dumps(rep).encode("utf-8"), "application/json")
        if self.path.startswith("/api/leaderboard"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            rows, config = [], None
            for sym in NIFTY50:
                try:
                    r = cached_backtest(sym, q)
                except Exception:
                    r = {"ok": False}
                if r.get("ok"):
                    config = config or r.get("config")
                    rows.append({k: r.get(k) for k in (
                        "symbol", "trades", "win_rate", "return_pct",
                        "buyhold_return_pct", "vs_buyhold", "profit_factor",
                        "max_drawdown_pct")})
                else:
                    rows.append({"symbol": sym, "skip": True})
            rows.sort(key=lambda x: (x.get("return_pct") if x.get("return_pct")
                                     is not None else -1e9), reverse=True)
            body = json.dumps({"ok": True, "rows": rows, "config": config})
            return self._send(200, body.encode("utf-8"), "application/json")
        if self.path.startswith("/api/tradefinder"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            uni = q.get("universe", ["nifty50"])[0]
            uni = uni if uni in ("nifty50", "fo") else "nifty50"
            try:
                rep = cached_tradefinder(uni)
            except Exception as e:
                rep = {"ok": False, "error": str(e), "rows": []}
            return self._send(200, json.dumps(rep).encode("utf-8"), "application/json")
        if self.path.startswith("/api/exitcompare"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            labels = {"cross": "Opposite 200 EMA cross", "sltp": "ATR stop + R:R target",
                      "either": "Stop/target or cross"}
            results = []
            for ex in ("cross", "sltp", "either"):
                qq = dict(q); qq["exit"] = [ex]
                rets, prof, beat, cnt = [], 0, 0, 0
                for sym in NIFTY50:
                    try:
                        r = cached_backtest(sym, qq)
                    except Exception:
                        r = {"ok": False}
                    if r.get("ok"):
                        cnt += 1
                        rets.append(r["return_pct"])
                        if r["return_pct"] > 0: prof += 1
                        if (r.get("vs_buyhold") or 0) > 0: beat += 1
                rets.sort()
                results.append({
                    "exit": ex, "label": labels[ex], "n": cnt,
                    "profitable": prof, "beat_bh": beat,
                    "avg_return": round(sum(rets) / len(rets), 1) if rets else 0,
                    "median_return": rets[len(rets) // 2] if rets else 0})
            results.sort(key=lambda x: x["beat_bh"], reverse=True)
            body = json.dumps({"ok": True, "results": results})
            return self._send(200, body.encode("utf-8"), "application/json")
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
