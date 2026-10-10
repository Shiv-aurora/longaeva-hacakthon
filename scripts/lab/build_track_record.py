"""Build app/static/data/track_record.json: the behaviour chain replayed on past guided quarters.

Reads the tidy inputs under data/lab (guidance records, United's quarterly table,
daily jet fuel, daily prices, the literature file, the Delta check) and the fare
clock output, and writes the file the Reality check page loads. No network. For
fixed inputs and seed the output is identical apart from `generated_at`.

`--no-test` never loads a quarter from 2024 onward; use it while changing the
code. The method is chosen on the quarters before 2024 and is the same with and
without the flag. The published file is built once without it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab import track_record


def write(payload: dict, path: Path) -> None:
    track_record.validate(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, allow_nan=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=track_record.SEED)
    parser.add_argument("--out", type=Path, default=track_record.OUTPUT)
    parser.add_argument("--no-test", action="store_true", help="withhold the locked test years (2024 onward)")
    parser.add_argument("--generated-at", default=None, help="fixed UTC timestamp, for reproducible bytes")
    args = parser.parse_args()
    if args.no_test and args.out.resolve() == track_record.OUTPUT.resolve():
        parser.error("--no-test must not overwrite the published file; pass --out")
    payload = track_record.build(evaluate_test=not args.no_test, seed=args.seed, generated_at=args.generated_at)
    write(payload, args.out)
    print(json.dumps({"out": str(args.out), "as_of": payload["as_of"], "test_split": payload["test_split"],
                      "headline": payload["headline"], "chosen": payload["selection"]["chosen"],
                      "selection": payload["selection"]["table"], "scores_by_split": payload["scores_by_split"]}, indent=2))


if __name__ == "__main__":
    main()
