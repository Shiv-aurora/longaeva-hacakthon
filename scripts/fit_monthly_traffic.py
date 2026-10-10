"""Build the declared release-aware monthly state-space traffic study."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.monthly_traffic import build_traffic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operating", type=Path)
    parser.add_argument("--spec", type=Path, default=ROOT / "data/monthly_traffic_spec.json")
    args = parser.parse_args()
    source = args.operating
    if source is None:
        root = ROOT / "data/operating_history"
        source = root / json.loads((root / "latest.json").read_text())["snapshot_id"]
    result = build_traffic(source, args.spec, progress=lambda event: print(json.dumps(event), file=sys.stderr, flush=True))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
