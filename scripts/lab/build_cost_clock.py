"""Build app/static/data/cost_clock.json: how spot jet fuel reaches United's reported price per gallon.

Reads the tidy inputs under data/lab (daily jet fuel, United's and Delta's
quarterly tables, the verified literature file, the Delta check) and writes the
output the pages, the simulator and the daily estimate load. No network. For
fixed inputs and seed the output is identical apart from `generated_at`.

`--no-test` withholds the model's errors on 2024-2026 and everything estimated
from those years; use it while changing the code. The published file is built once without it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab import cost_clock, market_data


def write(payload: dict, path: Path) -> None:
    cost_clock.validate(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, allow_nan=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=cost_clock.SEED)
    parser.add_argument("--draws", type=int, default=cost_clock.N_DRAWS, help="predictive draws per range")
    parser.add_argument("--fuel", type=Path, default=market_data.FUEL_CSV)
    parser.add_argument("--ual", type=Path, default=cost_clock.UAL_CSV)
    parser.add_argument("--dal", type=Path, default=cost_clock.DAL_CSV)
    parser.add_argument("--literature", type=Path, default=cost_clock.LITERATURE_JSON)
    parser.add_argument("--dal-check", type=Path, default=cost_clock.DAL_CHECK_JSON)
    parser.add_argument("--out", type=Path, default=cost_clock.OUTPUT)
    parser.add_argument("--no-test", action="store_true", help="withhold the locked test years")
    parser.add_argument("--generated-at", default=None, help="fixed UTC timestamp, for reproducible bytes")
    args = parser.parse_args()
    payload = cost_clock.build(seed=args.seed, n_draws=args.draws, fuel_path=args.fuel, ual_path=args.ual,
                               dal_path=args.dal, literature_path=args.literature, dal_check_path=args.dal_check,
                               evaluate_test=not args.no_test, generated_at=args.generated_at)
    write(payload, args.out)
    block = payload["predicted_2026q3"]
    print(json.dumps({"out": str(args.out), "as_of": payload["as_of"], "test_split": payload["test_split"],
                      "headline": payload["headline"], "fit": {k: payload["fit"][k] for k in
                                                                ("spec", "lag_days", "basis_usd_gal", "slope", "ci", "r2", "n")},
                      "by_split": {k: v and {"mae": v["mae"], "n": v["n"]} for k, v in payload["by_split"].items()},
                      "predicted_2026q3": {k: block[k] for k in ("p10", "p50", "p90", "complete")},
                      "inputs": payload["inputs"]}, indent=2))


if __name__ == "__main__":
    main()
