"""Recheck the forensic record directly against original ZIP lines and T2 CSV.

This audit does not import the trace builder, operating parser or traffic model.
"""
import argparse
from collections import Counter, defaultdict
import csv
from decimal import Decimal
import hashlib
from io import BytesIO, TextIOWrapper
import json
from pathlib import Path
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.evidence import digest
from app.source_store import read_source


def audit(record):
    checks = 0
    def check(value, name):
        nonlocal checks
        if not value: raise ValueError("Independent revision trace check failed: " + name)
        checks += 1
    index = record["source_rows"]
    sources = {s["sha256"]: s for s in record["sources"]}
    queries = defaultdict(set)
    snapshot = ROOT / "data/traffic_vintages" / record["vintage_snapshot_id"]
    manifest = json.loads((snapshot / "manifest.json").read_text())
    check(digest({k:v for k,v in manifest.items() if k != "snapshot_id"}) == snapshot.name, "vintage manifest")
    original = json.loads((snapshot / "artifact.json").read_text())
    check(digest(original) == record["vintage_artifact_digest"] == manifest["artifact_digest"], "vintage artifact")
    design = json.loads((snapshot.parent / "designs" / manifest["design_id"] / "design.json").read_text())
    check(digest(design) == record["vintage_design_id"] == manifest["design_id"], "original vintage design")
    declared_sources = {s["sha256"]: s for s in design["spec"]["sources"]}
    for sha, source in sources.items(): check(source == declared_sources.get(sha), "exact declared source metadata/scope")
    for case, selection in record["quarter_selections"].items():
        if case == "final":
            refs = {}
            for row in original["evaluation_targets"]["observations"]:
                if row["carrier"] == "AA" and row["period"] in ("2021-01", "2021-02", "2021-03"):
                    for release in row["scope_releases"]: refs[release["scope"], release["period"]] = release
            expected = [refs[k] for k in sorted(refs)]
        else:
            origin = next(o for o in original["origins"] if o["issue_on"] == case)
            expected = [r for r in origin["selected_releases"] if r["period"] in ("2021-01", "2021-02", "2021-03")]
        check(selection["releases"] == expected and len(expected) == 6, "exact issue quarter releases")
        for ref in expected: queries[ref["source_sha256"]].add(ref["period"])
    for scope in ("DD", "FD"):
        prior = [s for s in sources.values() if s["scope"] == scope and s["period_first"] == "2020-01" and s["period_last"] == "2020-12"]
        check(len(prior) == 1, "one prior-year reference per scope")
        queries[prior[0]["sha256"]].add("2020-01")
    check(set(queries) == set(sources), "exact source set")
    originals = {}
    by_source_period = defaultdict(list)
    for sha, periods in sorted(queries.items()):
        content, metadata = read_source(sha)
        check(metadata["source_url"] == sources[sha]["source_url"], "original URL")
        with ZipFile(BytesIO(content)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".asc")]
            check(len(names) == 1, "original ASC member")
            with z.open(names[0]) as binary:
                for line, text in enumerate(TextIOWrapper(binary, encoding="utf-8-sig"), 1):
                    if not text.strip(): continue
                    f = text.rstrip("\r\n").split("|")
                    if len(f) in (29, 33) and f[-1] == "": f.pop()
                    period = f"{int(f[0]):04d}-{int(f[1]):02d}"
                    if period not in periods or f[10] != "AA" or f[14] != "F" or f[17] not in ("1", "3"): continue
                    raw = {"source_sha256": sha, "source_member": names[0], "source_line": line,
                        "scope": sources[sha]["scope"], "period": period, "text": text.rstrip("\r\n"), "fields": f}
                    row_id = digest(raw)
                    check(index.get(row_id) == {"row_id": row_id, **raw}, "exact original source line/fields")
                    originals[row_id] = raw
                    by_source_period[sha, period].append(row_id)
        print(json.dumps({"archives_checked": len({r["source_sha256"] for r in originals.values()}), "source_rows_checked": len(originals)}), file=sys.stderr, flush=True)
    check(set(originals) == set(index), "no omitted or invented retained raw row")
    for case, selection in record["quarter_selections"].items():
        ids = [row_id for ref in selection["releases"] for row_id in by_source_period[ref["source_sha256"], ref["period"]]]
        check(selection["row_ids"] == ids, "complete quarter source row set")
    for comparison in record["literal_non_date_payload_comparisons"] + record["january_release_comparisons"]:
        scope = comparison["scope"]
        if "earlier_case" in comparison:
            old_selection = record["quarter_selections"][comparison["earlier_case"]]
            earlier = {i:originals[i] for i in old_selection["row_ids"] if originals[i]["scope"] == scope and originals[i]["period"] == "2021-01"}
            selection = record["quarter_selections"][comparison["later_case"]]
        else:
            earlier = {i:r for i,r in originals.items() if r["scope"] == scope and r["period"] == "2020-01"}
            selection = record["quarter_selections"][comparison["case"]]
        later = {i:originals[i] for i in selection["row_ids"] if originals[i]["scope"] == scope and originals[i]["period"] == "2021-01"}
        old_counts, new_counts = (Counter(tuple(r["fields"][2:]) for r in rows.values()) for rows in (earlier, later))
        check(comparison["matched_rows"] == sum((old_counts & new_counts).values()), "full payload multiset overlap")
        check(comparison["every_payload_matches"] == (bool(earlier and later) and old_counts == new_counts), "full date-excluded multiset equality")
        check(comparison["earlier_rows"] == len(earlier) and comparison["later_rows"] == len(later), "cohort counts")
        seen_old, seen_new = set(), set()
        for pair in comparison["pairs"]:
            a, b = pair["earlier_row_id"], pair["later_row_id"]
            check(a in earlier and b in later and a not in seen_old and b not in seen_new, "one-to-one source pairing")
            check(earlier[a]["fields"][2:] == later[b]["fields"][2:], "every literal non-date field")
            check(pair["non_date_payload_digest"] == digest(earlier[a]["fields"][2:]), "paired payload hash")
            seen_old.add(a); seen_new.add(b)
        check(len(seen_old) == comparison["matched_rows"], "pair count")
        check(sorted(set(earlier) - seen_old) == comparison["unmatched_earlier_row_ids"], "unmatched earlier rows")
        check(sorted(set(later) - seen_new) == comparison["unmatched_later_row_ids"], "unmatched later rows")
    tm_folder = ROOT / "data/t2_history" / record["t2_snapshot_id"]
    tm = json.loads((tm_folder / "manifest.json").read_text())
    check(digest({k:v for k,v in tm.items() if k != "snapshot_id"}) == tm_folder.name, "T2 manifest")
    check(record["t2_source"] == tm["source"] and record["t2_panel_digest"] == tm["panel_digest"], "T2 source binding")
    content, _ = read_source(tm["source"]["sha256"])
    saved = {r["source_line"]: r for r in record["t2_original_witnesses"]}
    t2 = {}
    with ZipFile(BytesIO(content)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        with z.open(name) as b:
            for line, row in enumerate(csv.DictReader(TextIOWrapper(b, encoding="utf-8-sig")), 2):
                if row["YEAR"] != "2021" or row["QUARTER"] != "1" or row["UNIQUE_CARRIER"] != "AA" or row["SERVICE_CLASS"] != "F" or row["AIRCRAFT_CONFIG"] not in ("1", "3"): continue
                witness = saved.get(line)
                check(witness is not None and witness["original_fields"] == row and witness["source_member"] == name, "original T2 F line")
                key = tuple(row[f] for f in ("UNIQUE_CARRIER_ENTITY", "AIRCRAFT_GROUP", "AIRCRAFT_TYPE", "AIRCRAFT_CONFIG"))
                check(key not in t2 and witness["grain"] == list(key), "T2 reporting grain")
                t2[key] = row
                for f, tf in (("rpm", "REV_PAX_MILES_140"), ("asm", "AVL_SEAT_MILES_320"), ("departures", "REV_ACRFT_DEP_PERF_510")):
                    expected = str(Decimal(row[tf])) if row[tf].strip() else None
                    check(witness["values"][f] == expected, "original T2 quantity including missing")
    check(len(saved) == len(t2), "exact T2 witness coverage")
    for case, selection in record["quarter_selections"].items():
        sums = defaultdict(lambda: defaultdict(int))
        ids = defaultdict(list)
        for row_id in selection["row_ids"]:
            row = originals[row_id]; f = row["fields"]
            key = tuple(f[i] for i in (11, 15, 16, 17)); ids[key].append(row_id)
            pax = sum(int(f[i]) for i in (24,25,26)) if len(f) == 32 else int(f[22])
            seats = sum(int(f[i]) for i in (21,22,23)) if len(f) == 32 else int(f[21])
            for field, value in (("rpm", pax*int(f[13])), ("asm", seats*int(f[13])), ("departures", int(f[18])), ("passengers", pax), ("seats", seats)):
                sums[key][field] += value
        result = record["quarter_t2_comparisons"][case]
        check([r["grain"] for r in result["grains"]] == [list(k) for k in sorted(sums.keys() | t2.keys())], "complete quarterly grains")
        exact = 0
        for row in result["grains"]:
            key = tuple(row["grain"])
            if key in sums:
                check(row["segment"] == {"row_ids":ids[key], **sums[key]}, "quarter segment source sums")
            else: check(row["segment"] is None, "missing segment grain remains absent")
            expected = {}
            for field, tf in (("rpm", "REV_PAX_MILES_140"), ("asm", "AVL_SEAT_MILES_320"), ("departures", "REV_ACRFT_DEP_PERF_510")):
                expected[field] = str(Decimal(sums[key][field]) - Decimal(t2[key][tf])) if key in sums and key in t2 and t2[key][tf].strip() else None
            check(row["difference_segment_minus_t2"] == expected, "original quarterly residual")
            status = "missing_segment_grain" if key not in sums else "missing_t2_grain" if key not in t2 else "exact_rpm_asm_departures" if all(v is not None and Decimal(v) == 0 for v in expected.values()) else "difference_or_missing_quantity"
            check(row["status"] == status, "quarter reporting residual classification")
            exact += status == "exact_rpm_asm_departures"
        check(result["exact_rpm_asm_departure_grains"] == exact, "exact quarter grain count")
    return {"checks_passed": checks, "original_segment_rows": len(originals), "source_archives": len(sources), "original_t2_f_rows": len(t2),
        "scope": "Original line completeness, date-excluded payload multisets/pairs, source selections and independent integer quarterly sums/T2 residuals. Entity/route delta rankings are descriptive trace output, not separately audited here. No historical correction or estimation acceptance."}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("trace", type=Path); args = parser.parse_args()
    record = json.loads(args.trace.read_text())
    if digest(record) != args.trace.parent.name: raise ValueError("Forensic record checksum differs")
    code = args.trace.parent / "trace_traffic_revisions.py"
    if hashlib.sha256(code.read_bytes()).hexdigest() != record["trace_code_sha256"]: raise ValueError("Preserved trace code differs")
    result = {"trace_id": args.trace.parent.name, "audit_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), **audit(record)}
    target = ROOT / "data/traffic_revision_checks" / (digest(result) + ".json")
    target.parent.mkdir(exist_ok=True)
    value = json.dumps(result, indent=2) + "\n"
    if target.exists() and target.read_text() != value: raise ValueError("Immutable trace audit differs")
    if not target.exists(): target.write_text(value)
    print(json.dumps({"audit_file":str(target), **result}, indent=2))


if __name__ == "__main__": main()
