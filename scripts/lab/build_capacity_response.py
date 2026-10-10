"""Build app/static/data/capacity_response.json: how United's capacity moves after fuel prices change.

Reads the tidy inputs under data/lab (daily jet fuel, United's and Delta's
quarterly tables) and writes the output the pages and the simulator load. No
network. For fixed inputs and seed the output is identical apart from
`generated_at`.

`--no-test` withholds whatever uses 2024-2026 quarters; use it while changing
the code. The published file is built once without it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab import capacity_response, market_data


def write(payload: dict, path: Path) -> None:
    capacity_response.validate(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, allow_nan=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=capacity_response.SEED)
    parser.add_argument("--draws", type=int, default=capacity_response.N_DRAWS, help="joint draws of the curve (at least 500)")
    parser.add_argument("--fuel", type=Path, default=market_data.FUEL_CSV)
    parser.add_argument("--ual", type=Path, default=capacity_response.UAL_CSV)
    parser.add_argument("--dal", type=Path, default=capacity_response.DAL_CSV)
    parser.add_argument("--out", type=Path, default=capacity_response.OUTPUT)
    parser.add_argument("--no-test", action="store_true", help="withhold the locked test years")
    parser.add_argument("--generated-at", default=None, help="fixed UTC timestamp, for reproducible bytes")
    args = parser.parse_args()
    payload = capacity_response.build(seed=args.seed, n_draws=args.draws, fuel_path=args.fuel, ual_path=args.ual,
                                      dal_path=args.dal, evaluate_test=not args.no_test, generated_at=args.generated_at)
    write(payload, args.out)
    print(json.dumps({"out": str(args.out), "as_of": payload["as_of"], "n": payload["n"],
                      "test_split": payload["test_split"], "headline": payload["headline"],
                      "primary_spec": payload["primary_spec"]["id"],
                      "curve": [{k: row[k] for k in ("quarters_after", "p10", "p50", "p90")} for row in payload["curve"]],
                      "inputs": payload["inputs"]}, indent=2))


if __name__ == "__main__":
    main()
