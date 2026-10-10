#!/usr/bin/env python3
"""Regenerate EIA CSV from an auditable official-source transcription."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.data import parse_eia_week_table, save_fuel_csv
source = Path(__file__).resolve().parents[1] / "data" / "eia_2026_weekly_source.txt"
rows = parse_eia_week_table(source.read_text(encoding="utf-8"))
save_fuel_csv(rows)
print(f"Saved {len(rows)} actual EIA daily observations: {rows[0]['date']} through {rows[-1]['date']} (${rows[-1]['usd_per_gallon']}/gal)")
