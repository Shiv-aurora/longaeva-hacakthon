"""Build app/static/data/q3_call.json and freeze the forecast in data/lab/q3_call_frozen.json.

Run it once, after scripts/lab/build_track_record.py and before United reports.
It reads the committed inputs and the published track record, computes the
forecast block, writes the block's canonical bytes to the frozen file and the
page's file around it. No network.

A frozen forecast is never replaced by accident: if the frozen file exists the
script stops, unless `--refreeze` is passed. Scoring after the report is a
separate script (scripts/lab/score_q3_call.py) that does not touch the block.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab import forward_call


def freeze(out: Path, frozen: Path, refreeze: bool = False, **options) -> dict:
    """Compute, freeze and write the forecast. Raises FileExistsError rather than replace a frozen forecast."""
    if frozen.exists() and not refreeze:
        raise FileExistsError(f"A frozen forecast already exists at {frozen}. It was not changed. "
                              "Pass --refreeze to replace it with a new forecast.")
    generated_at = options.pop("generated_at", None)
    block = forward_call.forecast_block(**options)
    payload = forward_call.wrap(block, generated_at=generated_at)
    frozen.parent.mkdir(parents=True, exist_ok=True)
    frozen.write_bytes(forward_call.canonical(block))
    forward_call.validate(payload, frozen)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, allow_nan=False) + "\n")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=forward_call.OUTPUT)
    parser.add_argument("--frozen", type=Path, default=forward_call.FROZEN)
    parser.add_argument("--track-record", type=Path, default=forward_call.TRACK_RECORD)
    parser.add_argument("--meta", type=Path, default=forward_call.META, help="meta.json with the outside comparators")
    parser.add_argument("--refreeze", action="store_true", help="replace an existing frozen forecast")
    parser.add_argument("--seed", type=int, default=forward_call.tr.SEED)
    parser.add_argument("--frozen-at", default=None, help="fixed UTC timestamp inside the block, for reproducible bytes")
    parser.add_argument("--generated-at", default=None, help="fixed UTC timestamp, for reproducible bytes")
    parser.add_argument("--no-cross-check", action="store_true", help="skip the simulator cross-check")
    args = parser.parse_args()
    try:
        payload = freeze(args.out, args.frozen, args.refreeze, track_record=args.track_record, meta=args.meta, seed=args.seed,
                         frozen_at=args.frozen_at, generated_at=args.generated_at, cross_check=not args.no_cross_check)
    except FileExistsError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    block = payload["forecast"]
    print(json.dumps({"out": str(args.out), "frozen": str(args.frozen), "sha256": payload["sha256"],
                      "frozen_at": payload["frozen_at"], "data_cutoff": payload["data_cutoff"], "variant": block["method"]["variant"],
                      "headline": payload["headline"],
                      "chain": [{k: link.get(k) for k in ("link", "p10", "p50", "p90", "management")} for link in block["chain"]],
                      "odds": {k: v for k, v in block["odds"].items() if not isinstance(v, str)},
                      "baseline_calls": block["scoring"]["baseline_calls"]}, indent=2))


if __name__ == "__main__":
    main()
