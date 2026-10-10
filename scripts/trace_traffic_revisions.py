"""Trace observed AA date-field reuse without changing historical observations.

The matching compares every literal source field except year/month. T2 is a
quarterly reporting-scope witness, never a substitute monthly observation.
"""
from collections import defaultdict, deque
import csv
from decimal import Decimal
import hashlib
from io import BytesIO, TextIOWrapper
import json
from pathlib import Path
import re
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.evidence import digest
from app.source_store import read_source

VERSION = "traffic-revision-trace-0.1.1"
ISSUES = ("2022-01-31", "2022-02-28", "2022-04-30")
GRAIN = (11, 15, 16, 17)  # reporting entity, aircraft group/type/configuration
T2_GRAIN = ("UNIQUE_CARRIER_ENTITY", "AIRCRAFT_GROUP", "AIRCRAFT_TYPE", "AIRCRAFT_CONFIG")
T2_FIELDS = {"rpm": "REV_PAX_MILES_140", "asm": "AVL_SEAT_MILES_320", "departures": "REV_ACRFT_DEP_PERF_510"}


def fields(text):
    result = text.rstrip("\r\n").split("|")
    if len(result) in (29, 33) and result[-1] == "": result.pop()
    if len(result) not in (28, 32): raise ValueError("Original segment layout differs")
    return result


