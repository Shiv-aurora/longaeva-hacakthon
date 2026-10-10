"""Preserve inspected complete Form 41 exports, or rebuild offline."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.form41_history import build_form41_history, preserve_export


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--downloads", type=Path, help="Acquisition catalog with official URLs, complete filters and local files")
    group.add_argument("--sources", type=Path, help="Saved manifest for network-free rebuild")
    parser.add_argument("--cutoff", default="2026-10-09")
    parser.add_argument("--start-year", type=int, default=2013)
    args = parser.parse_args()
    sources = json.loads(args.sources.read_text())["sources"] if args.sources else [preserve_export(e) for e in json.loads(args.downloads.read_text())]
    result = build_form41_history(sources, args.cutoff, start_year=args.start_year)
    print(json.dumps({k: result[k] for k in ("snapshot_id", "path", "coverage", "checks", "vintage_status", "boundary")}, indent=2))


if __name__ == "__main__":
    main()
