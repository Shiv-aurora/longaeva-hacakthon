"""Fit the frozen-model four-case traffic source ablation."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.monthly_traffic_vintages import build_study


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument('--spec',type=Path,default=ROOT/'data/monthly_traffic_vintage_spec.json');args=parser.parse_args()
    print(json.dumps(build_study(json.loads(args.spec.read_text()),progress=lambda event:print(json.dumps(event),file=sys.stderr,flush=True)),indent=2))
