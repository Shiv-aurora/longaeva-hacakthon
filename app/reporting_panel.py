"""Audited reporting-entity join, not a geographically relabeled training set."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re

import duckdb

from .evidence import digest
from .operating_history import METRICS, OPERATING
from .form41_history import HISTORY as FORM41
from .source_store import ROOT

VERSION = "reporting-panel-0.1.0"
HISTORY = ROOT / "data" / "reporting_panel"
SCOPE = "Scheduled passenger T-100 DD/FD traffic joined by exact certificate carrier, reporting entity and quarter. Form 41 observations keep their original service and equipment scope. No financial-to-analytical-region relabeling, regional operator attribution, SEC consolidation, revenue allocation or missing-month imputation. FF traffic is absent."
REGION_MARKET = {"D": "domestic", "A": "atlantic", "P": "pacific", "L": "latin_america"}


def quarter(period: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        raise ValueError("Invalid monthly reporting period")
    return f"{period[:4]}-Q{(int(period[5:]) - 1) // 3 + 1}"


def months(period: str) -> list[str]:
    if not re.fullmatch(r"\d{4}-Q[1-4]", period):
        raise ValueError("Invalid quarterly reporting period")
    first = (int(period[-1]) - 1) * 3 + 1
    return [f"{period[:4]}-{m:02d}" for m in range(first, first + 3)]


def partition(period: str) -> str:
    return "fit_2013_2019" if period[:4] <= "2019" else "selection_2020_2023" if period[:4] <= "2023" else "locked_test_2024_plus"


def _json_dates(value):
    # DuckDB infers ISO publication strings as DATE inside nested structures.
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_dates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_dates(v) for v in value]
    return value


def reporting_panel(normalized: list[dict], selected: list[dict], p12: list[dict], p52: list[dict], p12a: list[dict]) -> dict:
    releases = {}
    for release in selected:
        key = (release["scope"], release["period"])
        quarter(key[1])
        if key in releases or key[0] not in {"DD", "FD"}:
            raise ValueError("Duplicate or unsupported selected traffic release")
        releases[key] = release["source_sha256"]
    traffic = defaultdict(list)
    seen = set()
    for archive_index, archive in enumerate(normalized):
        for row_index, row in enumerate(archive["rows"]):
            if releases.get((row["scope"], row["period"])) != row["source_sha256"]:
                continue
            if archive["source_sha256"] != row["source_sha256"]:
                raise ValueError("Traffic row disagrees with its source archive")
            grain = tuple(row[k] for k in ("period", "carrier", "carrier_entity", "carrier_wac", "market", "scope"))
            if grain in seen:
                raise ValueError("Duplicate selected traffic grain")
            seen.add(grain)
            traffic[(row["carrier"], row["carrier_entity"], quarter(row["period"]))].append(
                {**row, "normalized_archive_index": archive_index, "normalized_row_index": row_index})
    financial = defaultdict(list)
    aircraft = defaultdict(list)
    for schedule, rows, groups in (("P12", p12, financial), ("P52", p52, aircraft)):
        for row in rows:
            months(row["period"])
            if row["schedule"] != schedule:
                raise ValueError("Financial schedule disagrees with panel")
            groups[(row["unique_carrier"], row["reporting_entity"], row["period"])].append(row)
    joined = []
    for key in sorted(traffic.keys() | financial.keys() | aircraft.keys(), key=repr):
        carrier, entity, period = key
        t, f, a = traffic[key], financial[key], aircraft[key]
        flags = []
        identities = {r["airline_id"] for r in f + a}
        regions = {r["reporting_region"] for r in f + a}
        if carrier is None or entity is None or None in identities:
            flags.append("missing_reporting_identity")
        if len(identities) > 1 or len(regions) > 1 or len(f) > 1:
            flags.append("ambiguous_financial_reporting_boundary")
        if not t:
            flags.append("no_selected_passenger_traffic")
        if not f:
            flags.append("no_P12_statement")
        if not a:
            flags.append("no_P52_aircraft_records")
        observed_months = sorted({r["period"] for r in t})
        expected_months = months(period)
        if observed_months != expected_months:
            flags.append("traffic_entity_months_incomplete_no_zero_imputation")
        missing_releases = [f"{scope}:{month}" for month in expected_months for scope in ("DD", "FD") if (scope, month) not in releases]
        if missing_releases:
            flags.append("quarter_DD_FD_release_coverage_incomplete")
        totals = {metric: sum(r[metric] for r in t) if t else None for metric in METRICS}
        if t and totals["rpm"] > totals["asm"]:
            flags.append("traffic_rpm_exceeds_asm")
        geography = defaultdict(list)
        for row in t:
            geography[row["market"]].append(row)
        crosswalk = [{"analytical_market": market, **{metric: sum(r[metric] for r in rows) for metric in METRICS}}
                     for market, rows in sorted(geography.items())]
        region = next(iter(regions)) if len(regions) == 1 else None
        if region in REGION_MARKET and any(r["market"] != REGION_MARKET[region] for r in t):
            flags.append("financial_region_spans_other_analytical_markets")
        if region in {"I", "S"}:
            flags.append("aggregate_financial_region_keep_separate")
        if any(r["quality_flags"] for r in f + a):
            flags.append("source_financial_quality_flags")
        matched = bool(t and f and a and carrier and entity and len(identities) == 1 and None not in identities and len(regions) == 1 and len(f) == 1)
        joined.append({"unique_carrier": carrier, "reporting_entity": entity, "period": period,
            "partition": partition(period), "airline_ids": sorted(identities, key=repr),
            "reporting_regions": sorted(regions, key=repr), "matched_reporting_identity": matched,
            "observed_traffic_months": observed_months, "missing_scope_month_releases": missing_releases,
            "traffic": totals, "load_factor": totals["rpm"] / totals["asm"] if totals["asm"] else None,
            "analytical_market_crosswalk": crosswalk, "traffic_source_legs": t,
            "P12_rows": f, "P52_rows": a, "quality_flags": flags,
            "cross_schedule_fit_eligible": False,
            "fit_exclusion": "Entity matching alone does not reconcile scheduled-passenger traffic with Form 41 all-service costs, transport-related revenue, aircraft scope or omitted foreign-to-foreign operations."})
    # P12A lacks reporting-entity IDs. Keep certificate fuel beside the join;
    # never copy a system fuel total into every entity/region.
    fuel_groups = defaultdict(list)
    for row in p12a:
        if row["schedule"] != "P12A":
            raise ValueError("Fuel schedule disagrees with panel")
        fuel_groups[(row["unique_carrier"], row["airline_id"], quarter(row["period"]))].append(row)
    fuel = []
    for key, rows in sorted(fuel_groups.items(), key=lambda item: repr(item[0])):
        carrier, airline, period = key
        observed = sorted(r["period"] for r in rows)
        if len(observed) != len(set(observed)):
            raise ValueError("Duplicate certificate fuel month")
        fields = sorted(rows[0]["values"])
        if any(sorted(r["values"]) != fields for r in rows):
            raise ValueError("Inconsistent certificate fuel fields")
        complete = observed == months(period)
        values = {field: sum(r["values"][field] for r in rows) if complete and all(r["values"][field] is not None for r in rows) else None for field in fields}
        flags = sorted({flag for r in rows for flag in r["quality_flags"]})
        if not complete:
            flags.append("fuel_months_incomplete_no_zero_imputation")
        if carrier is None or airline is None:
            flags.append("missing_reporting_identity")
        gallons, cost = values.get("TOTAL_GALLONS"), values.get("TOTAL_COST")
        fuel.append({"unique_carrier": carrier, "airline_id": airline, "period": period,
            "partition": partition(period), "observed_months": observed,
            "complete_quarter": complete, "values": values,
            "nonnull_month_counts": {field: sum(r["values"][field] is not None for r in rows) for field in fields},
            "reported_total_fuel_cost_per_gallon": cost / gallons if not flags and gallons is not None and gallons > 0 and cost is not None and cost >= 0 else None,
            "quality_flags": flags, "monthly_source_rows": rows})
    coverage = []
    for carrier in ("UA", "DL", "AA", "US"):
        rows = [r for r in joined if r["unique_carrier"] == carrier]
        matched = [r for r in rows if r["matched_reporting_identity"]]
        coverage.append({"unique_carrier": carrier, "entity_quarter_rows": len(rows),
            "matched_entity_quarters": len(matched), "matched_quarters": len({r["period"] for r in matched}),
            "complete_traffic_entity_quarters": sum(r["observed_traffic_months"] == months(r["period"]) and not r["missing_scope_month_releases"] for r in matched),
            "geography_crosswalk_rows": sum("financial_region_spans_other_analytical_markets" in r["quality_flags"] for r in rows),
            "fuel_complete_quarters": sum(r["complete_quarter"] for r in fuel if r["unique_carrier"] == carrier),
            "cross_schedule_fit_eligible": 0})
    return {"entity_quarters": joined, "certificate_fuel_quarters": fuel, "coverage": coverage,
        "quality_counts": dict(sorted(Counter(flag for r in joined for flag in r["quality_flags"]).items())), "scope": SCOPE}


def _read_snapshot(path: Path, kind: str) -> tuple[dict, dict]:
    manifest = json.loads((path / "manifest.json").read_text())
    identity = manifest.get("snapshot_id")
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity) or digest({k: v for k, v in manifest.items() if k != "snapshot_id"}) != identity:
        raise ValueError("Upstream manifest checksum failed")
    files = {"normalized.json": manifest["normalized_digest"], "panel.json": manifest["panel_digest"]} if kind == "traffic" else {f"{s}.json": d for s, d in manifest["panel_digests"].items()}
    data = {}
    for name, expected in files.items():
        data[name] = json.loads((path / name).read_text())
        if digest(data[name]) != expected:
            raise ValueError("Upstream historical panel checksum failed")
    return manifest, data


def build_reporting_panel(operating: Path, form41: Path, destination: Path = HISTORY) -> dict:
    tm, td = _read_snapshot(operating, "traffic")
    fm, fd = _read_snapshot(form41, "financial")
    panel = reporting_panel(td["normalized.json"], td["panel.json"]["selected_releases"], *(fd[f"{s}.json"]["rows"] for s in ("P12", "P52", "P12A")))
    code = Path(__file__).read_bytes()
    manifest = {"version": VERSION, "upstream_snapshots": {"operating": tm["snapshot_id"], "form41": fm["snapshot_id"]},
        "upstream_cutoffs": {"operating": tm["cutoff"], "form41": fm["cutoff"]},
        "available_on": max([s["available_on"] for s in fm["sources"]] +
            [datetime.fromisoformat(s["retrieved_at_utc"]).date().isoformat() for s in tm["sources"] + [tm["wac_source"]]]),
        "vintage_status": "Current-download reconstruction; entity alignment does not create historical publication vintages. Temporal partitions remain unevaluated.",
        "scope": SCOPE, "coverage": panel["coverage"], "quality_counts": panel["quality_counts"],
        "panel_digest": digest(panel), "builder_code_sha256": hashlib.sha256(code).hexdigest()}
    manifest["snapshot_id"] = digest(manifest)
    target = destination / manifest["snapshot_id"]
    target.mkdir(parents=True, exist_ok=True)
    for name, payload in (("panel.json", json.dumps(panel, allow_nan=False, separators=(",", ":")).encode()),
                          ("manifest.json", json.dumps(manifest, allow_nan=False, indent=2).encode()), ("builder.py", code)):
        path = target / name
        if path.exists() and path.read_bytes() != payload:
            raise ValueError("Immutable reporting panel was modified")
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(payload)
    connection = duckdb.connect()
    try:
        for table in ("entity_quarters", "certificate_fuel_quarters"):
            parquet = target / f"{table}.parquet"
            if not parquet.exists():
                escaped = str(parquet).replace("'", "''")
                connection.execute(f"COPY (SELECT unnest({table}) AS row FROM read_json_auto(?, sample_size=-1, maximum_object_size=268435456)) TO '{escaped}' (FORMAT PARQUET)", [str(target / "panel.json")])
            restored = _json_dates([r[0] for r in connection.execute("SELECT row FROM read_parquet(?)", [str(parquet)]).fetchall()])
            if digest(restored) != digest(panel[table]):
                raise ValueError("Reporting-panel Parquet differs from JSON")
    finally:
        connection.close()
    pointer = destination / "latest.tmp"
    pointer.write_text(json.dumps({"snapshot_id": manifest["snapshot_id"]}) + "\n")
    pointer.replace(destination / "latest.json")
    return {**manifest, "path": str(target)}


def latest_snapshot(destination: Path) -> Path:
    identity = json.loads((destination / "latest.json").read_text())["snapshot_id"]
    if not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise ValueError("Invalid latest snapshot ID")
    return destination / identity
