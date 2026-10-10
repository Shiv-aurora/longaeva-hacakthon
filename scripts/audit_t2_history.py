"""Independent Decimal audit from original T2 and P52 CSVs, not importer parsing."""
from collections import defaultdict, Counter
import csv
from decimal import Decimal
import hashlib
import io
import json
import math
from pathlib import Path
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.evidence import digest
from app.source_store import read_source

FIELDS = ("AVL_SEAT_MILES_320", "REV_PAX_MILES_140", "REV_ACRFT_HRS_AIRBORNE_610", "HOURS_AIRBORNE_650", "AIR_DAYS_EQUIP_810", "AIRCRAFT_FUELS_921")
TARGETS = {"capacity_asm_per_assigned_day": (FIELDS[0], FIELDS[4]),
    "reported_hours_per_assigned_day": (FIELDS[3], FIELDS[4]),
    "revenue_hours_per_assigned_day": (FIELDS[2], FIELDS[4]),
    "asm_per_reported_hour": (FIELDS[0], FIELDS[3]), "asm_per_revenue_hour": (FIELDS[0], FIELDS[2])}


def csv_rows(content):
    with ZipFile(io.BytesIO(content)) as z:
        names=[n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(names)!=1: raise ValueError("Expected one original CSV")
        text=z.read(names[0]).decode("utf-8-sig")
    reader=csv.DictReader(io.StringIO(text))
    for row in reader: yield reader.line_num,row


def number(value):
    return Decimal(value) if value.strip() else None


def identity(r, region):
    return (r["UNIQUE_CARRIER"], int(r["AIRLINE_ID"]) if r["AIRLINE_ID"] else None,
        r["UNIQUE_CARRIER_ENTITY"],r[region],f"{r['YEAR']}-Q{r['QUARTER']}",
        r["AIRCRAFT_GROUP"],r["AIRCRAFT_TYPE"],r["AIRCRAFT_CONFIG"])


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument("--snapshot",type=Path);args=parser.parse_args()
    snapshot=args.snapshot or ROOT / "data/t2_history" / json.loads((ROOT / "data/t2_history/latest.json").read_text())["snapshot_id"]
    manifest=json.loads((snapshot / "manifest.json").read_text())
    if digest({k:v for k,v in manifest.items() if k!="snapshot_id"})!=manifest["snapshot_id"]:raise ValueError("Manifest checksum failed")
    panel=json.loads((snapshot / "panel.json").read_text())
    if digest(panel)!=manifest["panel_digest"]:raise ValueError("Panel checksum failed")
    classes={r["aircraft_type"]:r["fleet_group"] for r in manifest["classification"]["rows"]}
    source,_=read_source(manifest["source"]["sha256"])
    originals={}; groups=defaultdict(list); unknown=set()
    for line,r in csv_rows(source):
        if int(r["YEAR"])<2013 or r["UNIQUE_CARRIER"] not in {"UA","DL","AA"} or r["SERVICE_CLASS"]!="Z" or r["CARRIER_REGION"] not in {"D","A","P","L"} or r["AIRCRAFT_CONFIG"] not in {"1","3"} or r["AIRCRAFT_TYPE"]=="999":continue
        originals[line]=r
        if r["AIRCRAFT_TYPE"] not in classes:unknown.add((r["UNIQUE_CARRIER"],f"{r['YEAR']}-Q{r['QUARTER']}"));continue
        groups[(r["UNIQUE_CARRIER"],classes[r["AIRCRAFT_TYPE"]],f"{r['YEAR']}-Q{r['QUARTER']}")].append((line,r))
    checked=Counter(); witnesses=[]
    if len(panel["group_quarters"])!=len(groups):raise ValueError("Group coverage differs from original T2")
    for saved in panel["group_quarters"]:
        key=(saved["unique_carrier"],saved["fleet_group"],saved["period"])
        rows=groups[key]
        if sorted(saved["source_lines"])!=sorted(line for line,_ in rows):raise ValueError("Original source line set differs")
        general=(key[0],key[2]) in unknown or len({r["AIRLINE_ID"] for _,r in rows})!=1 or any(not r["AIRLINE_ID"] or not r["UNIQUE_CARRIER_ENTITY"] or r["NUM_MONTHS"]!="3" for _,r in rows)
        general=general or any(number(r[FIELDS[1]]) is not None and number(r[FIELDS[0]]) is not None and number(r[FIELDS[1]])>number(r[FIELDS[0]]) for _,r in rows)
        totals={}
        for field in FIELDS:
            values=[number(r[field]) for _,r in rows]
            total=sum(values,Decimal(0)) if all(v is not None and v>=0 for v in values) else None
            totals[field]=total
            expected=saved["all_service_totals"][field]
            if (total is None)!=(expected is None) or total is not None and not math.isclose(float(total),expected,rel_tol=1e-12,abs_tol=1e-8):raise ValueError("Original quantity sum differs: "+field)
        target_values={}
        for target,(numerator,denominator) in TARGETS.items():
            a,b=totals[numerator],totals[denominator]
            value=a/b if not general and a is not None and b is not None and b>0 else None
            if value is not None and target.endswith("hours_per_assigned_day") and value>24:value=None
            stored=saved["targets"][target]
            if (value is None)!=(stored is None) or value is not None and not math.isclose(float(value),stored,rel_tol=1e-12,abs_tol=1e-8):raise ValueError("Original target/gate differs: "+target)
            if value is not None:checked[target]+=1
            target_values[target]=str(value) if value is not None else None
        witnesses.append({"key":list(key),"source_lines":saved["source_lines"],"Decimal_targets":target_values})
    fm_path=ROOT / "data/form41_history" / manifest["form41_snapshot_id"] / "manifest.json"
    fm=json.loads(fm_path.read_text())
    if digest({k:v for k,v in fm.items() if k!="snapshot_id"})!=fm["snapshot_id"]:raise ValueError("Original P52 manifest differs")
    p52_sha=next(s["sha256"] for s in fm["sources"] if s["schedule"]=="P52")
    p52_bytes,_=read_source(p52_sha);p52={}
    for line,r in csv_rows(p52_bytes):
        if int(r["YEAR"])>=2013:
            key=identity(r,"REGION")
            if key in p52:raise ValueError("Duplicate original P52 grain")
            p52[key]=(line,r)
    counts={field:Counter() for field in (FIELDS[3],FIELDS[4],FIELDS[5])}
    if len(panel["P52_reconciliation"])!=len(originals):raise ValueError("Reconciliation row coverage differs")
    for saved in panel["P52_reconciliation"]:
        r=originals[saved["t2_source_line"]]; key=identity(r,"CARRIER_REGION");match=p52.get(key)
        if saved["p52_source_line"]!=(match[0] if match else None):raise ValueError("Original P52 line differs")
        for tf,pf in ((FIELDS[3],"TOTAL_AIR_HOURS"),(FIELDS[4],"AIR_DAYS_ASSIGN"),(FIELDS[5],"AIR_FUELS_ISSUED")):
            a=number(r[tf]); b=number(match[1][pf]) if match else None
            b=b*1000 if b is not None else None
            difference=a-b if a is not None and b is not None else None
            status="no_P52_match" if not match else "missing_quantity" if difference is None else "within_P52_rounding_resolution" if abs(difference)<=Decimal("5.000001") else "beyond_P52_rounding_resolution"
            if saved["comparisons"][tf]["status"]!=status:raise ValueError("P52 reconciliation status differs")
            stored=saved["comparisons"][tf]["difference"]
            if (difference is None)!=(stored is None) or difference is not None and not math.isclose(float(difference),stored,rel_tol=1e-12,abs_tol=1e-6):raise ValueError("P52 difference differs")
            counts[tf][status]+=1
    t2_keys={identity(r,"CARRIER_REGION") for r in originals.values()}
    missing={line:r for key,(line,r) in p52.items() if key not in t2_keys and r["UNIQUE_CARRIER"] in {"UA","DL","AA"} and r["REGION"] in {"D","A","P","L"} and r["AIRCRAFT_CONFIG"] in {"1","3"} and r["AIRCRAFT_TYPE"]!="999"}
    if len(missing)!=len(panel["P52_without_T2_Z"]):raise ValueError("Reverse P52 coverage differs")
    reverse_counts=Counter()
    for saved in panel["P52_without_T2_Z"]:
        r=missing[saved["p52_source_line"]]
        values=[number(r[f]) for f in ("TOTAL_AIR_HOURS","AIR_DAYS_ASSIGN","AIR_FUELS_ISSUED")]
        status="reported_zero_aircraft_quantities" if all(v==0 for v in values) else "positive_reported_exposure_or_activity" if any(v is not None and v>0 for v in values) else "unknown_or_invalid_aircraft_quantities"
        if saved["status"]!=status:raise ValueError("Reverse P52 status differs")
        for field,value in zip((FIELDS[3],FIELDS[4],FIELDS[5]),values):
            stored=saved["P52_quantities"][field]
            if (value is None)!=(stored is None) or value is not None and not math.isclose(float(value*1000),stored,rel_tol=1e-12,abs_tol=1e-6):raise ValueError("Reverse P52 quantity differs")
        reverse_counts[status]+=1
    report={"version":"t2-original-CSV-audit-0.1.0","snapshot_id":manifest["snapshot_id"],
        "t2_source_sha256":manifest["source"]["sha256"],"p52_source_sha256":p52_sha,
        "group_quarters_checked":len(witnesses),"original_T2_Z_records_checked":len(originals),
        "eligible_targets_checked":dict(checked),"P52_reconciliation_counts":{k:dict(v) for k,v in counts.items()},
        "P52_without_T2_counts":dict(reverse_counts),
        "witnesses":witnesses,"audit_code_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope":"Input accounting/gates across retained quarters, including 2024+; no model selection, locked forecast scores or simulator validation"}
    target=ROOT / "data/t2_checks" / (manifest["snapshot_id"]+".json");target.parent.mkdir(exist_ok=True)
    if target.exists() and json.loads(target.read_text())!=report:raise ValueError("Immutable T2 audit differs")
    if not target.exists():target.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k!="witnesses"},indent=2))


if __name__=="__main__":main()
