#!/usr/bin/env python3
"""
Build corp_actions.json from NSE's OFFICIAL corporate-actions feed.

Instead of guessing splits/bonuses from price gaps, we use NSE's published
list: exact symbol, ex-date, and ratio. Output maps each symbol to a list of
{ex_date, factor, subject}, where `factor` is the price multiplier applied to
all bars BEFORE the ex-date (e.g. 1:1 bonus -> 0.5, 1:10 split -> 0.1).

  Bonus A:B  -> A new shares for B held -> factor = B / (A + B)
  Split Rs X -> Rs Y -> factor = Y / X

Usage:  python3 corp_actions.py
"""

import re
import json
import time
import datetime
import urllib.request
import http.cookiejar

NSE = "https://www.nseindia.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
OUT = "corp_actions.json"
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))


def _warm():
    for u in (NSE + "/", NSE + "/companies-listing/corporate-filings-actions"):
        try:
            _opener.open(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=15).read()
        except Exception:
            pass


def fetch(frm, to):
    url = (f"{NSE}/api/corporates-corporateActions?index=equities"
           f"&from_date={frm:%d-%m-%Y}&to_date={to:%d-%m-%Y}")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json",
        "Referer": NSE + "/companies-listing/corporate-filings-actions"})
    return json.loads(_opener.open(req, timeout=30).read().decode("utf-8"))


def parse_factor(subject):
    s = subject.lower()
    m = re.search(r"bonus\s+(\d+)\s*:\s*(\d+)", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return b / (a + b) if (a + b) else None
    if "split" in s or "sub-division" in s or "sub division" in s:
        nums = re.findall(r"(?:rs\.?|re\.?)\s*([\d.]+)", s)
        if len(nums) >= 2:
            frm_v, to_v = float(nums[0]), float(nums[1])
            return to_v / frm_v if frm_v else None
    return None


def iso(ex):
    d, mon, y = ex.split("-")
    return datetime.date(int(y), MONTHS[mon], int(d)).isoformat()


def run():
    _warm()
    start = datetime.date(2021, 1, 1)
    end = datetime.date(2026, 6, 30)
    out = {}
    seen = set()
    d = start
    chunk = datetime.timedelta(days=90)
    while d < end:
        to = min(d + chunk, end)
        try:
            recs = fetch(d, to)
        except Exception as e:
            print(f"  {d}..{to}: fetch error {e}; retrying"); _warm(); time.sleep(1)
            try:
                recs = fetch(d, to)
            except Exception as e2:
                print(f"  {d}..{to}: failed again {e2}"); recs = []
        n = 0
        for a in recs:
            subj = (a.get("subject") or "").strip()
            sym = (a.get("symbol") or "").strip().upper()
            ex = a.get("exDate") or ""
            if not (sym and ex and ("-" in ex)):
                continue
            f = parse_factor(subj)
            if not f or f <= 0 or f >= 1.0:        # only price-reducing actions
                continue
            try:
                exd = iso(ex)
            except Exception:
                continue
            k = (sym, exd, subj)
            if k in seen:
                continue
            seen.add(k)
            out.setdefault(sym, []).append({"ex_date": exd, "factor": round(f, 6),
                                            "subject": subj})
            n += 1
        print(f"  {d:%Y-%m-%d}..{to:%Y-%m-%d}: {len(recs):>4} recs, {n} bonus/split")
        d = to
        time.sleep(0.4)
    for sym in out:
        out[sym].sort(key=lambda x: x["ex_date"])
    json.dump(out, open(OUT, "w"), indent=0)
    print(f"\nWrote {OUT}: {len(out)} symbols, {sum(len(v) for v in out.values())} actions")
    for s in ("RELIANCE", "TRENT", "BEL", "HDFCBANK", "WIPRO"):
        if s in out:
            print(f"  {s}: {out[s]}")


if __name__ == "__main__":
    run()
