"""Point-in-time aware source manifests and parsers. No invented observations."""
from __future__ import annotations
import csv
import json
import math
import re
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, stdev

BASE = Path(__file__).resolve().parents[1]
FUEL_CSV = BASE / "data" / "eia_gulf_coast_2026.csv"
Q2_JSON = BASE / "data" / "ual_q2_2026.json"
FUEL_SOURCE = "https://www.eia.gov/dnav/pet/hist/EER_EPJK_PF4_RGC_DPGD.htm"


def parse_eia_week_table(source: str) -> list[dict]:
    """Parse exact EIA Mon..Fri weekly-layout transcription. Do not impute missing days."""
    values: dict[str, float] = {}
    for raw in source.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        fields = [x.strip() for x in raw.split("|")]
        if len(fields) != 6:
            raise ValueError(f"EIA weekly row must have five days: {raw}")
        m = re.fullmatch(r"(\d{4})\s+([A-Z][a-z]{2})-\s*(\d+)\s+to\s+[A-Z][a-z]{2}-\s*\d+", fields[0])
        if m is None:
            raise ValueError(f"Invalid EIA week heading: {fields[0]}")
        start = date.fromisoformat(f"{m.group(1)}-{_months()[m.group(2)]:02d}-{int(m.group(3)):02d}")
        if start.weekday() != 0:
            raise ValueError(f"Week start must be Monday: {start}")
        for i, cell in enumerate(fields[1:]):
            # An empty trading-day cell is absent evidence; forward-filling it
            # would create an observation that the source never supplied.
            if not cell:
                continue
            val = float(cell)
            if not 0.1 <= val <= 20 or not math.isfinite(val):
                raise ValueError(f"Bad fuel price {val}")
            key = (start + timedelta(days=i)).isoformat()
            if key in values:
                raise ValueError(f"Duplicate date {key}")
            values[key] = val
    if not values:
        raise ValueError("No EIA prices parsed")
    return [{"date": k, "usd_per_gallon": values[k]} for k in sorted(values)]


def _months() -> dict[str, int]:
    return {name: i for i, name in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def save_fuel_csv(records: list[dict], target: Path = FUEL_CSV) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "usd_per_gallon"])
        w.writeheader()
        w.writerows(records)


def load_fuel() -> list[dict]:
    with FUEL_CSV.open(newline="", encoding="utf-8") as f:
        result = [{"date": row["date"], "usd_per_gallon": float(row["usd_per_gallon"])} for row in csv.DictReader(f)]
    if not result:
        raise ValueError("No fuel observations")
    previous = None
    for row in result:
        d = date.fromisoformat(row["date"])
        if previous is not None and d <= previous:
            raise ValueError("Fuel series must have unique increasing dates")
        if not math.isfinite(row["usd_per_gallon"]) or row["usd_per_gallon"] <= 0:
            raise ValueError("Nonpositive or nonfinite fuel observation")
        previous = d
    return result


def load_ual() -> dict:
    data = json.loads(Q2_JSON.read_text(encoding="utf-8"))
    assert data["period_days"] == 91
    assert data["prasm_cents_per_asm"] < data["trasm_cents_per_asm"]
    return data


def fuel_volatility(observations: list[dict], window: int = 65) -> float:
    """Historical log return stdev, capped for prototype stability, not a calibrated distribution."""
    recent = [x["usd_per_gallon"] for x in observations[-(window+1):]]
    log_returns = [math.log(b / a) for a, b in zip(recent, recent[1:])]
    return max(0.005, min(0.09, stdev(log_returns))) if len(log_returns) > 2 else 0.03


def manifest(fuel: list[dict], ual: dict) -> dict:
    return {
      "fuel": {"source": "US EIA Gulf Coast jet-fuel spot FOB", "url": FUEL_SOURCE,
               "frequency": "trading days", "units": "USD/gallon", "observation_count": len(fuel),
               "last_observation": fuel[-1]["date"], "status": "observed, EIA public series",
               "caution": "Gulf Coast spot is not UAL's delivered fuel cost; no point-in-time vintage reconstruction."},
      "company": {"source": "UAL Q2 2026 earnings release", "url": ual["source_url"],
                  "published_on": ual["published_on"], "period_end": ual["period_end"],
                  "status": "reported historical financial and operating figures"},
      "fare": {"source": "User-provided, documented observed quote CSV", "status": "optional, absent until imported",
               "caution": "Offered ticket prices differ from fares actually paid and from UAL's reported yield/PRASM; comparable date/route/cabin/advance purchase needed."},
      "not_in_v1": ["No observed daily UAL fare panel bundled", "No UAL stock-price predictive validation", "No identified causal elasticity", "No historical as-of vintage backtest"]
    }
