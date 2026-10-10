"""Build a separately labeled correction from verified original monthly records."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.traffic_corrections import build_reviewed


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--spec", type=Path, default=ROOT / "data/traffic_correction_spec.json")
    args = parser.parse_args()
    print(json.dumps(build_reviewed(json.loads(args.spec.read_text()), args.workers,
        progress=lambda event: print(json.dumps(event), file=sys.stderr, flush=True)), indent=2))


if __name__ == "__main__": main()
