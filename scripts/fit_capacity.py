"""Fit the declared T2 covered-aircraft capacity experiment from preserved inputs."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.quarterly_capacity import build_capacity


def main():
    p=argparse.ArgumentParser();p.add_argument("--t2",type=Path);p.add_argument("--spec",type=Path,default=ROOT/"data/t2_capacity_spec.json")
    args=p.parse_args();spec=json.loads(args.spec.read_text())
    result=build_capacity(args.t2 or ROOT/"data/t2_history"/spec["source_snapshot_id"],args.spec)
    summary={k:v for k,v in result.items() if k!='scorecard'}
    summary['scores']=[{k:s[k] for k in ('model','selection_equal_weight_MAE_asm_per_day','prediction_count','series_horizon_cells')} for s in result['scorecard']]
    print(json.dumps(summary,indent=2))


if __name__=="__main__":main()
