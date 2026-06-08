#!/usr/bin/env python3
"""
Backtest the NSE scanner strategy on 5 years of daily data from prices.db.

Reproduces the dashboard's Pine logic adapted to DAILY candles with WEEKLY as
the higher-timeframe confirmation (the daily-equivalent of the 5m/15m setup,
since free 5-year intraday data doesn't exist). Sector / gainer-loser filters
can't be reconstructed historically, so they're neutralised; everything else
carries over. Exits: stop-loss at ATR*1.5, target at 2R.

CLI:  python3 backtest.py RELIANCE
"""

import os
import sys
import json
import math
import sqlite3
import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
# Full DB (all ~3000 symbols) if present locally; else the shipped slim DB
# (top-500 liquid names) that's baked into the deployed image.
DB_PATH = next((p for p in (os.path.join(_HERE, "prices.db"),
                            os.path.join(_HERE, "prices_web.db"))
                if os.path.exists(p)), os.path.join(_HERE, "prices.db"))

# ── indicator helpers (plain Python, no numpy) ───────────────────────
def ema(xs, n):
    a = 2 / (n + 1)
    out, e = [], None
    for x in xs:
        e = x if e is None else a * x + (1 - a) * e
        out.append(e)
    return out

def rma(xs, n):                      # Wilder smoothing
    a = 1 / n
    out, e = [], None
    for x in xs:
        e = x if e is None else a * x + (1 - a) * e
        out.append(e)
    return out

def sma(xs, n):
    out, s = [], 0.0
    from collections import deque
    q = deque()
    for x in xs:
        q.append(x); s += x
        if len(q) > n:
            s -= q.popleft()
        out.append(s / len(q))
    return out

def stdev(xs, n):
    from collections import deque
    out, q = [], deque()
    for x in xs:
        q.append(x)
        if len(q) > n:
            q.popleft()
        m = sum(q) / len(q)
        out.append(math.sqrt(sum((v - m) ** 2 for v in q) / len(q)))
    return out

def rsi(closes, n=14):
    gains, losses = [0.0], [0.0]
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0)); losses.append(max(-ch, 0.0))
    ag, al = rma(gains, n), rma(losses, n)
    out = []
    for g, l in zip(ag, al):
        out.append(100.0 if l == 0 else 100 - 100 / (1 + g / l))
    return out

def macd(closes):
    e12, e26 = ema(closes, 12), ema(closes, 26)
    line = [a - b for a, b in zip(e12, e26)]
    sig = ema(line, 9)
    hist = [a - b for a, b in zip(line, sig)]
    return line, sig, hist

def atr(h, l, c, n=14):
    tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return rma(tr, n)


# ── data ─────────────────────────────────────────────────────────────
# Clean ticker renames: current symbol -> predecessor ticker(s) whose pre-rename
# history we stitch on. Only pure renames (no price adjustment) — NOT demergers.
ALIASES = {"ETERNAL": ["ZOMATO"]}


def load(symbol):
    symbol = symbol.upper()
    syms = [symbol] + ALIASES.get(symbol, [])
    con = sqlite3.connect(DB_PATH)
    ph = ",".join("?" * len(syms))
    rows = con.execute(
        f"SELECT dt,o,h,l,c,v,symbol FROM prices WHERE symbol IN ({ph})", syms
    ).fetchall()
    con.close()
    best = {}                       # one row per date; prefer the current symbol
    for dt, o, h, l, c, v, sym in rows:
        if dt not in best or sym == symbol:
            best[dt] = (o, h, l, c, v)
    bars = [{"dt": dt, "o": r[0], "h": r[1], "l": r[2], "c": r[3], "v": r[4]}
            for dt, r in sorted(best.items())]
    return _adjust_corporate_actions(bars, syms)


_CA = None


def _load_ca():
    """Load NSE's official split/bonus list (built by corp_actions.py)."""
    global _CA
    if _CA is None:
        try:
            with open(os.path.join(_HERE, "corp_actions.json")) as f:
                _CA = json.load(f)
        except Exception:
            _CA = {}
    return _CA


