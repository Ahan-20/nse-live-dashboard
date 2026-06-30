#!/usr/bin/env python3
"""Build fo_list.json: the current F&O underlying stock universe from NSE.
Run periodically (monthly) — the F&O list drifts as NSE adds/removes scrips.

Source: nsearchives.nseindia.com/content/fo/fo_mktlots.csv (NSE official)
"""

import csv
import io
import json
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
EXCLUDE = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
           "BANKEX", "SENSEX", "Symbol"}


def run():
    req = urllib.request.Request(URL, headers={
        "User-Agent": UA, "Referer": "https://www.nseindia.com/"})
    text = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    syms = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        sym = row[1].strip().upper()
        if sym and sym not in EXCLUDE and not sym.startswith("UNDERLY"):
            syms.append(sym)
    syms = sorted(set(syms))
    json.dump(syms, open("fo_list.json", "w"))
    print(f"Wrote fo_list.json: {len(syms)} F&O underlyings")
    print("Sample:", syms[:10])


if __name__ == "__main__":
    run()
