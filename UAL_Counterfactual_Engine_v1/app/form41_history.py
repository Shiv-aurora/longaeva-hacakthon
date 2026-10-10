"""Current-download Form 41 reconstruction; certificate and schedule grains survive.

These full-table exports have no row-level filing/release vintages. Acquisition
dates bound snapshot availability; they are never backdated into an as-of panel.
"""
from __future__ import annotations

from collections import Counter
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse
from zipfile import BadZipFile, ZipFile

import duckdb

from .evidence import digest
from .source_store import ROOT, RAW, byte_hash, preserve_source, read_source

VERSION = "form41-history-0.1.0"
HISTORY = ROOT / "data" / "form41_history"
TABLE_CODES = {"P12": "FMI", "P52": "FMK", "P12A": "FMH"}
COMMON = "AIRLINE_ID UNIQUE_CARRIER CARRIER CARRIER_NAME CARRIER_GROUP_NEW YEAR QUARTER".split()
QUARTER_DIMS = COMMON + "UNIQUE_CARRIER_NAME UNIQUE_CARRIER_ENTITY REGION CARRIER_GROUP".split()
P12_FIELDS = "NET_INCOME OP_PROFIT_LOSS TRANS_REV_PAX MAIL TOTAL_PROPERTY PROP_FREIGHT PROP_BAG TOTAL_CHARTER CHARTER_PAX CHARTER_PROP TOTAL_MISC_REV RES_CANCEL_FEES MISC_OP_REV PUB_SVC_REVENUE TRANS_REVENUE OP_REVENUES FLYING_OPS MAINTENANCE PAX_SERVICE AIRCFT_SERVICES PROMOTION_SALES GENERAL_ADMIN GENERAL_SERVICES DEPREC_AMORT TRANS_EXPENSES OP_EXPENSES INTEREST_LONG_DEBT INTEREST_EXP_OTH FOREIGN_EX_GAINS CAP_GAINS_PROP CAP_GAINS_OTHER OTHER_INCOME_NET NON_OP_INCOME INCOME_PRE_TAX INCOME_TAX INCOME_BEFORE_OTH DISCONT_OPS EXTRA_ITEMS INCOME_TAX_EXTRA INCOME_TAX_CREDITS ACCTG_CHANGES".split()
P52_FIELDS = "PILOT_FLY_OPS OTH_FLT_FLY_OPS TRAIN_FLY_OPS PERS_EXP_FLY_OPS PRO_FLY_OPS INTERCHG_FLY_OPS FUEL_FLY_OPS OIL_FLY_OPS RENTAL_FLY_OPS OTHER_FLY_OPS INS_FLY_OPS BENEFITS_FLY_OPS INCIDENT_FLY_OPS PAY_TAX_FLY_OPS OTH_TAX_FLY_OPS OTHER_EXP_FLY_OPS TOT_FLY_OPS AIRFRAME_LABOR ENGINE_LABOR AIRFRAME_REPAIR ENGINE_REPAIRS INTERCHG_CHARG AIRFRAME_MATERIALS ENGINE_MATERIALS AIRFRAME_ALLOW AIRFRAME_OVERHAULS ENGINE_ALLOW ENGINE_OVERHAULS TOT_DIR_MAINT AP_MT_BURDEN TOT_FLT_MAINT_MEMO NET_OBSOL_PARTS AIRFRAME_DEP ENGINE_DEP PARTS_DEP ENG_PARTS_DEP OTH_FLT_EQUIP_DEP OTH_FLT_EQUIP_DEP_GRP_I FLT_EQUIP_A_EXP FLY_OPS_EXP_I_A TOT_AIR_OP_EXPENSES DEV_N_PREOP_EXP OTH_INTANGIBLES EQUIP_N_HANGAR_DEP G_PROP_DEP CAP_LEASES_DEP TOTAL_AIR_HOURS AIR_DAYS_ASSIGN AIR_FUELS_ISSUED".split()
FUEL_PREFIXES = "SALA SDOM SDOMT SATL SPAC SLAT SINT TS NALA NDOM NDOMT NATL NPAC NLAT MAC NINT TN TDOMT TINT TOTAL".split()
SCHEMAS = {
    "P12": (QUARTER_DIMS, P12_FIELDS),
    "P52": (QUARTER_DIMS + "AIRCRAFT_CONFIG AIRCRAFT_GROUP AIRCRAFT_TYPE".split(), P52_FIELDS),
    "P12A": (COMMON + ["MONTH"], [f"{p}_{kind}" for kind in ("GALLONS", "COST") for p in FUEL_PREFIXES]),
}
REGIONS = {"D": "BTS domestic", "A": "BTS Atlantic", "P": "BTS Pacific", "L": "BTS Latin America", "I": "BTS international aggregate", "S": "BTS system aggregate"}
BOUNDARY = "DOT certificate reporting carriers and their BTS financial regions. System/international totals, detailed regions, aircraft types and non-flight equipment remain separate. These are not SEC holding-company totals or proposal US-plus-Canada markets; feeder operators are not attributed to a major airline. US Airways remains separate."
VINTAGE = "Current full-table download reconstruction. No row-level filing/release dates or independently preserved historical revisions. Snapshot available only from acquisition date; do not treat earlier observations as point-in-time inputs."


