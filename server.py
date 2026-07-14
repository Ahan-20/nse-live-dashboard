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


# ── Local .env loader (no extra dependency) ──────────────────────────
# On a local Mac run, credentials live in ~/Downloads/nse-live/.env so they
# never reach git. On Railway they come from the platform's env vars, so
# missing .env is fine. Must run BEFORE intraday is imported.
def _load_dotenv(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip(); v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import backtest as bt
try:
    from intraday import PROVIDER as INTRADAY
except Exception:
    INTRADAY = None

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


# ── Daily IV recorder → 30-day IV percentile (TEF-90 step 9) ─────────
# Kite gives current IV only, not historical. So we record ATM IV once per
# trading day into a small SQLite file. Once we have ≥ 30 days of history,
# we can compute the percentile automatically. Until then, the factor stays
# manual with a "collecting: N/30 days" note.

import sqlite3

IV_DB = os.path.join(HERE, "iv_history.db")


def _iv_db():
    con = sqlite3.connect(IV_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS iv_daily(
        date TEXT PRIMARY KEY,
        underlying TEXT,
        atm_iv REAL,
        spot REAL,
        recorded_at REAL
    )""")
    con.commit()
    return con


def _record_atm_iv_if_new(chain):
    """Persist today's ATM IV once per day. Idempotent."""
    if not chain or not chain.get("strikes"):
        return
    today = datetime.date.today().isoformat()
    con = _iv_db()
    exists = con.execute(
        "SELECT 1 FROM iv_daily WHERE date=? AND underlying=?",
        (today, chain.get("underlying", "NIFTY"))).fetchone()
    if exists:
        con.close(); return
    # ATM IV = IV of the strike closest to spot
    spot = chain.get("spot") or 0
    strikes = chain.get("strikes", [])
    if not spot or not strikes:
        con.close(); return
    closest = min(strikes, key=lambda r: abs(r["strike"] - spot))
    atm_iv = 0.0
    for side in ("ce", "pe"):
        d = closest.get(side) or {}
        iv = d.get("iv", 0) or 0
        if iv > 0:
            atm_iv = float(iv)
            break
    if atm_iv > 0:
        con.execute("INSERT OR REPLACE INTO iv_daily VALUES (?,?,?,?,?)",
                    (today, chain.get("underlying", "NIFTY"),
                     atm_iv, spot, time.time()))
        con.commit()
    con.close()


def _iv_percentile(underlying="NIFTY", window_days=90):
    """Return (auto, ok, detail) for the IV Percentile factor.
    Needs ≥ 30 rows of history to compute meaningfully."""
    try:
        con = _iv_db()
        rows = con.execute(
            "SELECT date, atm_iv FROM iv_daily WHERE underlying=? "
            "ORDER BY date DESC LIMIT ?",
            (underlying, window_days)).fetchall()
        con.close()
    except Exception:
        return (False, None, "Verify IV Percentile > 50 on Sensibull")
    n = len(rows)
    if n < 30:
        return (False, None,
                f"Verify IV Percentile > 50 on Sensibull (collecting: {n}/30 days)")
    ivs = sorted(r[1] for r in rows if r[1] and r[1] > 0)
    today_iv = rows[0][1]                  # rows[0] is newest by SELECT ORDER BY DESC
    # Percentile of today's IV among the window
    below = sum(1 for v in ivs if v < today_iv)
    pctile = (below / len(ivs)) * 100 if ivs else 0
    return (True, pctile > 50,
            f"IV Percentile = {pctile:.0f}% ({n}d window; today IV {today_iv:.1f})")


# ── Position close-out cost from live leg LTPs (kills Sensibull loop) ─
def _compute_close_out_cost(strategy, legs):
    """Given quoted legs (LTP each), return per-share cost to CLOSE the position.
      bps = short PE + long PE hedge:  cost = sellPE - buyPE
      bcs = short CE + long CE hedge:  cost = sellCE - buyCE
      ss  = short CE + short PE:       cost = sellCE + sellPE  (buy both back)
      ic  = 4 legs:                    cost = (sellCE-buyCE) + (sellPE-buyPE)
    Returns None if any required leg has no LTP.
    """
    by_id = {l["leg_id"]: l for l in legs if "leg_id" in l}
    if not by_id:
        return None
    def L(leg_id):
        v = by_id.get(leg_id)
        return v["ltp"] if v and v.get("ltp") is not None else None
    try:
        if strategy == "bps":
            s, b = L("sell"), L("buy")
            return round(s - b, 2) if (s is not None and b is not None) else None
        if strategy == "bcs":
            s, b = L("sell"), L("buy")
            return round(s - b, 2) if (s is not None and b is not None) else None
        if strategy == "ss":
            c, p = L("sellce"), L("sellpe")
            return round(c + p, 2) if (c is not None and p is not None) else None
        if strategy == "ic":
            sc, bc = L("sellce"), L("buyce")
            sp, bp = L("sellpe"), L("buype")
            if None in (sc, bc, sp, bp):
                return None
            return round((sc - bc) + (sp - bp), 2)
    except Exception:
        return None
    return None


# ── High-impact event calendar (auto-tick TEF-90 step 10) ───────────
_events_cache = None
_events_at = 0.0


def _load_events():
    """Read events.json once and cache. Returns list of {date, name, impact}."""
    global _events_cache, _events_at
    if _events_cache is not None and (time.time() - _events_at) < 3600:
        return _events_cache
    try:
        with open(os.path.join(HERE, "events.json")) as f:
            d = json.load(f)
        _events_cache = d.get("events", [])
        _events_at = time.time()
    except Exception:
        _events_cache = []
        _events_at = time.time()
    return _events_cache


def _check_event_window(today, days=3):
    """(auto, ok, detail) — 'ok' is True when NO high-impact event falls within
    `days` calendar days from `today`. Returns detail string naming what/when."""
    horizon = today + datetime.timedelta(days=days)
    events = _load_events()
    hits = []
    for e in events:
        try:
            ed = datetime.date.fromisoformat(e["date"])
        except Exception:
            continue
        if today <= ed <= horizon and e.get("impact") == "high":
            hits.append((ed, e["name"]))
    hits.sort()
    if hits:
        d, n = hits[0]
        days_off = (d - today).days
        when = "today" if days_off == 0 else f"in {days_off} day{'s' if days_off > 1 else ''}"
        return True, False, f"⚠ {n} {when}"
    # Also pull upcoming results from NSE corporate-actions (best-effort, cached)
    try:
        earnings = _upcoming_earnings_next(days)
        if earnings:
            e = earnings[0]
            return True, False, f"⚠ Results: {e['symbol']} on {e['date']}"
    except Exception:
        pass
    return True, True, f"no high-impact events in next {days} days ✓"


_earnings_cache = {"t": 0.0, "data": []}


def _upcoming_earnings_next(days=3):
    """Best-effort: query NSE's corporate-actions endpoint for FORTHCOMING results
    within `days`. Cached 1 h. Empty list on any failure."""
    if time.time() - _earnings_cache["t"] < 3600 and _earnings_cache["data"]:
        return _earnings_cache["data"]
    try:
        d = nse_get("/api/corporates-corporateActions?index=equities&subject=Financial%20Results")
    except Exception:
        return []
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=days)
    hits = []
    for row in (d or []):
        try:
            # NSE format: "24-Aug-2026"
            ex = row.get("exDate") or row.get("recDate") or ""
            d1 = datetime.datetime.strptime(ex, "%d-%b-%Y").date()
            if today <= d1 <= horizon:
                hits.append({"symbol": row.get("symbol", "?"), "date": d1.isoformat()})
        except Exception:
            continue
    _earnings_cache["data"] = hits
    _earnings_cache["t"] = time.time()
    return hits


# ── Kite Connect: request-token → access-token exchange ──────────────
def _kite_exchange_request_token(request_token):
    """POST to /session/token with a SHA-256 checksum. Returns access_token."""
    import hashlib
    api_key = os.environ.get("KITE_API_KEY", "")
    api_secret = os.environ.get("KITE_API_SECRET", "")
    if not api_key or not api_secret:
        raise RuntimeError("KITE_API_KEY / KITE_API_SECRET must be set")
    checksum = hashlib.sha256(
        (api_key + request_token + api_secret).encode()).hexdigest()
    body = urllib.parse.urlencode({
        "api_key": api_key,
        "request_token": request_token,
        "checksum": checksum,
    }).encode()
    req = urllib.request.Request(
        "https://api.kite.trade/session/token", data=body,
        headers={"X-Kite-Version": "3",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8"))
    tok = ((d.get("data") or {}).get("access_token")) or ""
    if not tok:
        raise RuntimeError(f"no access_token in response: {d}")
    return tok


# Kite access-token store lives inside the app dir so the service user can
# actually write it. systemd's ProtectSystem=strict + ReadWritePaths=$APP_DIR
# allows this; /etc/... would fail silently.
KITE_TOKEN_FILE = os.path.join(HERE, ".kite_token")


def _persist_kite_access_token(token):
    """Save the fresh access_token so it survives a service restart.
    Writes to $APP_DIR/.kite_token (nselive user can write); also updates the
    in-memory env for the currently-running worker."""
    os.environ["KITE_ACCESS_TOKEN"] = token
    try:
        with open(KITE_TOKEN_FILE, "w") as f:
            f.write(token)
        os.chmod(KITE_TOKEN_FILE, 0o600)
    except Exception as e:
        # Surface persist failures in logs so we don't silently rot again
        print(f"⚠️  Could not persist Kite token to {KITE_TOKEN_FILE}: {e}",
              flush=True)


def _load_persisted_kite_token():
    """On boot, restore the last-known access_token from disk if any."""
    if os.environ.get("KITE_ACCESS_TOKEN"):
        return                         # env-var wins
    try:
        if os.path.exists(KITE_TOKEN_FILE):
            with open(KITE_TOKEN_FILE) as f:
                tok = f.read().strip()
            if tok:
                os.environ["KITE_ACCESS_TOKEN"] = tok
                print(f"↺ Restored Kite access_token from {KITE_TOKEN_FILE}",
                      flush=True)
    except Exception as e:
        print(f"⚠️  Could not read {KITE_TOKEN_FILE}: {e}", flush=True)


_load_persisted_kite_token()


# ── BSE (SENSEX is a BSE index, not on NSE) ──────────────────────────
_bse_jar = http.cookiejar.CookieJar()
_bse_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_bse_jar))
BSE_HEADERS = {"User-Agent": HEADERS["User-Agent"], "Accept": "application/json",
               "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}
_sensex_last = None     # keep last good value (BSE flaky from datacenter IPs)


def _bse_warm():
    for u in ("https://www.bseindia.com/", "https://www.bseindia.com/sensex/code/16/"):
        try:
            _bse_opener.open(urllib.request.Request(
                u, headers={"User-Agent": HEADERS["User-Agent"]}), timeout=10).read()
        except Exception:
            pass


def fetch_sensex():
    """Live BSE SENSEX (scripcode 1) -> {val, chg, up}. Falls back to the last
    good value on transient datacenter blocks rather than going blank."""
    global _sensex_last
    url = ("https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w"
           "?Debtflag=&scripcode=1&seriesid=")
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=BSE_HEADERS)
            d = json.loads(_bse_opener.open(req, timeout=12).read().decode("utf-8"))
            cr = d.get("CurrRate", {})
            ltp = float(str(cr.get("LTP", "0")).replace(",", ""))
            pchg = float(str(cr.get("PcChg", "0")).replace(",", "") or 0)
            if ltp > 0:
                _sensex_last = {"val": f"{ltp:,.2f}", "chg": f"{pchg:+.2f}%",
                                "up": pchg >= 0}
                return _sensex_last
        except Exception:
            _bse_warm()
    return _sensex_last     # last good (or None on cold start)


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
        # Abhishek's 1H rule: stock crossed its 1H 200 EMA, then the next
        # 1H candle breaks the prior 1H candle's high (mirror for short).
        # Needs intraday data; gracefully reports "needs intraday" otherwise.
        h1 = {"state": "needs intraday", "fired": None}
        if INTRADAY and INTRADAY.enabled:
            # Need 220+ 1H bars for the 200 EMA. ~7 mkt hours × 20 trading days = 140,
            # so we ask for ~60 calendar days to comfortably clear the threshold.
            bars1h = INTRADAY.get_candles(sym, "1h", days=60)
            if len(bars1h) >= 220:
                c1 = [b["c"] for b in bars1h]
                e1 = bt.ema(c1, 200)
                # find most recent cross
                cross_i = None; cdir = None
                for i in range(len(c1) - 1, 200, -1):
                    if c1[i] > e1[i] and c1[i - 1] <= e1[i - 1]:
                        cross_i, cdir = i, "up"; break
                    if c1[i] < e1[i] and c1[i - 1] >= e1[i - 1]:
                        cross_i, cdir = i, "dn"; break
                if cross_i is not None and cross_i + 1 < len(bars1h):
                    prev_high = bars1h[cross_i]["h"]
                    prev_low = bars1h[cross_i]["l"]
                    nxt = bars1h[cross_i + 1]
                    if cdir == "up" and nxt["h"] > prev_high:
                        h1 = {"state": "LONG SIGNAL", "fired": nxt["dt"]}
                    elif cdir == "dn" and nxt["l"] < prev_low:
                        h1 = {"state": "SHORT SIGNAL", "fired": nxt["dt"]}
                    else:
                        h1 = {"state": "Crossed, waiting for break", "fired": None}
                else:
                    h1 = {"state": "No recent 1H cross", "fired": None}
            else:
                h1 = {"state": "intraday data insufficient", "fired": None}
        rows.append({
            "symbol": sym, "chg": round(ch, 2), "ltp": round(ltp, 2),
            "side": "gainer" if ch >= 0 else "loser",
            "sector": rep[0] if rep else "—",
            "sector_chg": round(rep[1], 2) if rep else None,
            "daily": {"label": d_lbl, "dir": d_dir, "dist": round(d_dist, 2) if d_dist is not None else None},
            "weekly": {"label": w_lbl, "dir": w_dir, "dist": round(w_dist, 2) if w_dist is not None else None},
            "h1_rule": h1,                # Abhishek's 1H breakout rule
            "cross_today": d_dir is not None, "cross_dir": cross_dir,
            "cross_any": (d_dir is not None) or (w_dir is not None),
            "setup": setup,
        })
    rank = {"LONG": 4, "SHORT": 4, "CROSS-UP": 3, "CROSS-DN": 3, "": 0}
    rows.sort(key=lambda r: (rank.get(r["setup"], 0), r["weekly"]["dir"] is not None,
                             abs(r["chg"])), reverse=True)
    return {"ok": True, "universe": universe, "asOf": (all_idx.get("timestamp", "") or "")[-8:],
            "data_date": live_date, "rows": rows,
            "intraday_connected": bool(INTRADAY and INTRADAY.enabled),
            "intraday_provider": INTRADAY.name if INTRADAY else "none"}


_tf_cache = {"t": 0.0, "data": {}}


def cached_tradefinder(universe):
    now = time.time()
    if universe in _tf_cache["data"] and now - _tf_cache["t"] < 30:
        return _tf_cache["data"][universe]
    d = build_tradefinder(universe)
    _tf_cache["data"][universe] = d
    _tf_cache["t"] = now
    return d


# ── Bull Put Spread setup screener ───────────────────────────────────
# Implements the documented 10-step funnel (steps 1-5 automated; steps 7-9 need
# the option chain, which NSE blocks from datacenter IPs — so those become
# "open in Sensibull / NSE option chain" manual links).
try:
    with open(os.path.join(HERE, "fo_list.json")) as _f:
        FO_LIST = set(json.load(_f))
except Exception:
    FO_LIST = set()
try:
    with open(os.path.join(HERE, "lot_sizes.json")) as _f:
        LOT_SIZES = json.load(_f)        # {symbol: int}
except Exception:
    LOT_SIZES = {}


_bullput_cache = {"t": 0.0, "data": None}


def _stock_daily_indicators(sym):
    """Returns dict with last close, 200 EMA, EMA slope%, ADX(14) — or None."""
    try:
        bars = bt.load(sym)
        if len(bars) < 250:
            return None
        h = [b["h"] for b in bars]; l = [b["l"] for b in bars]; c = [b["c"] for b in bars]
        e200 = bt.ema(c, 200)
        adx = bt.adx(h, l, c, 14)
        slope = bt.slope_pct(e200, 5)
        return {"close": c[-1], "ema200": e200[-1],
                "slope_pct": slope[-1], "adx": adx[-1]}
    except Exception:
        return None


def _bullput_verify_from_chain(symbol, spot):
    """Auto-compute the 4 flowchart checklist items from a live Kite option chain.
    Returns {mp, delta, put_wall, call_wall} — each a dict with {ok, detail}."""
    chain = INTRADAY.option_chain(symbol)
    if not chain or not chain.get("strikes"):
        return None
    strikes = chain["strikes"]
    max_pain = chain.get("max_pain", 0)
    cmp_ = chain.get("spot") or spot

    # (7) Max Pain ≥ CMP  → market makers benefit from price staying up/rising
    mp_ok = max_pain >= cmp_
    mp_detail = (f"Max Pain {int(max_pain)} vs CMP {int(cmp_)} "
                 f"({int(max_pain - cmp_):+d})")

    # (8) Sell PE Δ 0.20-0.30  → find a PE strike with abs(Δ) in that range.
    #     Uses put-call parity approximation from OTM PE prices:
    #       For a PE below spot, if strike moves up by 1, PE gains ~|Δ|.
    #     Cleaner: use the strike whose PE LTP suggests it's about 20-30% OTM.
    #     Delta ≈ 0.25 for a PE roughly 1 ATR below spot on typical F&O expiries.
    #     We approximate by picking the strike between 92-96% of spot (delta band
    #     for 20-30 days to expiry).
    tgt_lo = cmp_ * 0.92
    tgt_hi = cmp_ * 0.96
    delta_pe = None
    for r in strikes:
        s = r["strike"]
        if tgt_lo <= s <= tgt_hi and r.get("pe") and r["pe"].get("ltp", 0) > 0:
            delta_pe = s
            break
    delta_ok = delta_pe is not None
    delta_detail = (f"~Δ 0.20-0.30 PE strike ≈ {int(delta_pe)}"
                    if delta_pe else "no PE strike in the 92-96% band with liquid LTP")

    # (9a) Put OI wall near sell strike  → a big Put-OI strike within 3% of it
    put_wall_ok = False
    put_wall_detail = "no Put OI wall near sell strike"
    if delta_pe is not None:
        put_ois = [(r["strike"], (r.get("pe") or {}).get("oi", 0))
                   for r in strikes if r["strike"] <= cmp_]
        if put_ois:
            biggest = max(put_ois, key=lambda x: x[1])
            median = sorted(o for _, o in put_ois if o > 0)
            med = median[len(median) // 2] if median else 0
            near = abs(biggest[0] - delta_pe) / delta_pe <= 0.03
            strong = med > 0 and biggest[1] > 2 * med
            put_wall_ok = near and strong
            put_wall_detail = (f"biggest Put OI @ {int(biggest[0])} "
                               f"({biggest[1]:,}) — "
                               f"{'near' if near else 'far from'} sell strike {int(delta_pe)}")

    # (9b) Call OI NOT stacked above CMP  → total Call OI above CMP should NOT
    #      dominate total Call OI below. If it does, there's overhead resistance.
    calls_above = sum((r.get("ce") or {}).get("oi", 0)
                      for r in strikes if r["strike"] > cmp_)
    calls_below = sum((r.get("ce") or {}).get("oi", 0)
                      for r in strikes if r["strike"] <= cmp_)
    total_calls = calls_above + calls_below
    if total_calls == 0:
        call_ok = False
        call_detail = "no Call OI data"
    else:
        ratio = calls_above / total_calls
        # Healthy = calls balanced or leaning below CMP (no overhead ceiling).
        # If >60% of Call OI is stacked above CMP, that's a bearish overhead.
        call_ok = ratio < 0.60
        call_detail = (f"{int(ratio*100)}% of Call OI is above CMP "
                       f"({'stacked overhead' if not call_ok else 'not overhead-heavy'})")

    return {
        "mp":        {"ok": mp_ok,        "detail": mp_detail},
        "delta":     {"ok": delta_ok,     "detail": delta_detail},
        "put_wall":  {"ok": put_wall_ok,  "detail": put_wall_detail},
        "call_wall": {"ok": call_ok,      "detail": call_detail},
    }


def build_bullput():
    all_idx = nse_get("/api/allIndices")
    by_name = {r.get("index"): float(r.get("percentChange", 0))
               for r in all_idx.get("data", [])}
    nifty50_chg = by_name.get("NIFTY 50", 0)

    sensex_data = None
    try:
        sensex_data = fetch_sensex()
    except Exception:
        pass
    sensex_chg = None
    if sensex_data:
        try:
            sensex_chg = float(sensex_data["chg"].replace("%", "").replace("+", ""))
        except Exception:
            sensex_chg = None

    # STEP 1: Market bias gate — both Sensex & Nifty must be positive.
    market_ok = (nifty50_chg > 0) and (sensex_chg is not None and sensex_chg > 0)

    # STEP 3: Sector strength — find sectors > 0.5%, mark "strong" if > 1%.
    strong_sectors = {disp for disp, ch in (
        (r.get("index"), float(r.get("percentChange", 0)))
        for r in all_idx.get("data", []))
        if disp and ch > 1.0 and disp.startswith("NIFTY") and disp != "NIFTY 50"}

    # STEP 2: gainers from F&O bucket of live-analysis-variations
    def variations(kind, bucket):
        d = nse_get("/api/live-analysis-variations?index=" + kind)
        return (d.get(bucket) or {}).get("data", [])

    fo_gainers = variations("gainers", "FOSec")
    nifty50_gainers_set = {s.get("symbol") for s in variations("gainers", "NIFTY")}
    fo_gainers_set = {s.get("symbol") for s in fo_gainers}

    rows = []
    for s in fo_gainers:
        sym = s.get("symbol")
        if not sym:
            continue
        ch = float(s.get("perChange", 0))
        if ch <= 0:                    # we only want upside momentum for Bull Put
            continue
        ltp = float(s.get("ltp", 0) or 0)

        # STEP 4: F&O universe matching (out of 4) — F&O member, F&O gainer,
        # in Nifty 50 gainers (optional), part of a strong sector.
        sectors = SECTOR_MAP.get(sym, [])
        sector_chgs = [(d, by_name.get(d.upper())) for d in sectors
                       if by_name.get(d.upper()) is not None]
        rep_sector = max(sector_chgs, key=lambda x: x[1]) if sector_chgs else None
        in_strong_sector = rep_sector and rep_sector[0] in strong_sectors

        match_filters = {
            "fo": sym in FO_LIST,
            "fo_gainer": sym in fo_gainers_set,
            "nifty50_gainer": sym in nifty50_gainers_set,
            "strong_sector": bool(in_strong_sector),
        }
        match_score = sum(match_filters.values())

        # STEP 5: Daily timeframe confirmation — price > 200 EMA, EMA rising, ADX
        ind = _stock_daily_indicators(sym)
        if not ind:
            daily = {"ok": False, "reason": "no data"}
            daily_pass = False
        else:
            above = ind["close"] > ind["ema200"]
            rising = ind["slope_pct"] > 0
            trending = ind["adx"] > 20
            daily_pass = above and rising and trending
            daily = {
                "ok": True, "close": round(ind["close"], 2),
                "ema200": round(ind["ema200"], 2),
                "above_ema": above, "rising_ema": rising,
                "ema_slope_pct": round(ind["slope_pct"], 2),
                "adx": round(ind["adx"], 1), "adx_trending": trending,
                "passes": daily_pass,
            }

        # Funnel verdict
        passes_step4 = match_score >= 3
        all_pass = market_ok and passes_step4 and daily_pass

        # ── Auto-tick steps 7-9 from live Kite option chain (per stock) ──
        # Only fetch for candidates that already pass daily filters — keeps the
        # scan fast (5-10 chain fetches instead of 20).
        verify = None
        if all_pass and INTRADAY and INTRADAY.enabled and hasattr(INTRADAY, "option_chain"):
            try:
                verify = _bullput_verify_from_chain(sym, ltp)
            except Exception:
                verify = None

        rows.append({
            "symbol": sym, "chg": round(ch, 2), "ltp": round(ltp, 2),
            "lot_size": LOT_SIZES.get(sym),
            "sector": rep_sector[0] if rep_sector else "—",
            "sector_chg": round(rep_sector[1], 2) if rep_sector else None,
            "fo_match": {"score": match_score, "filters": match_filters},
            "daily": daily,
            "passes_all": all_pass,
            "verify_auto": verify,     # None = still manual; dict = auto-ticked
            "links": {
                "sensibull": f"https://web.sensibull.com/option-chain?tradingsymbol={sym}",
                "nse_chain": f"https://www.nseindia.com/option-chain?symbol={sym}",
            },
        })

    # Rank: passes_all first, then by match_score, then by % change
    rows.sort(key=lambda r: (
        r["passes_all"], r["fo_match"]["score"],
        r["daily"]["passes"] if r["daily"].get("ok") else False,
        r["chg"]), reverse=True)

    return {
        "ok": True,
        "asOf": (all_idx.get("timestamp", "") or "")[-8:],
        "market": {
            "nifty_chg": round(nifty50_chg, 2),
            "sensex_chg": round(sensex_chg, 2) if sensex_chg is not None else None,
            "ok": market_ok,
            "note": ("Both indices positive — proceed." if market_ok
                     else "Market not positive on both indices — AVOID Bull Put Spread."),
        },
        "strong_sectors": sorted(strong_sectors),
        "n_pass": sum(1 for r in rows if r["passes_all"]),
        "n_candidates": len(rows),
        "rows": rows,
    }


def cached_bullput():
    now = time.time()
    if _bullput_cache["data"] and now - _bullput_cache["t"] < 45:
        return _bullput_cache["data"]
    d = build_bullput()
    _bullput_cache["data"] = d
    _bullput_cache["t"] = now
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


# ── THETA EDGE FILTER (TEF-90) score ─────────────────────────────────
# Auto-scored factors from historical + live data. Manual items (PCR, Max Pain,
# IV, OI, event calendar) come from the UI checklist.
_tef_cache = {"t": 0.0, "data": None}


def build_tef_score():
    """Auto-checkable side of TEF-90. Returns raw values so the UI can render
    a per-factor checklist and let the user tick the manual items."""
    # NIFTY 50 historicals via NIFTYBEES (1:1 ETF tracker) — we have 6y in DB.
    bars = bt.load("NIFTYBEES")
    if len(bars) < 220:
        return {"ok": False, "error": "not enough NIFTY history"}
    c = [b["c"] for b in bars]; h = [b["h"] for b in bars]; l = [b["l"] for b in bars]
    o = [b["o"] for b in bars]
    e200 = bt.ema(c, 200); e20 = bt.ema(c, 20)
    a = bt.atr(h, l, c, 14)
    rsiv = bt.rsi(c, 14)

    # Live NIFTY 50 % change today (NIFTYBEES tracks NIFTY 1:1 in % terms, so
    # we compute all indicators on NIFTYBEES bars and scale today's implied
    # close from the live NIFTY %-change).
    nifty_live = None; live_pchg = None
    try:
        d = nse_get("/api/allIndices")
        row = next((r for r in d.get("data", []) if r.get("index") == "NIFTY 50"), None)
        if row:
            nifty_live = float(row.get("last"))
            live_pchg = float(row.get("percentChange", 0))
    except Exception:
        pass
    prev_c = c[-1]                                # last NIFTYBEES close from DB
    # Today's implied NIFTYBEES close, scaled from live NIFTY 50's %-change:
    cmp = prev_c * (1 + (live_pchg or 0) / 100)

    # === TEF-90 factor computations ===
    today = datetime.date.today()

    # (Removed: Date filter. Abhishek prefers score to reflect market state only,
    # not the calendar-based OI-build heuristic.)

    # === Event calendar (RBI/Fed/Budget/results in next 3 days) ===
    ev_auto = ev_ok = False
    ev_detail = "Confirm no RBI/Fed/Budget/results in next 3 days"
    try:
        ev_auto, ev_ok, ev_detail = _check_event_window(today, days=3)
    except Exception:
        pass

    # 2. Sideways / Near 200 EMA: distance from 200 EMA in ATR units
    dist_pct = (cmp - e200[-1]) / e200[-1] * 100 if e200[-1] else 0
    near_ema = abs(dist_pct) < 3.0                # within ~3% = sideways-ish

    # 3. No strong HH/HL or LH/LL: use 20-day range slope, ADX
    adx = bt.adx(h, l, c, 14)[-1] if hasattr(bt, "adx") else _adx14(h, l, c)
    # EMA slope (5-day)
    slope20 = ((c[-1] - c[-6]) / c[-6] * 100) if len(c) >= 6 else 0
    no_strong_trend = adx < 20 and abs(slope20) < 2.5

    # 4. Range-bound: last 3 daily ranges as % of ATR
    last_ranges = [(h[i] - l[i]) for i in range(-3, 0)]
    avg_range = sum(last_ranges) / 3
    range_bound = avg_range < 1.2 * a[-1]         # today's ATR bar

    # 5. Small candles: median body / range over 5 sessions
    def body_ratio(i):
        rng = h[i] - l[i]
        return (abs(c[i] - o[i]) / rng) if rng > 0 else 0
    med_body = sorted(body_ratio(i) for i in range(-5, 0))[2]
    small_candles = med_body < 0.55

    # 6. No gap open: today's move vs prev close < 1%
    #    (this doubles as one of the auto-skip red-flag checks)
    gap_pct = abs(live_pchg) if live_pchg is not None else 0
    no_gap = gap_pct < 1.0

    # 7. RSI 40-60 (part of the sideways classifier)
    rsi = rsiv[-1]
    rsi_neutral = 40 <= rsi <= 60

    # === Market classifier ===
    if near_ema and no_strong_trend and rsi_neutral:
        market_type = "sideways"
    elif dist_pct > 3 and slope20 > 1 and adx > 20:
        market_type = "trend_up"
    elif dist_pct < -3 and slope20 < -1 and adx > 20:
        market_type = "trend_down"
    else:
        market_type = "mixed"

    # === PCR + Max Pain + OI walls + IV %ile from live NIFTY option chain ===
    pcr_auto = mp_auto = oi_walls_auto = iv_auto = False
    pcr_ok = mp_ok = oi_walls_ok = iv_ok = None
    pcr_detail = "Verify PCR is 0.9-1.2 on Sensibull"
    mp_detail = "Verify Max Pain within ±100 pts of CMP"
    oi_walls_detail = "Both Put + Call OI walls near CMP (both sides supported)"
    iv_detail = "Verify IV Percentile > 50 on Sensibull"
    pcr_val = None; max_pain_val = None
    chain_error = None                         # surface Kite errors via API
    if INTRADAY and INTRADAY.enabled and hasattr(INTRADAY, "option_chain"):
        try:
            chain = INTRADAY.option_chain("NIFTY")
            if not chain:
                # Rich diagnostic: what names + segments are ACTUALLY in the CSV,
                # so we can see what value to filter on.
                try:
                    inst = INTRADAY._nfo_instruments()
                    n_all = len(inst)
                    # Sample distinct name values (top 20 by frequency)
                    from collections import Counter
                    name_counts = Counter(r.get("name","") for r in inst)
                    top_names = name_counts.most_common(15)
                    segment_counts = Counter(r.get("segment","") for r in inst)
                    # Anything that even LOOKS like NIFTY (case-insensitive contains)
                    nifty_like = sorted({r.get("name","") for r in inst
                                         if "NIFTY" in r.get("name","").upper()})[:10]
                    # Sample the first row's structure so we see field mapping
                    sample = None
                    if inst:
                        s = inst[0]
                        sample = {k: str(s.get(k))[:40] for k in
                                  ("name", "tradingsymbol", "segment",
                                   "instrument_type", "expiry", "strike")}
                    chain_error = {
                        "n_total": n_all,
                        "top_15_names": top_names,
                        "segments": dict(segment_counts),
                        "nifty_like_names": nifty_like,
                        "sample_row": sample,
                    }
                except Exception as e2:
                    chain_error = f"chain=None + instruments fetch failed: {e2}"
            if chain:
                pcr_val = chain.get("pcr")
                max_pain_val = chain.get("max_pain")
                nifty_spot = chain.get("spot") or nifty_live or 0
                pcr_auto = True
                pcr_ok = (pcr_val is not None) and (0.9 <= pcr_val <= 1.2)
                pcr_detail = f"live PCR = {pcr_val}"
                mp_auto = True
                if max_pain_val and nifty_spot:
                    mp_ok = abs(max_pain_val - nifty_spot) <= 100
                    mp_detail = (f"Max Pain {int(max_pain_val)} vs CMP {int(nifty_spot)} "
                                 f"({int(max_pain_val - nifty_spot):+d} pts)")

                # Record today's ATM IV (idempotent) and compute the percentile
                _record_atm_iv_if_new(chain)
                iv_auto, iv_ok, iv_detail = _iv_percentile("NIFTY")

                # ── OI walls: check Put wall below CMP AND Call wall above CMP ──
                # A "wall" = strike whose OI exceeds 2× the median non-zero OI in
                # its side. A healthy range-bound market has BOTH so a Strangle
                # / Iron Condor has support and resistance to lean on.
                strikes = chain.get("strikes", [])
                if strikes and nifty_spot:
                    put_ois = [(r["strike"], (r["pe"] or {}).get("oi", 0))
                               for r in strikes if r["strike"] < nifty_spot and r.get("pe")]
                    call_ois = [(r["strike"], (r["ce"] or {}).get("oi", 0))
                                for r in strikes if r["strike"] > nifty_spot and r.get("ce")]
                    put_median = sorted(o for _, o in put_ois if o > 0)
                    call_median = sorted(o for _, o in call_ois if o > 0)
                    def _median(xs): return xs[len(xs) // 2] if xs else 0
                    p_med = _median(put_median)
                    c_med = _median(call_median)
                    # Find biggest Put OI below CMP and biggest Call OI above
                    put_wall = max(put_ois, key=lambda x: x[1], default=(None, 0))
                    call_wall = max(call_ois, key=lambda x: x[1], default=(None, 0))
                    has_put_wall = put_wall[0] is not None and p_med > 0 and put_wall[1] > 2 * p_med
                    has_call_wall = call_wall[0] is not None and c_med > 0 and call_wall[1] > 2 * c_med
                    oi_walls_auto = True
                    oi_walls_ok = has_put_wall and has_call_wall
                    if put_wall[0] and call_wall[0]:
                        oi_walls_detail = (
                            f"Put wall @ {int(put_wall[0])} (OI {put_wall[1]:,}); "
                            f"Call wall @ {int(call_wall[0])} (OI {call_wall[1]:,})")
        except Exception as e:
            chain_error = f"exception: {type(e).__name__}: {e}"

    # Auto-score tally (6 base items + optional 2 auto-ticked via Kite chain)
    auto_score = sum([near_ema, no_strong_trend, range_bound,
                      small_candles, no_gap, rsi_neutral])
    # PCR + Max Pain + OI walls + IV percentile + event calendar all
    # auto-tick when their upstream data is available.
    if pcr_auto and pcr_ok: auto_score += 1
    if mp_auto and mp_ok: auto_score += 1
    if oi_walls_auto and oi_walls_ok: auto_score += 1
    if iv_auto and iv_ok: auto_score += 1
    if ev_auto and ev_ok: auto_score += 1
    auto_total = auto_score
    # Base 6 items; +1 each for the auto-tickable items whose data is present
    auto_score_max = 6
    if pcr_auto:      auto_score_max += 1
    if mp_auto:       auto_score_max += 1
    if oi_walls_auto: auto_score_max += 1
    if iv_auto:       auto_score_max += 1
    if ev_auto:       auto_score_max += 1

    return {
        "ok": True,
        "as_of": today.isoformat(),
        "nifty_live": round(nifty_live, 2) if nifty_live else None,
        "cmp": round(cmp, 2),         # NIFTYBEES-scale (for internal math)
        "prev_close": round(prev_c, 2),
        "day_pchg": round(gap_pct, 2) if live_pchg is not None else None,
        "ema200": round(e200[-1], 2),
        "dist_from_ema_pct": round(dist_pct, 2),
        "adx": round(adx, 1),
        "slope_5d_pct": round(slope20, 2),
        "atr": round(a[-1], 2),
        "avg_range_3d": round(avg_range, 2),
        "median_body_ratio_5d": round(med_body, 2),
        "rsi": round(rsi, 1),
        "market_type": market_type,
        "auto_score": auto_total,
        "auto_score_max": auto_score_max,
        "pcr": pcr_val,
        "max_pain": max_pain_val,
        "chain_error": chain_error,           # None on success
        "factors": [
            {"id":"near_ema",   "step":1, "auto":True,  "ok":near_ema,  "detail":f"NIFTY {'{:+.2f}'.format(dist_pct)}% from 200 EMA"},
            {"id":"no_trend",   "step":2, "auto":True,  "ok":no_strong_trend, "detail":f"ADX {adx:.1f} · 5d slope {slope20:+.2f}%"},
            {"id":"range_bound","step":3, "auto":True,  "ok":range_bound, "detail":f"avg 3-day range {avg_range:.2f} vs ATR {a[-1]:.2f}"},
            {"id":"small_cndl", "step":4, "auto":True,  "ok":small_candles, "detail":f"median body/range {med_body:.2f} (5d)"},
            {"id":"no_gap",     "step":5, "auto":True,  "ok":no_gap,    "detail":f"today gap {gap_pct:.2f}%"},
            {"id":"rsi",        "step":6, "auto":True,  "ok":rsi_neutral, "detail":f"RSI {rsi:.1f}"},
            # Manual items — the UI will render them as un-ticked toggles
            {"id":"pcr",        "step":7, "auto":pcr_auto, "ok":pcr_ok,
             "detail":pcr_detail},
            {"id":"maxpain",    "step":8, "auto":mp_auto, "ok":mp_ok,
             "detail":mp_detail},
            {"id":"iv",         "step":9, "auto":iv_auto, "ok":iv_ok, "detail":iv_detail},
            {"id":"no_event",   "step":10,"auto":ev_auto, "ok":ev_ok,
             "detail":ev_detail},
            {"id":"oi_walls",   "step":11,"auto":oi_walls_auto, "ok":oi_walls_ok,
             "detail":oi_walls_detail},
        ],
    }


def _adx14(h, l, c):
    """Minimal ADX(14) so we don't crash if backtest.adx isn't exposed."""
    n = 14
    if len(c) < n + 2:
        return 0
    tr = [max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])) for i in range(1, len(c))]
    up = [max(0, h[i] - h[i-1]) for i in range(1, len(c))]
    dn = [max(0, l[i-1] - l[i]) for i in range(1, len(c))]
    pdm = [u if u > d else 0 for u, d in zip(up, dn)]
    ndm = [d if d > u else 0 for u, d in zip(up, dn)]
    def rma(xs):
        v = sum(xs[:n]) / n
        out = [v]
        for x in xs[n:]:
            v = (v * (n - 1) + x) / n; out.append(v)
        return out
    atrs = rma(tr); pdi = rma(pdm); ndi = rma(ndm)
    dx = []
    for a_, p_, n_ in zip(atrs, pdi, ndi):
        s = p_ + n_
        dx.append(100 * abs(p_ - n_) / s if s > 0 else 0)
    if len(dx) < n: return 0
    adx = sum(dx[:n]) / n
    for x in dx[n:]:
        adx = (adx * (n - 1) + x) / n
    return adx


