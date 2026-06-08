#!/usr/bin/env python3
"""
Build a local SQLite price DB from NSE daily bhavcopy archives.

NSE publishes one end-of-day "bhavcopy" per trading day containing OHLCV for
every stock. We download those archives (no API key) and store EQ-series rows
into prices(symbol, dt, o, h, l, c, v). One download = all symbols for that day,
so a single pass builds history for the entire market.

Two archive formats, with a cutover around 2024-07-08:
  OLD  https://nsearchives.nseindia.com/content/historical/EQUITIES/<YYYY>/<MON>/cm<DD><MON><YYYY>bhav.csv.zip
  NEW  https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip

Usage:
  python3 ingest.py 2021-06-08 2026-06-08          # date range
  python3 ingest.py --days 365                       # last N calendar days
Re-runs are incremental: days already in the DB (or known-missing) are skipped.
"""

import io
import os
import sys
import csv
import zipfile
import sqlite3
import datetime
import urllib.request
import urllib.error
import http.cookiejar
from concurrent.futures import ThreadPoolExecutor

ARCH = "https://nsearchives.nseindia.com"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prices.db")
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
NEW_FROM = datetime.date(2024, 7, 8)   # UDiFF cutover

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))


def _urls_for(d):
    """Candidate archive URLs for a date, preferred format first."""
    new = f"{ARCH}/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    mon = MONTHS[d.month - 1]
    old = f"{ARCH}/content/historical/EQUITIES/{d.year}/{mon}/cm{d.day:02d}{mon}{d.year}bhav.csv.zip"
    return (new, old) if d >= NEW_FROM else (old, new)


def _fetch_zip(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://www.nseindia.com/"})
    with _opener.open(req, timeout=30) as r:
        return r.read()


def _parse(raw):
    """Return list of (symbol, dt_iso, o,h,l,c,v) for EQ rows in a bhavcopy zip."""
    out = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = z.namelist()[0]
        text = z.read(name).decode("utf-8", "ignore")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return out
    head = [h.strip() for h in rows[0]]
    if "TckrSymb" in head:                       # NEW UDiFF
        ix = {k: head.index(k) for k in
              ("TckrSymb", "SctySrs", "TradDt", "OpnPric", "HghPric",
               "LwPric", "ClsPric", "TtlTradgVol")}
        for r in rows[1:]:
            if len(r) <= ix["TtlTradgVol"]:
                continue
            if r[ix["SctySrs"]].strip() != "EQ":
                continue
            try:
                out.append((r[ix["TckrSymb"]].strip(), r[ix["TradDt"]].strip()[:10],
                            float(r[ix["OpnPric"]]), float(r[ix["HghPric"]]),
                            float(r[ix["LwPric"]]), float(r[ix["ClsPric"]]),
                            float(r[ix["TtlTradgVol"]] or 0)))
            except (ValueError, IndexError):
                continue
    else:                                        # OLD
        ix = {k: head.index(k) for k in
              ("SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE",
               "TIMESTAMP", "TOTTRDQTY")}
        for r in rows[1:]:
            if len(r) <= ix["TOTTRDQTY"]:
                continue
            if r[ix["SERIES"]].strip() != "EQ":
                continue
            try:
                ts = datetime.datetime.strptime(r[ix["TIMESTAMP"]].strip(), "%d-%b-%Y").date()
                out.append((r[ix["SYMBOL"]].strip(), ts.isoformat(),
                            float(r[ix["OPEN"]]), float(r[ix["HIGH"]]),
                            float(r[ix["LOW"]]), float(r[ix["CLOSE"]]),
                            float(r[ix["TOTTRDQTY"]] or 0)))
            except (ValueError, IndexError):
                continue
    return out


def _db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS prices(
        symbol TEXT, dt TEXT, o REAL, h REAL, l REAL, c REAL, v REAL,
        PRIMARY KEY(symbol, dt))""")
    con.execute("""CREATE TABLE IF NOT EXISTS days(
        dt TEXT PRIMARY KEY, status TEXT)""")   # status: ok | none
    con.execute("CREATE INDEX IF NOT EXISTS ix_sym ON prices(symbol, dt)")
    con.commit()
    return con


def _day(d):
    """Download+parse one date. Returns ('ok', rows) | ('none', []) | ('err', [])."""
    for url in _urls_for(d):
        try:
            return ("ok", _parse(_fetch_zip(url)))
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                continue
            return ("err", [])
        except Exception:
            continue
    return ("none", [])


def run(start, end):
    con = _db()
    done = {r[0] for r in con.execute("SELECT dt FROM days").fetchall()}
    dates = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in done:   # skip weekends + cached
            dates.append(d)
        d += datetime.timedelta(days=1)
    print(f"DB: {DB_PATH}\nDates to fetch: {len(dates)} (cached: {len(done)})")

    ok = miss = err = total_rows = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        for i, (d, (status, rows)) in enumerate(zip(dates, ex.map(_day, dates)), 1):
            if status == "ok":
                con.executemany(
                    "INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?)",
                    [(s, dt, o, h, l, c, v) for (s, dt, o, h, l, c, v) in rows])
                con.execute("INSERT OR REPLACE INTO days VALUES (?, 'ok')", (d.isoformat(),))
                ok += 1
                total_rows += len(rows)
            elif status == "none":
                con.execute("INSERT OR REPLACE INTO days VALUES (?, 'none')", (d.isoformat(),))
                miss += 1
            else:
                err += 1
            if i % 50 == 0:
                con.commit()
                print(f"  {i}/{len(dates)}  ok={ok} holiday/none={miss} err={err} rows={total_rows}")
    con.commit()
    n_sym = con.execute("SELECT COUNT(DISTINCT symbol) FROM prices").fetchone()[0]
    n_row = con.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    print(f"DONE. trading days ok={ok}, holidays/missing={miss}, errors={err}")
    print(f"DB now: {n_sym} symbols, {n_row} rows")
    con.close()


if __name__ == "__main__":
    a = sys.argv[1:]
    today = datetime.date(2026, 6, 8)            # pinned (sandbox clock differs)
    if a and a[0] == "--days":
        end = today
        start = end - datetime.timedelta(days=int(a[1]))
    elif len(a) == 2:
        start = datetime.date.fromisoformat(a[0])
        end = datetime.date.fromisoformat(a[1])
    else:
        end = today
        start = end - datetime.timedelta(days=365 * 5)
    run(start, end)
