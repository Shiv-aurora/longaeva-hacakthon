"""Score the frozen third-quarter forecast once United has reported.

Fills `outcome` and `scores` in app/static/data/q3_call.json. The `forecast` block
is not touched: the script checks it against its SHA-256 and against
data/lab/q3_call_frozen.json before and after, and stops if either differs.

Give the reported figures on the command line:

  python scripts/lab/score_q3_call.py --eps 2.91 --reported-on 2026-10-20 \
      --fuel-price 3.95 --trasm-yoy 0.14 --source https://www.sec.gov/...

or, once data/lab/ual_quarterly.csv holds the quarter, read them from it:

  python scripts/lab/score_q3_call.py --from-quarterly

A consensus estimate that was public before the report may be added with
`--consensus`, `--consensus-as-of` and `--consensus-url`; it is recorded beside
the outcome as supplied after the freeze. No network.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab import filings, forward_call


def reported(period: str, path: Path) -> dict:
    """The reported quarter and its year-ago unit revenue from the quarterly table."""
    rows = {row["quarter"]: row for row in filings.load_ual_quarterly(path)}
    year_ago = f"{int(period[:4]) - 1}{period[4:]}"
    if period not in rows or rows[period]["adj_eps_diluted"] is None:
        raise SystemExit(f"{path} has no adjusted EPS for {period} yet")
    row, before = rows[period], rows.get(year_ago)
    trasm = None
    if row["trasm_cents"] is not None and before and before["trasm_cents"]:
        trasm = row["trasm_cents"] / before["trasm_cents"] - 1.0
    return {"actual_eps": row["adj_eps_diluted"], "reported_on": row["filed_on"].isoformat(),
            "fuel_price": row["fuel_price_usd_gal"], "trasm_yoy": trasm,
            "source": f"{path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name}, accession {row['accession']}"}


def run(call: Path, frozen: Path, **figures) -> dict:
    payload = json.loads(call.read_text())
    block_bytes = forward_call.canonical(payload["forecast"])
    if frozen.read_bytes() != block_bytes:
        raise SystemExit(f"{frozen} does not match the forecast block in {call}; nothing was written")
    scored = forward_call.score(payload, **figures)
    if forward_call.canonical(scored["forecast"]) != block_bytes or frozen.read_bytes() != block_bytes:
        raise SystemExit("the forecast block changed while scoring; nothing was written")
    forward_call.validate(scored, frozen)
    call.write_text(json.dumps(scored, indent=1, allow_nan=False) + "\n")
    return scored


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--call", type=Path, default=forward_call.OUTPUT)
    parser.add_argument("--frozen", type=Path, default=forward_call.FROZEN)
    parser.add_argument("--eps", type=float, help="adjusted diluted EPS United reported, US dollars")
    parser.add_argument("--reported-on", help="date of the results, YYYY-MM-DD")
    parser.add_argument("--fuel-price", type=float, help="average fuel price per gallon United reported")
    parser.add_argument("--trasm-yoy", type=float, help="TRASM change year over year as a fraction, 0.14 for 14%%")
    parser.add_argument("--source", help="where the figures come from (URL or accession)")
    parser.add_argument("--from-quarterly", action="store_true", help="read the figures from data/lab/ual_quarterly.csv")
    parser.add_argument("--quarterly", type=Path, default=filings.QUARTERLY_CSV)
    parser.add_argument("--consensus", type=float, help="consensus EPS that was public before the report")
    parser.add_argument("--consensus-as-of", help="date of that consensus")
    parser.add_argument("--consensus-url", help="where it was published")
    args = parser.parse_args()
    if args.from_quarterly:
        figures = reported(json.loads(args.call.read_text())["forecast"]["target"]["period"], args.quarterly)
    elif args.eps is None or not args.reported_on:
        parser.error("give --eps and --reported-on, or --from-quarterly")
    else:
        figures = {"actual_eps": args.eps, "reported_on": args.reported_on, "fuel_price": args.fuel_price,
                   "trasm_yoy": args.trasm_yoy, "source": args.source}
    if args.consensus is not None:
        if not (args.consensus_as_of and args.consensus_url):
            parser.error("--consensus needs --consensus-as-of and --consensus-url")
        figures["consensus"] = {"value": args.consensus, "as_of": args.consensus_as_of, "source_url": args.consensus_url}
    scored = run(args.call, args.frozen, **figures)
    print(json.dumps({"call": str(args.call), "sha256": scored["sha256"], "outcome": scored["outcome"],
                      "scores": scored["scores"]}, indent=2))


if __name__ == "__main__":
    main()
