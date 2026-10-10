"""Build all declared issue histories from preserved rolling BTS downloads."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.operating_vintages import build_vintages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", type=Path, default=ROOT / "data/traffic_vintage_sources.json")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    print(json.dumps(build_vintages(json.loads(args.sources.read_text()), workers=args.workers), indent=2))


if __name__ == "__main__":
    main()
