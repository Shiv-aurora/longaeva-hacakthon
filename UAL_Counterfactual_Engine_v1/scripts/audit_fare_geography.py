"""Independent DB1B endpoint oracle; imports no fare/geography producer code."""
from collections import Counter, defaultdict
import argparse
import csv
import hashlib
from io import StringIO, TextIOWrapper
import json
from pathlib import Path
import re
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from app.evidence import digest
from app.source_store import read_source


def require(condition, message):
    if not condition:
        raise ValueError(message)


def original_endpoints(path, source):
    counts = Counter(); origins = Counter(); destinations = Counter()
    first = {}; last = {}; witnesses = defaultdict(list); hashes = {}; sets = defaultdict(set)
    records = endpoint_total = 0
    with ZipFile(path) as archive:
        members = [n for n in archive.namelist() if n.lower().endswith(".asc")]
        require(len(members) == 1, "Oracle ASC member differs")
        member = members[0]
        with archive.open(member) as stream:
            for line, text in enumerate(TextIOWrapper(stream, encoding="utf-8-sig"), 1):
                if not text.strip():
                    continue
                f = text.rstrip("\r\n").split("|")
                require(len(f) >= 10, "Oracle ticket header differs")
                date = f[2].strip()
                require(re.fullmatch(r"\d{4}[1-4]", date) is not None and date[:4] <= "2023"
                    and date[:4] + "-Q" + date[-1] == source["quarter"], "Oracle quarter/locked boundary differs")
                require(f[3].strip().isdigit(), "Oracle coupon count differs")
                n = int(f[3]); require(1 <= n <= 23, "Oracle coupon bound differs")
                if len(f) == 11 + n * 11 and f[-1] == "":
                    f = f[:-1]
                require(len(f) == 10 + 11 * n, "Oracle repeating fields differ")
                header = (f[5], f[7], f[9])
                coupons = [tuple(f[10 + i * 11:21 + i * 11][j] for j in (6, 8, 10)) for i in range(n)]
                records += 1; endpoint_total += 1 + n
                for position, triplet in enumerate([header, *coupons]):
                    airport, city, wac = [s.strip() for s in triplet]
                    require(re.fullmatch(r"[A-Z0-9]{3}", airport) is not None and city.isascii() and city.isdigit()
                        and wac.isascii() and wac.isdigit() and int(city) > 0 and int(wac) > 0, "Oracle endpoint differs")
                    key = airport, int(city), int(wac)
                    counts[key] += 1
                    if position == 0:
                        origins[key] += 1
                    else:
                        destinations[key] += 1
                    sets[key[:2]].add(key[2]); first.setdefault(key, line); last[key] = line
                    if len(witnesses[key]) < 2:
                        witnesses[key].append({"line": line, "endpoint_position": position})
                    hashes.setdefault(key, hashlib.sha256()).update((str(line) + ":" + str(position) + ":").encode() + text.encode())
    return {"counts": counts, "origins": origins, "destinations": destinations, "sets": sets, "first": first,
        "last": last, "witnesses": witnesses, "hashes": hashes, "records": records, "endpoints": endpoint_total, "member": member}


def audit_part(design_path, quarter):
    design_path = Path(design_path); design = json.loads(design_path.read_text())
    design_id = design_path.parent.name; require(digest(design) == design_id, "Geography design identity differs")
    base = design_path.parents[2]; specification = design["specification"]
    definition, _ = read_source(specification["definition_source_sha256"])
    require(definition.startswith(b"%PDF"), "Oracle field-definition binding differs")
    content, metadata = read_source(specification["wac_source_sha256"])
    require(metadata["source_url"] == specification["wac_source_url"], "Oracle WAC URL differs")
    reader = csv.DictReader(StringIO(content.decode("utf-8-sig")))
    require(reader.fieldnames == ["Code", "Description"], "Oracle WAC schema differs")
    descriptions = {str(int(r["Code"])): r["Description"] for r in reader}
    crosswalk = specification["crosswalk"]
    require(descriptions == {k: v["description"] for k, v in crosswalk.items()}, "Oracle WAC descriptions differ")
    source = next(s for s in specification["sources"] if s["quarter"] == quarter)
    _, metadata = read_source(source["sha256"])
    require(metadata["source_url"] == source["url"] and metadata["bytes"] == source["bytes"], "Oracle raw source binding differs")
    part_id = digest({"design_id": design_id, "source_sha256": source["sha256"]})
    folder = base / "parts" / part_id
    part = json.loads((folder / "part.json").read_text()); manifest = json.loads((folder / "manifest.json").read_text())
    require(manifest == {"design_id": design_id, "source_sha256": source["sha256"], "part_digest": digest(part)}, "Oracle geography part manifest differs")
    require(part["quarter"] == quarter and part["source_sha256"] == source["sha256"], "Oracle geography part source differs")
    raw = original_endpoints(ROOT / "data/raw" / source["sha256"] / "source.bin", source)
    require(part["records"] == raw["records"] and part["endpoint_occurrences"] == raw["endpoints"] and part["source_member"] == raw["member"], "Original geography totals differ")
    require(len(part["rows"]) == len(raw["counts"]), "Original geography key count differs")
    keys = [(r["airport"], r["city_market_id"], r["wac"]) for r in part["rows"]]
    require(keys == sorted(raw["counts"]), "Original geography exact keys differ")
    checks = 7
    for row, key in zip(part["rows"], keys):
        mapping = crosswalk.get(str(key[2])); wacs = sorted(raw["sets"][key[:2]])
        status = "conflicting_wac" if len(wacs) > 1 else "unmapped_wac" if mapping is None or mapping["market"] is None else "unambiguous"
        expected = {"quarter": quarter, "airport": key[0], "city_market_id": key[1], "wac": key[2],
            "wac_description": None if mapping is None else mapping["description"], "observed_wacs": wacs,
            "assignment_status": status, "market": mapping["market"] if status == "unambiguous" else None,
            "endpoint_occurrences": raw["counts"][key], "origin_occurrences": raw["origins"][key],
            "coupon_destination_occurrences": raw["destinations"][key], "source_line_first": raw["first"][key],
            "source_line_last": raw["last"][key], "source_line_witnesses": raw["witnesses"][key],
            "contributor_digest": raw["hashes"][key].hexdigest(), "source_sha256": source["sha256"],
            "source_member": raw["member"], "published_on": source["published_on"]}
        require(row == expected, "Original geography quantities, positions, witnesses, mapping or contributor digest differ")
        checks += len(expected)
    return {"design_id": design_id, "quarter": quarter, "source_sha256": source["sha256"], "part_digest": digest(part),
        "raw_ticket_records": raw["records"], "endpoint_occurrences": raw["endpoints"], "rows": len(keys), "checks_passed": checks}


