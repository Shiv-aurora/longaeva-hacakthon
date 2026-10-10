"""Run or replay a preserved joint-driver failure search without network access."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.failure_search import FailureSearch, SearchAxis, search_failures
from app.monthly import MonthlyScenario
from app.model_archive import capture_source, replay_saved, run_preserved

MODEL_SOURCE = capture_source("failure_search")


def replay(path: Path) -> dict:
    return replay_saved(path, "failure_search")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, help="JSON with base_scenario and search; defaults use 10000 paths, fuel 0–60%, demand -20–0%")
    parser.add_argument("--replay", type=Path)
    args = parser.parse_args()
    if args.replay:
        result = replay(args.replay)
        print("Search replay verified:", result["manifest"]["run_id"])
        return
    config = json.loads(args.config.read_text()) if args.config else {}
    spec = config.get("search", asdict(FailureSearch()))
    search = FailureSearch(**{**spec, "x": SearchAxis(**spec["x"]), "y": SearchAxis(**spec["y"])})
    result = run_preserved(MODEL_SOURCE, lambda: search_failures(MonthlyScenario(**config.get("base_scenario", {})), search))
    target = ROOT / "data" / "failure_searches" / (result["manifest"]["run_id"] + ".json")
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        replay(target)
    else:
        target.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"run_file": str(target), "counts": result["counts"], "policy_disagreement_points": result["policy_disagreement_points"], "runtime_seconds": result["runtime_seconds"], "status": result["status"]}, indent=2))


if __name__ == "__main__":
    main()
