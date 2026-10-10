"""Import preserved official T2 exports and build same-table capacity targets."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.t2_history import build_t2_history, preserve_export
from app.reporting_panel import latest_snapshot, FORM41
from app.evidence import digest


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--downloads", type=Path, default=ROOT / "data/t2_downloads.json")
    p.add_argument("--form41", type=Path)
    p.add_argument("--from-snapshot", type=Path, help="Rebuild using preserved source hashes and classification; no Downloads dependency")
    p.add_argument("--cutoff", default="2026-10-09")
    p.add_argument("--classification", type=Path, default=ROOT / "data/t2_aircraft_classification.json")
    args = p.parse_args()
    if args.from_snapshot:
        m = json.loads((args.from_snapshot / "manifest.json").read_text())
        if digest({k:v for k,v in m.items() if k!="snapshot_id"}) != m["snapshot_id"]: raise ValueError("Source snapshot manifest differs")
        source=m["source"]; classes=m["classification"]
        form41=args.form41 or FORM41/m["form41_snapshot_id"]
    else:
        source=preserve_export(json.loads(args.downloads.read_text())); classes=args.classification
        form41=args.form41 or latest_snapshot(FORM41)
    result = build_t2_history(source, form41, classes, args.cutoff)
    print(json.dumps({k: result[k] for k in ("snapshot_id", "path", "scanned_rows", "retained_rows", "coverage", "reconciliation_counts")}, indent=2))


if __name__ == "__main__": main()