def audit_snapshot(path):
    path = Path(path); manifest = json.loads(path.read_text()); identity = manifest["snapshot_id"]
    require(digest({k: v for k, v in manifest.items() if k != "snapshot_id"}) == identity and path.parent.name == identity,
        "Geography snapshot identity differs")
    base = path.parents[1]; design_path = base / "designs" / manifest["design_id"] / "design.json"
    design = json.loads(design_path.read_text())
    require(manifest["code_sha256"] == design["code_sha256"] == hashlib.sha256((path.parent / "fare_geography.py").read_bytes()).hexdigest(), "Archived geography code differs")
    expected = [f"{y}-Q{q}" for y in range(2013, 2024) for q in (1, 2, 3, 4)]
    require([p["quarter"] for p in manifest["parts"]] == expected, "Complete geography quarters differ")
    reports = []; rows = []
    for saved in manifest["parts"]:
        report = audit_part(design_path, saved["quarter"])
        require(all(saved[k] == report[k] for k in ("part_digest", "source_sha256", "rows", "endpoint_occurrences"))
            and saved["records"] == report["raw_ticket_records"], "Snapshot geography part binding differs")
        require(saved["part_id"] == digest({"design_id": manifest["design_id"], "source_sha256": saved["source_sha256"]}), "Snapshot geography part identity differs")
        rows.extend(json.loads((base / "parts" / saved["part_id"] / "part.json").read_text())["rows"])
        reports.append(report); print(json.dumps({"phase": "geography_quarter_audited", **report}), file=sys.stderr, flush=True)
    require(rows == json.loads((path.parent / "endpoints.json").read_text()), "Snapshot geography JSON differs")
    table = manifest["tables"]["endpoints"]; parquet = path.parent / "endpoints.parquet"
    require(table["rows"] == len(rows) and table["content_digest"] == digest(rows)
        and table["sha256"] == hashlib.sha256(parquet.read_bytes()).hexdigest(), "Snapshot geography table binding differs")
    import duckdb
    con = duckdb.connect()
    native = [json.loads(r[0]) for r in con.execute("SELECT to_json(e) FROM read_parquet(?) e", [str(parquet)]).fetchall()]; con.close()
    require(native == rows, "Native geography Parquet content differs")
    return {"snapshot_id": identity, "design_id": manifest["design_id"], "quarter_reports": reports,
        "checks_passed": sum(r["checks_passed"] for r in reports) + len(rows) + 8, "native_parquet_rows": len(native),
        "scope": "All original endpoint occurrences and native output verified. Declared model crosswalk is not independently proved issuer geography; fares, traffic travel-object alignment, causal estimation and financial integration remain separate."}


def save_report(report):
    code = Path(__file__).read_bytes(); code_sha = hashlib.sha256(code).hexdigest()
    report = {"audit_code_sha256": code_sha, **report}
    identity = digest(report); base = ROOT / "data/fare_geography_checks"
    base.mkdir(parents=True, exist_ok=True); folder = base / "code"; folder.mkdir(exist_ok=True)
    path = folder / (code_sha + ".py")
    require(not path.exists() or path.read_bytes() == code, "Immutable geography auditor differs")
    if not path.exists():
        path.write_bytes(code)
    path = base / (identity + ".json"); text = json.dumps(report, indent=2, allow_nan=False) + "\n"
    require(not path.exists() or path.read_text() == text, "Immutable geography audit differs")
    if not path.exists():
        path.write_text(text)
    return {"audit_id": identity, **report}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path, help="design.json for a quarter, or complete manifest.json")
    parser.add_argument("--quarter")
    args = parser.parse_args()
    report = audit_part(args.path, args.quarter) if args.quarter else audit_snapshot(args.path)
    print(json.dumps(save_report(report), indent=2))


if __name__ == "__main__":
    main()
