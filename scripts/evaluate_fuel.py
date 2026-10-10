#!/usr/bin/env python3
"""Actual held-out 1- and 5-print EIA fuel forecasting sanity check. NOT validation of airline signal."""
from __future__ import annotations
import math
from statistics import mean
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.data import load_fuel
p=[r['usd_per_gallon'] for r in load_fuel()]
# At each origin, all predictors use past observations only. No optimized hyperparameters.
def evaluate(h):
    random_walk, mean_revert=[],[]
    for idx in range(130,len(p)-h):
        old=p[idx];realized=p[idx+h]
        prev20=mean(p[idx-19:idx+1])
        rw=old
        # Assume 0.978 per business observation as in engine daily log process.
        mr=math.exp((.978**h)*math.log(old)+(1-.978**h)*math.log(prev20))
        random_walk.append(abs(realized-rw))
        mean_revert.append(abs(realized-mr))
    return len(random_walk),mean(random_walk),mean(mean_revert)
for h in (1,5):
    n,rw,mr=evaluate(h)
    print(f"{h}-print holdout ({n} rolling origins): naive persistence MAE ${rw:.4f}/gal; mean-reverting prototype MAE ${mr:.4f}/gal; change {100*(mr/rw-1):+.2f}%")
print('Fuel-only descriptive diagnostic, not a vintage-correct trading or UAL fare/earnings backtest.')