def official_url(schedule: str, url: str) -> bool:
    parsed = urlparse(url)
    return (schedule in TABLE_CODES and parsed.scheme == "https" and parsed.hostname == "www.transtats.bts.gov"
            and parsed.path.lower() == "/dl_selectfields.aspx"
            and parse_qs(parsed.query).get("gnoyr_VQ") == [TABLE_CODES[schedule]])


def preserve_export(entry: dict, raw: Path = RAW) -> dict:
    schedule, url = entry["schedule"], entry["source_url"]
    if not official_url(schedule, url):
        raise ValueError("Form 41 source must identify the inspected official schedule")
    if entry.get("filters") != {"year": "All Years", "period": "All", "fields": "All"}:
        raise ValueError("Form 41 requires an explicitly complete table export")
    source = preserve_source(Path(entry["path"]).read_bytes(), url, "application/zip", raw)
    definition = preserve_source(Path(entry["definition_path"]).read_bytes(), url, "text/plain", raw)
    return {**source, "schedule": schedule, "filters": entry["filters"], "definition_sha256": definition["sha256"]}


def _integer(value: str, field: str, low: int, high: int, nullable: bool = False) -> int | None:
    if nullable and not value.strip():
        return None
    if not re.fullmatch(r"\d+", value.strip()) or not low <= int(value) <= high:
        raise ValueError(f"Invalid Form 41 {field}: {value!r}")
    return int(value)


def _amount(value: str, field: str) -> Decimal | None:
    if not value.strip():
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid Form 41 number in {field}") from exc
    if not amount.is_finite() or abs(amount) > Decimal("1e15"):
        raise ValueError(f"Nonfinite or excessive Form 41 number in {field}")
    return amount


def units(schedule: str) -> dict:
    result = {}
    for field in SCHEMAS[schedule][1]:
        if schedule == "P12A":
            source_unit, unit, scale = ("gallons", "gallons_million", "0.000001") if field.endswith("GALLONS") else ("USD", "USD_million", "0.000001")
        elif field in ("TOTAL_AIR_HOURS", "AIR_DAYS_ASSIGN", "AIR_FUELS_ISSUED"):
            source_unit, unit, scale = {"TOTAL_AIR_HOURS": ("hours_thousand", "hours", "1000"), "AIR_DAYS_ASSIGN": ("days_thousand", "days", "1000"), "AIR_FUELS_ISSUED": ("gallons_thousand", "gallons_million", "0.001")}[field]
        else:
            source_unit, unit, scale = "USD_thousand", "USD_million", "0.001"
        result[field] = {"source_unit": source_unit, "normalized_unit": unit, "multiplier": scale}
    return result


