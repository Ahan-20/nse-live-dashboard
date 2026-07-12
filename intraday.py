#!/usr/bin/env python3
"""
Intraday data provider — abstraction layer.

Every consumer of intraday OHLC asks for bars through `get_candles(symbol, tf,
from_dt, to_dt)`. The active provider is chosen by env var INTRADAY_PROVIDER:
  - "none"  (default) : returns [] — nothing connected yet, UI shows placeholders
  - "groww" : authenticates with GROWW_API_KEY + GROWW_API_SECRET and uses
              Groww's historical candles API (intervals 15m and 60m).

When you set the env vars and INTRADAY_PROVIDER=groww in Railway, every screen
that uses intraday automatically lights up — no other code changes needed.

Cache: results are kept in memory with a TTL (5 min) so a single page render
doesn't hammer the broker.
"""

import os
import json
import time
import datetime
import urllib.request
import urllib.parse


SUPPORTED_TF = {"15m": 15, "1h": 60}    # minutes per bar


class IntradayProvider:
    name = "none"
    enabled = False

    def get_candles(self, symbol, tf, days=30):
        """Return list of {dt, o, h, l, c, v} or [] if unavailable."""
        return []


# ── Groww implementation ─────────────────────────────────────────────
class GrowwProvider(IntradayProvider):
    name = "groww"

    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret
        self.enabled = bool(api_key and api_secret)
        self._token = None
        self._token_at = 0.0
        self._cache = {}     # (sym, tf, days) -> (t, bars)

    def _access_token(self):
        # Tokens last ~24h; refresh every 12h. Uses TOTP flow under the hood;
        # the python SDK does this with GrowwAPI.get_access_token(api_key, totp).
        # Implemented inline here so we don't add a hard dependency.
        if self._token and (time.time() - self._token_at) < 12 * 3600:
            return self._token
        try:
            import pyotp                       # optional dep, only if Groww is on
            totp = pyotp.TOTP(self.api_secret).now()
            url = "https://api.groww.in/v1/token/api/access"
            data = urllib.parse.urlencode({"key_type": "approval", "totp": totp}).encode()
            req = urllib.request.Request(url, data=data, headers={
                "Authorization": f"Bearer {self.api_key}",
                "X-API-VERSION": "1.0",
                "Content-Type": "application/x-www-form-urlencoded",
            })
            d = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
            self._token = d.get("token") or d.get("payload", {}).get("token")
            self._token_at = time.time()
        except Exception:
            self._token = None
        return self._token

    def get_candles(self, symbol, tf, days=30):
        if tf not in SUPPORTED_TF:
            return []
        cache_key = (symbol, tf, days)
        if cache_key in self._cache:
            t, bars = self._cache[cache_key]
            if time.time() - t < 300:           # 5-min cache
                return bars
        token = self._access_token()
        if not token:
            return []
        end = datetime.datetime.now()
        start = end - datetime.timedelta(days=days)
        url = ("https://api.groww.in/v1/historical/candles?"
               + urllib.parse.urlencode({
                   "exchange": "NSE", "segment": "CASH",
                   "groww_symbol": f"NSE-EQ-{symbol}",
                   "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
                   "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
                   "candle_interval": tf,
               }))
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "X-API-VERSION": "1.0", "Accept": "application/json"})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
            raw = d.get("candles") or d.get("payload", {}).get("candles") or []
            bars = []
            for c in raw:
                # [ts, open, high, low, close, volume]
                ts = c[0]
                if isinstance(ts, (int, float)) and ts > 1e11:
                    ts = ts / 1000
                if isinstance(ts, (int, float)):
                    dt = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    dt = str(ts)
                bars.append({"dt": dt, "o": float(c[1]), "h": float(c[2]),
                             "l": float(c[3]), "c": float(c[4]),
                             "v": float(c[5] if len(c) > 5 else 0)})
            self._cache[cache_key] = (time.time(), bars)
            return bars
        except Exception:
            return []


