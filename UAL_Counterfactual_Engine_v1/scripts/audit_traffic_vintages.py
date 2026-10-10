"""Independent raw sums and chronological selections for every issue history.

No operating-history, vintage-builder or traffic-estimator functions are imported.
Repeated records in overlapping rolling releases are source versions, not new
independent economic observations.
"""
import argparse
from collections import defaultdict
from datetime import datetime
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
from scripts.audit_monthly_traffic import number, raw_fields, region, SERIES


def release_metadata(source):
    name = source["source_url"].rsplit("/", 1)[-1].upper()
    fields = name.split(".")
    scope = "DD" if "DD" in fields else "FD" if "FD" in fields else None
    position = fields.index("WAC")
    first, last = fields[position + 1:position + 3]
    published = datetime.strptime(fields[-2], "%d%b%Y").date().isoformat()
    if scope is None or not re.fullmatch(r"REL\d{2}", fields[-3]):
        raise ValueError("Independent archive metadata differs")
    return {"scope": scope, "first": first[:4] + "-" + first[4:], "last": last[:4] + "-" + last[4:],
        "published_on": published, "release_number": int(fields[-3][3:]), "source_sha256": source["sha256"]}


def select_releases(metadata, cutoff, totals):
    grouped = defaultdict(list)
    for source in metadata:
        if source["published_on"] > cutoff: continue
        for index in range(number(source["first"]), number(source["last"]) + 1):
            period = f"{index//12:04d}-{index%12+1:02d}"
            if not "2013-01" <= period <= "2023-12": continue
            grouped[source["scope"], period].append(source)
    selected = {}
    for key, candidates in grouped.items():
        rank = max((s["published_on"], s["release_number"]) for s in candidates)
        tied = sorted((s for s in candidates if (s["published_on"], s["release_number"]) == rank), key=lambda s:s["source_sha256"])
        if len(tied) > 1:
            signatures = {digest([[*k, v[0], v[1], sorted(v[2])] for k,v in sorted(totals[s["source_sha256"]].items()) if k[2] == key[1]]) for s in tied}
            if len(signatures) != 1: raise ValueError("Independent tied release quantities conflict")
        winner = tied[0]
        selected[key] = {"scope": key[0], "period": key[1], "source_sha256": winner["source_sha256"], "published_on": winner["published_on"],
            "equivalent_source_sha256s": [s["source_sha256"] for s in tied]}
    return selected


def raw_sums(sources, raw_root=None, progress=None):
    totals, records = {}, 0
    for source in sources:
        content, _ = read_source(source["sha256"], raw_root) if raw_root is not None else read_source(source["sha256"])
        meta = release_metadata(source)
        sums = defaultdict(lambda: [0, 0, set()])
        with ZipFile(BytesIO(content)) as archive:
            member = next(n for n in archive.namelist() if n.lower().endswith(".asc"))
            with archive.open(member) as binary:
                for text in TextIOWrapper(binary, encoding="utf-8-sig"):
                    if not text.strip(): continue
                    fields = raw_fields(text)
                    if fields[10] not in ("UA", "DL", "AA") or fields[14] != "F" or fields[17] not in ("1", "3"): continue
                    period = f"{int(fields[0]):04d}-{int(fields[1]):02d}"
                    if not "2013-01" <= period <= "2023-12": continue
                    market = region(int(fields[4]), int(fields[8]))
                    if (fields[10], market) not in SERIES: continue
                    legacy = len(fields) == 32
                    seats = sum(int(fields[i]) for i in (21, 22, 23)) if legacy else int(fields[21])
                    passengers = sum(int(fields[i]) for i in (24, 25, 26)) if legacy else int(fields[22])
                    distance = int(fields[13])
                    sums[fields[10], market, period][0] += passengers * distance
                    sums[fields[10], market, period][1] += seats * distance
                    sums[fields[10], market, period][2].add(fields[11]); records += 1
        totals[source["sha256"]] = sums
        if progress: progress({"archives": len(totals), "raw_segment_versions": records})
    return totals, records


def issued_totals(totals, releases):
    sums = defaultdict(lambda: [0, 0, set(), set(), None])
    for (scope, period), release in releases.items():
        sha = release["source_sha256"]
        if ("FD" if scope == "DD" else "DD", period) not in releases: continue
        for key, values in totals[sha].items():
            if key[2] != period: continue
            aggregate = sums[key]
            aggregate[0] += values[0]; aggregate[1] += values[1]
            aggregate[2] |= values[2]; aggregate[3].add(sha)
    for key, aggregate in sums.items():
        aggregate[4] = max(releases[scope, key[2]]["published_on"] for scope in ("DD", "FD"))
    return {k: v for k, v in sums.items() if 0 < v[0] <= v[1]}