def _adjust_corporate_actions(bars, syms):
    """Back-adjust for splits/bonuses using NSE's OFFICIAL corporate-actions
    list — exact symbol, ex-date and ratio. Bhavcopy is unadjusted, so a 1:1
    bonus shows as a fake ~50% crash; here every bar BEFORE an action's ex-date
    is scaled by that action's factor (e.g. 1:1 bonus -> 0.5). Only stocks that
    actually had an action are touched, on the exact date — no gap-guessing."""
    ca = _load_ca()
    actions = []
    for s in syms:                       # include alias predecessors (e.g. ZOMATO)
        for a in ca.get(s, []):
            actions.append((a["ex_date"], a["factor"]))
    if not actions or not bars:
        return bars
    for b in bars:
        m = 1.0
        for exd, f in actions:
            if exd > b["dt"]:            # bar is before the ex-date -> scale it
                m *= f
        if m != 1.0:
            b["o"] *= m; b["h"] *= m; b["l"] *= m; b["c"] *= m
    return bars


def weekly_htf_bull_bear(bars):
    """For each daily bar, trend from the last COMPLETED week (no lookahead)."""
    # group into ISO weeks
    weeks, key_of = {}, []
    order = []
    for b in bars:
        y, w, _ = datetime.date.fromisoformat(b["dt"]).isocalendar()
        k = (y, w)
        if k not in weeks:
            weeks[k] = {"c": b["c"]}; order.append(k)
        weeks[k]["c"] = b["c"]                      # last close of week
        key_of.append(k)
    wkeys = order
    wcloses = [weeks[k]["c"] for k in wkeys]
    we9, we21, we50 = ema(wcloses, 9), ema(wcloses, 21), ema(wcloses, 50)
    bull_of, bear_of = {}, {}
    for i, k in enumerate(wkeys):
        bull_of[k] = we9[i] > we21[i] > we50[i]
        bear_of[k] = we9[i] < we21[i] < we50[i]
    # map each day to PREVIOUS week's verdict
    idx = {k: i for i, k in enumerate(wkeys)}
    out_bull, out_bear = [], []
    for k in key_of:
        i = idx[k]
        if i == 0:
            out_bull.append(False); out_bear.append(False)
        else:
            pk = wkeys[i - 1]
            out_bull.append(bull_of[pk]); out_bear.append(bear_of[pk])
    return out_bull, out_bear