def cached_tef_score():
    now = time.time()
    if _tef_cache["data"] and now - _tef_cache["t"] < 60:
        return _tef_cache["data"]
    d = build_tef_score()
    _tef_cache["data"], _tef_cache["t"] = d, now
    return d


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
        if self.path.startswith("/api/bullput"):
            try:
                rep = cached_bullput()
            except Exception as e:
                rep = {"ok": False, "error": str(e), "rows": []}
            return self._send(200, json.dumps(rep).encode("utf-8"), "application/json")
        # Kite Connect daily-login browser flow.
        # Visiting /kite/login redirects you to Zerodha's OAuth page. After
        # you sign in there, Zerodha redirects back to /kite/callback with a
        # request_token that we exchange for an access_token (valid ~24h).
        if self.path.startswith("/kite/login"):
            key = os.environ.get("KITE_API_KEY", "")
            if not key:
                return self._send(500, b"KITE_API_KEY not set", "text/plain")
            url = f"https://kite.zerodha.com/connect/login?v=3&api_key={key}"
            self.send_response(302)
            self.send_header("Location", url); self.end_headers()
            return
        if self.path.startswith("/kite/callback"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            rtok = (q.get("request_token", [""])[0] or "").strip()
            status = (q.get("status", [""])[0] or "").strip()
            if status != "success" or not rtok:
                return self._send(400, b"Kite login failed", "text/plain")
            try:
                access = _kite_exchange_request_token(rtok)
            except Exception as e:
                return self._send(500, f"Kite exchange failed: {e}".encode(), "text/plain")
            # Persist so the next server restart still has it.
            _persist_kite_access_token(access)
            os.environ["KITE_ACCESS_TOKEN"] = access
            html = (b"<html><body style='font-family:sans-serif;background:#0d1b2a;color:#c9d1d9;padding:40px'>"
                    b"<h2 style='color:#00ff9d'>&#10003; Kite connected</h2>"
                    b"<p>Access token stored. You can close this tab.</p>"
                    b"<p>Token expires at ~06:00 IST tomorrow \xe2\x80\x94 revisit "
                    b"<a style='color:#00e5ff' href='/kite/login'>/kite/login</a> then.</p>"
                    b"</body></html>")
            return self._send(200, html, "text/html; charset=utf-8")
        if self.path.startswith("/api/tef-picks"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            strat = (q.get("strategy", ["ic"])[0] or "ic").lower()
            if strat not in ("ic", "ss"):
                return self._send(200,
                    json.dumps({"ok": False, "error": f"unsupported strategy '{strat}'"}).encode(),
                    "application/json")
            if not INTRADAY or not INTRADAY.enabled or not hasattr(INTRADAY, "strategy_picks"):
                return self._send(200,
                    json.dumps({"ok": False, "error": "Kite not connected"}).encode(),
                    "application/json")
            try:
                # Pass NIFTY spot from allIndices so we don't depend on Kite's
                # underlying_value field (which is sometimes missing).
                spot_override = None
                try:
                    ai = nse_get("/api/allIndices")
                    row = next((r for r in ai.get("data", []) if r.get("index") == "NIFTY 50"), None)
                    if row: spot_override = float(row.get("last") or 0)
                except Exception:
                    pass
                picks = INTRADAY.strategy_picks("NIFTY", strat, spot_override=spot_override)
                # New: picks is either a full picks dict (has 'legs') or an
                # {error: reason} dict — pass either through so UI can show it.
                if not picks:
                    rep = {"ok": False, "error": "option chain unavailable"}
                elif isinstance(picks, dict) and "error" in picks and "legs" not in picks:
                    rep = {"ok": False, "error": picks["error"]}
                else:
                    rep = {"ok": True, "picks": picks}
            except Exception as e:
                rep = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            return self._send(200, json.dumps(rep).encode("utf-8"), "application/json")
        if self.path.startswith("/api/tef-score"):
            try:
                rep = cached_tef_score()
            except Exception as e:
                rep = {"ok": False, "error": str(e)}
            return self._send(200, json.dumps(rep).encode("utf-8"), "application/json")
        if self.path.startswith("/api/kite-debug"):
            # Returns raw Kite state so we can see WHY option chain fetch fails.
            # Read-only diagnostic; safe to expose (no secrets in output).
            try:
                inst = INTRADAY._nfo_instruments()
                nifty_opts = [r for r in inst
                              if r.get("name") == "NIFTY"
                              and r.get("segment") == "NFO-OPT"]
                # Pick nearest upcoming expiry
                today = datetime.date.today().isoformat()
                upcoming = sorted({r["expiry"] for r in nifty_opts if r["expiry"] >= today})
                near_expiry = upcoming[0] if upcoming else None
                same_expiry = [r for r in nifty_opts if r["expiry"] == near_expiry] if near_expiry else []
                # Grab first CE + first PE + do a /quote call
                sample_ce = next((r for r in same_expiry if r["instrument_type"] == "CE"), None)
                sample_pe = next((r for r in same_expiry if r["instrument_type"] == "PE"), None)
                probes = [x for x in (sample_ce, sample_pe) if x]
                raw_quote_response = None
                lookup_keys_tried = []
                lookup_results = {}
                if probes:
                    # Try both formats — token AND EXCHANGE:TRADINGSYMBOL — so
                    # we can see which one Kite actually accepts.
                    identifiers = [f"NFO:{p['tradingsymbol']}" for p in probes]
                    qs = "&".join(f"i={urllib.parse.quote(idn)}" for idn in identifiers)
                    try:
                        raw_quote_response = INTRADAY._get(f"/quote?{qs}")
                    except Exception as e:
                        raw_quote_response = {"error": f"{type(e).__name__}: {e}",
                                              "url_tried": f"/quote?{qs}"}
                    # Show what keys we'd try and whether they hit
                    data = ((raw_quote_response or {}).get("data") or {})
                    for p in probes:
                        k1 = str(p["token"])
                        k2 = f"NFO:{p['tradingsymbol']}"
                        lookup_keys_tried.append({
                            "tradingsymbol": p["tradingsymbol"],
                            "token": p["token"],
                            "tried_str_token": k1 in data,
                            "tried_nfo_prefix": k2 in data,
                            "actual_response_keys": list(data.keys())[:5],
                        })
                rep = {
                    "ok": True,
                    "total_nfo_rows": len(inst),
                    "nifty_opt_rows": len(nifty_opts),
                    "upcoming_expiries": upcoming[:5],
                    "nearest_expiry": near_expiry,
                    "strikes_in_nearest": len(same_expiry),
                    "sample_ce": sample_ce and {k: sample_ce[k] for k in
                                    ("tradingsymbol","token","strike","expiry","instrument_type")},
                    "sample_pe": sample_pe and {k: sample_pe[k] for k in
                                    ("tradingsymbol","token","strike","expiry","instrument_type")},
                    "raw_quote_response": raw_quote_response,
                    "lookup_analysis": lookup_keys_tried,
                }
            except Exception as e:
                rep = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            return self._send(200, json.dumps(rep, default=str).encode("utf-8"),
                              "application/json")
        if self.path.startswith("/api/position-quote"):
            # GET: /api/position-quote?legs=<url-encoded JSON list>&strategy=bps
            # Frontend just URL-encodes the position's legs so we can bulk-price
            # them and return current close-out cost. Kills the last Sensibull dep.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            legs_raw = q.get("legs", ["[]"])[0]
            strategy = q.get("strategy", [""])[0]
            try:
                legs = json.loads(legs_raw)
                if not INTRADAY or not INTRADAY.enabled or not hasattr(INTRADAY, "quote_option_legs"):
                    return self._send(200, json.dumps(
                        {"ok": False, "error": "Kite not connected"}).encode(),
                        "application/json")
                quoted = INTRADAY.quote_option_legs(legs)
                # Compute the strategy-specific close-out cost per share.
                current_cost = _compute_close_out_cost(strategy, quoted)
                rep = {"ok": True, "legs": quoted, "current_cost": current_cost}
            except Exception as e:
                rep = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            return self._send(200, json.dumps(rep).encode("utf-8"), "application/json")
        if self.path.startswith("/api/tradefinder"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            uni = q.get("universe", ["nifty50"])[0]
            uni = uni if uni in ("nifty50", "fo") else "nifty50"
            try:
                rep = cached_tradefinder(uni)
            except Exception as e:
                rep = {"ok": False, "error": str(e), "rows": []}
            return self._send(200, json.dumps(rep).encode("utf-8"), "application/json")
        if self.path.startswith("/api/tfcompare"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sym = (q.get("symbol", [""])[0] or "").strip().upper()
            if not sym:
                return self._send(400, b'{"ok":false,"error":"missing symbol"}',
                                  "application/json")
            try:
                rep = bt.multi_timeframe_compare(sym)
                rep["ok"] = True
                rep["intraday_connected"] = bool(INTRADAY and INTRADAY.enabled)
                rep["intraday_provider"] = INTRADAY.name if INTRADAY else "none"
            except Exception as e:
                rep = {"ok": False, "error": str(e)}
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


def _startup_banner():
    """Clear on-boot status: shows the user (a) whether intraday is wired up,
    (b) whether their current public IP matches the one Groww has whitelisted.
    Catches the most common 'why is intraday silently failing?' cause."""
    print(f"\nNSE live dashboard  →  http://{HOST}:{PORT}")
    if INTRADAY and INTRADAY.enabled:
        print(f"  • Intraday provider : {INTRADAY.name}  (15m + 1H signals: ON)")
        # Check public IP vs Groww-registered IP
        registered = os.environ.get("GROWW_REGISTERED_IP", "").strip()
        try:
            req = urllib.request.Request("https://api.ipify.org",
                                         headers={"User-Agent": "curl/8"})
            current = urllib.request.urlopen(req, timeout=8).read().decode().strip()
            if registered and current != registered:
                print(f"  ⚠️  Public IP changed! Currently {current}, Groww has {registered}")
                print(f"     Groww calls will fail until you re-register {current}.")
            elif registered:
                print(f"  • Public IP         : {current}  (matches Groww ✓)")
            else:
                print(f"  • Public IP         : {current}  (set GROWW_REGISTERED_IP in .env "
                      f"to enable drift check)")
        except Exception as e:
            print(f"  • Public IP check skipped: {e}")
    else:
        print("  • Intraday provider : none  (15m + 1H show '🔌 connect' placeholders)")
        print("    → Set INTRADAY_PROVIDER=groww + GROWW_API_KEY + GROWW_API_SECRET in .env")
    print("Press Ctrl+C to stop.\n")


if __name__ == "__main__":
    _startup_banner()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
