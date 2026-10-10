"""Join verified preserved BTS/Form 41 snapshots without network access."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.reporting_panel import build_reporting_panel, latest_snapshot, OPERATING, FORM41


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--operating", type=Path, help="Explicit operating snapshot directory")
    parser.add_argument("--form41", type=Path, help="Explicit Form 41 snapshot directory")
    args = parser.parse_args()
    result = build_reporting_panel(args.operating or latest_snapshot(OPERATING), args.form41 or latest_snapshot(FORM41))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
