"""Build app/static/data/market_clock.json from the tidy daily fuel and price files.

Two stages, in this order. Stage 1 cuts the data at the end of the select split,
computes every variant, picks the trading rule and writes the frozen
specification to the output file. Stage 2 computes the locked test split once
for that specification and replaces the file with the full result. `--stage
select` stops after stage 1 and never reads a number from the test split, which
is how the specification was developed. No network.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab import market_clock as mc
from app.lab.market_data import FUEL_CSV, PRICES_CSV
from app.source_store import byte_hash

OUT = ROOT / "app" / "static" / "data" / "market_clock.json"


def describe_inputs(paths) -> list[dict]:
    out = []
    for path in paths:
        path = Path(path).resolve()
        shown = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.as_posix()
        out.append({"path": shown, "sha256": byte_hash(path.read_bytes())})
    return out


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(json.dumps(document, indent=1, allow_nan=False) + "\n")
    os.replace(partial, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fuel", type=Path, default=FUEL_CSV)
    parser.add_argument("--prices", type=Path, default=PRICES_CSV)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--seed", type=int, default=mc.DEFAULT_SEED)
    parser.add_argument("--n-boot-events", type=int, default=5000)
    parser.add_argument("--n-boot-block", type=int, default=2000)
    parser.add_argument("--stage", choices=("select", "full"), default="full",
                        help="select: freeze the specification on fit and select and stop; full: then compute the test split once")
    parser.add_argument("--generated-at", help="UTC ISO timestamp to record (default: now)")
    args = parser.parse_args()
    generated_at = args.generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inputs = describe_inputs([args.fuel, args.prices])
    panel = mc.load_panel(args.fuel, args.prices)
    frozen = mc.freeze(panel, mc.SPLITS, args.seed, args.n_boot_events, args.n_boot_block)
    pending = mc.pending_document(frozen, inputs, generated_at)
    if args.stage == "select":
        pending["fit_and_select"] = mc._only(frozen["early"], ("fit", "select"))
        pending["variants_detail"] = mc._clean(frozen["explored"]["variants"])
        write_json(args.out, pending)
        print(json.dumps({"out": str(args.out), "stage": "select", "spec_sha256": frozen["spec_sha256"],
                          "chosen_rule": frozen["explored"]["chosen_rule"], "variants": len(pending["variants_tried"])}, indent=2))
        return
    write_json(args.out, pending)   # the specification is on disk before the test split is computed
    document = mc.finish(panel, frozen, inputs, generated_at)
    problems = mc.validate(document)
    if problems or document["spec_sha256"] != pending["spec_sha256"]:
        raise SystemExit("market_clock.json does not conform: " + "; ".join(problems or ["specification changed after freezing"]))
    write_json(args.out, document)
    print(json.dumps({"out": str(args.out), "stage": "full", "as_of": document["as_of"], "spec_sha256": document["spec_sha256"],
                      "headline": document["headline"], "plain_summary": document["plain_summary"],
                      "variants": len(document["variants_tried"])}, indent=2))


if __name__ == "__main__":
    main()
