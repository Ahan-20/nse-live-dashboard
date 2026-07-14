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
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Include Kite's actual error body so we can see WHY it 400'd
            try:
                body = e.read().decode("utf-8", "ignore")[:400]
            except Exception:
                body = "<unreadable>"
            raise RuntimeError(f"HTTP {e.code} {e.reason} on {path[:80]} · "
                               f"kite says: {body}") from e

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

    # ── Option-chain support (for TEF-90 auto-tick) ─────────────────────
    _NFO_CACHE = None
    _NFO_AT = 0.0

    def _nfo_instruments(self):
        """Parse Kite's NFO instrument CSV. Cached for 24h. Uses csv.reader so
        quoted commas in the 'name' field don't miscount columns.
        Returns list of dicts: {token, tradingsymbol, name, expiry, strike,
                                lot_size, instrument_type, segment}."""
        if self._NFO_CACHE and (time.time() - self._NFO_AT) < 24 * 3600:
            return self._NFO_CACHE
        req = urllib.request.Request(self.BASE + "/instruments/NFO",
                                     headers=self._headers())
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode("utf-8", "ignore")
        import csv as _csv
        import io as _io
        reader = _csv.reader(_io.StringIO(text))
        header = next(reader, None)
        if not header:
            return []
        # Map column names → index so we're robust to Kite adding new columns
        idx = {name.strip().lower(): i for i, name in enumerate(header)}
        need = ("instrument_token", "tradingsymbol", "name", "expiry",
                "strike", "lot_size", "instrument_type", "segment")
        if not all(k in idx for k in need):
            return []
        rows = []
        for p in reader:
            if len(p) < len(header):
                continue
            try:
                rows.append({
                    "token": int(p[idx["instrument_token"]]),
                    "tradingsymbol": p[idx["tradingsymbol"]].strip(),
                    "name": p[idx["name"]].strip(),
                    "expiry": p[idx["expiry"]].strip(),
                    "strike": float(p[idx["strike"]]) if p[idx["strike"]] else 0,
                    "lot_size": int(p[idx["lot_size"]]) if p[idx["lot_size"]] else 0,
                    "instrument_type": p[idx["instrument_type"]].strip(),
                    "segment": p[idx["segment"]].strip(),
                })
            except (ValueError, IndexError):
                continue
        type(self)._NFO_CACHE = rows
        type(self)._NFO_AT = time.time()
        return rows

    _CHAIN_CACHE = {}

    def option_chain(self, underlying="NIFTY", expiry=None):
        """Return the full option chain for the given underlying + expiry.
        If expiry is None, uses the nearest weekly/monthly expiry.

        Returns {
          'underlying': str, 'expiry': 'YYYY-MM-DD', 'spot': float,
          'strikes': [{
              'strike': float,
              'ce': {'ltp': float, 'oi': int, 'iv': float},
              'pe': {'ltp': float, 'oi': int, 'iv': float},
          }],
          'total_call_oi': int, 'total_put_oi': int,
          'pcr': float, 'max_pain': float,
        }"""
        cache_key = (underlying, expiry or "nearest")
        if cache_key in self._CHAIN_CACHE:
            t, data = self._CHAIN_CACHE[cache_key]
            if time.time() - t < 60:                        # 1-min cache
                return data

        instruments = self._nfo_instruments()
        # Filter to this underlying's OPTIONS only
        opts = [r for r in instruments
                if r["name"] == underlying.upper() and r["segment"] == "NFO-OPT"]
        if not opts:
            return None
        # Pick expiry: nearest upcoming one, or the one the caller asked for
        today = datetime.date.today().isoformat()
        upcoming = sorted({r["expiry"] for r in opts if r["expiry"] >= today})
        if not upcoming:
            return None
        target_expiry = expiry or upcoming[0]
        opts = [r for r in opts if r["expiry"] == target_expiry]
        if not opts:
            return None

        # Group into strikes {strike: {'CE': token, 'PE': token}}
        by_strike = {}
        for r in opts:
            slot = by_strike.setdefault(r["strike"], {})
            slot[r["instrument_type"]] = r
        strikes_sorted = sorted(by_strike.keys())

        # Kite's /quote endpoint takes up to 500 instruments per call.
        # Trim to a sensible window around ATM (need spot first, which we
        # derive from any liquid contract's underlying_value).
        # Trick: call /quote for the ATM-ish middle 30 strikes; that gives
        # us the spot from every payload plus full OI/IV. Then widen if needed.
        all_tokens = []
        for s in strikes_sorted:
            for it in ("CE", "PE"):
                if it in by_strike[s]:
                    all_tokens.append(by_strike[s][it]["token"])
        # Kite's /quote endpoint prefers EXCHANGE:TRADINGSYMBOL (colon URL-encoded).
        # Raw tokens sometimes return 400. Batch 400 per call.
        identifiers = []
        for s in strikes_sorted:
            for it in ("CE", "PE"):
                if it in by_strike[s]:
                    identifiers.append(f"NFO:{by_strike[s][it]['tradingsymbol']}")
        quotes = {}
        for i in range(0, len(identifiers), 400):
            batch = identifiers[i:i + 400]
            qs = "&".join(f"i={urllib.parse.quote(idn)}" for idn in batch)
            try:
                d = self._get(f"/quote?{qs}")
            except Exception:
                continue
            quotes.update((d.get("data") or {}))

        # Assemble the chain
        strike_rows = []
        total_ce_oi = 0
        total_pe_oi = 0
        spot = 0.0
        for s in strikes_sorted:
            slot = by_strike[s]
            row = {"strike": s, "ce": None, "pe": None}
            for it in ("CE", "PE"):
                if it not in slot:
                    continue
                # Kite returns keyed by "NFO:tradingsymbol"
                q = quotes.get(f"NFO:{slot[it]['tradingsymbol']}") or \
                    quotes.get(str(slot[it]["token"]))
                if not q:
                    continue
                spot = spot or float(q.get("underlying_value") or 0)
                oi = int(q.get("oi") or 0)
                ltp = float(q.get("last_price") or 0)
                iv = (q.get("ohlc", {}).get("iv") or 0)  # Kite includes iv in some responses
                data = {"ltp": ltp, "oi": oi, "iv": float(iv or 0)}
                if it == "CE":
                    row["ce"] = data
                    total_ce_oi += oi
                else:
                    row["pe"] = data
                    total_pe_oi += oi
            if row["ce"] or row["pe"]:
                strike_rows.append(row)

        # PCR (put-call ratio) — total put OI / total call OI
        pcr = (total_pe_oi / total_ce_oi) if total_ce_oi > 0 else 0.0

        # Max Pain — strike at which OPTION WRITERS collectively lose the least.
        # For each candidate expiry price K*, total pain = sum over all strikes S of
        #   Call OI at S * max(K* - S, 0)   +   Put OI at S * max(S - K*, 0)
        # Max Pain = K* that MINIMIZES that total. We evaluate at each listed strike.
        def total_pain_at(kstar):
            pain = 0.0
            for r in strike_rows:
                s = r["strike"]
                ce_oi = (r["ce"] or {}).get("oi", 0)
                pe_oi = (r["pe"] or {}).get("oi", 0)
                pain += ce_oi * max(kstar - s, 0)
                pain += pe_oi * max(s - kstar, 0)
            return pain
        candidates = [(s, total_pain_at(s)) for s in strikes_sorted]
        max_pain = min(candidates, key=lambda x: x[1])[0] if candidates else 0.0

        data = {
            "underlying": underlying.upper(),
            "expiry": target_expiry,
            "spot": spot,
            "strikes": strike_rows,
            "total_call_oi": total_ce_oi,
            "total_put_oi": total_pe_oi,
            "pcr": round(pcr, 3),
            "max_pain": max_pain,
        }
        self._CHAIN_CACHE[cache_key] = (time.time(), data)
        return data

    def strategy_picks(self, underlying, strategy, expiry=None, spot_override=None):
        """Pick exact strikes for a strategy from the live option chain.
        Returns {expiry, spot, legs, net_credit, max_loss, be_low, be_high}
        on success, or {"error": <reason>} so the UI can show what went wrong.

        Δ target: 0.15-0.20 for sells (Abhishek's TEF-90 spec). We approximate
        via OTM% since Kite's /quote doesn't reliably return IV — for weekly
        expiries, Δ 0.15-0.20 sits at ~3-4% OTM; monthlies at ~4-5% OTM.

        `spot_override` lets the caller pass a known-good spot (e.g. from NSE
        allIndices) when Kite's underlying_value field is 0/missing.
        """
        chain = self.option_chain(underlying, expiry)
        if not chain:
            return {"error": "option_chain returned None"}
        if not chain.get("strikes"):
            return {"error": f"chain had no strikes for {underlying}"}
        # Prefer explicit override; then chain-provided spot; then a
        # last-resort estimate from the strike whose |CE-PE| is smallest
        # (put-call-parity approximation of the synthetic future = spot).
        spot = spot_override or chain.get("spot") or 0
        if not spot:
            balanced = [(r["strike"], abs((r["ce"] or {}).get("ltp", 0) - (r["pe"] or {}).get("ltp", 0)))
                        for r in chain["strikes"]
                        if r.get("ce") and r.get("pe")
                        and (r["ce"].get("ltp") or 0) > 0 and (r["pe"].get("ltp") or 0) > 0]
            if balanced:
                spot = min(balanced, key=lambda x: x[1])[0]
        if not spot:
            return {"error": "no usable spot (Kite underlying_value missing "
                             "and no strike with both CE+PE LTP)"}
        strikes = chain["strikes"]

        # How OTM the sell strikes should be (Δ 0.15-0.20 band).
        # Nearest-expiry weekly is tighter than monthly.
        exp_iso = chain["expiry"]
        try:
            days_out = (datetime.date.fromisoformat(exp_iso) - datetime.date.today()).days
        except Exception:
            days_out = 7
        sell_otm_pct = 3.0 if days_out <= 7 else 4.5
        hedge_gap_pct = 1.0                    # Iron Condor: hedge 1% further OTM

        def _closest(target_strike, side):
            """Nearest strike with a liquid option on that side.
            Liquidity signal = LTP > 0 OR OI > 10k (covers far-OTM strikes that
            have deep OI but haven't traded in the last few minutes)."""
            side_key = side.lower()
            cand = []
            for r in strikes:
                d = r.get(side_key)
                if not d: continue
                ltp = d.get("ltp") or 0
                oi = d.get("oi") or 0
                if ltp > 0 or oi > 10000:
                    cand.append(r)
            if not cand: return None
            return min(cand, key=lambda r: abs(r["strike"] - target_strike))

        def _tgt_ce(pct): return spot * (1 + pct / 100)
        def _tgt_pe(pct): return spot * (1 - pct / 100)

        # Small helper: use LTP if positive, else 0.5*(bid+ask) approx via OI-weighted mid
        def _ltp_of(strike_row, side):
            d = strike_row.get(side.lower()) or {}
            return d.get("ltp") or 0

        if strategy == "ss":                    # SHORT STRANGLE (2 legs, undefined risk)
            sell_ce = _closest(_tgt_ce(sell_otm_pct), "CE")
            sell_pe = _closest(_tgt_pe(sell_otm_pct), "PE")
            if not sell_ce:
                return {"error": f"no liquid CE near {int(_tgt_ce(sell_otm_pct))} (targeted Δ 0.15-0.20)"}
            if not sell_pe:
                return {"error": f"no liquid PE near {int(_tgt_pe(sell_otm_pct))} (targeted Δ 0.15-0.20)"}
            credit = round(_ltp_of(sell_ce, "CE") + _ltp_of(sell_pe, "PE"), 2)
            return {
                "strategy": "ss", "expiry": exp_iso, "spot": spot,
                "legs": [
                    {"leg_id": "sellce", "strike": sell_ce["strike"],
                     "ltp": _ltp_of(sell_ce, "CE"), "type": "CE", "side": "sell"},
                    {"leg_id": "sellpe", "strike": sell_pe["strike"],
                     "ltp": _ltp_of(sell_pe, "PE"), "type": "PE", "side": "sell"},
                ],
                "net_credit": credit,
                "max_loss": None,                # undefined risk
                "sl_at_close_out_cost": round(credit * 2, 2),
                "be_low":  round(sell_pe["strike"] - credit, 2),
                "be_high": round(sell_ce["strike"] + credit, 2),
                "days_to_expiry": days_out,
            }

        if strategy == "ic":                    # IRON CONDOR (4 legs, defined risk)
            sell_ce = _closest(_tgt_ce(sell_otm_pct), "CE")
            buy_ce  = _closest(_tgt_ce(sell_otm_pct + hedge_gap_pct), "CE")
            sell_pe = _closest(_tgt_pe(sell_otm_pct), "PE")
            buy_pe  = _closest(_tgt_pe(sell_otm_pct + hedge_gap_pct), "PE")
            missing = []
            if not sell_ce: missing.append(f"sell CE near {int(_tgt_ce(sell_otm_pct))}")
            if not buy_ce:  missing.append(f"buy CE near {int(_tgt_ce(sell_otm_pct+hedge_gap_pct))}")
            if not sell_pe: missing.append(f"sell PE near {int(_tgt_pe(sell_otm_pct))}")
            if not buy_pe:  missing.append(f"buy PE near {int(_tgt_pe(sell_otm_pct+hedge_gap_pct))}")
            if missing:
                return {"error": "no liquid strikes for: " + ", ".join(missing)}
            # Ensure hedges are actually FURTHER out than sells
            if buy_ce["strike"] <= sell_ce["strike"]:
                return {"error": f"CE hedge {int(buy_ce['strike'])} not above sell {int(sell_ce['strike'])}"}
            if buy_pe["strike"] >= sell_pe["strike"]:
                return {"error": f"PE hedge {int(buy_pe['strike'])} not below sell {int(sell_pe['strike'])}"}
            ce_credit = _ltp_of(sell_ce, "CE") - _ltp_of(buy_ce, "CE")
            pe_credit = _ltp_of(sell_pe, "PE") - _ltp_of(buy_pe, "PE")
            credit = round(ce_credit + pe_credit, 2)
            ce_width = buy_ce["strike"] - sell_ce["strike"]
            pe_width = sell_pe["strike"] - buy_pe["strike"]
            max_width = max(ce_width, pe_width)
            return {
                "strategy": "ic", "expiry": exp_iso, "spot": spot,
                "legs": [
                    {"leg_id": "sellce", "strike": sell_ce["strike"],
                     "ltp": _ltp_of(sell_ce, "CE"), "type": "CE", "side": "sell"},
                    {"leg_id": "buyce",  "strike": buy_ce["strike"],
                     "ltp": _ltp_of(buy_ce, "CE"),  "type": "CE", "side": "buy"},
                    {"leg_id": "sellpe", "strike": sell_pe["strike"],
                     "ltp": _ltp_of(sell_pe, "PE"), "type": "PE", "side": "sell"},
                    {"leg_id": "buype",  "strike": buy_pe["strike"],
                     "ltp": _ltp_of(buy_pe, "PE"),  "type": "PE", "side": "buy"},
                ],
                "net_credit": credit,
                "max_loss": round(max_width - credit, 2),
                "ce_wing_width": ce_width,
                "pe_wing_width": pe_width,
                "be_low":  round(sell_pe["strike"] - credit, 2),
                "be_high": round(sell_ce["strike"] + credit, 2),
                "days_to_expiry": days_out,
            }

        return None                              # BPS/BCS live on Bull Put Setup / Trade Finder

    def quote_option_legs(self, legs):
        """Look up live LTP for each leg spec.
        legs = [{'symbol': 'RELIANCE', 'expiry': 'YYYY-MM-DD',
                 'strike': float, 'type': 'CE'|'PE'}, ...]
        Returns [{...leg, 'ltp': float|None, 'oi': int|None, 'token': int|None}, ...]"""
        instruments = self._nfo_instruments()
        # Index instruments by (name, expiry, strike, instrument_type) for O(1) lookup
        by_key = {}
        for r in instruments:
            if r.get("segment") != "NFO-OPT":
                continue
            key = (r["name"].upper(), r["expiry"], round(float(r["strike"]), 2),
                   r["instrument_type"])
            by_key[key] = r

        # Resolve tokens for each requested leg
        want = []
        for leg in legs:
            key = (leg["symbol"].upper(), leg["expiry"], round(float(leg["strike"]), 2),
                   leg["type"].upper())
            inst = by_key.get(key)
            want.append({**leg, "token": inst["token"] if inst else None,
                         "tradingsymbol": inst["tradingsymbol"] if inst else None})
        tokens = [w["token"] for w in want if w["token"]]
        if not tokens:
            return [{**w, "ltp": None, "oi": None} for w in want]

        # Batch quote — use NFO:tradingsymbol (Kite's preferred identifier)
        identifiers = [f"NFO:{w['tradingsymbol']}" for w in want if w.get("tradingsymbol")]
        quotes = {}
        for i in range(0, len(identifiers), 400):
            batch = identifiers[i:i + 400]
            qs = "&".join(f"i={urllib.parse.quote(idn)}" for idn in batch)
            try:
                d = self._get(f"/quote?{qs}")
                quotes.update((d.get("data") or {}))
            except Exception:
                continue

        # Attach LTP + OI to each leg (Kite returns keyed by NFO:tradingsymbol)
        out = []
        for w in want:
            q = None
            if w.get("tradingsymbol"):
                q = quotes.get(f"NFO:{w['tradingsymbol']}") or \
                    quotes.get(str(w["token"]))
            if q:
                out.append({**w, "ltp": float(q.get("last_price") or 0),
                            "oi":  int(q.get("oi") or 0)})
            else:
                out.append({**w, "ltp": None, "oi": None})
        return out


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
