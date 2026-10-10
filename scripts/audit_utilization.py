"""Check estimation targets against original P52 CSV and recalculate score cells."""
from collections import defaultdict
from decimal import Decimal
from io import BytesIO, StringIO
import csv
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.evidence import digest
from app.source_store import read_source


def main():
    destination = ROOT / "data/utilization"
    identity = json.loads((destination / "latest.json").read_text())["snapshot_id"]
    path = destination / identity
    manifest = json.loads((path / "manifest.json").read_text())
    artifact = json.loads((path / "artifact.json").read_text())
    if digest(artifact) != manifest["artifact_digest"]:
        raise ValueError("Utilization estimate checksum failed")
    upstream = ROOT / "data/form41_history" / manifest["form41_snapshot_id"]
    sources = json.loads((upstream / "manifest.json").read_text())["sources"]
    source = next(s for s in sources if s["schedule"] == "P52")
    content, _ = read_source(source["sha256"])
    classes = {int(r["aircraft_type"]): r["fleet_group"] for r in artifact["classification"]["rows"]}
    raw = defaultdict(list)
    with ZipFile(BytesIO(content)) as archive:
        member = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
        reader = csv.DictReader(StringIO(archive.read(member).decode("utf-8-sig")))
        for row in reader:
            period = f"{int(row['YEAR']):04d}-Q{int(row['QUARTER'])}"
            if not "2013-Q1" <= period <= "2023-Q4" or row["UNIQUE_CARRIER"] not in {"UA", "DL", "AA"} or row["REGION"] not in {"D", "A", "P", "L"} or row["AIRCRAFT_CONFIG"] not in {"1", "3"}:
                continue
            group = classes.get(int(row["AIRCRAFT_TYPE"]))
            if group is not None:
                raw[(row["UNIQUE_CARRIER"], group, period)].append((reader.line_num, row))
    quantities = {}
    witnesses = []
    for observed in artifact["panel"]["observations"]:
        key = (observed["unique_carrier"], observed["fleet_group"], observed["period"])
        selected = raw[key]
        hours = sum(Decimal(r["TOTAL_AIR_HOURS"]) * 1000 for _, r in selected)
        days = sum(Decimal(r["AIR_DAYS_ASSIGN"]) * 1000 for _, r in selected)
        if abs(float(hours) - observed["hours"]) > 1e-7 or abs(float(days) - observed["aircraft_days"]) > 1e-7 or abs(float(hours/days) - observed["utilization_hpd"]) > 1e-10:
            raise ValueError(f"Raw utilization quantities differ: {key}")
        if {line for line, _ in selected} != {r["source_line"] for r in observed["source_rows"]}:
            raise ValueError("Utilization source line set differs")
        quantities[key] = float(hours/days)
        if len(witnesses) < 8:
            line, r = selected[0]
            witnesses.append({"key": list(key), "source_member": member, "source_line": line,
                "raw_hours_thousand": r["TOTAL_AIR_HOURS"], "raw_days_thousand": r["AIR_DAYS_ASSIGN"],
                "group_hours": str(hours), "group_days": str(days)})
    estimation = artifact["estimation"]
    errors = defaultdict(list)
    for prediction in estimation["selection_predictions"]:
        target = (prediction["carrier"], prediction["group"], prediction["target"])
        if abs(quantities[target] - prediction["actual_hpd"]) > 1e-10:
            raise ValueError("Selection forecast uses an incorrect reported target")
        key = (prediction["model"], prediction["carrier"], prediction["group"], prediction["horizon_quarters"])
        errors[key].append(prediction["prediction_hpd"] - quantities[target])
    checked = []
    for model in estimation["scorecard"]:
        cell_mae = []
        for cell in model["series_horizon_scores"]:
            values = errors[(model["model"], cell["carrier"], cell["group"], cell["horizon_quarters"])]
            mae = sum(abs(e) for e in values)/len(values)
            if len(values) != cell["targets"] or abs(mae - cell["MAE_hpd"]) > 1e-10:
                raise ValueError("Selection MAE/count differs from independent arithmetic")
            cell_mae.append(mae)
        overall = sum(cell_mae)/len(cell_mae)
        if abs(overall - model["selection_equal_weight_MAE_hpd"]) > 1e-10:
            raise ValueError("Equal-weight selection score differs")
        checked.append({"model": model["model"], "equal_weight_MAE_hpd": overall, "cells": len(cell_mae)})
    result = {"utilization_snapshot": identity, "P52_source_sha256": source["sha256"],
        "verified_group_quarter_targets": len(quantities), "verified_source_rows": sum(len(raw[k]) for k in quantities),
        "checked_model_scores": checked, "raw_source_witnesses": witnesses,
        "audit_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "Independent source quantities and score arithmetic; no causal, interval-calibration or 2024+ forecast claim."}
    output = ROOT / "data/utilization_checks"; output.mkdir(exist_ok=True)
    result_path = output / f"{identity}.json"; result_path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"targets": len(quantities), "source_rows": result["verified_source_rows"], "models": len(checked), "path": str(result_path)}))


if __name__ == "__main__":
    main()
