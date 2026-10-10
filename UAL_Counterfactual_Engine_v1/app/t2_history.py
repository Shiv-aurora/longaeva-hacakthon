"""Quarterly aircraft capacity observations, with service aggregates kept separate."""
from __future__ import annotations

from collections import Counter, defaultdict
import calendar
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse
from zipfile import ZipFile

import duckdb

from .evidence import digest
from .source_store import ROOT, RAW, preserve_source, read_source, byte_hash

VERSION = "t2-aircraft-capacity-0.1.0"
HISTORY = ROOT / "data/t2_history"
METRICS = "AVL_SEAT_MILES_320 REV_PAX_MILES_140 REV_TON_MILES_240 REV_TON_MILES_FREIGHT_247 REV_TON_MILES_MAIL_249 AVL_TON_MILES_280 REV_ACRFT_MILES_FLOWN_410 REV_ACRFT_DEP_PERF_510 REV_ACRFT_HRS_AIRBORNE_610 NON_REV_ACRFT_HRS_AIRBORNE_620 ACRFT_HRS_RAMPTORAMP_630 HOURS_AIRBORNE_650 AIR_DAYS_EQUIP_810 AIR_DAYS_ROUTE_820 AIRCRAFT_FUELS_921".split()
DIMS = "YEAR QUARTER AIRLINE_ID UNIQUE_CARRIER UNIQUE_CARRIER_NAME UNIQUE_CARRIER_ENTITY CARRIER_REGION CARRIER CARRIER_NAME CARRIER_GROUP CARRIER_GROUP_NEW AIRCRAFT_GROUP AIRCRAFT_TYPE AIRCRAFT_CONFIG SERVICE_CLASS NUM_MONTHS".split()
TARGET_FIELDS = {"capacity_asm_per_assigned_day": ("AVL_SEAT_MILES_320", "AIR_DAYS_EQUIP_810"),
    "reported_hours_per_assigned_day": ("HOURS_AIRBORNE_650", "AIR_DAYS_EQUIP_810"),
    "revenue_hours_per_assigned_day": ("REV_ACRFT_HRS_AIRBORNE_610", "AIR_DAYS_EQUIP_810"),
    "asm_per_reported_hour": ("AVL_SEAT_MILES_320", "HOURS_AIRBORNE_650"),
    "asm_per_revenue_hour": ("AVL_SEAT_MILES_320", "REV_ACRFT_HRS_AIRBORNE_610")}
P52_FIELDS = {"HOURS_AIRBORNE_650": ("TOTAL_AIR_HOURS", 1),
    "AIR_DAYS_EQUIP_810": ("AIR_DAYS_ASSIGN", 1), "AIRCRAFT_FUELS_921": ("AIR_FUELS_ISSUED", 1000000)}


def official_url(url):
    p = urlparse(url)
    return p.scheme == "https" and p.hostname == "www.transtats.bts.gov" and p.path.lower() == "/dl_selectfields.aspx" and parse_qs(p.query).get("gnoyr_VQ") == ["FIH"]


def preserve_export(entry, raw=RAW):
    if not official_url(entry["source_url"]) or entry.get("filters") != {"year": "All Years", "period": "All", "fields": "All"}:
        raise ValueError("T2 requires the inspected official full-table export")
    s = preserve_source(Path(entry["path"]).read_bytes(), entry["source_url"], "application/zip", raw)
    d = preserve_source(Path(entry["definition_path"]).read_bytes(), entry["source_url"], "text/plain", raw)
    return {**s, "filters": entry["filters"], "definition_sha256": d["sha256"]}


def integer(value, field, low, high, nullable=False):
    if not value.strip() and nullable: return None
    if not re.fullmatch(r"\d+", value.strip()) or not low <= int(value) <= high:
        raise ValueError("Invalid T2 dimension: " + field)
    return int(value)


def amount(value, field):
    if not value.strip(): return None
    try: result = Decimal(value)
    except InvalidOperation as e: raise ValueError("Invalid T2 quantity: " + field) from e
    if not result.is_finite() or abs(result) > Decimal("1e15"):
        raise ValueError("Nonfinite/excessive T2 quantity: " + field)
    return float(result)


