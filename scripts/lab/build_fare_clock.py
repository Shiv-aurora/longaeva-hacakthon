"""Build app/static/data/fare_clock.json: the recovery curve, its draws and the industry fare elasticities.

Reads the tidy inputs under data/lab (monthly CPI, daily jet fuel, United's
quarterly earnings-release table, the verified literature file) and writes the
output the pages and the simulator load. No network. For fixed inputs and seed
the output is identical apart from `generated_at`. Without the United quarterly
file only the industry part runs and the United fields are null with the reason.

`--no-test` withholds every number from the locked test years (2024-2026); use
it while changing the code. The published file is built once without it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab import fare_clock, market_data


def write(payload: dict, path: Path) -> None:
    fare_clock.validate(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, allow_nan=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=fare_clock.SEED)
    parser.add_argument("--draws", type=int, default=fare_clock.N_DRAWS, help="joint draws of the curve (at least 500)")
    parser.add_argument("--cpi", type=Path, default=market_data.CPI_CSV)
    parser.add_argument("--fuel", type=Path, default=market_data.FUEL_CSV)
    parser.add_argument("--ual", type=Path, default=fare_clock.UAL_CSV)
    parser.add_argument("--literature", type=Path, default=fare_clock.LITERATURE_JSON)
    parser.add_argument("--out", type=Path, default=fare_clock.OUTPUT)
    parser.add_argument("--no-test", action="store_true", help="withhold the locked test split")
    parser.add_argument("--generated-at", default=None, help="fixed UTC timestamp, for reproducible bytes")
    args = parser.parse_args()
    payload = fare_clock.build(seed=args.seed, n_draws=args.draws, cpi_path=args.cpi, fuel_path=args.fuel,
                               ual_path=args.ual, literature_path=args.literature,
                               evaluate_test=not args.no_test, generated_at=args.generated_at)
    write(payload, args.out)
    print(json.dumps({"out": str(args.out), "as_of": payload["as_of"], "n": payload["n"],
                      "united": payload["united_status"], "test_split": payload["test_split"],
                      "headline": payload["headline"], "curve": payload["curve"] and [
                          {k: row[k] for k in ("quarters_after", "p10", "p50", "p90")} for row in payload["curve"]],
                      "inputs": payload["inputs"]}, indent=2))


if __name__ == "__main__":
    main()
