#!/usr/bin/env python3
"""Deterministically show SHA-256 checksums of public snapshots and source metadata."""
import hashlib,json
from pathlib import Path
from datetime import datetime,timezone
root=Path(__file__).resolve().parents[1]
files=['data/eia_2026_weekly_source.txt','data/eia_gulf_coast_2026.csv','data/ual_q2_2026.json']
for rel in files:
  content=(root/rel).read_bytes()
  print(f'{hashlib.sha256(content).hexdigest()}  {rel}  {len(content)} bytes')
print('Original EIA daily price source: https://www.eia.gov/dnav/pet/hist/EER_EPJK_PF4_RGC_DPGD.htm')
print('UAL quarterly source and publication date: see data/ual_q2_2026.json')
print('Date of reference snapshot: 2026-10-08. This is not a vintage backtest.')
