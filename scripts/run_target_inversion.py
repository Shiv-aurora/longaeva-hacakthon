"""Run or replay an operating target and optional FCFF valuation scenario."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.failure_search import SearchAxis
from app.monthly import MonthlyScenario
from app.target_inversion import OperatingTarget, ValuationScenario, TargetInversion, invert_target
from app.model_archive import capture_source, replay_saved, run_preserved

MODEL_SOURCE = capture_source("target_inversion")


def specification(spec: dict) -> TargetInversion:
    return TargetInversion(**{**spec, 'x': SearchAxis(**spec['x']), 'y': SearchAxis(**spec['y']),
        'target': OperatingTarget(**spec['target']), 'valuation': ValuationScenario(**spec['valuation']) if spec.get('valuation') else None})


def replay(path: Path) -> dict:
    return replay_saved(path, "target_inversion")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, help='JSON with base_scenario and inversion; valuation assumptions must be explicit')
    parser.add_argument('--replay', type=Path)
    args = parser.parse_args()
    if args.replay:
        result = replay(args.replay)
        print('Target inversion replay verified:', result['manifest']['run_id'])
        return
    config = json.loads(args.config.read_text()) if args.config else {}
    result = run_preserved(MODEL_SOURCE, lambda: invert_target(MonthlyScenario(**config.get('base_scenario', {})), specification(config.get('inversion', asdict(TargetInversion())))))
    target = ROOT / 'data' / 'target_inversions' / (result['manifest']['run_id'] + '.json')
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        replay(target)
    else:
        target.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'run_file': str(target), 'counts': result['counts'], 'runtime_seconds': result['runtime_seconds'], 'status': result['status']}, indent=2))


if __name__ == '__main__':
    main()
