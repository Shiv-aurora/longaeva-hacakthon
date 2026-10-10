"""Is the simulator's range the right width? One quantity, computed the same way in United's history and in the simulator.

History: how far United's revenue per available seat mile landed from a naive forecast
made 2, 3 and 4 quarters earlier. The forecast is last year's same quarter moved by the
year-over-year growth seen when the forecast was made, so its miss is
    log x[t] - log x[t-h] - (log x[t-4] - log x[t-4-h]),
the change in year-over-year growth over h quarters. Seasonality and steady growth
cancel. A miss that uses any quarter from 2020Q1 to 2021Q4 is left out.

Simulator: it has no seasonality and starts from the last reported quarter, so the same
distance is its no-shock revenue per seat mile in the 1st, 2nd and 3rd reported quarter
(2, 3 and 4 quarters after the last reported one). A naive forecast's miss carries last
year's surprise as well as this year's, so it is compared with the gap between two
independent simulated worlds, not with one world's distance from the median.

The test is of the engine's assumed `demand_monthly_log_sd`: the share of real misses
from 2022 onward inside the simulator's 80% range. One other value is tried, fitted on
misses dated 2013-2019 so that the simulator's gaps have the same root mean square, and
it is reported with the same coverage. The lab runner keeps the assumed value; the
fitted one stays in the file as the variant that was tried. Operating margin and the
fuel price paid go through the same comparison, and the range is rerun with one noise
switched off at a time to show which of them sets its width.

scripts/lab/build_range_check.py writes the result to app/static/data/range_check.json;
app/lab/scenario.py reads the `runner` block there to say what the check found.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timezone
import inspect
import json
import math
from pathlib import Path
import re

import numpy as np

from ..data import fuel_volatility
from ..monthly import VERSION, MonthlyScenario, _world
from . import response as R
from . import scenario as S

ROOT = S.ROOT
QUARTERLY = ROOT / "data" / "lab" / "ual_quarterly.csv"
OUTPUT = S.RANGE_CHECK
HORIZONS = (2, 3, 4)  # quarters after the last reported quarter; the simulator's first three reported quarters
PANDEMIC = ("2020Q1", "2021Q4")
FIT_YEARS = (2013, 2019)
CHECK_FROM = 2022
SENSITIVITY_SKIP = "2022Q1"
TARGET_SHARE = .8
ROUGHLY = (.7, .9)  # what "roughly 8 in 10" means here
GRID = tuple(round(.0025 * k, 4) for k in range(25))
ASSUMED = MonthlyScenario.demand_monthly_log_sd
MODE = "measured"
QUANTITIES = {
    "unit_revenue": {"role": "checked", "unit": "percent (log change x 100)",
                     "in_history": "total revenue / available seat miles", "in_simulator": "revenue / available seat miles"},
    "operating_margin": {"role": "cross-check", "unit": "points of revenue",
                         "in_history": "adjusted pre-tax income / total revenue (reported from 2014Q4; the table has no operating income)",
                         "in_simulator": "operating income / revenue"},
    "fuel_price": {"role": "diagnosis", "unit": "percent (log change x 100)", "in_history": "price paid per gallon",
                   "in_simulator": "fuel expense / available seat miles (gallons per seat mile are fixed, so it moves with the price paid)"}}
REQUIRED = ("generated_at", "as_of", "inputs", "question", "headline", "plain_summary", "compared", "check", "fitted_variant",
            "quantities", "simulator", "runner", "limits", "method")


def _index(name: str) -> int:
    return int(name[:4]) * 4 + int(name[5]) - 1


def label(index: int) -> str:
    return f"{index // 4}Q{index % 4 + 1}"


def load_history(path: Path | None = None) -> tuple[dict[str, dict[int, float]], str]:
    """Quarterly levels by quantity, keyed by quarter index, and the last period end. Blank cells are left out."""
    levels: dict[str, dict[int, float]] = {name: {} for name in (*QUANTITIES, "fuel_share")}
    last = ""
    with (QUARTERLY if path is None else Path(path)).open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            def cell(key):
                try:
                    return float(row[key])
                except (KeyError, TypeError, ValueError):
                    return None
            quarter = int(row["period_end"][:4]) * 4 + (int(row["period_end"][5:7]) - 1) // 3
            last = max(last, row["period_end"])
            revenue, seats, income, price, fuel = (cell(k) for k in ("total_revenue_musd", "asm_m", "adj_pretax_income_musd",
                                                                     "fuel_price_usd_gal", "fuel_expense_musd"))
            if revenue and seats and revenue > 0 and seats > 0:
                levels["unit_revenue"][quarter] = math.log(revenue / seats)
                if income is not None:
                    levels["operating_margin"][quarter] = income / revenue
                if fuel is not None:
                    levels["fuel_share"][quarter] = fuel / revenue
            if price and price > 0:
                levels["fuel_price"][quarter] = math.log(price)
    return levels, last


def surprises(levels: dict[int, float], ahead: int, skip: tuple[str, ...] = ()) -> list[dict]:
    """Misses of the naive seasonal forecast made `ahead` quarters earlier, with the split each belongs to."""
    low, high = _index(PANDEMIC[0]), _index(PANDEMIC[1])
    skipped = {_index(name) for name in skip}
    rows = []
    for t in sorted(levels):
        used = (t, t - ahead, t - 4, t - 4 - ahead)
        if any(q not in levels or low <= q <= high or q in skipped for q in used):
            continue
        split = ("fit" if FIT_YEARS[0] <= min(used) // 4 and t // 4 <= FIT_YEARS[1]
                 else "check" if min(used) // 4 >= CHECK_FROM else None)
        if split is not None:
            rows.append({"quarter": label(t), "split": split,
                         "value": levels[t] - levels[t - ahead] - levels[t - 4] + levels[t - 4 - ahead]})
    return rows


def simulate(demand_sd: float, mode: str = MODE, paths: int = S.DEFAULT_PATHS, seed: int = 1,
             response_modes: dict | None = None, fuel_noise: bool = True) -> dict:
    """No-shock reported quarters on the lab runner's own worlds: one array (paths, quarters) per quantity."""
    c = S._context()
    req, _, engine_pct, errors = S._checked({"fuel_shock_pct": 0, "horizon_months": 12, "paths": paths, "seed": seed,
                                             "demand_monthly_log_sd": demand_sd}, c)
    if errors:
        raise ValueError("; ".join(errors))
    response = {**S.default_responses(skip=response_modes or ()), **(response_modes or {})}[mode]
    if not isinstance(response, R.Response):
        raise R.NotComputed(f"the {mode} response is not built: {response}")
    s = S._engine_scenario(req, c, engine_pct)
    raw = S._worlds(s, c)
    if not fuel_noise:
        raw["fuel_world"], raw["demand_world"] = _world(s, raw["spot_usd_per_gallon"], 0.0)
    roll = S._roll(raw, s, response.monthly_shares(s.paths, s.horizon_months, s.seed), shocked=False)
    periods = list(S._periods(raw["months"], c["lead"]).items())[:len(HORIZONS)]

    def total(name):
        return np.stack([roll[name][:, index].sum(axis=1) for _, index in periods], axis=1)

    revenue, seats, income, fuel = (total(n) for n in ("revenue_musd", "asm_million", "operating_income_musd", "fuel_expense_musd"))
    return {"periods": [period for period, _ in periods], "unit_revenue": np.log(revenue / seats),
            "operating_margin": income / revenue, "fuel_price": np.log(fuel / seats), "operating_income_musd": income}