def _quality(schedule: str, values: dict[str, Decimal | None]) -> list[str]:
    flags = []
    if schedule == "P52":
        for field in ("AIR_FUELS_ISSUED", "TOTAL_AIR_HOURS", "AIR_DAYS_ASSIGN"):
            if values[field] is not None and values[field] < 0:
                flags.append(f"negative_reported_aircraft_quantity_{field}")
    if schedule == "P12":
        if all(values[k] is not None for k in ("OP_REVENUES", "OP_EXPENSES", "OP_PROFIT_LOSS")):
            if abs(values["OP_REVENUES"] - values["OP_EXPENSES"] - values["OP_PROFIT_LOSS"]) > Decimal("0.03"):
                flags.append("operating_identity_difference_over_30_usd")
    if schedule == "P12A":
        # Totals are alternative views, never extra consumption to be added.
        identities = {"SDOMT": ["SALA", "SDOM"], "SINT": ["SATL", "SPAC", "SLAT"], "TS": ["SDOMT", "SINT"],
            "NDOMT": ["NALA", "NDOM"], "NINT": ["NATL", "NPAC", "NLAT", "MAC"], "TN": ["NDOMT", "NINT"],
            "TDOMT": ["SDOMT", "NDOMT"], "TINT": ["SINT", "NINT"], "TOTAL": ["TS", "TN"]}
        for kind in ("GALLONS", "COST"):
            for total, parts in identities.items():
                keys = [f"{p}_{kind}" for p in parts]
                if values[f"{total}_{kind}"] is not None and all(values[k] is not None for k in keys):
                    if abs(values[f"{total}_{kind}"] - sum(values[k] for k in keys)) > Decimal("0.03"):
                        flags.append(f"fuel_identity_difference_{total}_{kind}")
        if any(v is not None and v < 0 for k, v in values.items() if k.endswith("GALLONS")):
            flags.append("negative_reported_fuel_quantity")
    return flags


