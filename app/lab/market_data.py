"""Loaders for the tidy daily fuel, daily price and monthly CPI inputs.

Days and months the source did not publish are absent from the files and stay
absent here: nothing is forward filled or interpolated. Windows are inclusive.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
from functools import lru_cache
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "data" / "lab"
FUEL_CSV = LAB / "fuel_daily.csv"
PRICES_CSV = LAB / "prices_daily.csv"
CPI_CSV = LAB / "cpi_monthly.csv"
TICKERS = ("UAL", "DAL", "AAL", "LUV", "SPY", "JETS", "HO=F")
CPI_SERIES = ("CUSR0000SETG01", "CUUR0000SETG01", "CUSR0000SA0")
FUEL = "FUEL"  # Accepted wherever a ticker is: the EIA spot jet fuel series.

Series = tuple[list[date], np.ndarray]


@lru_cache(maxsize=16)
def _cached(path: str, stamp: int) -> tuple[tuple[str, ...], ...]:
    with open(path, newline="") as stream:
        return tuple(tuple(row) for row in csv.reader(stream))


def _table(path: Path) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    rows = _cached(str(path), Path(path).stat().st_mtime_ns)
    return rows[0], rows[1:]


def _day(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _series(rows) -> Series:
    return [date.fromisoformat(r[0]) for r in rows], np.array([float(r[-1]) for r in rows], dtype=float)


def load_fuel_daily(path: Path = FUEL_CSV) -> Series:
    """EIA Gulf Coast kerosene-type jet fuel spot price, USD per gallon."""
    header, rows = _table(path)
    if header != ("date", "usd_per_gallon"):
        raise ValueError("Unexpected fuel_daily.csv header")
    return _series(rows)


def load_prices(ticker: str, field: str = "adj_close", path: Path = PRICES_CSV) -> Series:
    """Daily prices for one ticker. `adj_close` includes dividends; `close` does not."""
    header, rows = _table(path)
    if header != ("date", "ticker", "close", "adj_close") or field not in ("close", "adj_close"):
        raise ValueError("Unexpected prices_daily.csv header or price field")
    column = header.index(field)
    picked = [(r[0], r[column]) for r in rows if r[1] == ticker]
    if not picked:
        raise ValueError(f"No daily prices for {ticker}")
    return _series(picked)


def load_cpi(series_id: str, path: Path = CPI_CSV) -> Series:
    """Monthly CPI index, dated on the first day of the reference month."""
    header, rows = _table(path)
    if header != ("date", "series_id", "value"):
        raise ValueError("Unexpected cpi_monthly.csv header")
    picked = [(r[0], r[2]) for r in rows if r[1] == series_id]
    if not picked:
        raise ValueError(f"No CPI observations for {series_id}")
    return _series(picked)


def load_series(name: str, field: str = "adj_close", fuel_path: Path = FUEL_CSV, prices_path: Path = PRICES_CSV) -> Series:
    return load_fuel_daily(fuel_path) if name == FUEL else load_prices(name, field, prices_path)


def fuel_window_mean(start_date: date | str, end_date: date | str, path: Path = FUEL_CSV) -> tuple[float, int]:
    """Mean fuel price over the prints dated start..end, and how many prints that is.

    A window with no prints returns (nan, 0); it is never filled from outside.
    """
    start, end = _day(start_date), _day(end_date)
    if end < start:
        raise ValueError("Fuel window ends before it starts")
    dates, values = load_fuel_daily(path)
    picked = [v for d, v in zip(dates, values) if start <= d <= end]
    return (float(np.mean(picked)), len(picked)) if picked else (math.nan, 0)


def _quarter_bounds(day: date) -> tuple[date, date]:
    first = date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)
    following = date(first.year + (first.month == 10), (first.month + 3) % 12 or 12, 1)
    return first, following - timedelta(days=1)


def quarterly_mean_fuel(start_date: date | str, end_date: date | str, path: Path = FUEL_CSV) -> list[dict]:
    """Mean fuel price for each calendar quarter that the window touches.

    Only prints inside both the quarter and the window count, so a quarter cut by
    the window or by the end of the data shows a short `n_prints` and `last_print`.
    """
    start, end = _day(start_date), _day(end_date)
    if end < start:
        raise ValueError("Fuel window ends before it starts")
    dates, values = load_fuel_daily(path)
    result, cursor = [], start
    while cursor <= end:
        first, last = _quarter_bounds(cursor)
        picked = [(d, v) for d, v in zip(dates, values) if max(first, start) <= d <= min(last, end)]
        result.append({"period": f"{first.year}Q{(first.month + 2) // 3}",
                       "period_start": first.isoformat(), "period_end": last.isoformat(),
                       "usd_per_gallon": float(np.mean([v for _, v in picked])) if picked else None,
                       "n_prints": len(picked),
                       "first_print": picked[0][0].isoformat() if picked else None,
                       "last_print": picked[-1][0].isoformat() if picked else None})
        cursor = last + timedelta(days=1)
    return result


def daily_log_returns(ticker: str, field: str = "adj_close", fuel_path: Path = FUEL_CSV,
                      prices_path: Path = PRICES_CSV) -> Series:
    """Log change between consecutive prints, dated on the later print.

    A return across a missing day spans more than one session; use
    aligned_log_returns to see the calendar gap behind each return.
    """
    dates, values = load_series(ticker, field, fuel_path, prices_path)
    return dates[1:], np.diff(np.log(values))


def aligned_levels(names: list[str], field: str = "adj_close", fuel_path: Path = FUEL_CSV,
                   prices_path: Path = PRICES_CSV) -> tuple[list[date], np.ndarray]:
    """Levels on the dates every named series printed; one column per name."""
    loaded = [dict(zip(*load_series(name, field, fuel_path, prices_path))) for name in names]
    common = sorted(set.intersection(*(set(series) for series in loaded)))
    return common, np.array([[series[d] for series in loaded] for d in common], dtype=float).reshape(len(common), len(names))


def aligned_log_returns(names: list[str], field: str = "adj_close", fuel_path: Path = FUEL_CSV,
                        prices_path: Path = PRICES_CSV) -> tuple[list[date], np.ndarray, np.ndarray]:
    """Log returns between consecutive common dates, so every column spans the same interval.

    Returns (dates, returns[n, len(names)], gap_days[n]); gap_days is the calendar
    distance each return covers (1 on consecutive weekdays, 3 over a weekend).
    """
    dates, levels = aligned_levels(names, field, fuel_path, prices_path)
    gaps = np.array([(b - a).days for a, b in zip(dates, dates[1:])], dtype=int)
    return dates[1:], np.diff(np.log(levels), axis=0), gaps
