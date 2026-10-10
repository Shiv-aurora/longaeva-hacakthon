"""Loader and derived series for the TSA daily checkpoint count: an observed demand series, not a forecast.

Every derived value dated t uses only counts dated t or earlier. Days TSA did not
publish stay absent: a 7-day average exists only where all seven calendar days
are present, and a growth rate only where both of its ends exist.

Growth rates are fractions (0.012 is 1.2%):
- Daily: the 7-day trailing average against the 7-day trailing average 364 days
  earlier. 364 days is 52 weeks, so both windows hold the same weekdays.
- Month and quarter: the mean daily count against the mean of the same calendar
  period a year earlier. A period the data ends inside is compared with the prior
  year from the period's start to the same month and day, and `days_used` and
  `prior_days_used` say how many days stand behind each mean.
"""
from __future__ import annotations

import calendar
import csv
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path

import numpy as np

from app.source_store import byte_hash

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "data" / "lab"
TSA_CSV = LAB / "tsa_daily.csv"
UAL_CSV = LAB / "ual_quarterly.csv"
DEMAND_JSON = ROOT / "app" / "static" / "data" / "demand_signal.json"
WEEKDAY_LAG = 364  # 52 weeks: the same weekday one year earlier.
DAILY_START = date(2025, 1, 1)
BASE_YEAR = 2019  # First year TSA publishes, and the level a pandemic quarter is measured against.
PANDEMIC_START = "2020Q1"  # The WHO declared the pandemic on 11 March 2020.
TARGET_QUARTER = "2026Q3"  # The quarter United reports on 20 October 2026.
UAL_COLUMNS = {"asm": "asm_m", "rpm": "rpm_m", "trasm": "trasm_cents"}
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")

Series = tuple[list[date], np.ndarray]


def load_tsa_daily(path: Path = TSA_CSV) -> Series:
    """People screened at TSA checkpoints per day. Dates ascend and are unique; missing days are absent."""
    with open(path, newline="") as stream:
        rows = list(csv.reader(stream))
    if not rows or rows[0] != ["date", "travelers"] or len(rows) < 2:
        raise ValueError("tsa_daily.csv needs the header date,travelers and at least one day")
    dates = [date.fromisoformat(r[0]) for r in rows[1:]]
    values = np.array([int(r[1]) for r in rows[1:]], dtype=float)
    if any(b <= a for a, b in zip(dates, dates[1:])) or not (values > 0).all():
        raise ValueError("tsa_daily.csv has repeated or unordered dates, or a non-positive count")
    return dates, values


def missing_ranges(dates: list[date]) -> list[dict]:
    """Calendar days between the first and last date that have no count, as inclusive ranges."""
    gaps = []
    for a, b in zip(dates, dates[1:]):
        if (b - a).days > 1:
            gaps.append({"first": (a + timedelta(days=1)).isoformat(), "last": (b - timedelta(days=1)).isoformat(),
                         "days": (b - a).days - 1})
    return gaps


def trailing_mean(dates: list[date], values: np.ndarray, window: int = 7) -> np.ndarray:
    """Mean of the `window` calendar days ending on each date; nan unless all of them are present."""
    out = np.full(len(dates), np.nan)
    for i in range(window - 1, len(dates)):
        if (dates[i] - dates[i - window + 1]).days == window - 1:  # Dates are unique, so no day is skipped.
            out[i] = values[i - window + 1:i + 1].mean()
    return out


def growth_vs_days_earlier(dates: list[date], series: np.ndarray, lag_days: int = WEEKDAY_LAG) -> np.ndarray:
    """series[t] / series[t - lag_days] - 1; nan where either end is missing. 364 days keeps the weekday."""
    index = {d: i for i, d in enumerate(dates)}
    out = np.full(len(dates), np.nan)
    for i, day in enumerate(dates):
        j = index.get(day - timedelta(days=lag_days))
        if j is not None and math.isfinite(series[i]) and math.isfinite(series[j]) and series[j] > 0:
            out[i] = series[i] / series[j] - 1
    return out


