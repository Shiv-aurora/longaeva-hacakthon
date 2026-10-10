"""Observed BTS segment aggregates with archive-release selection and provenance.

Operating carriers are not marketing carriers or consolidated financial entities.
Regional operators remain unassigned; no entire feeder is attributed to United.
"""
from __future__ import annotations

from collections import defaultdict
import csv
from datetime import date
import hashlib
from io import BytesIO, StringIO, TextIOWrapper
import json
from pathlib import Path
import re
from urllib.parse import urlparse
from zipfile import ZipFile

import duckdb

from .evidence import digest
from .source_store import RAW, ROOT, preserve_source, read_source

VERSION = "bts-operating-history-0.1.0"
OPERATING = ROOT / "data" / "operating_history"
SEGMENT_REFERENCE = "https://www.bts.gov/sites/bts.dot.gov/files/docs/explore-topics-and-geography/topics/airlines-and-airports/230176/reference-file-db28-segment-data-product.pdf"
WAC_URL = "https://www.transtats.bts.gov/Download_Lookup.asp?Y11x72=Y_jbeYQ_NeRN_PbQRf"
CATALOGS = {
    "DD": "https://www.bts.gov/browse-statistical-products-and-data/bts-publications/data-bank-28ds-t-100-domestic-segment-data",
    "FD": "https://www.bts.gov/browse-statistical-products-and-data/bts-publications/%E2%80%A2-data-bank-28is-t-100-and-t-100f",
}
MONTHS = {name: i for i, name in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
CANADA = {906, 911, 916, 921, 926, 931, 934, 936, 941, 946, 951, 956, 961}
MARKETS = ("domestic", "atlantic", "pacific", "latin_america")
METRICS = ("asm", "rpm", "seats", "passengers", "departures_performed", "departures_scheduled", "ramp_minutes", "airborne_minutes", "segment_records")


def archive_metadata(url: str) -> dict:
    parsed = urlparse(url)
    name = Path(parsed.path).name.upper()
    if parsed.scheme != "https" or parsed.hostname not in {"www.bts.gov", "bts.gov"}:
        raise ValueError("BTS segment archives must use the official HTTPS source")
    match = re.fullmatch(r"(?:DB28SEG\.(DD|FD)|(?:(DD)\.DB28DS|(FD)\.DB28IS))\.WAC\.(\d{6})\.(\d{6})\.REL(\d{2})\.(\d{2})([A-Z]{3})(\d{4})\.ZIP", name)
    if not match:
        raise ValueError("Unsupported BTS segment archive name; use DD/FD WAC releases")
    scope = next(x for x in match.group(1, 2, 3) if x)
    first, last = match.group(4, 5)
    date.fromisoformat(first[:4] + "-" + first[4:] + "-01")
    date.fromisoformat(last[:4] + "-" + last[4:] + "-01")
    published = date(int(match.group(9)), MONTHS[match.group(8)], int(match.group(7))).isoformat()
    if first > last or published[:7].replace("-", "") < last:
        raise ValueError("BTS archive has invalid period/release dates")
    return {"scope": scope, "period_first": first[:4] + "-" + first[4:],
            "period_last": last[:4] + "-" + last[4:], "release_number": int(match.group(6)),
            "published_on": published, "catalog_url": CATALOGS[scope]}


def preserve_archive(path: Path, url: str, raw: Path = RAW) -> dict:
    return {**archive_metadata(url), **preserve_source(path.read_bytes(), url, "application/zip", raw)}


def wac_lookup(content: bytes) -> dict[int, str]:
    reader = csv.DictReader(StringIO(content.decode("utf-8-sig")))
    if reader.fieldnames != ["Code", "Description"]:
        raise ValueError("BTS WAC lookup schema differs")
    result = {}
    for row in reader:
        code = int(row["Code"])
        if code in result or not row["Description"]:
            raise ValueError("Duplicate/missing BTS WAC lookup entry")
        result[code] = row["Description"]
    if not result or not CANADA <= result.keys():
        raise ValueError("BTS WAC lookup lacks Canadian provinces")
    return result


def endpoint_market(wac: int, lookup: dict[int, str]) -> str | None:
    if wac not in lookup:
        return None
    if wac in {3, 4} or 100 <= wac < 400:
        return "latin_america"
    if wac == 5 or 700 <= wac < 900:
        return "atlantic" if wac == 733 else "pacific"  # India uses UAL's Atlantic reporting entity.
    if 400 <= wac < 700 or wac in {975, 977}:
        return "atlantic"
    if wac in CANADA or 0 < wac < 100:
        return "domestic"
    return None


def segment_market(origin: int, destination: int, lookup: dict[int, str]) -> str:
    a, b = endpoint_market(origin, lookup), endpoint_market(destination, lookup)
    if a is None or b is None:
        return "unmapped"
    if a == b:
        return a
    if a == "domestic":
        return b
    if b == "domestic":
        return a
    return "cross_region"  # Never silently choose one region for an ambiguous pair.


def segment_fields(text: str, line: int) -> list[str]:
    fields = text.rstrip("\r\n").split("|")
    if len(fields) in {29, 33} and fields[-1] == "":
        fields.pop()
    if len(fields) not in {28, 32}:
        raise ValueError(f"BTS segment line {line}: expected 28/32 fields")
    return fields


def parse_record(text: str, line: int) -> dict:
    fields = segment_fields(text, line)
    legacy = len(fields) == 32
    number_indices = (0, 1, 4, 8, 12, 13, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27)
    if legacy:
        number_indices += (28, 29, 30, 31)
    decimal_indices = {20, 27, 28} if legacy else {20, 23, 24}
    carrier_wac_index = 31 if legacy else 27
    number_indices = tuple(i for i in number_indices if i not in decimal_indices and i != carrier_wac_index)
    if any(not re.fullmatch(r"\d+(?:\.\d+)?", fields[i]) for i in decimal_indices) or any(not re.fullmatch(r"\d+", fields[i]) for i in number_indices):
        raise ValueError(f"BTS segment line {line}: invalid or missing numeric field")
    if any(fields[i] != "NULL" and not re.fullmatch(r"\d+", fields[i]) for i in (3, 7)):
        raise ValueError(f"BTS segment line {line}: invalid optional city-market identifier")
    if fields[carrier_wac_index] not in {"", "NULL"} and not re.fullmatch(r"\d+", fields[carrier_wac_index]):
        raise ValueError(f"BTS segment line {line}: invalid optional carrier country code")
    n = {i: int(fields[i]) for i in number_indices}
    period = date(n[0], n[1], 1).strftime("%Y-%m")
    if not all(re.fullmatch(r"[A-Z0-9]{2,3}", fields[i]) for i in (2, 6, 10)) or not re.fullmatch(r"[A-Z0-9]{5}", fields[11]):
        raise ValueError(f"BTS segment line {line}: invalid airport/carrier/entity code")
    # The legacy FD layout contains middle/coach cabin fields. Sum reported
    # cabin values; new layouts already contain total seats and passengers.
    seats = sum(n[i] for i in (21, 22, 23)) if legacy else n[21]
    passengers = sum(n[i] for i in (24, 25, 26)) if legacy else n[22]
    ramp, airborne = (n[29], n[30]) if legacy else (n[25], n[26])
    carrier_wac = int(fields[carrier_wac_index]) if fields[carrier_wac_index] not in {"", "NULL"} else None
    return {"period": period, "carrier": fields[10], "carrier_entity": fields[11],
            "carrier_wac": carrier_wac, "origin": fields[2], "destination": fields[6],
            "origin_wac": n[4], "destination_wac": n[8], "service_class": fields[14],
            "aircraft_group": n[15], "aircraft_type": n[16], "aircraft_configuration": n[17],
            "asm": seats * n[13], "rpm": passengers * n[13], "seats": seats, "passengers": passengers,
            "departures_performed": n[18], "departures_scheduled": n[19],
            "ramp_minutes": ramp, "airborne_minutes": airborne, "segment_records": 1,
            "missing_city_market_metadata": any(fields[i] == "NULL" for i in (3, 7)),
            "missing_carrier_country_metadata": carrier_wac is None,
            "layout": "legacy_32" if legacy else "segment_28"}


def normalize_archive(content: bytes, source: dict, lookup: dict[int, str]) -> dict:
    metadata = archive_metadata(source["source_url"])
    aggregates = {}
    seen, layouts = set(), set()
    issues = defaultdict(int)
    counts = {"raw_records": 0, "scheduled_passenger_records": 0}
    with ZipFile(BytesIO(content)) as archive:
        members = [m for m in archive.infolist() if m.filename.lower().endswith(".asc")]
        if len(members) != 1 or members[0].file_size > 100_000_000 or members[0].flag_bits & 1:
            raise ValueError("BTS archive must contain one unencrypted ASC member below 100 MB")
        member = members[0]
        with archive.open(member) as binary, TextIOWrapper(binary, encoding="utf-8-sig") as stream:
            for line, text in enumerate(stream, 1):
                if not text.strip():
                    continue
                fields = segment_fields(text, line)
                period = date(int(fields[0]), int(fields[1]), 1).strftime("%Y-%m")
                if not metadata["period_first"] <= period <= metadata["period_last"]:
                    raise ValueError("BTS record falls outside its archive period")
                counts["raw_records"] += 1
                layouts.add("legacy_32" if len(fields) == 32 else "segment_28")
                if fields[14] != "F" or fields[17] not in {"1", "3"}:
                    continue
                record = parse_record(text, line)
                counts["scheduled_passenger_records"] += 1
                natural_key = tuple(record[k] for k in ("period", "origin", "origin_wac", "destination", "destination_wac", "carrier", "carrier_entity", "aircraft_group", "aircraft_type", "aircraft_configuration"))
                if natural_key in seen:
                    raise ValueError(f"Duplicate scheduled segment key in BTS archive at line {line}")
                seen.add(natural_key)
                market = segment_market(record["origin_wac"], record["destination_wac"], lookup)
                if market not in MARKETS:
                    issues[market + "_segment_records"] += 1
                if record["rpm"] > record["asm"]:
                    issues["passengers_exceed_seats_records"] += 1
                if record["missing_city_market_metadata"]:
                    issues["missing_city_market_metadata_records"] += 1
                if record["missing_carrier_country_metadata"]:
                    issues["missing_carrier_country_metadata_records"] += 1
                key = (record["period"], record["carrier"], record["carrier_entity"], record["carrier_wac"], market)
                if key not in aggregates:
                    aggregates[key] = {"period": key[0], "carrier": key[1], "carrier_entity": key[2],
                        "carrier_wac": key[3], "market": key[4], "source_sha256": source["sha256"],
                        "source_member": member.filename, "scope": metadata["scope"], "published_on": metadata["published_on"],
                        **{metric: 0 for metric in METRICS}}
                for metric in METRICS:
                    aggregates[key][metric] += record[metric]
    return {"source_sha256": source["sha256"], "source_member": member.filename,
            "layouts": sorted(layouts), "counts": counts, "issues": dict(sorted(issues.items())),
            "rows": [aggregates[k] for k in sorted(aggregates, key=repr)]}


def monthly_panel(normalized: list[dict], sources: list[dict], cutoff: str, start_year: int = 2013) -> dict:
    date.fromisoformat(cutoff)
    if not 2013 <= start_year <= int(cutoff[:4]):
        raise ValueError("Operating history begins in 2013")
    releases = {}
    for source in sources:
        meta = archive_metadata(source["source_url"])
        if meta["published_on"] > cutoff:
            continue
        y, m = map(int, meta["period_first"].split("-"))
        period = meta["period_first"]
        while period <= meta["period_last"]:
            key = (meta["scope"], period)
            rank = (meta["published_on"], meta["release_number"])
            existing = releases.get(key)
            if existing and rank == existing["rank"] and source["sha256"] != existing["sha256"]:
                raise ValueError("Conflicting same-release BTS archives")
            if not existing or rank > existing["rank"]:
                releases[key] = {"rank": rank, "sha256": source["sha256"]}
            m = m % 12 + 1
            y += m == 1
            period = f"{y:04d}-{m:02d}"
    aggregates = {}
    for archive in normalized:
        for row in archive["rows"]:
            selection = releases.get((row["scope"], row["period"]))
            if not selection or selection["sha256"] != row["source_sha256"] or int(row["period"][:4]) < start_year:
                continue
            key = (row["period"], row["carrier"], row["carrier_wac"], row["market"])
            if key not in aggregates:
                aggregates[key] = {"period": key[0], "carrier": key[1], "carrier_wac": key[2], "market": key[3],
                    **{metric: 0 for metric in METRICS}, "carrier_entities": set(), "source_sha256s": set(), "published_on": row["published_on"]}
            aggregate = aggregates[key]
            for metric in METRICS:
                aggregate[metric] += row[metric]
            aggregate["carrier_entities"].add(row["carrier_entity"])
            aggregate["source_sha256s"].add(row["source_sha256"])
            aggregate["published_on"] = max(aggregate["published_on"], row["published_on"])
    rows = []
    for key in sorted(aggregates, key=repr):
        row = aggregates[key]
        row["carrier_entities"] = sorted(row["carrier_entities"])
        row["source_sha256s"] = sorted(row["source_sha256s"])
        row["asm_million"] = row["asm"] / 1_000_000
        row["rpm_million"] = row["rpm"] / 1_000_000
        row["load_factor"] = row["rpm"] / row["asm"] if row["asm"] else None
        row["partition"] = "fit_2013_2019" if row["period"][:4] <= "2019" else "selection_2020_2023" if row["period"][:4] <= "2023" else "locked_test_2024_plus"
        row["both_archive_scopes_available"] = all((scope, row["period"]) in releases for scope in ("DD", "FD"))
        row["quality_flags"] = (["rpm_exceeds_asm"] if row["rpm"] > row["asm"] else []) + (["unmapped_market"] if row["market"] not in MARKETS else [])
        if row["carrier_wac"] is None:
            row["quality_flags"].append("missing_carrier_country_metadata")
        rows.append(row)
    return {"rows": rows, "selected_releases": [{"scope": k[0], "period": k[1], "source_sha256": v["sha256"], "published_on": v["rank"][0]} for k, v in sorted(releases.items()) if int(k[1][:4]) >= start_year]}


def build_operating_history(sources: list[dict], wac_source: dict, cutoff: str, destination: Path = OPERATING, raw: Path = RAW) -> dict:
    if not sources or len({s["sha256"] for s in sources}) != len(sources):
        raise ValueError("BTS history requires unique archive sources")
    content, wac_metadata = read_source(wac_source["sha256"], raw)
    if wac_metadata["source_url"] != WAC_URL:
        raise ValueError("BTS WAC lookup does not use the expected official URL")
    lookup = wac_lookup(content)
    normalized = []
    for source in sources:
        content, raw_metadata = read_source(source["sha256"], raw)
        if source["source_url"] != raw_metadata["source_url"]:
            raise ValueError("BTS archive URL differs from preserved metadata")
        try:
            normalized.append(normalize_archive(content, source, lookup))
        except ValueError as exc:
            raise ValueError(f"{Path(urlparse(source['source_url']).path).name}: {exc}") from exc
    panel = monthly_panel(normalized, sources, cutoff)
    if not panel["rows"]:
        raise ValueError("No operating observations are available at this cutoff")
    mapping = {"domestic": "U.S. states and Canada; excludes Caribbean/Pacific territories",
               "atlantic": "Europe, Africa, Middle East, India (WAC 733), Greenland and Saint Pierre/Miquelon",
               "pacific": "Asia excluding India, Oceania, and U.S. Pacific possessions (WAC 5)",
               "latin_america": "Central/South America, Caribbean, Puerto Rico and U.S. Virgin Islands (WAC 3/4)",
               "status": "Explicit analytical market mapping; issuer reconciliation and historical country-boundary review remain required"}
    coverage = []
    for carrier in ("UA", "DL", "AA", "US"):
        rows = [r for r in panel["rows"] if r["carrier"] == carrier]
        periods = sorted({r["period"] for r in rows})
        coverage.append({"operating_carrier": carrier, "months": len(periods), "first": periods[0] if periods else None,
                         "last": periods[-1] if periods else None,
                         "months_by_market": {market: len({r["period"] for r in rows if r["market"] == market}) for market in MARKETS},
                         "months_with_dd_and_fd_archives": len({r["period"] for r in rows if r["both_archive_scopes_available"]}),
                         "quality_flag_rows": sum(bool(r["quality_flags"]) for r in rows),
                         "carrier_entities": sorted({e for r in rows for e in r["carrier_entities"]})})
    manifest = {"version": VERSION, "cutoff": cutoff, "sources": sources, "wac_source": wac_source,
                "segment_reference_url": SEGMENT_REFERENCE, "market_mapping": mapping, "coverage": coverage,
                "normalized_digest": digest(normalized), "panel_digest": digest(panel),
                "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "units": {"asm": "seat_miles", "rpm": "passenger_miles", "seats": "seats", "passengers": "passengers", "ramp_minutes": "minutes", "airborne_minutes": "minutes"},
                "archive_checks": [{k: v for k, v in a.items() if k != "rows"} for a in normalized],
                "vintage_status": "Archive-release-date reconstruction from presently downloaded files. Filename release dates are preserved; historical bytes and exact publication timestamps are not independently recovered.",
                "boundary": "Scheduled passenger segments (class F, configuration 1/3), by operating carrier. UA/DL/AA exclude unassigned feeder flights. US Airways remains separate. Foreign-to-foreign (FF) flying is absent. No issuer-reconciled regional or competitor aggregate is claimed."}
    manifest["snapshot_id"] = digest(manifest)
    target = destination / manifest["snapshot_id"]
    target.mkdir(parents=True, exist_ok=True)
    for name, value in (("normalized.json", normalized), ("panel.json", panel), ("manifest.json", manifest)):
        path = target / name
        if path.exists() and digest(json.loads(path.read_text())) != digest(value):
            raise ValueError("Immutable BTS history was modified")
        if not path.exists():
            with path.open("x") as stream:
                json.dump(value, stream, indent=2, allow_nan=False)
                stream.write("\n")
    parquet = target / "monthly.parquet"
    connection = duckdb.connect()
    try:
        if not parquet.exists():
            escaped = str(parquet).replace("'", "''")
            connection.execute(f"COPY (SELECT unnest(rows) AS row FROM read_json_auto(?)) TO '{escaped}' (FORMAT PARQUET)", [str(target / "panel.json")])
        restored = [r[0] for r in connection.execute("SELECT row FROM read_parquet(?)", [str(parquet)]).fetchall()]
        for row in restored:
            if isinstance(row["published_on"], date):
                row["published_on"] = row["published_on"].isoformat()
        if digest(restored) != digest(panel["rows"]):
            raise ValueError("BTS Parquet differs from the preserved monthly panel")
    finally:
        connection.close()
    pointer = destination / "latest.tmp"
    pointer.write_text(json.dumps({"snapshot_id": manifest["snapshot_id"]}) + "\n")
    pointer.replace(destination / "latest.json")
    return {**manifest, "path": str(target)}


def operating_history_status(destination: Path = OPERATING) -> dict:
    latest = destination / "latest.json"
    if not latest.exists():
        return {"status": "not_built", "message": "Build BTS history with scripts/build_operating_history.py"}
    snapshot = json.loads(latest.read_text())["snapshot_id"]
    if not re.fullmatch(r"[a-f0-9]{64}", snapshot):
        raise ValueError("Invalid BTS snapshot identifier")
    target = destination / snapshot
    manifest = json.loads((target / "manifest.json").read_text())
    if manifest.get("snapshot_id") != snapshot or digest({k: v for k, v in manifest.items() if k != "snapshot_id"}) != snapshot:
        raise ValueError("BTS manifest checksum failed")
    for name, key in (("normalized.json", "normalized_digest"), ("panel.json", "panel_digest")):
        if digest(json.loads((target / name).read_text())) != manifest[key]:
            raise ValueError("BTS historical data checksum failed")
    return {"status": "available", **manifest}
