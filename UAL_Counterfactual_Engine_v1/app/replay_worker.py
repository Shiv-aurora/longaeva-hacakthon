"""Fixed replay entry point copied into locally registered model code bundles.

Invoked in an isolated Python process; inputs are data, never executable source.
"""
from pathlib import Path
import json
import sys


def deterministic(result):
    """Only the two documented wall-clock fields and archive receipt vary."""
    result = {k: v for k, v in result.items() if k not in {"runtime_seconds", "replay_code"}}
    if "summary" in result:
        result["summary"] = {k: v for k, v in result["summary"].items() if k != "runtime_seconds"}
    return result


def main():
    # -I removes the invoking checkout and PYTHONPATH; only this verified bundle
    # supplies app modules. No live data loads: both inputs are always explicit.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from app.evidence import digest
    from app.monthly import MonthlyScenario, simulate_monthly
    saved = json.load(sys.stdin)
    kind = sys.argv[1]
    m = saved["manifest"]
    inputs = {"snapshot": saved["replay_inputs"]["evidence_snapshot"],
              "fuel": saved["replay_inputs"]["fuel_observations"]}
    if kind == "monthly":
        result = simulate_monthly(MonthlyScenario(**saved["scenario"]), **inputs)
    elif kind == "failure_search":
        from app.failure_search import SearchAxis, FailureSearch, search_failures
        s = m["search"]
        spec = FailureSearch(**{**s, "x": SearchAxis(**s["x"]), "y": SearchAxis(**s["y"])})
        result = search_failures(MonthlyScenario(**m["base_scenario"]), spec, **inputs)
    elif kind == "target_inversion":
        from app.failure_search import SearchAxis
        from app.target_inversion import OperatingTarget, ValuationScenario, TargetInversion, invert_target
        s = m["inversion"]
        spec = TargetInversion(**{**s, "x": SearchAxis(**s["x"]), "y": SearchAxis(**s["y"]),
            "target": OperatingTarget(**s["target"]),
            "valuation": ValuationScenario(**s["valuation"]) if s.get("valuation") else None})
        result = invert_target(MonthlyScenario(**m["base_scenario"]), spec, **inputs)
    elif kind == "convergence":
        from app.convergence import ConvergenceSpec, study_convergence
        result = study_convergence(MonthlyScenario(**m["base_scenario"]), ConvergenceSpec(**m["convergence"]), **inputs)
    else:
        raise ValueError("Unsupported replay kind")
    if digest(result["manifest"]) != digest(m):
        raise ValueError("Replay manifest differs: code, environment, configuration or preserved inputs")
    if digest(deterministic(result)) != digest(deterministic(saved)):
        raise ValueError("Replay result checksum differs (all deterministic fields are checked)")
    json.dump(result, sys.stdout, allow_nan=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Data validation failures are reported without dumping source/input data.
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
