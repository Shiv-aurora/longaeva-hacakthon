"""Raw DB1B airport/city/WAC evidence, separate from fare eligibility and demand.

Every original header and coupon endpoint is counted, including surface coupons,
other carriers and records excluded from fare estimation. No fare is read here.
"""
import csv
from datetime import datetime, timezone
import hashlib
from io import StringIO, TextIOWrapper
import json
from pathlib import Path
import re
from zipfile import ZipFile

from .evidence import digest
from .source_store import ROOT, read_source

VERSION = "db1b-endpoint-geography-0.1.0"
SCOPE = "Original quarterly airport/city/WAC endpoint occurrences, not passenger demand, population counts, observed yield or an exact fare/T-100 travel-object match. Geographic assignments follow the declared model crosswalk; conflicts remain unassigned."


def endpoint_fields(text, quarter):
    fields = text.rstrip("\r\n").split("|")
    if len(fields) < 10:
        raise ValueError("Incomplete geography ticket header")
    token = fields[2].strip()
    if not re.fullmatch(r"\d{4}[1-4]", token) or f"{token[:4]}-Q{token[4]}" != quarter or token[:4] > "2023":
        raise ValueError("Geography quarter or locked boundary differs")
    if not re.fullmatch(r"\d+", fields[3].strip()):
        raise ValueError("Invalid geography coupon count")
    count = int(fields[3])
    if not 1 <= count <= 23:
        raise ValueError("Invalid geography coupon count")
    if len(fields) == 11 + 11 * count and fields[-1] == "":
        fields.pop()
    if len(fields) != 10 + 11 * count:
        raise ValueError("Geography repeating coupon layout differs")
    result = []
    for position, start in enumerate([5, *(16 + 11 * i for i in range(count))]):
        airport, city, wac = (fields[start + offset].strip() for offset in (0, 2, 4))
        if not re.fullmatch(r"[A-Z0-9]{3}", airport):
            raise ValueError("Invalid geography airport")
        if any(not re.fullmatch(r"\d+", value) or int(value) <= 0 for value in (city, wac)):
            raise ValueError("Invalid geography city/WAC")
        result.append((position, airport, int(city), int(wac)))
    return result


def scan_archive(path, source, crosswalk):
    groups = {}; records = endpoints = 0
    with ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".asc")]
        if len(members) != 1:
            raise ValueError("Geography requires one ASC member")
        member = members[0]
        with archive.open(member) as stream:
            for line, text in enumerate(TextIOWrapper(stream, encoding="utf-8-sig"), 1):
                if not text.strip():
                    continue
                values = endpoint_fields(text, source["quarter"])
                records += 1; endpoints += len(values)
                for position, airport, city, wac in values:
                    key = airport, city, wac
                    if key not in groups:
                        groups[key] = {"endpoint_occurrences": 0, "origin_occurrences": 0,
                            "coupon_destination_occurrences": 0, "source_line_first": line,
                            "source_line_last": line, "source_line_witnesses": [], "hasher": hashlib.sha256()}
                    value = groups[key]
                    value["endpoint_occurrences"] += 1
                    value["origin_occurrences" if position == 0 else "coupon_destination_occurrences"] += 1
                    value["source_line_last"] = line
                    value["hasher"].update(f"{line}:{position}:".encode() + text.encode())
                    if len(value["source_line_witnesses"]) < 2:
                        value["source_line_witnesses"].append({"line": line, "endpoint_position": position})
    sets = {}
    for airport, city, wac in groups:
        sets.setdefault((airport, city), set()).add(wac)
    rows = []
    for (airport, city, wac), value in sorted(groups.items()):
        contributor_digest = value.pop("hasher").hexdigest()
        mapping = crosswalk.get(str(wac))
        conflict = len(sets[airport, city]) > 1
        status = "conflicting_wac" if conflict else "unmapped_wac" if not mapping or mapping["market"] is None else "unambiguous"
        rows.append({"quarter": source["quarter"], "airport": airport, "city_market_id": city,
            "wac": wac, "wac_description": mapping["description"] if mapping else None,
            "observed_wacs": sorted(sets[airport, city]), "assignment_status": status,
            "market": mapping["market"] if status == "unambiguous" else None, **value,
            "contributor_digest": contributor_digest, "source_sha256": source["sha256"],
            "source_member": member, "published_on": source["published_on"]})
    if sum(r["endpoint_occurrences"] for r in rows) != endpoints:
        raise ValueError("Geography endpoint conservation differs")
    return {"quarter": source["quarter"], "source_sha256": source["sha256"],
        "source_member": member, "records": records, "endpoint_occurrences": endpoints, "rows": rows}


def immutable_json(path, value):
    content = json.dumps(value, indent=2, allow_nan=False) + "\n"
    if path.exists() and path.read_text() != content:
        raise ValueError("Immutable geography file differs")
    if not path.exists():
        path.write_text(content)