def partition(year):
    return "fit_2013_2019" if year <= 2019 else "selection_2020_2023" if year <= 2023 else "locked_test_2024_plus"


def normalize_export(content, source, start_year=2013):
    if not official_url(source["source_url"]) or byte_hash(content) != source["sha256"]:
        raise ValueError("T2 source checksum or official URL differs")
    with ZipFile(io.BytesIO(content)) as z:
        members = [m for m in z.infolist() if m.filename.lower().endswith(".csv") and not m.is_dir()]
        if len(members) != 1 or members[0].file_size > 150000000 or members[0].flag_bits & 1:
            raise ValueError("Expected one bounded unencrypted T2 CSV member")
        text = z.read(members[0]).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or len(reader.fieldnames) != len(DIMS + METRICS) or set(reader.fieldnames) != set(DIMS + METRICS):
        raise ValueError("T2 full-field schema differs")
    rows, grains, scanned = [], set(), 0
    for raw in reader:
        scanned += 1
        if None in raw or any(v is None for v in raw.values()):
            raise ValueError("Malformed T2 CSV row")
        year, q = integer(raw["YEAR"], "year", 1900, 2100), integer(raw["QUARTER"], "quarter", 1, 4)
        if year < start_year: continue
        airline = integer(raw["AIRLINE_ID"], "airline ID", 1, 999999, True)
        carrier = raw["UNIQUE_CARRIER"].strip() or None
        entity, region, service = raw["UNIQUE_CARRIER_ENTITY"].strip(), raw["CARRIER_REGION"].strip(), raw["SERVICE_CLASS"].strip()
        aircraft = {}
        for field, high in (("AIRCRAFT_GROUP", 9), ("AIRCRAFT_TYPE", 999), ("AIRCRAFT_CONFIG", 9)):
            integer(raw[field], field, 0, high); aircraft[field.lower()] = str(int(raw[field]))
        reported_months = integer(raw["NUM_MONTHS"], "months reported", 1, 3, True)
        grain = (year, q, carrier, airline, entity, region, *aircraft.values(), service)
        identity_known = bool(carrier and airline and entity and region and service)
        if identity_known and grain in grains: raise ValueError("Duplicate T2 reporting grain")
        if identity_known: grains.add(grain)
        values = {k: amount(raw[k], k) for k in METRICS}
        flags = ["negative_reported_quantity_" + k for k, v in values.items() if v is not None and v < 0]
        if carrier is None or airline is None: flags.append("missing_certificate_identity")
        if not entity or not region or not service: flags.append("unresolved_reporting_service_identity_keep_source_row")
        if region not in {"D", "A", "P", "L", "I", "S"}: flags.append("unknown_reporting_region")
        if service not in {"F", "G", "L", "N", "P", "R", "K", "V", "Z"}: flags.append("unknown_service_class")
        if reported_months != 3: flags.append("quarter_reporting_months_incomplete")
        if values["REV_PAX_MILES_140"] is not None and values["AVL_SEAT_MILES_320"] is not None and values["REV_PAX_MILES_140"] > values["AVL_SEAT_MILES_320"]:
            flags.append("rpm_exceeds_asm")
        rows.append({"schedule": "T2", "period": f"{year}-Q{q}", "year": year, "quarter": q,
            "unique_carrier": carrier, "airline_id": airline, "carrier": raw["CARRIER"], "carrier_name": raw["CARRIER_NAME"],
            "reporting_entity": entity, "reporting_region": region, **aircraft,
            "service_class": service, "months_reported": reported_months, "values": values,
            "quality_flags": flags, "partition": partition(year), "source_sha256": source["sha256"],
            "source_member": members[0].filename, "source_line": reader.line_num})
    rows.sort(key=lambda r: (r["period"], str(r["unique_carrier"]), str(r["airline_id"]), r["reporting_entity"], r["aircraft_type"], r["aircraft_config"], r["aircraft_group"], r["service_class"]))
    return {"rows": rows, "scanned_rows": scanned, "retained_rows": len(rows),
        "quality_counts": dict(sorted(Counter(f for r in rows for f in r["quality_flags"]).items())),
        "service_class_counts": dict(sorted(Counter(r["service_class"] for r in rows).items()))}