def _bounds(day: date, freq: str) -> tuple[str, date, date]:
    if freq == "month":
        return (f"{day.year}-{day.month:02d}", date(day.year, day.month, 1),
                date(day.year, day.month, calendar.monthrange(day.year, day.month)[1]))
    if freq != "quarter":
        raise ValueError("freq is 'month' or 'quarter'")
    quarter = (day.month - 1) // 3
    last_month = 3 * quarter + 3
    return (f"{day.year}Q{quarter + 1}", date(day.year, 3 * quarter + 1, 1),
            date(day.year, last_month, calendar.monthrange(day.year, last_month)[1]))


def _year_earlier(day: date) -> date:
    return date(day.year - 1, day.month, min(day.day, calendar.monthrange(day.year - 1, day.month)[1]))


def period_means(dates: list[date], values: np.ndarray, freq: str) -> list[dict]:
    """Mean daily count per calendar month or quarter, its year-over-year growth and the days behind both.

    A complete period is compared with the whole of the same period a year earlier.
    A period the data ends inside is compared with the prior-year days from the
    period's start to the same month and day as its last count. Growth is None when
    the prior-year window has no counts or is itself missing days.
    """
    by_day = dict(zip(dates, values))
    groups: dict[str, list[date]] = {}
    for day in dates:
        groups.setdefault(_bounds(day, freq)[0], []).append(day)
    out = []
    for label, days in groups.items():
        _, first, last = _bounds(days[0], freq)
        in_period = (last - first).days + 1
        complete = len(days) == in_period
        mean = float(np.mean([by_day[d] for d in days]))
        prior_first = _year_earlier(first)
        prior_last = _bounds(prior_first, freq)[2] if complete else _year_earlier(days[-1])
        span = [prior_first + timedelta(days=i) for i in range((prior_last - prior_first).days + 1)]
        prior = [by_day[d] for d in span if d in by_day]
        comparable = bool(prior) and len(prior) == len(span)
        prior_mean = float(np.mean(prior)) if comparable else None
        out.append({"period": label, "period_start": first.isoformat(), "period_end": last.isoformat(),
                    "mean_daily_travelers": mean, "days_used": len(days), "days_in_period": in_period,
                    "complete": complete, "yoy": mean / prior_mean - 1 if comparable else None,
                    "prior_mean_daily_travelers": prior_mean, "prior_days_used": len(prior) if comparable else None,
                    "yoy_basis": None if not comparable else
                    "whole period a year earlier" if complete else
                    "start of period to the same month and day a year earlier"})
    return out


def mark_pandemic(quarters: list[dict]) -> list[dict]:
    """Add vs_2019, pandemic and yoy_base_pandemic to quarterly rows.

    Rule: a quarter is a pandemic quarter from 2020Q1 until the first complete
    quarter whose mean is back at or above the same quarter of 2019. A later
    quarter whose year-earlier quarter is a pandemic quarter is flagged
    yoy_base_pandemic: its growth is measured against a depressed base.
    """
    base = {q["period"][-2:]: q["mean_daily_travelers"] for q in quarters
            if q["period"].startswith(str(BASE_YEAR)) and q["complete"]}
    marked, recovered = [], False
    for q in quarters:
        reference = base.get(q["period"][-2:])
        ratio = q["mean_daily_travelers"] / reference if reference and q["complete"] else None
        started = q["period"] >= PANDEMIC_START
        if started and not recovered and ratio is not None and ratio >= 1:
            recovered = True
        marked.append({**q, "vs_2019": ratio, "pandemic": started and not recovered})
    flags = {q["period"]: q["pandemic"] for q in marked}
    for q in marked:
        year_before = f"{int(q['period'][:4]) - 1}{q['period'][4:]}"
        q["yoy_base_pandemic"] = bool(flags.get(year_before)) and not q["pandemic"]
    return marked


def _quarter_label(period_end: str) -> str:
    day = date.fromisoformat(period_end)
    if _bounds(day, "quarter")[2] != day:
        raise ValueError(f"period_end {period_end} is not the last day of a calendar quarter")
    return _bounds(day, "quarter")[0]


