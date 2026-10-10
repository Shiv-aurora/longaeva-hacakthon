"""Independently sum selected raw T-100 lines; no importer parser or join code."""
from collections import defaultdict
from io import BytesIO
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
    identity = json.loads((ROOT / "data/reporting_panel/latest.json").read_text())["snapshot_id"]
    joined = ROOT / "data/reporting_panel" / identity
    manifest = json.loads((joined / "manifest.json").read_text())
    panel = json.loads((joined / "panel.json").read_text())
    if digest(panel) != manifest["panel_digest"]:
        raise ValueError("Reporting panel checksum failed")
    operating = ROOT / "data/operating_history" / manifest["upstream_snapshots"]["operating"]
    selected = json.loads((operating / "panel.json").read_text())["selected_releases"]
    periods = {"2013-Q1", "2019-Q4", "2023-Q4", "2026-Q2"}
    carriers = {"UA", "DL", "AA", "US"}
    releases = {(r["scope"], r["period"]): r["source_sha256"] for r in selected}
    sources = {r["source_sha256"] for r in selected if f"{r['period'][:4]}-Q{(int(r['period'][5:])-1)//3+1}" in periods}
    totals = defaultdict(lambda: [0, 0, 0, 0])
    witnesses = []
    for sha in sorted(sources):
        content, _ = read_source(sha)
        with ZipFile(BytesIO(content)) as archive:
            member = next(n for n in archive.namelist() if n.lower().endswith(".asc"))
            scope = "DD" if ".dd." in member.lower() or member.lower().startswith("dd.") else "FD"
            for line_number, text in enumerate(archive.read(member).decode("utf-8-sig").splitlines(), 1):
                if not text.strip():
                    continue
                fields = text.split("|")
                if len(fields) in (29, 33) and not fields[-1]:
                    fields.pop()  # A trailing separator is present in some exports.
                if len(fields) not in (28, 32):
                    raise ValueError("Unexpected audit source layout")
                month = f"{int(fields[0]):04d}-{int(fields[1]):02d}"
                period = f"{month[:4]}-Q{(int(month[5:])-1)//3+1}"
                if period not in periods or releases.get((scope, month)) != sha or fields[10] not in carriers or fields[14] != "F" or fields[17] not in {"1", "3"}:
                    continue
                legacy = len(fields) == 32
                seats = sum(int(fields[i]) for i in (21, 22, 23)) if legacy else int(fields[21])
                passengers = sum(int(fields[i]) for i in (24, 25, 26)) if legacy else int(fields[22])
                distance = int(fields[13])
                key = (fields[10], fields[11], period)
                value = (seats * distance, passengers * distance, int(fields[30 if legacy else 26]), 1)
                totals[key] = [a+b for a, b in zip(totals[key], value)]
                if len(witnesses) < 12:
                    witnesses.append({"source_sha256": sha, "member": member, "line": line_number, "text": text})
    comparisons = []
    for row in panel["entity_quarters"]:
        key = (row["unique_carrier"], row["reporting_entity"], row["period"])
        if key[0] not in carriers or key[2] not in periods or not row["traffic_source_legs"]:
            continue
        expected = [row["traffic"][metric] for metric in ("asm", "rpm", "airborne_minutes", "segment_records")]
        if totals.pop(key, None) != expected:
            raise ValueError(f"Independent raw traffic sum differs: {key}")
        comparisons.append({"carrier": key[0], "entity": key[1], "period": key[2], "asm": expected[0], "rpm": expected[1], "airborne_minutes": expected[2], "segment_records": expected[3]})
    if totals:
        raise ValueError("Raw traffic entities were lost from the reporting panel")
    result = {"reporting_snapshot": identity, "periods": sorted(periods), "source_sha256s": sorted(sources),
        "comparisons": comparisons, "raw_line_witnesses": witnesses,
        "audit_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "Source alignment/accounting check only; 2024+ observations remain locked for forecast scoring."}
    target = ROOT / "data/reporting_panel_checks"
    target.mkdir(exist_ok=True)
    path = target / f"{identity}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"checked_entity_quarters": len(comparisons), "raw_segment_records": sum(r["segment_records"] for r in comparisons), "source_archives": len(sources), "path": str(path)}))


if __name__ == "__main__":
    main()
