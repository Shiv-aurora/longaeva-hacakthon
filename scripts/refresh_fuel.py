#!/usr/bin/env python3
"""Refresh public EIA Gulf Coast jet fuel prices via FRED CSV distribution.

Requires Internet on the user's own machine. This does not create an as-of historical vintage.
For provenance, retain snapshot and fetched-at date. It never fabricates weekend prices.
"""
from __future__ import annotations
import csv
from datetime import datetime, timezone, date
import io
import json
from pathlib import Path
import shutil
import sys
from urllib.request import Request, urlopen
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.data import FUEL_CSV, save_fuel_csv, load_fuel

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DJFUELUSGULF"

def fetch():
    req = Request(URL, headers={"User-Agent":"UAL-Counterfactual-Research/0.1 (public research CSV)"})
    with urlopen(req, timeout=30) as r:
        payload=r.read(1_800_000).decode("utf-8-sig")
    reader=csv.DictReader(io.StringIO(payload))
    if not reader.fieldnames:
        raise ValueError("No CSV header")
    date_field=next((x for x in ("observation_date", "DATE", "date") if x in reader.fieldnames), None)
    price_field=next((x for x in ("DJFUELUSGULF", "value") if x in reader.fieldnames), None)
    if date_field is None or price_field is None:
        raise ValueError("Unexpected CSV columns, refusing to overwrite snapshot")
    rows=[]
    for d in reader:
        val=d[price_field].strip()
        if val in ("", ".", "NA"):
            continue
        date.fromisoformat(d[date_field])
        price=float(val)
        if not 0.1 < price < 20:
            raise ValueError("Unreasonable fuel price")
        rows.append({"date":d[date_field],"usd_per_gallon":price})
    rows=sorted(rows,key=lambda x:x["date"])
    if len(rows)<1000:
        raise ValueError("Unexpectedly short FRED download; snapshot not replaced")
    current=load_fuel()
    if rows[-1]["date"] < current[-1]["date"]:
        raise ValueError("Fetched data is older than existing snapshot")
    backup=FUEL_CSV.with_name("eia_gulf_coast_2026_before_refresh.csv")
    shutil.copyfile(FUEL_CSV,backup)
    save_fuel_csv(rows)
    manifest={"retrieved_at_utc":datetime.now(timezone.utc).isoformat(),"csv_url":URL,
              "ultimate_source":"US Energy Information Administration", "fred_series":"DJFUELUSGULF",
              "observation_count":len(rows),"last_observation":rows[-1]["date"],
              "revisions_warning":"Not a real-time-vintage panel. Historical observations may have been revised."}
    (FUEL_CSV.parent/"refresh_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    print(f"Updated through {rows[-1]['date']} ({len(rows)} total observations); backup: {backup.name}")

if __name__ == "__main__":
    try: fetch()
    except Exception as exc:
        print("Fuel refresh failed, existing snapshot retained. Details:",exc,file=sys.stderr)
        raise SystemExit(1)