def load_ual_growth(path: Path = UAL_CSV) -> dict[str, dict]:
    """United's year-over-year growth in ASM, RPM and TRASM by calendar quarter, from the earnings-release table.

    Each quarter is compared with the row for the same quarter a year earlier.
    Where a quarter is listed twice, the row filed last is used. A blank cell
    stays blank: the growth rates that need it are None.
    """
    with open(path, newline="") as stream:
        reader = csv.DictReader(stream)
        absent = {"period_end", "filed_on", *UAL_COLUMNS.values()} - set(reader.fieldnames or [])
        if absent:
            raise ValueError(f"{Path(path).name} lacks the columns {sorted(absent)}")
        rows = sorted(reader, key=lambda r: (r["period_end"], r["filed_on"]))
    levels = {}
    for row in rows:
        levels[_quarter_label(row["period_end"])] = {
            name: float(row[column]) if row[column].strip() else None for name, column in UAL_COLUMNS.items()}
    growth = {}
    for label, current in levels.items():
        before = levels.get(f"{int(label[:4]) - 1}{label[4:]}")
        if before is not None:
            growth[label] = {name: current[name] / before[name] - 1
                             if current[name] is not None and before[name] is not None and before[name] > 0 else None
                             for name in UAL_COLUMNS}
    return growth