def normalize_export(content: bytes, source: dict, start_year: int = 2013) -> dict:
    schedule = source["schedule"]
    if schedule not in SCHEMAS or not official_url(schedule, source["source_url"]):
        raise ValueError("Unsupported Form 41 schedule or source URL")
    if byte_hash(content) != source["sha256"]:
        raise ValueError("Form 41 export content differs from its source hash")
    try:
        archive = ZipFile(io.BytesIO(content))
    except BadZipFile as exc:
        raise ValueError("Form 41 source is not a valid ZIP") from exc
    with archive:
        members = [i for i in archive.infolist() if not i.is_dir() and i.filename.lower().endswith(".csv")]
        if len(members) != 1 or members[0].file_size > 150_000_000:
            raise ValueError("Expected one bounded Form 41 CSV member")
        try:
            text = archive.read(members[0]).decode("utf-8-sig")
        except UnicodeError as exc:
            raise ValueError("Form 41 export is not UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text))
    dimensions, metrics = SCHEMAS[schedule]
    expected = set(dimensions + metrics)
    if not reader.fieldnames or len(reader.fieldnames) != len(expected) or set(reader.fieldnames) != expected:
        raise ValueError("Form 41 header differs from the inspected full-field schema")
    rows, keys, scanned = [], set(), 0
    scale = units(schedule)
    for record in reader:
        scanned += 1
        line = reader.line_num
        if None in record or any(v is None for v in record.values()):
            raise ValueError(f"Malformed Form 41 row at line {line}")
        year = _integer(record["YEAR"], "year", 1900, 2100)
        quarter = _integer(record["QUARTER"], "quarter", 1, 4)
        if year < start_year:
            continue
        month = _integer(record["MONTH"], "month", 1, 12) if schedule == "P12A" else None
        if month is not None and quarter != (month - 1) // 3 + 1:
            raise ValueError("Form 41 month/quarter mismatch")
        period = f"{year}-{month:02d}" if month else f"{year}-Q{quarter}"
        carrier = record["UNIQUE_CARRIER"].strip() or None
        airline_id = _integer(record["AIRLINE_ID"], "airline ID", 1, 999999, nullable=True)
        region, entity = record.get("REGION"), record.get("UNIQUE_CARRIER_ENTITY")
        flags = []
        if carrier is None or airline_id is None:
            flags.append("missing_unique_carrier_identity")
        if schedule != "P12A" and (not region or not entity):
            raise ValueError("Missing Form 41 financial reporting entity")
        if region and region not in REGIONS:
            flags.append("unknown_reporting_region")
        aircraft = {}
        if schedule == "P52":
            for field, upper in (("AIRCRAFT_TYPE", 999), ("AIRCRAFT_CONFIG", 9), ("AIRCRAFT_GROUP", 9)):
                _integer(record[field], field, 0, upper)
                aircraft[field.lower()] = record[field]
        key = (airline_id, carrier, entity, region, period, *aircraft.values())
        if key in keys:
            raise ValueError(f"Duplicate Form 41 reporting grain at line {line}")
        keys.add(key)
        raw_values = {k: _amount(record[k], k) for k in metrics}
        flags += _quality(schedule, raw_values)
        row = {"schedule": schedule, "period": period, "year": year, "quarter": quarter,
            "airline_id": airline_id, "unique_carrier": carrier, "carrier": record["CARRIER"], "carrier_name": record["CARRIER_NAME"],
            "carrier_group_new": record["CARRIER_GROUP_NEW"], "reporting_entity": entity, "reporting_region": region,
            **aircraft, "values": {k: None if v is None else float(v * Decimal(scale[k]["multiplier"])) for k, v in raw_values.items()},
            "quality_flags": flags, "source_sha256": source["sha256"], "source_member": members[0].filename, "source_line": line,
            "partition": "fit_2013_2019" if year <= 2019 else "selection_2020_2023" if year <= 2023 else "locked_test_2024_plus"}
        rows.append(row)
    rows.sort(key=lambda r: (r["period"], str(r["airline_id"]), str(r["reporting_entity"]), r.get("aircraft_type", ""), r.get("aircraft_config", ""), r.get("aircraft_group", "")))
    return {"rows": rows, "scanned_rows": scanned, "retained_rows": len(rows),
            "quality_counts": dict(sorted(Counter(f for r in rows for f in r["quality_flags"]).items())), "units": scale}


def _coverage(panels: dict) -> list[dict]:
    result = []
    for schedule, panel in sorted(panels.items()):
        for carrier in ("UA", "DL", "AA", "US"):
            rows = [r for r in panel["rows"] if r["unique_carrier"] == carrier]
            periods = sorted({r["period"] for r in rows})
            result.append({"schedule": schedule, "unique_carrier": carrier, "periods": len(periods),
                "frequency": "monthly" if schedule == "P12A" else "quarterly", "first": periods[0] if periods else None,
                "last": periods[-1] if periods else None, "rows": len(rows),
                "reporting_regions": sorted({r["reporting_region"] for r in rows if r["reporting_region"]}),
                "quality_flag_rows": sum(bool(r["quality_flags"]) for r in rows)})
    return result


def build_form41_history(sources: list[dict], cutoff: str, destination: Path = HISTORY, raw: Path = RAW, start_year: int = 2013) -> dict:
    cutoff_date = date.fromisoformat(cutoff)
    if not 1900 <= start_year <= cutoff_date.year:
        raise ValueError("Invalid Form 41 start year")
    if len(sources) != 3 or {s["schedule"] for s in sources} != set(SCHEMAS):
        raise ValueError("Provide one complete source for each of P12, P52 and P12A")
    panels, verified = {}, []
    for source in sorted(sources, key=lambda s: s["schedule"]):
        content, metadata = read_source(source["sha256"], raw)
        _, definition = read_source(source["definition_sha256"], raw)
        if metadata["source_url"] != source["source_url"] or definition["source_url"] != source["source_url"] or not official_url(source["schedule"], source["source_url"]):
            raise ValueError("Form 41 source metadata differs from preserved provenance")
        if source.get("filters") != {"year": "All Years", "period": "All", "fields": "All"}:
            raise ValueError("Form 41 source is not a complete export")
        acquired = datetime.fromisoformat(metadata["retrieved_at_utc"]).date()
        if acquired > cutoff_date:
            raise ValueError("Form 41 current-download snapshot was not acquired by this cutoff; historical release vintages are unavailable")
        panel = normalize_export(content, source, start_year)
        if not panel["rows"]:
            raise ValueError("No retained Form 41 observations")
        if any(r["year"] > acquired.year or (r["year"] == acquired.year and (int(r["period"][-2:]) if source["schedule"] == "P12A" else r["quarter"] * 3) > acquired.month) for r in panel["rows"]):
            raise ValueError("Form 41 export contains periods after its acquisition")
        panels[source["schedule"]] = panel
        verified.append({**metadata, "schedule": source["schedule"], "filters": source["filters"],
            "definition_sha256": source["definition_sha256"], "available_on": acquired.isoformat()})
    manifest = {"version": VERSION, "cutoff": cutoff, "start_year": start_year, "sources": verified, "coverage": _coverage(panels),
        "region_definitions": REGIONS, "vintage_status": VINTAGE, "boundary": BOUNDARY,
        "checks": {s: {k: v for k, v in p.items() if k != "rows"} for s, p in panels.items()},
        "panel_digests": {s: digest(p) for s, p in panels.items()}, "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    manifest["snapshot_id"] = digest(manifest)
    target = destination / manifest["snapshot_id"]
    target.mkdir(parents=True, exist_ok=True)
    for name, value in [(f"{s}.json", p) for s, p in panels.items()] + [("manifest.json", manifest)]:
        path = target / name
        if path.exists() and digest(json.loads(path.read_text())) != digest(value):
            raise ValueError("Immutable Form 41 snapshot was modified")
        if not path.exists():
            with path.open("x") as stream:
                json.dump(value, stream, allow_nan=False, separators=(",", ":"))
                stream.write("\n")
    connection = duckdb.connect()
    try:
        for schedule, panel in panels.items():
            parquet = target / f"{schedule}.parquet"
            if not parquet.exists():
                escaped = str(parquet).replace("'", "''")
                connection.execute(f"COPY (SELECT unnest(rows) AS row FROM read_json_auto(?, sample_size=-1, maximum_object_size=268435456)) TO '{escaped}' (FORMAT PARQUET)", [str(target / f"{schedule}.json")])
            restored = [r[0] for r in connection.execute("SELECT row FROM read_parquet(?)", [str(parquet)]).fetchall()]
            if digest(restored) != digest(panel["rows"]):
                raise ValueError(f"Form 41 {schedule} Parquet differs from its JSON panel")
    finally:
        connection.close()
    pointer = destination / "latest.tmp"
    pointer.write_text(json.dumps({"snapshot_id": manifest["snapshot_id"]}) + "\n")
    pointer.replace(destination / "latest.json")
    return {**manifest, "path": str(target)}


def form41_history_status(destination: Path = HISTORY) -> dict:
    latest = destination / "latest.json"
    if not latest.exists():
        return {"status": "not_built", "message": "Build Form 41 history with scripts/build_form41_history.py"}
    snapshot = json.loads(latest.read_text())["snapshot_id"]
    if not re.fullmatch(r"[a-f0-9]{64}", snapshot):
        raise ValueError("Invalid Form 41 snapshot identifier")
    target = destination / snapshot
    manifest = json.loads((target / "manifest.json").read_text())
    if manifest.get("snapshot_id") != snapshot or digest({k: v for k, v in manifest.items() if k != "snapshot_id"}) != snapshot:
        raise ValueError("Form 41 manifest checksum failed")
    for schedule, expected in manifest["panel_digests"].items():
        if digest(json.loads((target / f"{schedule}.json").read_text())) != expected:
            raise ValueError("Form 41 historical data checksum failed")
    return {"status": "available", **manifest}
