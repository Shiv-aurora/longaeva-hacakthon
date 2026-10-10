"""Save a monthly run or replay it using its preserved inputs and exact settings."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.monthly import MonthlyScenario, simulate_monthly
from app.model_archive import capture_source, replay_saved, run_preserved

MODEL_SOURCE = capture_source("monthly")


def replay(path: Path) -> dict:
    return replay_saved(path, "monthly")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cutoff", default="2026-07-16")
    parser.add_argument("--paths", type=int, default=10000)
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--cash-plan", type=Path, help="Explicit incremental funding/working-capital plan JSON")
    parser.add_argument("--purchase-plan", type=Path, help="Explicit purchase/deposit/financing allocations to fleet entries")
    parser.add_argument("--retention-plan", type=Path, help="Explicit retirement/replacement links and retention limits/costs")
    parser.add_argument("--delivery-delay", type=int, default=0)
    parser.add_argument("--delivery-count", type=int)
    parser.add_argument("--delivery-group", default="all", choices=("all", "narrowbody", "widebody"))
    parser.add_argument("--horizon", type=int, default=12)
    args = parser.parse_args()
    if args.replay:
        result = replay(args.replay)
        print("Replay verified:", result["manifest"]["run_id"])
        return
    cash_plan = json.loads(args.cash_plan.read_text()) if args.cash_plan else None
    purchase_plan = json.loads(args.purchase_plan.read_text()) if args.purchase_plan else None
    retention_plan = json.loads(args.retention_plan.read_text()) if args.retention_plan else None
    result = run_preserved(MODEL_SOURCE, lambda: simulate_monthly(MonthlyScenario(information_cutoff=args.cutoff, paths=args.paths,
        horizon_months=args.horizon, cash_plan=cash_plan, purchase_plan=purchase_plan, retention_plan=retention_plan,
        delivery_delay_months=args.delivery_delay, delivery_aircraft_count=args.delivery_count, delivery_group=args.delivery_group)))
    directory = ROOT / "data" / "runs"
    directory.mkdir(exist_ok=True)
    target = directory / (result["manifest"]["run_id"] + ".json")
    if target.exists():
        replay(target)
    else:
        with target.open("x") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
    print(json.dumps({"run_file": str(target), "summary": result["summary"]}, indent=2))


if __name__ == "__main__":
    main()
