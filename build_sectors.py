#!/usr/bin/env python3
"""Build sector_map.json: symbol -> [sector display names] from NSE's official
sector-index constituent lists. Used by the Trade Finder to map each moving
stock to its sector(s) so we can check sector/stock alignment."""

import csv
import io
import json
import urllib.request

ARCH = "https://nsearchives.nseindia.com/content/indices/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# sector-index CSV -> display name (matching the dashboard's allIndices labels)
SECTORS = {
    "ind_niftybanklist.csv": "Nifty Bank",
    "ind_niftyitlist.csv": "Nifty IT",
    "ind_niftyautolist.csv": "Nifty Auto",
    "ind_niftypharmalist.csv": "Nifty Pharma",
    "ind_niftymetallist.csv": "Nifty Metal",
    "ind_niftyfmcglist.csv": "Nifty FMCG",
    "ind_niftyenergylist.csv": "Nifty Energy",
    "ind_niftyrealtylist.csv": "Nifty Realty",
    "ind_niftymedialist.csv": "Nifty Media",
    "ind_niftypsubanklist.csv": "Nifty PSU Bank",
    "ind_niftyfinancelist.csv": "Nifty Financial Services",
    "ind_niftyfmcg.csv": "Nifty FMCG",
    "ind_niftyhealthcarelist.csv": "Nifty Healthcare",
    "ind_niftyconsumerdurableslist.csv": "Nifty Consumer Durables",
    "ind_niftyoilgaslist.csv": "Nifty Oil & Gas",
}


def fetch(name):
    req = urllib.request.Request(ARCH + name, headers={
        "User-Agent": UA, "Referer": "https://www.nseindia.com/"})
    return urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")


def run():
    out = {}
    for fname, disp in SECTORS.items():
        try:
            rows = list(csv.DictReader(io.StringIO(fetch(fname))))
        except Exception as e:
            print(f"  skip {fname}: {e}")
            continue
        n = 0
        for r in rows:
            sym = (r.get("Symbol") or "").strip().upper()
            if not sym or sym.startswith("DUMMY"):
                continue
            out.setdefault(sym, [])
            if disp not in out[sym]:
                out[sym].append(disp)
                n += 1
        print(f"  {disp:<26} {n} symbols")
    json.dump(out, open("sector_map.json", "w"), indent=0)
    print(f"\nWrote sector_map.json: {len(out)} symbols")


if __name__ == "__main__":
    run()