def build_geography(sources, specification, destination=None, progress=None):
    destination = Path(destination or ROOT / "data/fare_geography")
    destination.mkdir(parents=True, exist_ok=True)
    expected = {f"{year}-Q{quarter}" for year in range(2013, 2024) for quarter in (1, 2, 3, 4)}
    if len(sources) != 44 or {s["quarter"] for s in sources} != expected:
        raise ValueError("Geography requires all 44 declared quarters")
    if sources != specification["sources"]:
        raise ValueError("Geography inventory differs from specification")
    definition, _ = read_source(specification["definition_source_sha256"])
    if not definition.startswith(b"%PDF"):
        raise ValueError("Geography field definition differs")
    content, meta = read_source(specification["wac_source_sha256"])
    if meta["source_url"] != specification["wac_source_url"]:
        raise ValueError("Geography WAC source URL differs")
    reader = csv.DictReader(StringIO(content.decode("utf-8-sig")))
    if reader.fieldnames != ["Code", "Description"]:
        raise ValueError("Geography WAC source schema differs")
    entries = list(reader)
    descriptions = {str(int(r["Code"])): r["Description"] for r in entries}
    crosswalk = specification["crosswalk"]
    if len(entries) != len(descriptions) or descriptions != {k: v["description"] for k, v in crosswalk.items()}:
        raise ValueError("Geography WAC definitions differ")
    if any(v["market"] not in (None, "domestic", "atlantic", "pacific", "latin_america") for v in crosswalk.values()):
        raise ValueError("Geography model market differs")
    code_bytes = Path(__file__).read_bytes(); code = hashlib.sha256(code_bytes).hexdigest()
    design = {"version": VERSION, "specification": specification, "code_sha256": code, "scope": SCOPE,
        "rules": "Scan all ticket-header and coupon-destination endpoints, without fare eligibility or passenger expansion. Exact airport/city/WAC keys by quarter; no conflict majority vote or nearest-quarter fill. One occurrence per endpoint; witnesses and ordered raw-line/position digests."}
    design_id = digest(design); folder = destination / "designs" / design_id
    folder.mkdir(parents=True, exist_ok=True); immutable_json(folder / "design.json", design)
    if not (folder / "declaration_time.json").exists():
        immutable_json(folder / "declaration_time.json", {"declared_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "Local design saved before extraction; not external preregistration."})
    parts = []; rows = []
    for source in sorted(sources, key=lambda s: s["quarter"]):
        _, meta = read_source(source["sha256"])
        if meta["source_url"] != source["url"] or meta["bytes"] != source["bytes"]:
            raise ValueError("Geography raw source metadata differs")
        part_id = digest({"design_id": design_id, "source_sha256": source["sha256"]})
        folder = destination / "parts" / part_id; folder.mkdir(parents=True, exist_ok=True)
        if (folder / "part.json").exists():
            part = json.loads((folder / "part.json").read_text())
            manifest = json.loads((folder / "manifest.json").read_text())
            if manifest != {"design_id": design_id, "source_sha256": source["sha256"], "part_digest": digest(part)}:
                raise ValueError("Cached geography part differs")
        else:
            part = scan_archive(ROOT / "data/raw" / source["sha256"] / "source.bin", source, crosswalk)
            immutable_json(folder / "part.json", part)
            immutable_json(folder / "manifest.json", {"design_id": design_id, "source_sha256": source["sha256"], "part_digest": digest(part)})
        parts.append({"part_id": part_id, "part_digest": digest(part), "quarter": source["quarter"],
            "source_sha256": source["sha256"], "records": part["records"], "endpoint_occurrences": part["endpoint_occurrences"], "rows": len(part["rows"])})
        rows.extend(part["rows"])
        if progress:
            progress({"phase": "geography_quarter_parsed", **parts[-1]})
    if Path(__file__).read_bytes() != code_bytes:
        raise ValueError("Geography builder changed during extraction")
    import duckdb
    staging = destination / "staging" / design_id; staging.mkdir(parents=True, exist_ok=True)
    immutable_json(staging / "endpoints.json", rows)
    parquet = staging / "endpoints.parquet"
    con = duckdb.connect(); con.execute("SET threads=1")
    con.execute("CREATE TABLE endpoints AS SELECT * FROM read_json_auto(?)", [str(staging / "endpoints.json")])
    con.execute("COPY endpoints TO ? (FORMAT PARQUET)", [str(parquet)])
    loaded = [json.loads(r[0]) for r in con.execute("SELECT to_json(e) FROM read_parquet(?) e", [str(parquet)]).fetchall()]
    con.close()
    if digest(loaded) != digest(rows):
        raise ValueError("Geography native Parquet content differs")
    tables = {"endpoints": {"rows": len(rows), "sha256": hashlib.sha256(parquet.read_bytes()).hexdigest(), "content_digest": digest(rows)}}
    manifest = {"version": VERSION, "design_id": design_id, "parts": parts, "code_sha256": code,
        "tables": tables, "duckdb_version": duckdb.__version__, "scope": SCOPE}
    identity = digest(manifest); manifest["snapshot_id"] = identity
    folder = destination / identity; folder.mkdir(parents=True, exist_ok=True)
    immutable_json(folder / "manifest.json", manifest); immutable_json(folder / "endpoints.json", rows)
    output = folder / "endpoints.parquet"
    if output.exists() and hashlib.sha256(output.read_bytes()).hexdigest() != tables["endpoints"]["sha256"]:
        raise ValueError("Immutable geography Parquet differs")
    parquet.replace(output)
    saved = folder / "fare_geography.py"
    if saved.exists() and saved.read_bytes() != code_bytes:
        raise ValueError("Immutable geography code differs")
    if not saved.exists():
        saved.write_bytes(code_bytes)
    temporary = destination / "latest.tmp"; temporary.write_text(json.dumps({"snapshot_id": identity}) + "\n")
    temporary.replace(destination / "latest.json")
    return {"snapshot_id": identity, "design_id": design_id, "quarters": len(parts),
        "records": sum(p["records"] for p in parts), "endpoint_occurrences": sum(p["endpoint_occurrences"] for p in parts),
        "rows": len(rows), "conflicting_airport_city_quarters": len({(r["quarter"], r["airport"], r["city_market_id"]) for r in rows if r["assignment_status"] == "conflicting_wac"}),
        "unmapped_wac_rows": sum(r["assignment_status"] == "unmapped_wac" for r in rows), "scope": SCOPE}
