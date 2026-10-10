"""Build app/static/data/evidence.json and meta.json: the quoted sources and the facts that frame the dashboard.

Reads the tidy inputs under data/lab and the raw filings they point at
(data/lab_raw, which must be present: every quote is checked against the
document's bytes). No network: the report date and the two outside comparators
are the web lookups recorded in app.lab.evidence.WEB. For fixed inputs the
output is identical apart from `generated_at`.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab import evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, default=evidence.OUT_DIR)
    parser.add_argument("--generated-at", default=None, help="fixed UTC timestamp, for reproducible bytes")
    args = parser.parse_args()
    ledger, meta = evidence.build(generated_at=args.generated_at)
    evidence.write(ledger, meta, args.out_dir)
    print(json.dumps({"out_dir": str(args.out_dir), "as_of": {"evidence": ledger["as_of"], "meta": meta["as_of"]},
                      "counts": ledger["counts"], "report_date": meta["report_date"], "guidance": meta["guidance"],
                      "promises": [[p["made_on"], p["period"], p["recovery_low"], p["recovery_high"]]
                                   for p in meta["promises"]],
                      "comparators": {name: {key: item.get(key) for key in ("value", "as_of", "source_url", "reason")}
                                      for name, item in meta["comparators"].items()},
                      "inputs": len(ledger["inputs"])}, indent=2))


if __name__ == "__main__":
    main()