def segment_rows(content, sha, scope, periods):
    if hashlib.sha256(content).hexdigest() != sha: raise ValueError("Original ZIP checksum differs")
    if scope not in ("DD", "FD") or not periods or any(not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", p) or p > "2023-12" for p in periods):
        raise ValueError("Trace must exclude locked periods")
    output = []
    with ZipFile(BytesIO(content)) as archive:
        members = [m for m in archive.infolist() if m.filename.lower().endswith(".asc")]
        if len(members) != 1 or members[0].file_size > 500_000_000 or members[0].flag_bits & 1:
            raise ValueError("Expected one bounded unencrypted segment member")
        with archive.open(members[0]) as binary:
            for line, text in enumerate(TextIOWrapper(binary, encoding="utf-8-sig"), 1):
                if not text.strip(): continue
                f = fields(text)
                period = f"{int(f[0]):04d}-{int(f[1]):02d}"
                if period not in periods or f[10] != "AA" or f[14] != "F" or f[17] not in ("1", "3"): continue
                row = {"source_sha256": sha, "source_member": members[0].filename, "source_line": line,
                    "scope": scope, "period": period, "text": text.rstrip("\r\n"), "fields": f}
                output.append({"row_id": digest(row), **row})
    return output


def match_payloads(earlier, later):
    """One-to-one multiset matching; multiplicity and every non-date field matter."""
    available = defaultdict(deque)
    for row in earlier: available[(row["scope"], tuple(row["fields"][2:]))].append(row)
    pairs, unmatched = [], []
    for row in later:
        key = row["scope"], tuple(row["fields"][2:])
        if not available[key]: unmatched.append(row["row_id"]); continue
        old = available[key].popleft()
        pairs.append({"earlier_row_id": old["row_id"], "later_row_id": row["row_id"],
            "non_date_payload_digest": digest(list(key[1]))})
    remaining = sorted(row["row_id"] for rows in available.values() for row in rows)
    return {"earlier_rows": len(earlier), "later_rows": len(later), "matched_rows": len(pairs),
        "every_payload_matches": bool(earlier and later) and not remaining and not unmatched,
        "pairs": pairs, "unmatched_earlier_row_ids": remaining, "unmatched_later_row_ids": sorted(unmatched)}


def quantities(row):
    f = row["fields"]
    legacy = len(f) == 32
    seats = sum(int(f[i]) for i in (21, 22, 23)) if legacy else int(f[21])
    passengers = sum(int(f[i]) for i in (24, 25, 26)) if legacy else int(f[22])
    values = {"rpm": passengers * int(f[13]), "asm": seats * int(f[13]),
        "departures": int(f[18]), "passengers": passengers, "seats": seats}
    if any(v < 0 for v in values.values()): raise ValueError("Negative trace quantity")
    return values


def aggregate(rows, indices=GRAIN):
    result = {}
    for row in rows:
        key = tuple(row["fields"][i] for i in indices)
        if key not in result: result[key] = {"row_ids": [], **dict.fromkeys(quantities(row), 0)}
        result[key]["row_ids"].append(row["row_id"])
        for field, amount in quantities(row).items(): result[key][field] += amount
    return result


def revision_delta(earlier, later):
    old, new = aggregate(earlier), aggregate(later)
    details = []
    for key in sorted(old.keys() | new.keys()):
        a, b = old.get(key), new.get(key)
        details.append({"grain": list(key), "earlier": a, "later": b,
            "delta": {f: (b[f] if b else 0) - (a[f] if a else 0) for f in quantities((earlier or later)[0])}})
    routes_old, routes_new = aggregate(earlier, (11, 2, 6, 16, 17)), aggregate(later, (11, 2, 6, 16, 17))
    routes = [{"grain": list(key), "earlier_row_ids": routes_old.get(key, {}).get("row_ids", []),
        "later_row_ids": routes_new.get(key, {}).get("row_ids", []),
        "rpm_delta": routes_new.get(key, {}).get("rpm", 0) - routes_old.get(key, {}).get("rpm", 0)}
        for key in routes_old.keys() | routes_new.keys()]
    routes.sort(key=lambda row: (-abs(row["rpm_delta"]), row["grain"]))
    return {"entity_aircraft_deltas": details, "largest_route_aircraft_rpm_deltas": routes[:20],
        "quantity_units": "RPM/ASM in miles; counts otherwise. Zero for absent contribution is delta accounting, not an imputed observation."}


def t2_rows(content, sha):
    if hashlib.sha256(content).hexdigest() != sha: raise ValueError("T2 source checksum differs")
    output = []
    with ZipFile(BytesIO(content)) as archive:
        members = [m for m in archive.infolist() if m.filename.lower().endswith(".csv")]
        if len(members) != 1 or members[0].file_size > 150_000_000: raise ValueError("Expected bounded original T2 CSV")
        with archive.open(members[0]) as binary:
            for line, row in enumerate(csv.DictReader(TextIOWrapper(binary, encoding="utf-8-sig")), 2):
                # Filter dates/identity before inspecting any quantities; 2024+ remains locked.
                if row["YEAR"] != "2021" or row["QUARTER"] != "1" or row["UNIQUE_CARRIER"] != "AA" or row["SERVICE_CLASS"] != "F" or row["AIRCRAFT_CONFIG"] not in ("1", "3"): continue
                values = {}
                for key, field in T2_FIELDS.items():
                    value = Decimal(row[field]) if row[field].strip() else None
                    if value is not None and (not value.is_finite() or value < 0): raise ValueError("Invalid T2 witness quantity")
                    values[key] = str(value) if value is not None else None
                output.append({"source_sha256": sha, "source_member": members[0].filename,
                    "source_line": line, "grain": [row[k] for k in T2_GRAIN], "reporting_region": row["CARRIER_REGION"],
                    "values": values, "original_fields": row})
    return output


def compare_quarter(rows, witnesses):
    if not rows or any(r["period"] not in ("2021-01", "2021-02", "2021-03") for r in rows): raise ValueError("Quarter comparison periods differ")
    if {r["period"] for r in rows} != {"2021-01", "2021-02", "2021-03"}: raise ValueError("Quarter comparison incomplete")
    sums, originals = aggregate(rows), {}
    for row in witnesses:
        key = tuple(row["grain"])
        if key in originals: raise ValueError("Duplicate T2 F grain")
        originals[key] = row
    output = []
    for key in sorted(sums.keys() | originals.keys()):
        s, t = sums.get(key), originals.get(key)
        delta = {f: str(Decimal(s[f]) - Decimal(t["values"][f])) if s and t and t["values"][f] is not None else None for f in T2_FIELDS}
        output.append({"grain": list(key), "segment": s, "t2_source_line": t["source_line"] if t else None,
            "t2_values": t["values"] if t else None, "difference_segment_minus_t2": delta,
            "status": "missing_segment_grain" if not s else "missing_t2_grain" if not t else
                "exact_rpm_asm_departures" if all(v is not None and Decimal(v) == 0 for v in delta.values()) else "difference_or_missing_quantity"})
    return {"grains": output, "exact_rpm_asm_departure_grains": sum(r["status"] == "exact_rpm_asm_departures" for r in output),
        "scope": "Scheduled F passenger/combination certificate rows, by reporting entity and aircraft. T2 may include FF operations absent from DD/FD; residual differences remain explicit."}


def read_snapshot(folder, artifact_name, digest_field):
    manifest = json.loads((folder / "manifest.json").read_text())
    if digest({k: v for k, v in manifest.items() if k != "snapshot_id"}) != folder.name or manifest["snapshot_id"] != folder.name:
        raise ValueError("Trace input manifest differs")
    artifact = json.loads((folder / artifact_name).read_text())
    if digest(artifact) != manifest[digest_field]: raise ValueError("Trace input artifact differs")
    return manifest, artifact


def build_trace():
    root = ROOT / "data/traffic_vintages"
    identity = json.loads((root / "latest.json").read_text())["snapshot_id"]
    manifest, artifact = read_snapshot(root / identity, "artifact.json", "artifact_digest")
    design = json.loads((root / "designs" / manifest["design_id"] / "design.json").read_text())
    if digest(design) != manifest["design_id"]: raise ValueError("Trace input design differs")
    source_index = {s["sha256"]: s for s in design["spec"]["sources"]}
    releases = {}
    for issue in ISSUES:
        origin = next(o for o in artifact["origins"] if o["issue_on"] == issue)
        releases[issue] = [s for s in origin["selected_releases"] if s["period"] in ("2021-01", "2021-02", "2021-03")]
    final = {}
    for row in artifact["evaluation_targets"]["observations"]:
        if row["carrier"] == "AA" and row["period"] in ("2021-01", "2021-02", "2021-03"):
            for release in row["scope_releases"]: final[release["scope"], release["period"]] = release
    releases["final"] = [final[key] for key in sorted(final)]
    queries = defaultdict(set)
    for items in releases.values():
        if len(items) != 6: raise ValueError("Trace needs both scopes for every quarter month")
        for ref in items: queries[ref["source_sha256"], ref["scope"]].add(ref["period"])
    prior = {}
    for scope in ("DD", "FD"):
        matches = [s for s in source_index.values() if s["scope"] == scope and s["period_first"] == "2020-01" and s["period_last"] == "2020-12"]
        if len(matches) != 1: raise ValueError("Trace needs one declared 2020 annual source per scope")
        prior[scope] = matches[0]
        queries[matches[0]["sha256"], scope].add("2020-01")
    collections, all_rows, sources = {}, {}, []
    for (sha, scope), periods in sorted(queries.items()):
        content, metadata = read_source(sha)
        if metadata["source_url"] != source_index[sha]["source_url"]: raise ValueError("Trace source URL differs")
        sources.append(source_index[sha])
        rows = segment_rows(content, sha, scope, periods)
        for row in rows: all_rows[row["row_id"]] = row
        for period in sorted(periods): collections[sha, scope, period] = [r for r in rows if r["period"] == period]
    case_rows, comparisons = {}, []
    for case, refs in releases.items():
        case_rows[case] = [r for ref in refs for r in collections[ref["source_sha256"], ref["scope"], ref["period"]]]
        for scope in ("DD", "FD"):
            rows = [r for r in case_rows[case] if r["scope"] == scope and r["period"] == "2021-01"]
            previous = collections[prior[scope]["sha256"], scope, "2020-01"]
            comparisons.append({"case": case, "scope": scope, "earlier_period": "2020-01", "later_period": "2021-01", **match_payloads(previous, rows)})
    tm_root = ROOT / "data/t2_history"
    t2_id = json.loads((tm_root / "latest.json").read_text())["snapshot_id"]
    tm, _ = read_snapshot(tm_root / t2_id, "panel.json", "panel_digest")
    t2_content, t2_metadata = read_source(tm["source"]["sha256"])
    if t2_metadata["source_url"] != tm["source"]["source_url"]: raise ValueError("T2 trace URL differs")
    witnesses = t2_rows(t2_content, tm["source"]["sha256"])
    return {"version": VERSION, "reviewed_on": "2026-10-09",
        "status": "source_payload_reuse_confirmed_correction_and_boundary_review_open",
        "vintage_snapshot_id": identity, "vintage_artifact_digest": manifest["artifact_digest"],
        "vintage_design_id": manifest["design_id"], "t2_snapshot_id": t2_id, "t2_panel_digest": tm["panel_digest"],
        "sources": sources, "t2_source": tm["source"], "source_rows": all_rows,
        "quarter_selections": {case: {"releases": refs, "row_ids": [r["row_id"] for r in case_rows[case]]} for case, refs in releases.items()},
        "literal_non_date_payload_comparisons": comparisons, "t2_original_witnesses": witnesses,
        "january_release_comparisons": [{"earlier_case": ISSUES[0], "later_case": case, "scope": scope,
            **match_payloads([r for r in case_rows[ISSUES[0]] if r["scope"] == scope and r["period"] == "2021-01"],
                [r for r in case_rows[case] if r["scope"] == scope and r["period"] == "2021-01"])}
            for case in (ISSUES[1], ISSUES[2], "final") for scope in ("DD", "FD")],
        "quarter_t2_comparisons": {case: compare_quarter(rows, witnesses) for case, rows in case_rows.items()},
        "january_entity_route_aircraft_revisions": {scope: revision_delta(
            [r for r in case_rows[ISSUES[0]] if r["period"] == "2021-01" and r["scope"] == scope],
            [r for r in case_rows[ISSUES[1]] if r["period"] == "2021-01" and r["scope"] == scope]) for scope in ("DD", "FD")},
        "trace_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "interpretation": "Exact literal payload reuse is observed in published source bytes; it does not establish where the error arose. Earlier-released 2021 records have independent quarterly T2 support. Residual scope/quantity differences remain inspectable.",
        "estimation_gate": "This present-day finding changes no historical observation or score. Declare, review and preserve any correction separately before refitting; disclose hindsight and keep uncorrected vintage comparisons. Do not allocate T2 quarterly totals into invented monthly records."}


def main():
    record = build_trace()
    identity = digest(record)
    folder = ROOT / "data/traffic_revision_traces" / identity
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in (("record.json", (json.dumps(record, indent=2, allow_nan=False) + "\n").encode()),
                          ("trace_traffic_revisions.py", Path(__file__).read_bytes())):
        path = folder / name
        if path.exists() and path.read_bytes() != content: raise ValueError("Immutable trace differs")
        if not path.exists(): path.write_bytes(content)
    print(json.dumps({"trace_file": str(folder / "record.json"), "source_rows": len(record["source_rows"]),
        "status": record["status"], "comparisons": [{k: row[k] for k in ("case", "scope", "earlier_rows", "later_rows", "matched_rows", "every_payload_matches")} for row in record["literal_non_date_payload_comparisons"]],
        "quarter_exact_grains": {case: c["exact_rpm_asm_departure_grains"] for case, c in record["quarter_t2_comparisons"].items()}}, indent=2))


if __name__ == "__main__": main()
