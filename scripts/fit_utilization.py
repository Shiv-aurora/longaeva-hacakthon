"""Fit frozen-training utilization candidates and score the declared selection set."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.utilization import build_utilization, latest_snapshot, FORM41


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--form41", type=Path)
    parser.add_argument("--classification", type=Path, default=ROOT / "data/aircraft_classification.json")
    parser.add_argument("--specification", type=Path, default=ROOT / "data/utilization_spec.json")
    args = parser.parse_args()
    result = build_utilization(args.form41 or latest_snapshot(FORM41), args.classification, args.specification)
    summary = {k: v for k, v in result.items() if k != "scorecard"}
    summary["scorecard"] = [{k: v for k, v in row.items() if k != "series_horizon_scores"} for row in result["scorecard"]]
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
