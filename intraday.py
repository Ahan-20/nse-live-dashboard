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


# ── Factory ──────────────────────────────────────────────────────────
def make_provider():
    name = os.environ.get("INTRADAY_PROVIDER", "none").lower()
    if name == "groww":
        return GrowwProvider(os.environ.get("GROWW_API_KEY"),
                             os.environ.get("GROWW_API_SECRET"))
    return IntradayProvider()


PROVIDER = make_provider()
