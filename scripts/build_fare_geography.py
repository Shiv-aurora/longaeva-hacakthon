"""Recover full raw endpoint evidence from the separately preserved DB1B inventory."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from app.evidence import digest
from app.fare_geography import build_geography
from app.operating_history import endpoint_market, wac_lookup, WAC_URL
from app.source_store import read_source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--wac-sha", required=True)
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text())
    if args.inventory.stem != digest(inventory):
        raise ValueError("Fare inventory identity differs")
    fare_design = json.loads((ROOT / "data/fare_history/designs" / inventory["design_id"] / "design.json").read_text())
    if digest(fare_design) != inventory["design_id"] or fare_design["sources"] != inventory["sources"]:
        raise ValueError("Fare inventory/input design differs")
    if inventory["definition_source_sha256"] != fare_design["specification"]["definition_source_sha256"]:
        raise ValueError("Fare definition binding differs")
    content, _ = read_source(args.wac_sha); lookup = wac_lookup(content)
    specification = {"version": "db1b-geography-design-0.1.0", "inventory_id": args.inventory.stem,
        "fare_design_id": inventory["design_id"], "sources": inventory["sources"],
        "definition_source_sha256": inventory["definition_source_sha256"], "wac_source_sha256": args.wac_sha,
        "wac_source_url": WAC_URL, "crosswalk": {str(k): {"description": v, "market": endpoint_market(k, lookup)} for k, v in sorted(lookup.items())},
        "crosswalk_code_sha256": hashlib.sha256((ROOT / "app/operating_history.py").read_bytes()).hexdigest(),
        "crosswalk_code": (ROOT / "app/operating_history.py").read_text(),
        "crosswalk_label": "Existing model geographic convention, not an official BTS business-market definition. US territory codes 3/4 map to Latin America; Pacific territories remain Pacific. Keep domestic including Canada distinct from survey coverage.",
        "knowledge_label": "Current-download original records and filename publication proxies; no independently captured historical bytes or official historical airport registry implied."}
    print(json.dumps(build_geography(inventory["sources"], specification,
        progress=lambda event: print(json.dumps(event), file=sys.stderr, flush=True)), indent=2))


if __name__ == "__main__":
    main()