# ── strategy + simulation ────────────────────────────────────────────
def run_backtest(symbol, capital=100000.0, risk_pct=0.01, rr=2.0, sl_atr=1.5,
                 vol_conf=2.0, vol_early=1.4, require_retest=False,
                 pivot_lb=8, max_hold=60, mode="ema200cross",
                 exit_mode="cross", direction="both"):
    bars = load(symbol)
    if not bars:
        return {"ok": False, "error": f"'{symbol.upper()}' isn't in the dataset. "
                f"The hosted version covers the ~500 most-liquid NSE stocks "
                f"(e.g. RELIANCE, HDFCBANK, INFY). Check the exact NSE symbol."}
    if len(bars) < 250:
        return {"ok": False, "error": f"Not enough history for {symbol.upper()} "
                f"({len(bars)} daily bars; need 250+ for a 200 EMA backtest)."}

    o = [b["o"] for b in bars]; h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]; c = [b["c"] for b in bars]; v = [b["v"] for b in bars]
    n = len(bars)

    e9, e21, e50, e200 = ema(c, 9), ema(c, 21), ema(c, 50), ema(c, 200)
    e20 = ema(c, 20)
    av = atr(h, l, c, 14)
    vsma = sma(v, 20)
    rsiv = rsi(c, 14)
    ml, ms, mh = macd(c)
    bbsd = stdev(c, 20); bbm = sma(c, 20)
    kcm = ema(c, 20)
    htf_bull, htf_bear = weekly_htf_bull_bear(bars)

    # pivots (confirmed pivot_lb bars after the pivot bar -> no lookahead)
    res = [None] * n; sup = [None] * n
    cur_res = cur_sup = None
    for i in range(n):
        p = i - pivot_lb                        # candidate pivot bar, confirmed now
        if p - pivot_lb >= 0:
            win_h = h[p - pivot_lb:p + pivot_lb + 1]
            win_l = l[p - pivot_lb:p + pivot_lb + 1]
            if h[p] == max(win_h):
                cur_res = h[p]
            if l[p] == min(win_l):
                cur_sup = l[p]
        res[i] = cur_res; sup[i] = cur_sup

    # ── 200 EMA trend + pullback strategy (stateful, precomputed) ──
    # Uptrend = close above a RISING 200 EMA. Arm on a pullback that closes
    # below the 20 EMA; fire LONG when price reclaims the 20 EMA. Mirror short.
    sig_e200 = [(None, None)] * n
    armed_long = armed_short = False
    for i in range(200, n):
        up = c[i] > e200[i] and e200[i] > e200[i - 5]
        dn = c[i] < e200[i] and e200[i] < e200[i - 5]
        if up:
            armed_short = False
            if c[i] < e20[i]:
                armed_long = True
            elif armed_long and c[i] > e20[i] and c[i] > o[i]:
                sig_e200[i] = ("long", "200 EMA Pullback"); armed_long = False
        elif dn:
            armed_long = False
            if c[i] > e20[i]:
                armed_short = True
            elif armed_short and c[i] < e20[i] and c[i] < o[i]:
                sig_e200[i] = ("short", "200 EMA Pullback"); armed_short = False
        else:
            armed_long = armed_short = False

    # ── 200 EMA CROSS strategy (the pasted script): long when close crosses
    # above the 200 EMA, short when it crosses below. ──
    sig_cross = [(None, None)] * n
    for i in range(200, n):
        if c[i] > e200[i] and c[i - 1] <= e200[i - 1]:
            sig_cross[i] = ("long", "200 EMA Cross")
        elif c[i] < e200[i] and c[i - 1] >= e200[i - 1]:
            sig_cross[i] = ("short", "200 EMA Cross")

    def sig(i):
        if mode == "ema200cross":
            return sig_cross[i]
        if mode == "ema200":
            return sig_e200[i]
        if i < 200 or vsma[i] == 0:
            return None, None
        body = abs(c[i] - o[i]); rng = h[i] - l[i]
        br = body / rng if rng > 0 else 0
        uw = h[i] - max(o[i], c[i]); lw = min(o[i], c[i]) - l[i]
        s_bull = c[i] > o[i] and br >= 0.55 and (uw / body < 0.40 if body > 0 else False)
        s_bear = c[i] < o[i] and br >= 0.55 and (lw / body < 0.40 if body > 0 else False)
        cur_bull = e9[i] > e21[i] > e50[i] and c[i] > e200[i]
        cur_bear = e9[i] < e21[i] < e50[i] and c[i] < e200[i]
        early_bx = e9[i - 1] <= e21[i - 1] and e9[i] > e21[i] and c[i] > e50[i]
        early_sx = e9[i - 1] >= e21[i - 1] and e9[i] < e21[i] and c[i] < e50[i]
        vr = v[i] / vsma[i]
        rsi_bull = 45 < rsiv[i] < 72; rsi_bear = 28 < rsiv[i] < 55
        macd_bull = ml[i] > ms[i] and mh[i] > 0; macd_bear = ml[i] < ms[i] and mh[i] < 0
        broke_res = res[i] is not None and c[i] > res[i] and c[i - 1] <= res[i]
        broke_sup = sup[i] is not None and c[i] < sup[i] and c[i - 1] >= sup[i]
        bb_u = bbm[i] + 2 * bbsd[i]; bb_l = bbm[i] - 2 * bbsd[i]
        kc_u = kcm[i] + 1.5 * av[i]; kc_l = kcm[i] - 1.5 * av[i]
        sq_on = bb_u < kc_u and bb_l > kc_l
        sq_on_p = (bbm[i - 1] + 2 * bbsd[i - 1]) < (kcm[i - 1] + 1.5 * av[i - 1])
        sq_fire = (bb_u > kc_u and bb_l < kc_l) and sq_on_p
        mom = c[i] - (max(h[i - 19:i + 1]) + min(l[i - 19:i + 1])) / 2

        if mode == "strict":
            # Exact Pine port: requires the pivot breakout on this very bar.
            conf_long = (htf_bull[i] and cur_bull and broke_res and s_bull
                         and vr >= vol_conf and rsi_bull and macd_bull and not sq_on)
            conf_short = (htf_bear[i] and cur_bear and broke_sup and s_bear
                          and vr >= vol_conf and rsi_bear and macd_bear and not sq_on)
        else:
            # Daily-tuned: same filters, but trend continuation (EMA stack) instead
            # of a same-bar pivot breakout, and a realistic daily volume gate.
            conf_long = (htf_bull[i] and cur_bull and s_bull and vr >= 1.5
                         and rsi_bull and macd_bull and not sq_on)
            conf_short = (htf_bear[i] and cur_bear and s_bear and vr >= 1.5
                          and rsi_bear and macd_bear and not sq_on)
        early_long = (htf_bull[i] and early_bx and c[i] > e50[i] and vr >= vol_early
                      and rsiv[i] > 45 and not macd_bear)
        early_short = (htf_bear[i] and early_sx and c[i] < e50[i] and vr >= vol_early
                       and rsiv[i] < 55 and not macd_bull)
        sq_long = sq_fire and mom > 0 and htf_bull[i] and vr >= vol_early
        sq_short = sq_fire and mom < 0 and htf_bear[i] and vr >= vol_early

        if conf_long:  return "long", "Confirmed Long"
        if conf_short: return "short", "Confirmed Short"
        if sq_long:    return "long", "Squeeze Long"
        if sq_short:   return "short", "Squeeze Short"
        if early_long: return "long", "Early Long"
        if early_short:return "short", "Early Short"
        return None, None

    # simulate: signal on close of i -> enter at open of i+1.
    #   exit_mode "sltp"  -> ATR stop / RR target (intrabar)
    #   exit_mode "cross" -> opposite 200 EMA cross (at that bar's close)
    #   exit_mode "either"-> whichever happens first
    use_sltp = exit_mode in ("sltp", "either")
    use_cross = exit_mode in ("cross", "either")
    equity = capital
    peak = capital
    maxdd = 0.0
    trades = []
    eq_curve = [{"dt": bars[0]["dt"], "eq": capital}]
    i = 200
    while i < n - 1:
        d, label = sig(i)
        if not d or (direction != "both" and d != direction):
            i += 1; continue
        entry = o[i + 1]
        risk_ps = av[i] * sl_atr
        if risk_ps <= 0:
            i += 1; continue
        if d == "long":
            sl = entry - risk_ps; tp = entry + risk_ps * rr
        else:
            sl = entry + risk_ps; tp = entry - risk_ps * rr
        qty = math.floor((risk_pct * equity) / risk_ps)
        if qty < 1:
            qty = 1
        exit_px = exit_dt = reason = None
        j = i + 1
        while j < n:
            hi, lo = h[j], l[j]
            if use_sltp:
                if d == "long":
                    if lo <= sl: exit_px, reason = sl, "SL"
                    elif hi >= tp: exit_px, reason = tp, "TP"
                else:
                    if hi >= sl: exit_px, reason = sl, "SL"
                    elif lo <= tp: exit_px, reason = tp, "TP"
            if exit_px is None and use_cross and j > 0:
                if d == "long" and c[j] < e200[j] and c[j - 1] >= e200[j - 1]:
                    exit_px, reason = c[j], "Cross"
                elif d == "short" and c[j] > e200[j] and c[j - 1] <= e200[j - 1]:
                    exit_px, reason = c[j], "Cross"
            if exit_px is None and not use_cross and (j - (i + 1)) >= max_hold:
                exit_px, reason = c[j], "Time"      # safety cap (sltp-only mode)
            if exit_px is not None:
                exit_dt = bars[j]["dt"]; break
            j += 1
        if exit_px is None:                          # ran to end of data
            j = n - 1
            exit_px, reason, exit_dt = c[j], "End", bars[j]["dt"]
        pnl = qty * (exit_px - entry) * (1 if d == "long" else -1)
        equity += pnl
        peak = max(peak, equity)
        maxdd = max(maxdd, (peak - equity) / peak)
        r_mult = pnl / (qty * risk_ps) if qty * risk_ps else 0
        trades.append({"entry_dt": bars[i + 1]["dt"], "exit_dt": exit_dt,
                       "dir": d, "type": label, "entry": round(entry, 2),
                       "exit": round(exit_px, 2), "sl": round(sl, 2), "tp": round(tp, 2),
                       "qty": qty, "pnl": round(pnl, 2), "r": round(r_mult, 2),
                       "reason": reason, "bars_held": j - (i + 1)})
        eq_curve.append({"dt": exit_dt, "eq": round(equity, 2)})
        i = j      # re-evaluate the exit bar so an opposite cross can reverse

    return _report(symbol, bars, trades, capital, equity, maxdd, eq_curve)