def aircraft_key(row):
    return tuple(row[k] for k in ("unique_carrier", "airline_id", "reporting_entity", "reporting_region", "period", "aircraft_group", "aircraft_type", "aircraft_config"))


def capacity_panel(rows, classification, p52):
    classes = {str(int(r["aircraft_type"])): r["fleet_group"] for r in classification["rows"]}
    if len(classes) != len(classification["rows"]) or set(classes.values()) != {"narrowbody", "widebody"}:
        raise ValueError("Invalid T2 aircraft classification")
    index = {}
    for r in p52:
        key = aircraft_key(r)
        if key in index: raise ValueError("Duplicate P52 reconciliation grain")
        index[key] = r
    groups, unknown, reconciliation, selected_grains = defaultdict(list), set(), [], set()
    for row in rows:
        # Z already sums K+V (K=F+G; V=L+N+P+R). Never sum it with its components.
        if row["unique_carrier"] not in {"UA", "DL", "AA"} or row["service_class"] != "Z" or row["reporting_region"] not in {"D", "A", "P", "L"} or row["aircraft_config"] not in {"1", "3"} or row["aircraft_type"] == "999":
            continue
        key = aircraft_key(row)
        if key in selected_grains: raise ValueError("Duplicate selected T2 aircraft grain")
        selected_grains.add(key)
        if row["aircraft_type"] not in classes:
            unknown.add((row["unique_carrier"], row["period"]))
        else:
            groups[(row["unique_carrier"], classes[row["aircraft_type"]], row["period"])].append(row)
        match = index.get(key)
        comparisons = {}
        for field, (pfield, scale) in P52_FIELDS.items():
            a, b = row["values"][field], match["values"][pfield] * scale if match and match["values"][pfield] is not None else None
            difference = a - b if a is not None and b is not None else None
            comparisons[field] = {"t2": a, "p52": b, "difference": difference,
                "status": "no_P52_match" if not match else "missing_quantity" if difference is None else
                    "within_P52_rounding_resolution" if abs(difference) <= 5.000001 else "beyond_P52_rounding_resolution"}
        reconciliation.append({"t2_source_line": row["source_line"], "t2_source_sha256": row["source_sha256"],
            "key": {k: row[k] for k in ("unique_carrier", "airline_id", "reporting_entity", "reporting_region", "period", "aircraft_group", "aircraft_type", "aircraft_config")},
            "p52_source_line": match["source_line"] if match else None,
            "p52_source_sha256": match["source_sha256"] if match else None, "comparisons": comparisons})
    reverse = []
    for key, row in index.items():
        if row["unique_carrier"] not in {"UA", "DL", "AA"} or row["reporting_region"] not in {"D", "A", "P", "L"} or row["aircraft_config"] not in {"1", "3"} or row["aircraft_type"] == "999" or key in selected_grains:
            continue
        quantities = {k: row["values"][v] * scale if row["values"][v] is not None else None for k, (v, scale) in P52_FIELDS.items()}
        values = list(quantities.values())
        status = "reported_zero_aircraft_quantities" if all(v == 0 for v in values) else "positive_reported_exposure_or_activity" if any(v is not None and v > 0 for v in values) else "unknown_or_invalid_aircraft_quantities"
        reverse.append({"key": {k: row[k] for k in ("unique_carrier", "airline_id", "reporting_entity", "reporting_region", "period", "aircraft_group", "aircraft_type", "aircraft_config")},
            "fleet_group": classes.get(row["aircraft_type"]), "p52_source_sha256": row["source_sha256"], "p52_source_line": row["source_line"],
            "P52_quantities": quantities, "status": status})
    reverse.sort(key=lambda r: repr(r["key"]))
    output = []
    for (carrier, group, period), items in sorted(groups.items()):
        flags = []
        if (carrier, period) in unknown: flags.append("unmapped_aircraft_type_in_certificate_quarter")
        identities = {r["airline_id"] for r in items}
        if len(identities) != 1 or None in identities: flags.append("ambiguous_certificate_identity")
        if any(not r["reporting_entity"] for r in items): flags.append("missing_reporting_entity")
        if any(r["months_reported"] != 3 for r in items): flags.append("quarter_reporting_months_incomplete")
        if any("rpm_exceeds_asm" in r["quality_flags"] for r in items): flags.append("source_rpm_exceeds_asm")
        totals, field_flags = {}, {}
        for field in METRICS:
            values = [r["values"][field] for r in items]
            field_flags[field] = (["missing_quantity"] if any(v is None for v in values) else []) + (["negative_quantity"] if any(v is not None and v < 0 for v in values) else [])
            # Keep valid all-zero aggregates distinct from unknown/negative exposure.
            totals[field] = sum(values) if not field_flags[field] else None
        if totals["REV_PAX_MILES_140"] is not None and totals["AVL_SEAT_MILES_320"] is not None and totals["REV_PAX_MILES_140"] > totals["AVL_SEAT_MILES_320"]:
            flags.append("group_rpm_exceeds_asm")
        targets, exclusions = {}, {}
        for target, (numerator, denominator) in TARGET_FIELDS.items():
            reasons = flags + [f"{field}:{reason}" for field in (numerator, denominator) for reason in field_flags[field]]
            if totals[denominator] is not None and totals[denominator] <= 0: reasons.append("nonpositive_denominator")
            value = totals[numerator] / totals[denominator] if not reasons else None
            if target.endswith("hours_per_assigned_day") and value is not None and value > 24:
                reasons.append("reported_hours_exceed_24_per_day"); value = None
            targets[target] = value; exclusions[target] = reasons
        year, q = int(period[:4]), int(period[-1])
        calendar_days = sum(calendar.monthrange(year, m)[1] for m in range(3*q-2, 3*q+1))
        d = totals["AIR_DAYS_EQUIP_810"]
        valid = all(targets[k] is not None for k in ("capacity_asm_per_assigned_day", "reported_hours_per_assigned_day", "asm_per_reported_hour"))
        # This identity is accounting, not a causal relation or fitted ceiling.
        reconstructed = targets["reported_hours_per_assigned_day"] * targets["asm_per_reported_hour"] if valid else None
        omitted = [r for r in reverse if r["key"]["unique_carrier"] == carrier and r["key"]["period"] == period and r["fleet_group"] == group]
        comparisons = [r for r in reconciliation if r["t2_source_line"] in {i["source_line"] for i in items}]
        exposure_reconciled = bool(comparisons) and all(r["status"] == "reported_zero_aircraft_quantities" for r in omitted) and all(r["comparisons"][f]["status"] == "within_P52_rounding_resolution" for r in comparisons for f in ("HOURS_AIRBORNE_650", "AIR_DAYS_EQUIP_810"))
        output.append({"unique_carrier": carrier, "fleet_group": group, "period": period, "partition": partition(year),
            "all_service_totals": totals, "field_quality_flags": field_flags, "quality_flags": flags,
            "targets": targets, "target_exclusions": exclusions, "source_lines": [r["source_line"] for r in items],
            "source_sha256s": sorted({r["source_sha256"] for r in items}), "source_rows": len(items),
            "calendar_days": calendar_days,
            "implied_average_assigned_aircraft": d / calendar_days if not flags and d is not None and d > 0 else None,
            "capacity_decomposition_difference": reconstructed - targets["capacity_asm_per_assigned_day"] if valid else None,
            "P52_exposure_reconciled": exposure_reconciled,
            "P52_rows_without_T2": {"source_lines": [r["p52_source_line"] for r in omitted],
                "statuses": [{"status": status, "rows": count} for status, count in sorted(Counter(r["status"] for r in omitted).items())]},
            "cross_schedule_model_transfer_eligible": False})
    coverage = []
    for carrier in ("UA", "DL", "AA", "US"):
        periods = sorted({r["period"] for r in rows if r["unique_carrier"] == carrier})
        crows = [r for r in output if r["unique_carrier"] == carrier]
        coverage.append({"unique_carrier": carrier, "quarters": len(periods), "first": periods[0] if periods else None,
            "last": periods[-1] if periods else None, "group_quarters": len(crows),
            "eligible_targets": {k: sum(r["targets"][k] is not None for r in crows) for k in TARGET_FIELDS}})
    return {"group_quarters": output, "P52_reconciliation": reconciliation, "P52_without_T2_Z": reverse, "coverage": coverage,
        "P52_without_T2_counts": dict(sorted(Counter(r["status"] for r in reverse).items())),
        "reconciliation_counts": {field: dict(sorted(Counter(r["comparisons"][field]["status"] for r in reconciliation).items())) for field in P52_FIELDS},
        "unmapped_certificate_quarters": [{"carrier": c, "period": p} for c, p in sorted(unknown)],
        "scope": "T2 all-service Z only; detailed financial regions; passenger/combined aircraft configurations; exact DOT certificates with explicit NB/WB mapping. Includes reported operations omitted from public segment tables. No feeder attribution or geographic financial-region relabeling.",
        "target_status": "Observed quarterly ratios for aircraft rows represented in T2, not physical spare capacity, monthly observations or causal policy. P52-only exposures remain visible; T2 totals need not cover the full P52 group. Assigned-days exposure does not equal owned fleet inventory.",
        "transfer_status": "T2/P52 comparison retains unmatched quantities and discrepancies. Do not automatically substitute T2 targets into the already frozen P52 fit or transfer its coefficients."}


