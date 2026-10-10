"""Download public SEC facts or rebuild offline from a preserved raw manifest."""
from pathlib import Path
import argparse
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.financial_history import build_history, download_company_facts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--entities", nargs="+", choices=("UAL", "DAL", "AAL"), default=["UAL", "DAL", "AAL"])
    parser.add_argument("--cutoff", default="2026-10-09")
    parser.add_argument("--sources", type=Path, help="Existing historical manifest for offline rebuild")
    parser.add_argument("--user-agent", default="UAL Counterfactual Lab research")
    args = parser.parse_args()
    if args.sources:
        sources = json.loads(args.sources.read_text())["sources"]
    else:
        sources = []
        for entity in dict.fromkeys(args.entities):
            if sources:
                time.sleep(1)  # Below the SEC's request-rate ceiling.
            sources.append(download_company_facts(entity, user_agent=args.user_agent))
    result = build_history(sources, args.cutoff)
    print(json.dumps({k: result[k] for k in ("snapshot_id", "path", "coverage", "vintage_status", "boundary")}, indent=2))


if __name__ == "__main__":
    main()
