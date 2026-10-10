"""Build app/static/data/range_check.json: is the simulator's range the right width, and what sets it?

It reads data/lab/ual_quarterly.csv and data/lab/fuel_daily.csv and runs the business
simulator as the lab runner runs it (so app/static/data/fare_clock.json must be built).
No network. For fixed inputs the output is identical apart from `generated_at`. The lab
runner keeps the engine's assumed demand noise and quotes this file's `runner` block in
its assumptions, so rebuild app/static/data/signal.json when that block changes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab import range_check


def write(payload: dict, path: Path) -> None:
    range_check.validate(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, allow_nan=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=range_check.OUTPUT)
    parser.add_argument("--quarterly", type=Path, default=range_check.QUARTERLY)
    parser.add_argument("--generated-at", default=None, help="fixed UTC timestamp, for reproducible bytes")
    args = parser.parse_args()
    payload = range_check.build(quarterly=args.quarterly, generated_at=args.generated_at)
    write(payload, args.out)
    check = payload["check"]
    print(json.dumps({"out": str(args.out), "as_of": payload["as_of"], "headline": payload["headline"],
                      "check": {which: {k: check[which][k] for k in ("demand_monthly_log_sd", "inside", "of", "share")} for which in ("assumed", "fitted")},
                      "passes": check["passes"], "kept": check["kept"], "width": payload["simulator"]["width"]}, indent=2))


if __name__ == "__main__":
    main()