def _read_p52(path):
    manifest = json.loads((path / "manifest.json").read_text())
    if digest({k: v for k, v in manifest.items() if k != "snapshot_id"}) != manifest["snapshot_id"]:
        raise ValueError("P52 upstream manifest checksum failed")
    panel = json.loads((path / "P52.json").read_text())
    if digest(panel) != manifest["panel_digests"]["P52"]: raise ValueError("P52 panel checksum failed")
    return manifest, panel["rows"]


def _json_dates(value):
    if isinstance(value, date): return value.isoformat()
    if isinstance(value, dict): return {k: _json_dates(v) for k, v in value.items()}
    if isinstance(value, list): return [_json_dates(v) for v in value]
    return value


def build_t2_history(source, form41, classification_path, cutoff, destination=HISTORY, raw=RAW):
    cutoff_date = date.fromisoformat(cutoff)
    content, metadata = read_source(source["sha256"], raw)
    _, definition = read_source(source["definition_sha256"], raw)
    if not official_url(source["source_url"]) or metadata["source_url"] != source["source_url"] or definition["source_url"] != source["source_url"] or source.get("filters") != {"year": "All Years", "period": "All", "fields": "All"}:
        raise ValueError("T2 provenance differs from preserved sources")
    acquired = max(datetime.fromisoformat(x["retrieved_at_utc"]).date() for x in (metadata, definition))
    if acquired > cutoff_date: raise ValueError("T2 snapshot was not acquired by this cutoff; historical vintages unavailable")
    normalized = normalize_export(content, source)
    if not normalized["rows"] or any(r["year"] > acquired.year or r["year"] == acquired.year and r["quarter"]*3 > acquired.month for r in normalized["rows"]):
        raise ValueError("Empty T2 export or observation after acquisition")
    fm, p52 = _read_p52(Path(form41))
    p52_available = max(date.fromisoformat(s["available_on"]) for s in fm["sources"] if s["schedule"] == "P52")
    if p52_available > cutoff_date: raise ValueError("P52 reconciliation source is not available by cutoff")
    classification = classification_path if isinstance(classification_path, dict) else json.loads(Path(classification_path).read_text())
    lookup, _ = read_source(classification["type_lookup_sha256"], raw)
    descriptions = {r["Code"]: r["Description"] for r in csv.DictReader(io.StringIO(lookup.decode("utf-8-sig")))}
    if any(descriptions.get(r["aircraft_type"]) != r["description"] for r in classification["rows"]): raise ValueError("T2 classification differs from official descriptions")
    for key in ("configuration_lookup_sha256", "group_lookup_sha256"): read_source(classification[key], raw)
    panel = capacity_panel(normalized["rows"], classification, p52)
    code = Path(__file__).read_bytes()
    dependencies = {n: hashlib.sha256(Path(__file__).with_name(n).read_bytes()).hexdigest() for n in ("evidence.py", "source_store.py")}
    manifest = {"version": VERSION, "cutoff": cutoff, "available_on": max(acquired, p52_available).isoformat(),
        "source": {**metadata, "filters": source["filters"], "definition_sha256": source["definition_sha256"]},
        "form41_snapshot_id": fm["snapshot_id"], "classification": classification, "classification_digest": digest(classification),
        "normalized_digest": digest(normalized), "panel_digest": digest(panel), "coverage": panel["coverage"],
        "reconciliation_counts": panel["reconciliation_counts"], "quality_counts": normalized["quality_counts"],
        "P52_without_T2_counts": panel["P52_without_T2_counts"],
        "service_class_counts": normalized["service_class_counts"], "scanned_rows": normalized["scanned_rows"], "retained_rows": normalized["retained_rows"],
        "units": {k: "hours" if "HRS" in k or k == "HOURS_AIRBORNE_650" else "aircraft_days" if "AIR_DAYS" in k else "gallons" if k == "AIRCRAFT_FUELS_921" else "seat_miles" if k == "AVL_SEAT_MILES_320" else "passenger_miles" if k == "REV_PAX_MILES_140" else "departures" if k == "REV_ACRFT_DEP_PERF_510" else "aircraft_miles" if k == "REV_ACRFT_MILES_FLOWN_410" else "ton_miles" for k in METRICS},
        "source_multiplier": 1, "builder_code_sha256": hashlib.sha256(code).hexdigest(), "dependency_sha256s": dependencies,
        "duckdb_version": duckdb.__version__, "vintage_status": "October 2026 current-download reconstruction; no original historical release vintages or forecast scores"}
    manifest["snapshot_id"] = digest(manifest)
    target = Path(destination) / manifest["snapshot_id"]; target.mkdir(parents=True, exist_ok=True)
    for name, payload in (("normalized.json", json.dumps(normalized, allow_nan=False, separators=(",", ":")).encode()),
        ("panel.json", json.dumps(panel, allow_nan=False, separators=(",", ":")).encode()),
        ("manifest.json", json.dumps(manifest, allow_nan=False, indent=2).encode()), ("builder.py", code),
        *(("dependencies/" + n, Path(__file__).with_name(n).read_bytes()) for n in dependencies)):
        path = target / name; path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() != payload: raise ValueError("Immutable T2 history was modified")
        if not path.exists():
            with path.open("xb") as stream: stream.write(payload)
    con = duckdb.connect()
    try:
        for name, file, array, expected in (("rows", "normalized.json", "rows", normalized["rows"]),
            ("group_quarters", "panel.json", "group_quarters", panel["group_quarters"]),
            ("P52_reconciliation", "panel.json", "P52_reconciliation", panel["P52_reconciliation"]),
            ("P52_without_T2_Z", "panel.json", "P52_without_T2_Z", panel["P52_without_T2_Z"])):
            parquet = target / (name + ".parquet")
            if not parquet.exists():
                escaped = str(parquet).replace("'", "''")
                con.execute(f"COPY (SELECT unnest({array}) AS row FROM read_json_auto(?, sample_size=-1, maximum_object_size=268435456)) TO '{escaped}' (FORMAT PARQUET)", [str(target / file)])
            restored = _json_dates([r[0] for r in con.execute("SELECT row FROM read_parquet(?)", [str(parquet)]).fetchall()])
            if digest(restored) != digest(expected): raise ValueError("T2 Parquet differs from JSON: " + name)
    finally: con.close()
    pointer = Path(destination) / "latest.tmp"; pointer.write_text(json.dumps({"snapshot_id": manifest["snapshot_id"]}) + "\n"); pointer.replace(Path(destination) / "latest.json")
    return {**manifest, "path": str(target)}