def audit(artifact, spec, totals):
    checks, revisions, origin_summary, previous = 0, [], [], {}
    def check(condition, name):
        nonlocal checks
        if not condition: raise ValueError("Independent traffic vintage audit failed: " + name)
        checks += 1
    metadata = [release_metadata(s) for s in spec["sources"]]
    versions = artifact["observation_versions"]
    for identity, row in versions.items(): check(digest(row) == identity, "version content identity")
    def inspect(rows, expected, releases, issue):
        actual = {(r["carrier"], r["market"], r["period"]): r for r in rows}
        check(len(actual) == len(rows) and set(actual) == set(expected), "exact raw target keys")
        for key, values in expected.items():
            row = actual[key]
            check(row["rpm_million"] == values[0] / 1e6, "original RPM sum")
            check(row["asm_million"] == values[1] / 1e6, "original ASM sum")
            check(row["carrier_entities"] == sorted(values[2]) and row["source_sha256s"] == sorted(values[3]), "source/entities")
            check(row["published_on"] == values[4] and values[4] <= issue, "complete-scope release gate")
            check(row["scope_releases"] == [releases[s, key[2]] for s in ("DD", "FD")], "both selected releases")
        return actual
    for origin in artifact["origins"]:
        issue = origin["issue_on"]
        releases = select_releases(metadata, issue, totals)
        check(origin["selected_releases"] == [releases[k] for k in sorted(releases)], "chronological latest-scope selection")
        expected = issued_totals(totals, releases)
        rows = [versions[k] for k in origin["observation_ids"]]
        current = inspect(rows, expected, releases, issue)
        newest = max(k[2] for k in expected) if expected else None
        lag = number(issue[:7]) - number(newest) if newest else None
        check(origin["newest_complete_period"] == newest and origin["information_lag_months"] == lag, "actual information lag")
        for c in ("UA", "DL", "AA"):
            check(origin["observations_by_carrier"][c] == sum(k[0] == c for k in current), "carrier counts")
        for key in sorted(current.keys() & previous.keys()):
            if (current[key]["rpm_million"], current[key]["asm_million"]) != (previous[key]["rpm_million"], previous[key]["asm_million"]):
                revisions.append({"issue_on": issue, "carrier": key[0], "market": key[1], "period": key[2],
                    "previous_rpm_million": previous[key]["rpm_million"], "revised_rpm_million": current[key]["rpm_million"],
                    "previous_asm_million": previous[key]["asm_million"], "revised_asm_million": current[key]["asm_million"]})
        series = [{"carrier": c, "market": m, "observations": sum(k[:2] == (c, m) for k in current),
            "newest_period": max((k[2] for k in current if k[:2] == (c, m)), default=None)} for c, m in SERIES]
        origin_summary.append({"issue_on": issue, "information_lag_months": lag, "series": series})
        previous = current
    truth_releases = select_releases(metadata, spec["acquisition_cutoff"], totals)
    inspect(artifact["evaluation_targets"]["observations"], issued_totals(totals, truth_releases), truth_releases, spec["acquisition_cutoff"])
    return {"checks_passed": checks, "revision_events": revisions, "origins": origin_summary,
        "scope": "Independent original archive sums and every chronological release selection/history/target. Current-download reconstruction; not point-in-time forecast accuracy or independent economic sample count."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    root = ROOT / "data/traffic_vintages"
    target = args.snapshot or root / json.loads((root / "latest.json").read_text())["snapshot_id"]
    manifest = json.loads((target / "manifest.json").read_text())
    artifact = json.loads((target / "artifact.json").read_text())
    if digest(artifact) != manifest["artifact_digest"] or digest({k:v for k,v in manifest.items() if k != "snapshot_id"}) != target.name:
        raise ValueError("Independent vintage content checksum differs")
    design = json.loads((root / "designs" / manifest["design_id"] / "design.json").read_text())
    if digest(design) != manifest["design_id"]: raise ValueError("Independent vintage design checksum differs")
    spec = design["spec"]
    totals, count = raw_sums(spec["sources"], progress=lambda event: print(json.dumps(event),file=sys.stderr,flush=True))
    result = {"snapshot_id": target.name, "artifact_digest": manifest["artifact_digest"], "source_archives": len(totals),
        "raw_segment_versions": count, "audit_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), **audit(artifact, spec, totals)}
    output = ROOT / "data/traffic_vintage_checks"; output.mkdir(exist_ok=True)
    path = output / (digest(result) + ".json")
    content = json.dumps(result,indent=2,allow_nan=False)+"\n"
    if path.exists() and path.read_text() != content: raise ValueError("Immutable vintage audit differs")
    if not path.exists(): path.write_text(content)
    print(json.dumps({"audit_file":str(path), "checks_passed":result["checks_passed"],"source_archives":len(totals),
        "raw_segment_versions":count,"quantity_revision_events":len(result["revision_events"])},indent=2))


if __name__ == "__main__": main()
