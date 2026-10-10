"""Repeat a saved monthly scenario to measure its numerical sampling stability."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.convergence import ConvergenceSpec, study_convergence
from app.monthly import MonthlyScenario
from app.model_archive import capture_source, deterministic, replay_saved, run_preserved

MODEL_SOURCE = capture_source("convergence")
MONTHLY_SOURCE = capture_source("monthly")


def report_progress(event):
    if event["phase"] == "level_complete":
        print(json.dumps(event), file=sys.stderr, flush=True)
    elif event["completed_replica_paths"] % 100000 == 0:
        print(json.dumps(event), file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, help="Preserved monthly run; replayed before the study")
    parser.add_argument("--spec", type=Path, help="Explicit numerical tolerance/path-count settings JSON")
    parser.add_argument("--replay", type=Path, help="Replay a complete archived convergence study")
    args = parser.parse_args()
    if args.replay:
        result = replay_saved(args.replay, "convergence")
        print(json.dumps({"verified_study": result["manifest"]["run_id"], "summary": result["summary"]}, indent=2))
        return
    if not args.run:
        parser.error("--run or --replay is required")
    reference = replay_saved(args.run, "monthly")
    if reference["manifest"]["code_sha256"] != MONTHLY_SOURCE.code_sha256:
        raise ValueError("The saved run uses an earlier core; rerun it with the current model before this study")
    spec = ConvergenceSpec(**json.loads(args.spec.read_text())) if args.spec else ConvergenceSpec()
    inputs = reference["replay_inputs"]
    result = run_preserved(MODEL_SOURCE, lambda: study_convergence(MonthlyScenario(**reference["scenario"]), spec,
        snapshot=inputs["evidence_snapshot"], fuel=inputs["fuel_observations"], progress=report_progress))
    if result["manifest"]["reference_run_id"] != reference["manifest"]["run_id"]:
        raise ValueError("The study's reference differs from the independently replayed run")
    folder = ROOT / "data" / "convergence"
    folder.mkdir(exist_ok=True)
    target = folder / (result["manifest"]["run_id"] + ".json")
    if target.exists():
        existing = replay_saved(target, "convergence")
        if deterministic(existing) != deterministic(result):
            raise ValueError("Existing immutable study differs")
    else:
        with target.open("x") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
    print(json.dumps({"study_file": str(target), "summary": result["summary"]}, indent=2))


if __name__ == "__main__":
    main()