def gap(values: np.ndarray) -> np.ndarray:
    """Each simulated world against another, independent one."""
    return values - np.roll(values, len(values) // 2, axis=0)


def mean_squares(sim: dict, name: str = "unit_revenue") -> list[float]:
    return [float(v) for v in np.mean(gap(sim[name]) ** 2, axis=0)]


def curve(mode: str = MODE, **settings) -> list[dict]:
    """The simulator's mean squared gap in unit revenue, by horizon, at each demand noise on the grid."""
    return [{"demand_monthly_log_sd": sd, "mean_square": mean_squares(simulate(sd, mode, **settings))} for sd in GRID]


def _pooled(by_horizon: list[float], counts: list[int]) -> float:
    return sum(n * v for n, v in zip(counts, by_horizon)) / sum(counts)


def fit(values: dict[int, list[float]], spread: list[dict]) -> dict:
    """The demand noise at which the simulator's gaps have the history's root mean square, horizons weighted by their counts."""
    counts = [len(values[h]) for h in HORIZONS]
    target = sum(v * v for h in HORIZONS for v in values[h]) / sum(counts)
    xs = [row["demand_monthly_log_sd"] for row in spread]
    ys = [_pooled(row["mean_square"], counts) for row in spread]
    if any(b <= a for a, b in zip(ys, ys[1:])):
        raise ValueError("the simulated spread must rise with demand noise")
    return {"value": round(float(np.interp(target, ys, xs)), 4), "target_rms": math.sqrt(target), "observations": sum(counts),
            "at_lower_bound": bool(target <= ys[0]), "at_upper_bound": bool(target >= ys[-1])}


def simulation_error(sim: dict, counts: list[int], spread: list[dict], value: float) -> float:
    """Monte Carlo standard error of the fitted value: batch means of the pooled squared gap over the curve's slope at the value."""
    pairs = gap(sim["unit_revenue"])[:len(sim["unit_revenue"]) // 2] ** 2 @ (np.array(counts) / sum(counts))
    spread_of_means = float(np.std([batch.mean() for batch in np.array_split(pairs, S.BATCHES)], ddof=1)) / math.sqrt(S.BATCHES)
    xs = [row["demand_monthly_log_sd"] for row in spread]
    ys = [_pooled(row["mean_square"], counts) for row in spread]
    i = min(max(int(np.searchsorted(xs, value, side="right")) - 1, 0), len(xs) - 2)
    return spread_of_means * (xs[i + 1] - xs[i]) / (ys[i + 1] - ys[i])


def bands(sim: dict, name: str) -> list[tuple[float, float]]:
    """The simulator's 80% range for the gap, by horizon."""
    return [(float(low), float(high)) for low, high in np.quantile(gap(sim[name]), [.1, .9], axis=0).T]


def coverage(rows: dict[int, list[dict]], ranges: list[tuple[float, float]], split: str) -> dict:
    by_horizon = []
    for (low, high), h in zip(ranges, HORIZONS):
        values = [r["value"] for r in rows[h] if r["split"] == split]
        by_horizon.append({"quarters_ahead": h, "inside": sum(low <= v <= high for v in values), "of": len(values)})
    inside, of = sum(r["inside"] for r in by_horizon), sum(r["of"] for r in by_horizon)
    return {"inside": inside, "of": of, "share": round(inside / of, 3) if of else None, "by_horizon": by_horizon}


def roughly(share: float | None) -> bool:
    return share is not None and ROUGHLY[0] <= share <= ROUGHLY[1]


def reading(share: float | None) -> str:
    """A coverage share of the 80% range in a plain word."""
    return "about right" if roughly(share) else "too wide" if share is not None and share > ROUGHLY[1] else "too narrow"


def _musd(value: float) -> str:
    return f"-${abs(value):,.0f} million" if value < 0 else f"${value:,.0f} million"


def _held(c: dict) -> str:
    return f"{c['inside']} of {c['of']} ({c['share']:.0%})"


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {d:%B %Y}"


def fuel_window(known: list[dict], history: Path | None = None) -> dict:
    """The engine's fuel noise, the days it is measured on, and how those days compare with the longer fuel history."""
    window = inspect.signature(fuel_volatility).parameters["window"].default
    monthly = fuel_volatility(known) * math.sqrt(21)
    history = S.FUEL_DAILY if history is None else Path(history)
    against = {"status": "not_computed", "reason": f"{history.name} has no longer fuel history"}
    if history.is_file():
        with history.open(newline="", encoding="utf-8") as stream:
            rows = [(row["date"], float(row["usd_per_gallon"])) for row in csv.DictReader(stream)
                    if row.get("usd_per_gallon") and row["date"] <= known[-1]["date"]]
        days, changes = [day for day, _ in rows][1:], np.diff(np.log([price for _, price in rows]))
        if len(changes) > window and math.isclose(float(changes[-window:].std(ddof=1)) * math.sqrt(21), monthly, rel_tol=1e-6):
            def over(first: str, last: str) -> float:
                return float(np.std([c for day, c in zip(days, changes) if first <= day <= last], ddof=1)) * math.sqrt(21)

            since = f"{FIT_YEARS[0]}-01-01"
            rolling = [float(changes[i - window:i].std(ddof=1)) * math.sqrt(21) for i in range(window, len(changes) + 1) if days[i - 1] >= since]
            calm, recent = over(since, f"{FIT_YEARS[1]}-12-31"), over(f"{CHECK_FROM}-01-01", known[-1]["date"])
            against = {"status": "ok", "source": S._file(history), "fit_years_monthly_log_sd": round(calm, 4), "times_fit_years": round(monthly / calm, 2),
                       "check_years_monthly_log_sd": round(recent, 4), "times_check_years": round(monthly / recent, 2),
                       "windows_since": since, "windows": len(rolling), "median_window_monthly_log_sd": round(float(np.median(rolling)), 4),
                       "share_of_windows_less_volatile": round(float(np.mean(np.array(rolling) < rolling[-1])), 3)}
        else:
            against["reason"] = f"{history.name} does not match the engine's fuel prices over the window"
    return {"monthly_log_sd": round(monthly, 4), "trading_days": window, "from": known[-window - 1]["date"], "to": known[-1]["date"],
            "rule": "standard deviation of the daily log changes in Gulf Coast jet fuel over the last trading days before the information cutoff, "
                    "times the square root of 21 (app/data.py, app/monthly.py); the engine sets it, the lab runner does not",
            "against_history": against}


def _quantity(name: str, rows: dict, alt: dict, sims: dict) -> dict:
    """History and simulator spread of one quantity, and how much of the history each 80% range holds."""
    scale = 100
    history = {}
    for split in ("fit", "check"):
        history[split] = []
        for h in HORIZONS:
            picked = [r for r in rows[h] if r["split"] == split]
            values = np.array([r["value"] for r in picked]) * scale
            history[split].append({"quarters_ahead": h, "n": len(picked), "first": picked[0]["quarter"], "last": picked[-1]["quarter"],
                                   "rms": round(float(np.sqrt(np.mean(values ** 2))), 3),
                                   "p10": round(float(np.quantile(values, .1)), 3), "p90": round(float(np.quantile(values, .9)), 3)})
    simulator, cover, ratio = {}, {}, {}
    for which, sim in sims.items():
        ranges, squares = bands(sim, name), mean_squares(sim, name)
        simulator[which] = [{"quarters_ahead": h, "period": period, "p10": round(low * scale, 3), "p90": round(high * scale, 3),
                             "rms": round(math.sqrt(square) * scale, 3)}
                            for h, period, (low, high), square in zip(HORIZONS, sim["periods"], ranges, squares)]
        cover[which] = {"fit": coverage(rows, ranges, "fit"), "check": coverage(rows, ranges, "check"),
                        f"check_without_{SENSITIVITY_SKIP}": coverage(alt, ranges, "check")}
        ratio[which] = {split: [round(s["rms"] / r["rms"], 2) for s, r in zip(simulator[which], history[split])] for split in ("fit", "check")}
    return {**QUANTITIES[name], "history": history, "simulator": simulator, "coverage": cover, "simulator_to_history_rms": ratio}


def _runner_run(request: dict, demand_sd: float, response_modes: dict | None) -> dict:
    run = S.run_scenario({**request, "demand_monthly_log_sd": demand_sd}, response_modes)
    if run["status"] != "ok":
        raise ValueError("; ".join(run["errors"]))
    modes = {name: {"quarters": [{"period": q["period"], "operating_income_musd": q["operating_income_musd"]} for q in mode["quarters"][:4]],
                    "fcf_negative_12m": mode["prob"]["fcf_negative_12m"]}
             for name, mode in run["modes"].items() if mode["status"] == "ok"}
    return {"demand_monthly_log_sd": demand_sd, "fuel_usd_gal": run["fuel"]["level_usd_gal"], "modes": modes, "inputs": run["inputs"]}


def _width(sim: dict) -> float:
    low, high = np.quantile(sim["operating_income_musd"][:, 0], [.1, .9])
    return round(float(high - low), 1)


def build(quarterly: Path | None = None, generated_at: str | None = None, response_modes: dict | None = None,
          paths: int = S.DEFAULT_PATHS, seed: int = 1) -> dict:
    quarterly = QUARTERLY if quarterly is None else Path(quarterly)
    settings = {"paths": paths, "seed": seed, "response_modes": response_modes}
    levels, last = load_history(quarterly)
    rows = {name: {h: surprises(levels[name], h) for h in HORIZONS} for name in QUANTITIES}
    alt = {name: {h: surprises(levels[name], h, skip=(SENSITIVITY_SKIP,)) for h in HORIZONS} for name in QUANTITIES}
    fit_rows = {h: [r for r in rows["unit_revenue"][h] if r["split"] == "fit"] for h in HORIZONS}
    if min(len(v) for v in fit_rows.values()) < 8:
        raise ValueError(f"{quarterly.name} has too few {FIT_YEARS[0]}-{FIT_YEARS[1]} quarters to fit demand noise")
    counts = [len(fit_rows[h]) for h in HORIZONS]

    # The variant that is tried against the assumption: demand noise fitted to the earlier years.
    spread, demand_only = curve(MODE, **settings), curve("costs_only", **settings)
    fitted = fit({h: [r["value"] for r in fit_rows[h]] for h in HORIZONS}, spread)
    bound = fit({h: [r["value"] for r in fit_rows[h]] for h in HORIZONS}, demand_only)
    years = sorted({r["quarter"][:4] for h in HORIZONS for r in fit_rows[h]})
    without = {year: fit({h: [r["value"] for r in fit_rows[h] if r["quarter"][:4] != year] for h in HORIZONS}, spread)["value"] for year in years}
    value = fitted["value"]
    sims = {"assumed": simulate(ASSUMED, **settings), "fitted": simulate(value, **settings)}
    quantities = {name: _quantity(name, rows[name], alt[name], sims) for name in QUANTITIES}
    noise_error = round(simulation_error(sims["fitted"], counts, spread, value), 4)

    fit_label, check_label = f"{FIT_YEARS[0]}-{FIT_YEARS[1]}", f"{CHECK_FROM}-{last[:4]}"
    unit, margin, fuel = (quantities[name] for name in ("unit_revenue", "operating_margin", "fuel_price"))
    cover = unit["coverage"]
    passes = {which: roughly(cover[which]["check"]["share"]) for which in ("assumed", "fitted")}
    kept = ("so the assumption is kept" if passes["assumed"] and not passes["fitted"] else
            "both pass, and the assumption is kept" if passes["assumed"] else "so the assumed value fails this check and is still the default")

    request = {"fuel_shock_pct": 0, "horizon_months": 12, "paths": paths, "seed": seed}
    values = (("assumed", ASSUMED), ("fitted", value))
    no_shock = {which: _runner_run(request, sd, response_modes) for which, sd in values}
    today = S.today_request()
    if today.get("status") == "ok":
        runs = {which: _runner_run({**today["request"], "paths": paths, "seed": seed}, sd, response_modes) for which, sd in values}
        today_block = {"status": "ok", "request": today["request"], "fuel_as_of": today["context"]["fuel"]["date"], **runs}
    else:
        runs = {}
        today_block = {"status": "not_computed", "reason": today.get("reason") or "; ".join(today.get("errors", []))}

    # What sets the width: the same range with one noise switched off at a time.
    first = sims["assumed"]["periods"][0]
    whole, fuel_only = _width(sims["assumed"]), _width(simulate(0.0, **settings))
    demand_only_width = _width(simulate(ASSUMED, fuel_noise=False, **settings))
    width = {"period": first, "quantity": "no-shock operating income, P10 to P90, measured behavior", "musd": whole,
             "demand_noise_off_musd": fuel_only, "share_left_with_demand_noise_off": round(fuel_only / whole, 3),
             "fuel_noise_off_musd": demand_only_width, "share_left_with_fuel_noise_off": round(demand_only_width / whole, 3),
             "with_fitted_demand_noise_musd": _width(sims["fitted"])}
    fuel_led = width["share_left_with_demand_noise_off"] > max(.5, width["share_left_with_fuel_noise_off"])
    c = S._context()
    fuel_noise = fuel_window([r for r in c["fuel"] if r["date"] <= S.INFORMATION_CUTOFF])
    against = fuel_noise["against_history"]
    days = f"the {fuel_noise['trading_days']} trading days before the cutoff ({_day(fuel_noise['from'])} to {_day(fuel_noise['to'])})"
    shares = levels["fuel_share"]
    fuel_share = {"latest_quarter": label(max(shares)), "latest": round(shares[max(shares)], 3),
                  "mean_fit_years": round(float(np.mean([v for q, v in shares.items() if FIT_YEARS[0] <= q // 4 <= FIT_YEARS[1]])), 3),
                  "mean_check_years": round(float(np.mean([v for q, v in shares.items() if q // 4 >= CHECK_FROM])), 3)}

    held = {which: {split: {k: cover[which][split][k] for k in ("inside", "of", "share")} for split in ("fit", "check")} for which in ("assumed", "fitted")}
    volatile = ""
    if against["status"] == "ok":
        unusual = "an unusually volatile stretch: " if against["share_of_windows_less_volatile"] >= .9 else ""
        volatile = f", {unusual}more volatile than {against['share_of_windows_less_volatile']:.0%} of such windows since {against['windows_since'][:4]}"
    left = f"with demand noise switched off, {width['share_left_with_demand_noise_off']:.0%} of the width of {first} operating income remains"
    fuel_words = (f"Fuel noise comes from {days}{volatile}. " + (f"It sets most of the range's width: {left}." if fuel_led else f"{left[0].upper()}{left[1:]}."))
    runner = {"demand_monthly_log_sd": ASSUMED, "status": "assumed",
              "checked_on": "revenue per available seat mile against a naive seasonal forecast made 2 to 4 quarters earlier",
              "check_years": [CHECK_FROM, int(last[:4])], "target_share": TARGET_SHARE, "coverage": held["assumed"], "check_passed": passes["assumed"],
              "fitted_variant": {"demand_monthly_log_sd": value, "fit_years": list(FIT_YEARS), "coverage": held["fitted"],
                                 "check_passed": passes["fitted"], "used": False},
              "fuel_noise": {k: fuel_noise[k] for k in ("monthly_log_sd", "trading_days", "from", "to")}
                            | ({k: against[k] for k in ("times_fit_years", "share_of_windows_less_volatile")} if against["status"] == "ok" else {}),
              "width": width,
              "statement": f"Demand noise is an assumed {ASSUMED:.0%} a month. Checked against United's unit revenue, "
                           f"{held['assumed']['check']['inside']} of {held['assumed']['check']['of']} outcomes in {check_label} "
                           f"({held['assumed']['check']['share']:.0%}) fell inside the simulator's 80% range. {fuel_words} "
                           f"Source: {OUTPUT.relative_to(ROOT).as_posix()}."}

    a, f = held["assumed"], held["fitted"]
    verdict = (f"Roughly 8 in 10 is read as {ROUGHLY[0]:.0%} to {ROUGHLY[1]:.0%} inside: on {check_label} the assumed value "
               f"{'passes' if passes['assumed'] else 'fails'} and the fitted value {'passes' if passes['fitted'] else 'fails'}.")
    headline = (f"With demand noise at the assumed {ASSUMED:.0%} a month, the simulator's 80% range held {a['check']['inside']} of United's "
                f"{a['check']['of']} unit-revenue outcomes in {check_label} ({a['check']['share']:.0%}, against a nominal {TARGET_SHARE:.0%}); "
                f"a value fitted on {fit_label} ({value:.2%}) held {f['check']['inside']} ({f['check']['share']:.0%}), {kept}.")
    ranges = {which: no_shock[which]["modes"][MODE]["quarters"][0]["operating_income_musd"] for which in no_shock if MODE in no_shock[which]["modes"]}
    summary = [
        headline,
        "The question is whether the simulator's range is the right width.",
        "Compared: how far United's revenue per seat mile landed from a naive forecast made two, three and four quarters earlier "
        "(last year's quarter, moved by the latest year-over-year growth), against the gap between two simulated worlds at the same distance.",
        verdict,
        f"On {fit_label} the assumed value held {_held(a['fit'])}, which is {reading(a['fit']['share'])} for those years; "
        f"the fitted value held {_held(f['fit'])} there.",
        f"The value tried and set aside is {value:.2%} a month, where the simulator matches the {fit_label} spread. "
        f"Leaving out any one year gives {min(without.values()):.2%} to {max(without.values()):.2%}; if every surprise were demand it would be {bound['value']:.2%}.",
        f"Operating margin, same comparison: the simulator's 80% range holds {_held(margin['coverage']['assumed']['fit'])} outcomes in "
        f"{margin['history']['fit'][0]['first'][:4]}-{FIT_YEARS[1]}, which is {reading(margin['coverage']['assumed']['fit']['share'])}, and "
        f"{_held(margin['coverage']['assumed']['check'])} in {check_label}, which is {reading(margin['coverage']['assumed']['check']['share'])}."]
    wide = margin["simulator_to_history_rms"]["assumed"]
    summary.append(f"Simulated margin gaps are {wide['fit'][0]:.1f} times the earlier spread two quarters out and {wide['fit'][-1]:.1f} times at four; "
                   f"against the later one, {wide['check'][0]:.1f} and {wide['check'][-1]:.1f} times.")
    summary.append((f"Fuel volatility, not demand noise, sets most of the width. " if fuel_led else "")
                   + f"The no-shock range for {first} operating income is {_musd(whole)} wide (P10 to P90). With demand noise switched off it is still "
                   f"{_musd(fuel_only)} ({width['share_left_with_demand_noise_off']:.0%}); with fuel noise switched off it is "
                   f"{_musd(demand_only_width)} ({width['share_left_with_fuel_noise_off']:.0%}).")
    summary.append(f"Fuel volatility is measured over {days}."
                   + (f" That stretch was {against['times_fit_years']:.1f} times as volatile as {fit_label} and more volatile than "
                      f"{against['share_of_windows_less_volatile']:.0%} of such windows since {against['windows_since'][:4]}." if against["status"] == "ok" else ""))
    ratios = fuel["simulator_to_history_rms"]["assumed"]
    summary.append(f"Simulated fuel-price gaps are {ratios['fit'][0]:.1f} times the {fit_label} spread two quarters out and {ratios['fit'][-1]:.1f} times at four; "
                   f"against {check_label}, {ratios['check'][0]:.1f} and {ratios['check'][-1]:.1f} times.")
    if len(ranges) == 2:
        summary.append(f"The no-shock range for {first} operating income is {_musd(ranges['assumed']['p10'])} to {_musd(ranges['assumed']['p90'])}; "
                       f"the fitted value would make it {_musd(ranges['fitted']['p10'])} to {_musd(ranges['fitted']['p90'])}, "
                       f"{1 - width['with_fitted_demand_noise_musd'] / whole:.0%} narrower.")
    if runs and all(MODE in r["modes"] for r in runs.values()):
        share = {which: runs[which]["modes"][MODE]["fcf_negative_12m"] for which in runs}
        summary.append(f"At today's fuel price (${runs['assumed']['fuel_usd_gal']:.2f} a gallon paid), {share['assumed']:.1%} of simulated runs have negative "
                       f"free cash flow over 12 months with measured behavior; the fitted value would make it {share['fitted']:.1%}.")

    floor = math.sqrt(_pooled(spread[0]["mean_square"], counts)) / fitted["target_rms"]
    skip = f"check_without_{SENSITIVITY_SKIP}"
    limits = [
        "The simulator has no seasonality. The history is compared through year-over-year growth, which removes it there; nothing is tested about seasonal quarters.",
        f"The fuel volatility window is short and recent: {days}, the quarter of the fuel shock. "
        + ("One quarter of daily prices sets most of the width measured here, and it is not changed here." if fuel_led else "It is not changed here."),
        f"Few and overlapping outcomes: {a['check']['of']} check outcomes come from {cover['assumed']['check']['by_horizon'][0]['of']} quarters and "
        f"{a['fit']['of']} earlier outcomes from {counts[0]}, so the counts are not independent. {a['check']['share']:.0%} against a nominal "
        f"{TARGET_SHARE:.0%} is a difference of {abs(TARGET_SHARE * a['check']['of'] - a['check']['inside']):.1f} outcomes.",
        f"The assumed value was kept because of its coverage on {check_label}, so that figure is the reason for the choice, not an independent test of it.",
        f"The assumed value is {reading(a['fit']['share'])} for {fit_label} ({a['fit']['inside']} of {a['fit']['of']} inside). Unit revenue was "
        f"{unit['history']['check'][0]['rms'] / unit['history']['fit'][0]['rms']:.1f} times as variable in {check_label} as in {fit_label} two quarters out "
        f"(root mean square {unit['history']['check'][0]['rms']:.1f}% against {unit['history']['fit'][0]['rms']:.1f}%), so no single value fits both periods.",
        f"The rule keeps {SENSITIVITY_SKIP}, a quarter still held down by the pandemic. Without it the check years hold "
        f"{_held(cover['assumed'][skip])} unit-revenue outcomes at the assumed value and {_held(cover['fitted'][skip])} at the fitted one, "
        f"and {_held(margin['coverage']['assumed'][skip])} margin outcomes. That count is a sensitivity, not the check.",
        f"The fitted value is near zero because, with demand noise at zero, fares following fuel noise already give the simulator {floor:.0%} of the "
        f"{fit_label} spread in unit revenue. It depends on the simulator's fuel noise and on the measured recovery curve, and it carries simulation "
        f"error of about {noise_error:.2%} (one standard error at {paths:,} simulated worlds).",
        "Persistence (0.75 a month) is fixed inside the engine and the fuel-demand correlation (0.25) is left as assumed: "
        "about twenty overlapping outcomes per horizon cannot separate them from the noise level.",
        f"The simulator starts from {fuel_share['latest_quarter']}, when fuel was {fuel_share['latest']:.0%} of revenue against "
        f"{fuel_share['mean_fit_years']:.0%} on average in {fit_label}, so the same percentage move in fuel swings its margin more.",
        "The margin comparison uses adjusted pre-tax income, which includes interest and other non-operating items, because the quarterly table has no operating income.",
        "A naive forecast's miss is compared with the gap between two simulated worlds. That is exact only if surprises a year apart are unrelated.",
        "Ranges still leave out parameter and model uncertainty other than the recovery curve's."]

    inputs = {}
    for item in [S._file(quarterly), *(i for run in (*no_shock.values(), *runs.values()) for i in run["inputs"]),
                 *(source for row in today.get("basis", []) for source in row["source"]),
                 *([against["source"]] if against["status"] == "ok" else [])]:
        inputs[item["path"]] = {"path": item["path"], "sha256": item["sha256"]}
    for run in (*no_shock.values(), *runs.values()):
        del run["inputs"]
    return {
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": last,
        "inputs": list(inputs.values()),
        "question": "Is the simulator's range the right width?",
        "headline": headline, "plain_summary": summary,
        "compared": {
            "quantity": "unit_revenue",
            "why": "Demand noise multiplies revenue, so it is tested on revenue. Dividing by seat miles takes out capacity, which the simulator "
                   "plans and the history does not hold fixed. Total revenue per seat mile is used, not passenger revenue, because passenger "
                   "revenue was reclassified in 2018.",
            "history": "The miss of a naive seasonal forecast made h quarters earlier: log x[t] - log x[t-h] - (log x[t-4] - log x[t-4-h]), "
                       "the change in year-over-year growth over h quarters. Seasonality and steady growth cancel.",
            "simulator": "The gap between two independent simulated worlds in the no-shock revenue per seat mile of the matching reported quarter. "
                         "A naive forecast's miss carries last year's surprise as well as this year's, so two draws are compared, not one.",
            "horizons": [{"quarters_ahead": h, "simulator_period": period} for h, period in zip(HORIZONS, sims["assumed"]["periods"])],
            "left_out": f"Any miss that uses a quarter from {PANDEMIC[0]} to {PANDEMIC[1]}.",
            "splits": {"fit": {"years": fit_label, "outcomes": unit["history"]["fit"][0]["first"] + " to " + unit["history"]["fit"][0]["last"]},
                       "check": {"years": check_label, "outcomes": unit["history"]["check"][0]["first"] + " to " + unit["history"]["check"][0]["last"]}},
            "also_compared": {name: QUANTITIES[name]["role"] for name in ("operating_margin", "fuel_price")}},
        "check": {"quantity": "unit_revenue", "years": check_label, "target_share": TARGET_SHARE, "roughly": list(ROUGHLY),
                  "assumed": {"demand_monthly_log_sd": ASSUMED, **cover["assumed"]["check"]},
                  "fitted": {"demand_monthly_log_sd": value, **cover["fitted"]["check"]},
                  "passes": passes, "kept": "assumed", "verdict": verdict},
        "fitted_variant": {
            "status": "rejected" if not passes["fitted"] else "not adopted",
            "why": f"On {check_label} its 80% range held {_held(f['check'])} of real unit-revenue outcomes against {_held(a['check'])} for the assumed value.",
            "parameter": "demand_monthly_log_sd", "assumed": ASSUMED, "fitted": value, "fit_years": list(FIT_YEARS),
            "method": "The value at which the simulator's mean squared gap in unit revenue, horizons weighted by their number of outcomes, "
                      f"equals the history's on {fit_label}. The simulator runs as the lab runner runs it (measured behavior, no shock); "
                      "the gap is interpolated between grid points. Nothing from the check years enters the fit.",
            "observations": fitted["observations"], "target_rms_pct": round(100 * fitted["target_rms"], 3),
            "simulated_rms_at_fitted_pct": round(100 * math.sqrt(_pooled(mean_squares(sims["fitted"]), counts)), 3),
            "at_lower_bound": fitted["at_lower_bound"], "at_upper_bound": fitted["at_upper_bound"],
            "simulation_standard_error": noise_error,
            "leave_one_year_out": without, "range_leaving_one_year_out": [min(without.values()), max(without.values())],
            "if_all_surprise_were_demand": {"fitted": bound["value"],
                                            "meaning": "The same fit with costs-only behavior, where fares do not follow fuel: an upper bound."},
            "coverage": held,
            "curve": [{"demand_monthly_log_sd": row["demand_monthly_log_sd"], "simulated_rms_pct": round(100 * math.sqrt(_pooled(row["mean_square"], counts)), 3)}
                      for row in spread],
            "not_estimated": ["persistence of fuel and demand noise (0.75 a month, fixed inside the engine)", "fuel-demand correlation (0.25, assumed)",
                              "fuel noise (set by the engine from recent daily prices)"]},
        "quantities": quantities,
        "simulator": {"no_shock": no_shock, "today": today_block, "width": width, "fuel_noise": fuel_noise, "fuel_share_of_revenue": fuel_share},
        "runner": runner,
        "limits": limits,
        "method": {"paths": paths, "seed": seed, "mode": MODE, "horizons_quarters_ahead": list(HORIZONS), "grid": list(GRID),
                   "engine_version": VERSION, "information_cutoff": S.INFORMATION_CUTOFF,
                   "two_world_gap": "each simulated world minus the world half the path count further on"}}


def validate(payload: dict) -> None:
    missing = [key for key in REQUIRED if key not in payload]
    if missing:
        raise ValueError(f"range_check.json lacks {missing}")
    text = json.dumps(payload, allow_nan=False)
    if str(ROOT) in text or "/Users/" in text:
        raise ValueError("range_check.json contains an absolute local path")
    if not all(set(item) == {"path", "sha256"} and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in payload["inputs"]):
        raise ValueError("inputs must list path and sha256")
    if not (isinstance(payload["plain_summary"], list) and payload["plain_summary"] and all(isinstance(s, str) and s for s in payload["plain_summary"])
            and payload["plain_summary"][0] == payload["headline"]):
        raise ValueError("plain_summary must be a list of sentences that starts with the headline")
    runner = payload["runner"]
    if not (runner["demand_monthly_log_sd"] == ASSUMED == payload["check"]["assumed"]["demand_monthly_log_sd"] and isinstance(runner["statement"], str)
            and runner["fitted_variant"]["demand_monthly_log_sd"] == payload["fitted_variant"]["fitted"] and runner["fitted_variant"]["used"] is False):
        raise ValueError("runner must carry the assumed value, its statement and the fitted value that is not used")
    for quantity in payload["quantities"].values():
        for which in ("assumed", "fitted"):
            for cover in quantity["coverage"][which].values():
                if not 0 <= cover["inside"] <= cover["of"]:
                    raise ValueError("coverage counts must lie between zero and the number of outcomes")
