"""Preserve official BTS downloads and build monthly scheduled-traffic history."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.operating_history import build_operating_history, preserve_archive, WAC_URL
from app.source_store import fetch_https, preserve_source


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--downloads", type=Path, help="JSON list of official source hrefs and local archive paths")
    group.add_argument("--sources", type=Path, help="Saved BTS manifest; rebuild without network access")
    parser.add_argument("--cutoff", default="2026-10-09")
    parser.add_argument("--wac-sha", help="Preserved official WAC lookup; avoids its download")
    args = parser.parse_args()
    if args.sources:
        saved = json.loads(args.sources.read_text())
        sources, wac = saved["sources"], saved["wac_source"]
    else:
        from app.source_store import read_source
        sources = []
        for entry in json.loads(args.downloads.read_text()):
            url = entry["href"]
            if url.startswith("/"):
                url = "https://www.bts.gov" + url
            sources.append(preserve_archive(Path(entry["path"]), url))
        if args.wac_sha:
            _, wac = read_source(args.wac_sha)
        else:
            wac = preserve_source(fetch_https(WAC_URL), WAC_URL, "text/csv")
    result = build_operating_history(sources, wac, args.cutoff)
    print(json.dumps({k: result[k] for k in ("snapshot_id", "path", "coverage", "vintage_status", "boundary")}, indent=2))


if __name__ == "__main__":
    main()