def _correlation(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    a, b = np.array(pairs, dtype=float).T
    if a.std() == 0 or b.std() == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def _sample(rows: list[dict], name: str, rule: str, keep) -> dict:
    picked = [r for r in rows if keep(r) and r["tsa_yoy"] is not None]
    result = {"sample": name, "rule": rule}
    for key in UAL_COLUMNS:
        pairs = [(r["tsa_yoy"], r[f"ual_{key}_yoy"]) for r in picked if r[f"ual_{key}_yoy"] is not None]
        r_value = _correlation(pairs)
        result[f"tsa_vs_ual_{key}"] = {"r": None if r_value is None else round(r_value, 4), "n": len(pairs)}
    both = [r["period"] for r in picked if any(r[f"ual_{key}_yoy"] is not None for key in UAL_COLUMNS)]
    return {**result, "n": len(both), "first": both[0] if both else None, "last": both[-1] if both else None}


def versus_united(quarters: list[dict], ual_csv: Path = UAL_CSV) -> dict:
    """TSA growth beside United's capacity, traffic and unit-revenue growth, quarter by quarter."""
    path = Path(ual_csv)
    shown = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
    if not path.exists():
        return {"value": None, "reason": f"{shown} does not exist yet; rebuild with scripts/lab/fetch_tsa.py "
                                         "--offline once it does."}
    try:
        growth = load_ual_growth(path)
    except (ValueError, KeyError) as exc:
        return {"value": None, "reason": f"{shown} could not be read: {exc}"}
    rows = []
    for q in quarters:
        if not q["complete"] or q["yoy"] is None:
            continue
        united = growth.get(q["period"], {})
        rows.append({"period": q["period"], "tsa_yoy": round(q["yoy"], 6),
                     **{f"ual_{key}_yoy": None if united.get(key) is None else round(united[key], 6)
                        for key in UAL_COLUMNS},
                     "pandemic": q["pandemic"], "yoy_base_pandemic": q["yoy_base_pandemic"]})
    samples = [
        _sample(rows, "non_pandemic", "Complete quarters where neither the quarter nor the same quarter a year "
                "earlier is a pandemic quarter.", lambda r: not r["pandemic"] and not r["yoy_base_pandemic"]),
        _sample(rows, "pandemic_quarters_dropped_base_kept", "Complete quarters that are not pandemic quarters; "
                "growth measured against a pandemic quarter stays in.", lambda r: not r["pandemic"]),
        _sample(rows, "all_quarters", "Every complete quarter with a growth rate, pandemic quarters included.",
                lambda r: True)]
    return {"value": {
        "source": {"path": shown, "sha256": byte_hash(path.read_bytes())},
        "columns": {"tsa_yoy": "TSA mean daily travelers, growth on the same quarter a year earlier",
                    "ual_asm_yoy": "United available seat miles (capacity), same comparison",
                    "ual_rpm_yoy": "United revenue passenger miles (traffic), same comparison",
                    "ual_trasm_yoy": "United total revenue per available seat mile (unit revenue), same comparison"},
        "rows": rows, "correlation": samples[0], "other_samples": samples[1:],
        "note": "Pearson correlation of the quarterly growth rates. United's growth compares each quarter's "
                "earnings-release figure with the figure released for the same quarter a year earlier. The "
                "non_pandemic sample was fixed before any correlation was computed; the other two samples are "
                "listed so the choice can be checked."}}


def _millions(value: float) -> str:
    return f"{value / 1e6:.2f} million"


def _versus(growth: float) -> str:
    """'1.23% above', '0.45% below' or 'level with', from a growth fraction."""
    if round(abs(growth) * 100, 2) == 0:
        return "level with"
    return f"{abs(growth) * 100:.2f}% {'above' if growth > 0 else 'below'}"


def _quarter_words(period: str) -> str:
    year, quarter = int(period[:4]), int(period[5])
    return f"{MONTHS[3 * quarter - 3]} to {MONTHS[3 * quarter - 1]} {year}"


def _quarter_fact(quarters: dict[str, dict], period: str) -> dict:
    q = quarters.get(period)
    if q is None:
        return {"period": period, "months": _quarter_words(period), "yoy": None,
                "reason": "No TSA counts in this quarter."}
    return {"period": period, "months": _quarter_words(period),
            "mean_daily_travelers": round(q["mean_daily_travelers"], 1),
            "prior_mean_daily_travelers": None if q["prior_mean_daily_travelers"] is None
            else round(q["prior_mean_daily_travelers"], 1),
            "yoy": None if q["yoy"] is None else round(q["yoy"], 6),
            "days_used": q["days_used"], "prior_days_used": q["prior_days_used"], "complete": q["complete"],
            "yoy_basis": q["yoy_basis"]}


def _quarter_sentence(fact: dict) -> str:
    if fact.get("yoy") is None:
        return f"{fact['period']} has no year-over-year figure."
    partial = "" if fact["complete"] else f" (first {fact['days_used']} days only)"
    return (f"In {fact['months']}{partial} TSA screened an average of {_millions(fact['mean_daily_travelers'])} "
            f"travelers a day, {_versus(fact['yoy'])} the same period a year earlier ({fact['days_used']} days "
            f"against {fact['prior_days_used']}).")


def _united_sentence(united: dict) -> str:
    if not united["value"]:
        return "The comparison with United was not computed: " + united["reason"]
    c = united["value"]["correlation"]
    if not c["n"]:
        return "No quarter outside the pandemic has both a TSA and a United growth rate."
    parts = []
    for key, word in (("asm", "capacity"), ("rpm", "traffic"), ("trasm", "unit revenue")):
        pair = c[f"tsa_vs_ual_{key}"]
        parts.append(f"{word} not computed (n={pair['n']})" if pair["r"] is None
                     else f"{pair['r']:.2f} for {word} (n={pair['n']})")
    return (f"Across the {c['n']} quarters from {c['first']} to {c['last']} that the pandemic does not touch, the "
            f"correlation of TSA growth with United's growth is {', '.join(parts)}.")


def _pandemic_limit(quarters: list[dict]) -> str:
    marked = [q["period"] for q in quarters if q["pandemic"]]
    based = [q["period"] for q in quarters if q["yoy_base_pandemic"]]
    if not marked:
        return "No quarter met the pandemic rule."
    text = (f"Pandemic quarters ({marked[0]} to {marked[-1]}) are marked: a quarter is marked from {PANDEMIC_START} "
            f"until the mean is back at or above the same quarter of {BASE_YEAR}.")
    if based:
        text += (f" Growth in {based[0]} to {based[-1]} is measured against a pandemic quarter and is marked "
                 "yoy_base_pandemic.")
    return text


def demand_signal(tsa_csv: Path = TSA_CSV, ual_csv: Path = UAL_CSV, raw_inputs: list[dict] | None = None,
                  page_notes: list[str] | None = None, now: datetime | None = None,
                  target_quarter: str = TARGET_QUARTER) -> dict:
    """The demand_signal.json payload, computed from the tidy CSV (and United's quarterly file when present)."""
    tsa_csv = Path(tsa_csv)
    dates, values = load_tsa_daily(tsa_csv)
    average = trailing_mean(dates, values, 7)
    growth = growth_vs_days_earlier(dates, average, WEEKDAY_LAG)
    daily = [{"date": d.isoformat(), "travelers_7d": None if math.isnan(a) else round(float(a), 1),
              "yoy_7d": None if math.isnan(g) else round(float(g), 6)}
             for d, a, g in zip(dates, average, growth) if d >= DAILY_START]
    quarters = mark_pandemic(period_means(dates, values, "quarter"))
    months = period_means(dates, values, "month")
    pandemic_by_quarter = {q["period"]: q["pandemic"] for q in quarters}
    gaps = missing_ranges(dates)

    def tidy(row: dict, label: str) -> dict:
        return {label: row["period"], "mean_daily_travelers": round(row["mean_daily_travelers"], 1),
                "yoy": None if row["yoy"] is None else round(row["yoy"], 6), "days_used": row["days_used"],
                "days_in_period": row["days_in_period"], "complete": row["complete"],
                "prior_days_used": row["prior_days_used"], "yoy_basis": row["yoy_basis"]}

    quarterly = [{**tidy(q, "period"), "vs_2019": None if q["vs_2019"] is None else round(q["vs_2019"], 6),
                  "pandemic": q["pandemic"], "yoy_base_pandemic": q["yoy_base_pandemic"]} for q in quarters]
    monthly = [{**tidy(m, "month"),
                "pandemic": pandemic_by_quarter[_bounds(date.fromisoformat(m["period_start"]), "quarter")[0]]}
               for m in months]

    by_period = {q["period"]: q for q in quarters}
    year, quarter = int(target_quarter[:4]), int(target_quarter[5])
    previous_quarter = f"{year - 1}Q4" if quarter == 1 else f"{year}Q{quarter - 1}"
    target, previous = _quarter_fact(by_period, target_quarter), _quarter_fact(by_period, previous_quarter)
    united = versus_united(quarters, ual_csv)

    latest = next((row for row in reversed(daily) if row["yoy_7d"] is not None), None)
    partial = [q for q in quarters if not q["complete"]]

    def growth_clause(fact: dict) -> str:
        if fact.get("yoy") is None:
            return f"{fact['period']} has no year-over-year figure"
        return f"{_versus(fact['yoy'])} a year earlier in {fact['period']}"

    headline = f"TSA daily travelers: {growth_clause(target)}; {growth_clause(previous)}."
    summary = [f"TSA publishes how many people pass through its airport checkpoints each day. This file runs from "
               f"{dates[0].isoformat()} to {dates[-1].isoformat()}: {len(dates):,} days, "
               + ("with no date missing." if not gaps else
                  f"with {sum(g['days'] for g in gaps):,} dates missing inside that range."),
               _quarter_sentence(target), _quarter_sentence(previous)]
    if latest:
        summary.append(f"The 7-day average ending {latest['date']} is {_millions(latest['travelers_7d'])} a day, "
                       f"{_versus(latest['yoy_7d'])} the same weekdays {WEEKDAY_LAG} days earlier.")
    summary.append(_united_sentence(united))
    summary.append("The count covers every airline and airport and counts people screened, not tickets or revenue.")

    limits = [
        "The count is TSA's national total: all airlines and all airports with a TSA checkpoint. It is not a United "
        "figure and says nothing about United's share.",
        "It counts people screened at checkpoints, not tickets, fares, miles flown or revenue.",
        "United's ASM, RPM and TRASM cover its whole network, including flights that start outside the United "
        "States, where TSA does not screen.",
        f"The series starts on {dates[0].isoformat()}, so it cannot inform the 2013-2019 fit period, and "
        f"year-over-year growth starts in {dates[0].year + 1}.",
        "TSA revises the numbers. This file holds the values served on the retrieval dates listed under inputs, "
        "not the values first published." + "".join(f' TSA page note: "{note}"' for note in page_notes or []),
        "The running year comes from TSA's running-year page and earlier years from its per-year archive pages. "
        "Growth for the running year therefore compares a page TSA may still revise with pages it has already "
        "revised; the stored pages do not show how large such a revision is.",
        _pandemic_limit(quarters),
        f"The daily growth rate compares with {WEEKDAY_LAG} days earlier, which lines up weekdays but not holidays: "
        "a holiday that falls in a different week than a year before moves the rate for reasons other than demand.",
        "Month and quarter growth compares calendar periods, so the two years can differ in their mix of weekdays "
        "and by a leap day.",
        *[f"{q['period']} is incomplete ({q['days_used']} of {q['days_in_period']} days); its growth compares the "
          "prior year from the quarter's start to the same month and day." for q in partial],
        "This is an observed series, not a forecast. Nothing here predicts future travel.",
    ]
    if united["value"]:
        n = united["value"]["correlation"]["n"]
        limits.append(f"The United correlation rests on {n} quarters. It describes those quarters and does not "
                      "establish that TSA volume drives United's results.")
    now = now or datetime.now(timezone.utc)
    shown = tsa_csv.relative_to(ROOT).as_posix() if tsa_csv.is_relative_to(ROOT) else str(tsa_csv)
    inputs = [{"path": shown, "sha256": byte_hash(tsa_csv.read_bytes())}, *(raw_inputs or [])]
    if united["value"]:
        inputs.append(united["value"]["source"])
    return {
        "generated_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "as_of": dates[-1].isoformat(),
        "inputs": inputs,
        "headline": headline,
        "plain_summary": " ".join(summary),
        "source": {"publisher": "Transportation Security Administration",
                   "page": "https://www.tsa.gov/travel/passenger-volumes",
                   "series": "TSA checkpoint travel numbers, people screened per day"},
        "coverage": {"first": dates[0].isoformat(), "last": dates[-1].isoformat(), "days": len(dates),
                     "missing_days": sum(g["days"] for g in gaps), "missing_ranges": gaps},
        "definitions": {
            "travelers_7d": "Mean of the 7 calendar days ending on the date. Null unless all 7 days are present.",
            "yoy_7d": f"travelers_7d divided by travelers_7d {WEEKDAY_LAG} days earlier, minus 1. {WEEKDAY_LAG} "
                      "days is 52 weeks, so both windows hold the same weekdays. Uses no count dated after the date.",
            "mean_daily_travelers": "Mean of the daily counts published in the month or quarter.",
            "yoy": "mean_daily_travelers divided by the mean of the same period a year earlier, minus 1. For a "
                   "period the data ends inside, the prior-year mean runs from the period's start to the same month "
                   "and day only. Null when the prior-year window is missing a day.",
            "days_used": "Days with a count behind mean_daily_travelers; prior_days_used is the same for the "
                         "prior-year mean.",
            "vs_2019": f"mean_daily_travelers divided by the same quarter of {BASE_YEAR}.",
            "pandemic": f"True from {PANDEMIC_START} until the first complete quarter with vs_2019 at or above 1.",
            "yoy_base_pandemic": "True when the same quarter a year earlier is a pandemic quarter.",
            "growth_units": "Fractions: 0.012 is 1.2%.",
        },
        "daily": daily,
        "quarterly": quarterly,
        "current_quarter_note": {target_quarter: target, previous_quarter: previous,
                                 "text": f"{_quarter_sentence(target)} {_quarter_sentence(previous)}"},
        "versus_united": united["value"],
        "versus_united_reason": None if united["value"] else united["reason"],
        "monthly_control": monthly,
        "limits": limits,
    }


def write_demand_signal(payload: dict, path: Path = DEMAND_JSON) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=1, allow_nan=False) + "\n")
