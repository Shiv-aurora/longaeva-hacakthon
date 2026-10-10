"""Build app/static/data/signal.json: the third-quarter estimate marked to fuel each day, and the simulator's next year.

Run it after scripts/lab/build_track_record.py and scripts/lab/build_q3_call.py.
It reads the committed inputs, the published track record (for the chosen
variant) and the frozen call (to confirm the last day matches it), and runs the
business simulator once from today's fuel price. No network. For fixed inputs
the output is identical apart from `generated_at`.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab import forward_call, outlook


def write(payload: dict, path: Path) -> None:
    outlook.validate(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, allow_nan=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=outlook.OUTPUT)
    parser.add_argument("--track-record", type=Path, default=forward_call.TRACK_RECORD)
    parser.add_argument("--q3-call", type=Path, default=forward_call.OUTPUT)
    parser.add_argument("--no-forward", action="store_true", help="skip the simulator run")
    parser.add_argument("--generated-at", default=None, help="fixed UTC timestamp, for reproducible bytes")
    args = parser.parse_args()
    payload = outlook.build(track_record=args.track_record, q3_call=args.q3_call, generated_at=args.generated_at,
                            forward=not args.no_forward)
    write(payload, args.out)
    first, last = payload["daily"][0], payload["daily"][-1]
    print(json.dumps({"out": str(args.out), "as_of": payload["as_of"], "headline": payload["headline"], "days": len(payload["daily"]),
                      "first": first, "last": last, "settled_from": payload["settled_from"], "frozen_call": payload["frozen_call"],
                      "forward": payload["forward"].get("status")}, indent=2))


if __name__ == "__main__":
    main()