def _report(symbol, bars, trades, capital, equity, maxdd, eq_curve):
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    longs = [t for t in trades if t["dir"] == "long"]
    shorts = [t for t in trades if t["dir"] == "short"]
    gp = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in losses)
    nt = len(trades)
    by_type = {}
    for t in trades:
        d = by_type.setdefault(t["type"], {"n": 0, "win": 0, "pnl": 0.0})
        d["n"] += 1; d["win"] += 1 if t["pnl"] > 0 else 0; d["pnl"] += t["pnl"]

    # Buy & hold benchmark over the same (adjusted) period.
    bh_ret = (bars[-1]["c"] / bars[0]["c"] - 1) if bars[0]["c"] else 0.0
    strat_ret = (equity - capital) / capital

    def pct(x): return round(x * 100, 1)
    return {
        "ok": True, "symbol": symbol.upper(),
        "period": {"from": bars[0]["dt"], "to": bars[-1]["dt"], "bars": len(bars)},
        "capital": capital, "final_equity": round(equity, 2),
        "net_pnl": round(equity - capital, 2),
        "return_pct": pct(strat_ret),
        "buyhold_return_pct": pct(bh_ret),
        "buyhold_final": round(capital * (1 + bh_ret), 2),
        "vs_buyhold": pct(strat_ret - bh_ret),
        "trades": nt,
        "longs": len(longs), "shorts": len(shorts),
        "wins": len(wins), "losses": len(losses),
        "win_rate": pct(len(wins) / nt) if nt else 0,
        "avg_win": round(gp / len(wins), 2) if wins else 0,
        "avg_loss": round(-gl / len(losses), 2) if losses else 0,
        "avg_r": round(sum(t["r"] for t in trades) / nt, 2) if nt else 0,
        "expectancy": round(sum(t["pnl"] for t in trades) / nt, 2) if nt else 0,
        "profit_factor": round(gp / gl, 2) if gl else (float("inf") if gp else 0),
        "max_drawdown_pct": pct(maxdd),
        "best_trade": round(max((t["pnl"] for t in trades), default=0), 2),
        "worst_trade": round(min((t["pnl"] for t in trades), default=0), 2),
        "avg_bars_held": round(sum(t["bars_held"] for t in trades) / nt, 1) if nt else 0,
        "by_type": {k: {"n": d["n"], "win_rate": pct(d["win"] / d["n"]),
                        "pnl": round(d["pnl"], 2)} for k, d in by_type.items()},
        "equity_curve": eq_curve,
        "trade_list": trades,
    }


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"
    rep = run_backtest(sym)
    if not rep.get("ok"):
        print(rep.get("error")); sys.exit(1)
    print(f"\n  {rep['symbol']}  |  {rep['period']['from']} → {rep['period']['to']}  "
          f"({rep['period']['bars']} daily bars)")
    print("  " + "-" * 54)
    fields = [("Trades", rep["trades"]), ("Long / Short", f"{rep['longs']} / {rep['shorts']}"),
              ("Wins / Losses", f"{rep['wins']} / {rep['losses']}"),
              ("Win rate", f"{rep['win_rate']}%"), ("Net P&L", f"Rs {rep['net_pnl']:,}"),
              ("Return on capital", f"{rep['return_pct']}%"),
              ("Profit factor", rep["profit_factor"]), ("Avg R / trade", rep["avg_r"]),
              ("Expectancy", f"Rs {rep['expectancy']:,}"),
              ("Avg win / loss", f"Rs {rep['avg_win']:,} / Rs {rep['avg_loss']:,}"),
              ("Max drawdown", f"{rep['max_drawdown_pct']}%"),
              ("Best / Worst", f"Rs {rep['best_trade']:,} / Rs {rep['worst_trade']:,}"),
              ("Avg hold (bars)", rep["avg_bars_held"])]
    for k, val in fields:
        print(f"  {k:<20} {val}")
    print("  by signal type:")
    for k, d in rep["by_type"].items():
        print(f"    {k:<16} n={d['n']:<4} win={d['win_rate']}%  pnl=Rs {d['pnl']:,}")