# ── Kite Connect (Zerodha) implementation ────────────────────────────
class KiteProvider(IntradayProvider):
    """Kite Connect intraday candles.

    Auth model:
      - api_key + api_secret are static (from kite.trade dev console)
      - access_token expires DAILY at ~06:00 IST
      - Refreshed via /kite/login browser flow on the server (server.py handler)
      - We read the current token from env each call, so a mid-run refresh works

    Symbol model:
      - Kite uses instrument_tokens (integers), not symbols
      - We cache a symbol → token map from /instruments/NSE on first call
    """
    name = "kite"
    BASE = "https://api.kite.trade"

    def __init__(self, api_key):
        self.api_key = api_key
        self.enabled = bool(api_key)
        self._tok_cache = None            # symbol -> instrument_token
        self._tok_at = 0.0
        self._candle_cache = {}           # (sym, tf, days) -> (t, bars)

    def _headers(self):
        access = os.environ.get("KITE_ACCESS_TOKEN", "")
        return {
            "X-Kite-Version": "3",
            "Authorization": f"token {self.api_key}:{access}",
            "User-Agent": "nse-live-dashboard",
        }

    def _get(self, path, timeout=15):
        req = urllib.request.Request(self.BASE + path, headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _instrument_token(self, symbol):
        """Look up NSE:SYMBOL-EQ's instrument_token. Cached for 24h."""
        if self._tok_cache and (time.time() - self._tok_at) < 24 * 3600:
            return self._tok_cache.get(symbol)
        # /instruments/NSE returns CSV — parse it once, cache
        req = urllib.request.Request(self.BASE + "/instruments/NSE",
                                     headers=self._headers())
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", "ignore")
        toks = {}
        lines = text.splitlines()
        if not lines:
            return None
        # Kite CSV headers:
        # instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) < 12:
                continue
            tsym, itype = parts[2].strip(), parts[9].strip()
            if itype == "EQ" and tsym:
                try:
                    toks[tsym] = int(parts[0])
                except ValueError:
                    pass
        self._tok_cache = toks
        self._tok_at = time.time()
        return toks.get(symbol)

    # Kite interval labels differ from ours
    _KITE_TF = {"15m": "15minute", "1h": "60minute"}

    def get_candles(self, symbol, tf, days=30):
        if tf not in self._KITE_TF:
            return []
        cache_key = (symbol, tf, days)
        if cache_key in self._candle_cache:
            t, bars = self._candle_cache[cache_key]
            if time.time() - t < 300:                     # 5-min cache
                return bars
        token = self._instrument_token(symbol)
        if not token:
            return []
        end = datetime.datetime.now()
        start = end - datetime.timedelta(days=days)
        qs = urllib.parse.urlencode({
            "from": start.strftime("%Y-%m-%d %H:%M:%S"),
            "to":   end.strftime("%Y-%m-%d %H:%M:%S"),
        })
        try:
            d = self._get(f"/instruments/historical/{token}/{self._KITE_TF[tf]}?{qs}")
        except Exception:
            return []
        raw = ((d.get("data") or {}).get("candles")) or []
        bars = []
        for c in raw:
            # Kite: [ts_iso, open, high, low, close, volume, oi?]
            ts = c[0]
            if isinstance(ts, str):
                # Trim TZ suffix so fromisoformat handles it uniformly
                dt = ts.replace("T", " ")[:19]
            else:
                dt = str(ts)
            try:
                bars.append({
                    "dt": dt,
                    "o": float(c[1]), "h": float(c[2]),
                    "l": float(c[3]), "c": float(c[4]),
                    "v": float(c[5] if len(c) > 5 else 0),
                })
            except (ValueError, IndexError):
                continue
        self._candle_cache[cache_key] = (time.time(), bars)
        return bars


# ── Factory ──────────────────────────────────────────────────────────
def make_provider():
    name = os.environ.get("INTRADAY_PROVIDER", "none").lower()
    if name == "kite":
        return KiteProvider(os.environ.get("KITE_API_KEY"))
    if name == "groww":
        return GrowwProvider(os.environ.get("GROWW_API_KEY"),
                             os.environ.get("GROWW_API_SECRET"))
    return IntradayProvider()


PROVIDER = make_provider()
