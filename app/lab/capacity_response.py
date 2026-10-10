"""Capacity response: does an airline fly less after fuel gets dearer, and how fast.

Year-over-year growth in capacity (available seat miles, percent) is regressed
on the year-over-year change in the quarterly average spot price of jet fuel in
the same quarter and the four before it. Fuel changes are in steps of 10%
(log change over log 1.1), so a coefficient is percentage points of capacity
growth per 10% rise in fuel, and the running sum of the lag coefficients through
quarter k is the cumulative response k quarters after a lasting rise.

Two specifications on the fit years (2014Q1 to 2019Q4): the plain regression,
and the same with a linear time trend. The one with the lower BIC is primary;
the select years cannot arbitrate because every quarter of 2020-2023 is touched
by the pandemic (its own level, its year-ago base or one of its fuel lags falls
in 2020Q1-2021Q4). A verdict needs both specifications to agree.

Regressions run through stats.distributed_lag; a quarter is excluded by giving
it its own indicator, which removes it from the estimates exactly. Standard
errors are the classical ones scaled up wherever Newey-West is larger.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np

from app.lab import filings, market_data, stats

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "data" / "lab"
UAL_CSV = filings.QUARTERLY_CSV
DAL_CSV = LAB / "dal_quarterly.csv"
OUTPUT = ROOT / "app" / "static" / "data" / "capacity_response.json"

SPLITS = {"fit": (2013, 2019), "select": (2020, 2023), "test": (2024, 2026)}
PANDEMIC = (2020 * 4, 2021 * 4 + 3)    # 2020Q1..2021Q4 as year*4 + quarter-1, as in the fare clock
LEVEL = 0.90
SEED = 20260715
N_DRAWS = 1000
N_BOOT = 1000
MAX_LAG = 4
HAC_LAG = 4                            # year-over-year changes overlap by three quarters
MIN_PRINTS = 40                        # a quarter with fewer daily fuel prints has no average
STEP = math.log(1.10)
UNIT = ("percentage points of year-over-year capacity (ASM) growth per 10% rise in the jet fuel price, cumulative "
        "through the stated number of quarters after the rise")


# ------------------------------------------------------------------ panel

def quarter_index(day: date) -> int:
    return day.year * 4 + (day.month - 1) // 3


def quarter_label(index: int) -> str:
    return f"{index // 4}Q{index % 4 + 1}"


def quarter_end(index: int) -> str:
    month = 3 * (index % 4) + 3
    return date(index // 4, month, 31 if month in (3, 12) else 30).isoformat()


def split_of(index: int) -> str | None:
    return next((name for name, (first, last) in SPLITS.items() if first <= index // 4 <= last), None)


def quarterly_fuel(path: Path = market_data.FUEL_CSV) -> dict[int, float]:
    """Mean daily spot price by quarter, complete quarters only."""
    dates, _ = market_data.load_fuel_daily(path)
    return {int(row["period"][:4]) * 4 + int(row["period"][5]) - 1: row["usd_per_gallon"]
            for row in market_data.quarterly_mean_fuel(dates[0], dates[-1], path) if row["n_prints"] >= MIN_PRINTS}


def united_asm(path: Path = UAL_CSV) -> dict[int, float]:
    return {quarter_index(row["period_end"]): row["asm_m"] for row in filings.load_ual_quarterly(path)
            if row["asm_m"] is not None}


def united_fuel_price(path: Path = UAL_CSV) -> dict[int, float]:
    return {quarter_index(row["period_end"]): row["fuel_price_usd_gal"] for row in filings.load_ual_quarterly(path)
            if row["fuel_price_usd_gal"] is not None}


def delta_asm(path: Path = DAL_CSV) -> dict[int, float]:
    with open(path, newline="") as stream:
        return {quarter_index(date.fromisoformat(row["period_end"])): float(row["asm_m"])
                for row in csv.DictReader(stream) if row.get("asm_m")}


def year_change(levels: np.ndarray) -> np.ndarray:
    """levels[t] / levels[t-4]; NaN for the first four positions and wherever a level is missing."""
    out = np.full(len(levels), np.nan)
    out[4:] = levels[4:] / levels[:-4]
    return out


def build_panel(asm: dict[int, float], fuel: dict[int, float], max_lag: int = MAX_LAG) -> dict:
    """Regular quarterly grid: capacity growth (percent), fuel change (10% steps) and which quarters are clean."""
    q = np.arange(min(fuel), max(max(asm), max(fuel)) + 1)
    level = lambda table: np.array([table.get(int(i), np.nan) for i in q], dtype=float)
    fuel_ratio = year_change(level(fuel))
    growth = 100.0 * (year_change(level(asm)) - 1.0)
    # Touched: the quarter, its year-ago base, or the base of its oldest fuel lag lies in the pandemic window.
    touched = (q >= PANDEMIC[0]) & (q - 4 - max_lag <= PANDEMIC[1])
    level_touched = (q >= PANDEMIC[0]) & (q - 4 <= PANDEMIC[1])
    return {"q": q, "growth": growth, "fuel_steps": np.log(fuel_ratio) / STEP, "fuel_pct": 100.0 * (fuel_ratio - 1.0),
            "touched": touched, "level_touched": level_touched}


# ------------------------------------------------------------------ regression

def lag_fit(panel: dict, first: int, last: int, trend: bool = False, era_break: int | None = None,
            drop=None, fuel=None, max_lag: int = MAX_LAG) -> dict:
    """Capacity growth on fuel-change lags 0..max_lag over quarters first..last (quarter indices).

    `drop` marks grid quarters to exclude: each gets its own indicator, so its residual is zero and the
    other coefficients are what dropping the row would give. `trend` adds a linear trend (before
    `era_break` only, when that is set); `era_break` adds a separate level from that quarter on.
    """
    q = panel["q"]
    lo, hi = int(np.searchsorted(q, first)) - max_lag, int(np.searchsorted(q, last)) + 1
    if lo < 0 or hi > len(q):
        raise ValueError("Sample reaches outside the panel")
    cut = slice(lo, hi)
    y, x, grid = panel["growth"][cut].copy(), (panel["fuel_steps"] if fuel is None else fuel)[cut], q[cut]
    rows = np.arange(len(grid)) >= max_lag
    out_rows = rows & (np.asarray(drop, dtype=bool)[cut] if drop is not None else False)
    y[~rows | out_rows] = 0.0
    if not np.isfinite(y).all():
        raise ValueError("Capacity growth is missing inside the sample")
    controls, names = [], []
    if era_break is not None:
        controls.append((grid >= era_break).astype(float))
        names.append("level_after_break")
    if trend:
        before = grid < era_break if era_break is not None else np.ones(len(grid), dtype=bool)
        controls.append(np.where(before, grid - grid[rows & ~out_rows & before].mean(), 0.0))
        names.append("trend_per_quarter")
    controls += [(np.arange(len(grid)) == i).astype(float) for i in np.flatnonzero(out_rows)]
    result = stats.distributed_lag(y, x, max_lag, controls=np.column_stack(controls) if controls else None, hac_lag=HAC_LAG)
    fit = result["fit"]
    if fit.rank_deficient or fit.df_resid < 1:
        raise ValueError(f"Capacity regression is not identified: n={fit.n}, k={fit.k}")
    cols = result["lag_cols"]
    classical = stats.cumulative_lag_effects(fit.beta, fit.cov, cols)
    hac = result["cumulative"]
    scale = max(1.0, float(np.nanmax((hac["se"] / classical["se"]) ** 2)))
    se = classical["se"] * math.sqrt(scale)
    low, high = stats.conf_int(classical["cumulative"], se, LEVEL, df=fit.df_resid)
    clean = ~out_rows[result["kept"]]
    used = grid[result["kept"]][clean]
    resid, actual = fit.resid[clean], y[result["kept"]][clean]
    n, k = int(clean.sum()), fit.k - int(out_rows.sum())
    ssr = float(resid @ resid)
    return {"beta": fit.beta[cols], "cov": fit.cov[np.ix_(cols, cols)] * scale, "cumulative": classical["cumulative"],
            "se": se, "lo": low, "hi": high, "se_classical": classical["se"], "se_newey_west": hac["se"],
            "n": n, "k": k, "df": fit.df_resid, "r2": 1.0 - ssr / float(np.sum((actual - actual.mean()) ** 2)),
            "resid_sd": math.sqrt(ssr / fit.df_resid), "bic": n * math.log(ssr / n) + k * math.log(n),
            "intercept": float(fit.beta[0]), "controls": dict(zip(names, fit.beta[cols[-1] + 1:cols[-1] + 1 + len(names)])),
            "quarters": used, "resid": resid, "sample": f"{quarter_label(int(used[0]))} to {quarter_label(int(used[-1]))}",
            "mae": float(np.mean(np.abs(resid)))}


def curve_draws(beta, cov, df: int, n_draws: int, seed) -> np.ndarray:
    """Joint draws of the cumulative response by quarter: multivariate Student t on the lag coefficients, summed."""
    beta, cov = np.asarray(beta, dtype=float), np.asarray(cov, dtype=float)
    if beta.ndim != 1 or cov.shape != (len(beta), len(beta)) or not np.isfinite(cov).all():
        raise ValueError("beta and cov do not conform")
    rng = np.random.default_rng(seed)
    values, vectors = np.linalg.eigh((cov + cov.T) / 2.0)
    shocks = rng.standard_normal((n_draws, len(beta))) @ (vectors * np.sqrt(np.clip(values, 0.0, None))).T
    shocks = shocks / np.sqrt(rng.chisquare(df, size=(n_draws, 1)) / df)
    return np.cumsum(beta + shocks, axis=1)


def curve_rows(fit: dict, draws: np.ndarray | None = None) -> list[dict]:
    rows = []
    for k in range(len(fit["beta"])):
        row = {"quarters_after": k}
        if draws is not None:
            row.update(zip(("p10", "p50", "p90"), stats.quantiles(draws[:, k])))
        row.update(estimate=fit["cumulative"][k], se=fit["se"][k], lo=fit["lo"][k], hi=fit["hi"][k],
                   lag_coef=fit["beta"][k], se_classical=fit["se_classical"][k], se_newey_west=fit["se_newey_west"][k])
        rows.append(row)
    return rows


def describe(fit: dict) -> dict:
    return {k: fit[k] for k in ("sample", "n", "k", "df", "r2", "resid_sd", "bic", "intercept", "controls")}


# ------------------------------------------------------------------ wording generated from the numbers

def verdict(rows: list[dict]) -> str:
    """none | lower | higher, from the interval of the cumulative response at the last horizon."""
    last = rows[-1]
    return "lower" if last["hi"] < 0 else "higher" if last["lo"] > 0 else "none"


def _range(row: dict) -> str:
    return f"{row['estimate']:+.2f} points ({LEVEL:.0%} range {row['lo']:+.2f} to {row['hi']:+.2f})"


def headline(primary: list[dict], other: list[dict], n: int, sample: str, airline: str = "United") -> str:
    """One sentence on the response a year out; neutral unless both specifications agree on a sign."""
    first, second = verdict(primary), verdict(other)
    last, horizon = primary[-1], primary[-1]["quarters_after"]
    lead = (f"{horizon} quarters after a 10% rise in jet fuel, {airline}'s capacity growth differs by {_range(last)} "
            f"over {n} quarters ({sample})")
    if first == "none" and second == "none":
        return (f"{airline}'s capacity shows no measurable response to fuel prices: {lead}; the range includes zero, "
                "so this history cannot tell a response from none.")
    if first != second:
        return (f"{airline}'s capacity response to fuel prices cannot be told apart from zero: {lead}, and the second "
                f"specification gives {_range(other[-1])}; the two do not agree, so no response is established.")
    if first == "lower":
        return f"{airline} has flown less after fuel got dearer: {lead}; the range lies below zero in both specifications."
    return (f"{airline} did not cut capacity when fuel got dearer: {lead}; capacity grew faster after fuel rose in "
            "both specifications, which is the two moving together over these years, not fuel causing more flying.")


# ------------------------------------------------------------------ second looks (use test-year quarters)

def era_fit(panel: dict, trend: bool) -> dict:
    """Clean quarters before the pandemic and after it, with a separate level after it."""
    growth, q = panel["growth"], panel["q"]
    have = np.flatnonzero(np.isfinite(growth) & np.isfinite(panel["fuel_steps"]))
    first, last = int(q[have[0]]), int(q[have[-1]])
    return lag_fit(panel, first, last, trend=trend, era_break=PANDEMIC[1] + 1, drop=panel["touched"])


def pooled_fit(panels: dict[str, dict], seed: int, n_boot: int = N_BOOT, trend_for: tuple = ()) -> dict:
    """Common lag coefficients for several airlines on their clean quarters, each with its own levels.

    Stacked least squares with one level per airline and era, and for the airlines in `trend_for` a
    linear trend before the pandemic. The interval is a moving-block bootstrap over quarters (blocks
    of four), which keeps the airlines' rows of a quarter together because they face the same fuel
    price; with this few quarters it undercovers and is descriptive only.
    """
    stack = {}
    for name, panel in panels.items():
        lags, kept = stats.lag_matrix(panel["fuel_steps"], MAX_LAG)
        for row, t in zip(lags, kept):
            if np.isfinite(panel["growth"][t]) and not panel["touched"][t]:
                stack.setdefault(int(panel["q"][t]), []).append((name, row, panel["growth"][t]))
    quarters = np.array(sorted(stack))
    levels = [(name, era) for name in panels for era in (0, 1)]
    centre = float(quarters[quarters < PANDEMIC[0]].mean()) if (quarters < PANDEMIC[0]).any() else float(quarters.mean())

    def cumulative(sample) -> np.ndarray:
        items = [(int(t), *item) for t in sample for item in stack[int(t)]]
        x = np.array([np.r_[row, [float(name == a and (t > PANDEMIC[1]) == bool(era)) for a, era in levels],
                            [(t - centre) * (name == a and t < PANDEMIC[0]) for a in trend_for]]
                      for t, name, row, _ in items])
        return np.cumsum(stats.ols(x, [item[3] for item in items], add_const=False).beta[:MAX_LAG + 1])

    point = cumulative(quarters)
    boot = stats.block_bootstrap(cumulative, quarters, block_len=4, n_boot=n_boot, seed=seed)
    low, high = stats.percentile_interval(boot, LEVEL)
    counts = {name: sum(1 for t in quarters for item in stack[int(t)] if item[0] == name) for name in panels}
    return {"curve": [{"quarters_after": k, "estimate": point[k], "lo": low[k], "hi": high[k]} for k in range(MAX_LAG + 1)],
            "n_rows": int(sum(counts.values())), "rows_by_airline": counts, "n_quarters": len(quarters),
            "interval": f"{LEVEL:.0%} percentile interval, moving-block bootstrap over quarters (blocks of 4, {n_boot} resamples)"}


# ------------------------------------------------------------------ the output file

def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _shown(path: Path) -> str:
    path = Path(path).resolve()
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)


def _clean(value, places: int = 6):
    """JSON-ready copy: numpy to Python, floats rounded, non-finite to None."""
    if isinstance(value, dict):
        return {str(k): _clean(v, places) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v, places) for v in value]
    if isinstance(value, np.ndarray):
        return _clean(value.tolist(), places)
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return round(float(value), places) if math.isfinite(value) else None
    return value


def fuel_term(panel: dict, beta: np.ndarray) -> np.ndarray:
    """sum_j beta[j] * fuel change[t-j] on the panel grid; NaN where a lag is missing."""
    lags, kept = stats.lag_matrix(panel["fuel_steps"], len(beta) - 1)
    out = np.full(len(panel["q"]), np.nan)
    out[kept] = lags @ beta
    return out


def about_own_mean(panel: dict, beta: np.ndarray, rows: np.ndarray) -> dict:
    """Does the fuel term explain capacity growth around the rows' own mean? No level or trend is carried over."""
    if not len(rows):
        return {"n": 0}
    growth, term = panel["growth"][rows], fuel_term(panel, beta)[rows]
    plain, fitted = growth - growth.mean(), (growth - growth.mean()) - (term - term.mean())
    return {"n": int(len(rows)), "mae_no_response_pp": float(np.mean(np.abs(plain))),
            "mae_with_response_pp": float(np.mean(np.abs(fitted))),
            "response_helps": bool(np.mean(np.abs(fitted)) < np.mean(np.abs(plain)))}


def build(seed: int = SEED, n_draws: int = N_DRAWS, fuel_path: Path = market_data.FUEL_CSV, ual_path: Path = UAL_CSV,
          dal_path: Path = DAL_CSV, evaluate_test: bool = True, generated_at: str | None = None) -> dict:
    """Everything capacity_response.json carries. `evaluate_test=False` withholds whatever uses 2024-2026 quarters."""
    if n_draws < 500:
        raise ValueError("At least 500 joint draws are needed")
    fuel = quarterly_fuel(fuel_path)
    panel = build_panel(united_asm(ual_path), fuel)
    q, growth = panel["q"], panel["growth"]
    have = np.isfinite(growth) & np.isfinite(panel["fuel_steps"])
    fit_first = int(q[np.flatnonzero(have & (q // 4 >= SPLITS["fit"][0]))[0]])
    fit_last = SPLITS["fit"][1] * 4 + 3
    specs = {"plain": lag_fit(panel, fit_first, fit_last), "trend": lag_fit(panel, fit_first, fit_last, trend=True)}
    primary_id = min(specs, key=lambda name: specs[name]["bic"])
    other_id = next(name for name in specs if name != primary_id)
    primary, other = specs[primary_id], specs[other_id]
    draws = curve_draws(primary["beta"], primary["cov"], primary["df"], n_draws, seed)
    curve, other_curve = curve_rows(primary, draws), curve_rows(other)
    text = {"plain": "no other regressor", "trend": "a linear time trend"}
    spec_id = lambda name: f"united_yoy_asm_on_spot_fuel_lags0_{MAX_LAG}_{name}_fit"

    reported = np.log(year_change(np.array([united_fuel_price(ual_path).get(int(i), np.nan) for i in q]))) / STEP
    reported_first = int(q[np.flatnonzero(np.isfinite(reported))[0]]) + MAX_LAG
    looser_last = SPLITS["select"][1] * 4 + 3
    extra = {
        "reported_price": (lag_fit(panel, reported_first, fit_last, trend=primary_id == "trend", fuel=reported),
                           "As the primary specification with United's reported average price per gallon in place "
                           "of spot fuel; the reported price starts in 2013, so fewer quarters."),
        "pandemic_indicators_through_2023": (
            lag_fit(panel, fit_first, looser_last, trend=primary_id == "trend", era_break=PANDEMIC[1] + 5,
                    drop=panel["level_touched"]),
            "Looser pandemic rule on 2014-2023: an indicator for each quarter whose capacity or year-ago base "
            "falls in 2020Q1-2021Q4, which keeps the four quarters of 2023 (their fuel lags still reach into the "
            "pandemic and their growth is still the rebuild) with a level of their own. Not used: 2023 is not a "
            "clean year.")}
    variants = [{"id": spec_id(name), "primary": name == primary_id,
                 "description": f"Year-over-year capacity growth on year-over-year spot fuel changes at lags 0 to "
                                f"{MAX_LAG}, with {text[name]}; fit years.",
                 **describe(fit), "curve": curve_rows(fit), "verdict": verdict(curve_rows(fit))} for name, fit in specs.items()]
    variants += [{"id": name, "primary": False, "description": note, **describe(fit), "curve": curve_rows(fit),
                  "verdict": verdict(curve_rows(fit))} for name, (fit, note) in extra.items()]

    fit_rows = np.searchsorted(q, primary["quarters"])
    by_split = {
        "fit": {"n": primary["n"], "in_sample": True, "r2": primary["r2"], "resid_sd_pp": primary["resid_sd"],
                "mae_pp": primary["mae"], **about_own_mean(panel, primary["beta"], fit_rows)},
        "select": {"n": 0, "note": "No clean quarter: every quarter of 2020-2023 has its own level, its year-ago base "
                                   "or the base of a fuel lag inside 2020Q1-2021Q4."},
        "test": None,
        "definition": "mae_no_response_pp and mae_with_response_pp compare capacity growth around the split's own mean "
                      "with and without the primary fuel term (also taken around its own mean); no level or trend is "
                      "carried across splits."}
    points = [{"period_end": quarter_end(int(t)), "quarter": quarter_label(int(t)), "capacity_growth_pct": growth[i],
               "fuel_change_pct": panel["fuel_pct"][i], "fuel_steps_of_10pct": panel["fuel_steps"][i],
               "split": split_of(int(t)), "in_estimation": bool(t in set(primary["quarters"].tolist())),
               "pandemic_touched": bool(panel["touched"][i])}
              for i, t in enumerate(q) if have[i] and (evaluate_test or split_of(int(t)) != "test")]
    inputs = [fuel_path, ual_path]
    delta_curve, pooled, refit, last_quarter = None, None, None, int(q[np.flatnonzero(have)[-1]])
    if evaluate_test:
        test_rows = np.flatnonzero(have & ~panel["touched"] & (q // 4 >= SPLITS["test"][0]))
        by_split["test"] = {"in_sample": False, **about_own_mean(panel, primary["beta"], test_rows),
                            "other_specification": about_own_mean(panel, other["beta"], test_rows),
                            "sample": f"{quarter_label(int(q[test_rows[0]]))} to {quarter_label(int(q[test_rows[-1]]))}"}
        through = era_fit(panel, trend=primary_id == "trend")
        refit = {"description": "Every clean United quarter through the latest report: the primary specification with "
                                "a separate level after the pandemic. Uses test-year quarters; descriptive, not the "
                                "curve the simulator draws from.", **describe(through), "curve": curve_rows(through),
                 "verdict": verdict(curve_rows(through))}
        delta_panel = build_panel(delta_asm(dal_path), fuel)
        delta = era_fit(delta_panel, trend=False)
        before = int((delta["quarters"] <= PANDEMIC[0]).sum())
        delta_curve = {"description": "Delta, same regressors, clean quarters before the pandemic and after it with a "
                                      "separate level after it and no trend (too few early quarters for one).",
                       **describe(delta), "curve": curve_rows(delta), "verdict": verdict(curve_rows(delta)),
                       "quarters_before_pandemic": before, "quarters_after_pandemic": delta["n"] - before,
                       "caveat": f"Delta's file starts in 2017Q4, so only {before} clean quarters precede the pandemic; "
                                 "the rest are test-year quarters. A second look, not a test."}
        both, trended = {"UAL": panel, "DAL": delta_panel}, ("UAL",) if primary_id == "trend" else ()
        pooled = {"computed": True, **pooled_fit(both, seed + 1, trend_for=trended),
                  "description": "United and Delta stacked on their clean quarters: common fuel lags, a level for each "
                                 "airline before and after the pandemic"
                                 + (", and United's trend before it as in the primary specification." if trended else "."),
                  "without_trend": pooled_fit(both, seed + 1) if trended else None,
                  "caveat": "Both airlines face the same fuel series in the same quarters, so Delta's rows add little "
                            "independent evidence, and the sample mixes fit and test years. Descriptive."}
        pooled["verdict"] = verdict(pooled["curve"])
        inputs.append(dal_path)
        last_quarter = max(last_quarter, int(delta["quarters"][-1]))
    line = headline(curve, other_curve, primary["n"], primary["sample"])
    last = curve[-1]
    parts = [line, f"By quarter after a 10% fuel rise (P10, P50, P90, points of capacity growth): "
             + "; ".join(f"quarter {r['quarters_after']}: {r['p10']:+.2f}, {r['p50']:+.2f}, {r['p90']:+.2f}" for r in curve) + ".",
             f"The other specification ({text[other_id]}) gives {_range(other_curve[-1])} after {last['quarters_after']} quarters.",
             f"Of the {len(variants)} variants estimated without test-year quarters, "
             f"{sum(v['verdict'] == 'lower' for v in variants)} show capacity lower a year after fuel rises and "
             f"{sum(v['verdict'] == 'higher' for v in variants)} show it higher."]
    if evaluate_test:
        test = by_split["test"]
        parts.append(f"In {test['sample']} ({test['n']} quarters) adding the fitted response "
                     f"{'lowers' if test['response_helps'] else 'does not lower'} the error in capacity growth around its "
                     f"own mean ({test['mae_with_response_pp']:.2f} against {test['mae_no_response_pp']:.2f} points).")
        parts.append(f"Every clean United quarter through {refit['sample'][-6:]} ({refit['n']}): {_range(refit['curve'][-1])} "
                     f"after {last['quarters_after']} quarters.")
        parts.append(f"Delta, a second look on {delta_curve['n']} clean quarters ({delta_curve['quarters_before_pandemic']} before "
                     f"the pandemic, {delta_curve['quarters_after_pandemic']} in the test years): {_range(delta_curve['curve'][-1])} "
                     f"after {last['quarters_after']} quarters"
                     + {"lower": f", below zero on {delta_curve['df']} degrees of freedom: a hint of a cut, not a finding.",
                        "higher": ", above zero.", "none": ", not told apart from zero."}[delta_curve["verdict"]])
        parts.append(f"United and Delta pooled ({pooled['n_rows']} rows): {_range(pooled['curve'][-1])}.")
    limits = [
        "Co-movement, not cause: fuel prices and travel demand move with the economy, and in the fitted years "
        "United's capacity growth trended up while fuel first fell and then rose. Nothing here identifies what fuel "
        "alone does to capacity.",
        f"{primary['n']} quarters covering one fall and one rise in fuel; the fitted years contain no fuel shock of "
        "the size of 2022 or 2026.",
        "The plain regression and the one with a time trend disagree in size; the trend is the difference. The "
        "primary is chosen by BIC on the fit years because the select years hold no clean quarter.",
        "Schedules are set months ahead and fleet deliveries years ahead; a response slower than four quarters is "
        "outside the curve.",
        "Fuel is the Gulf Coast spot average for the quarter, not United's own cost; hedges in 2013-2016 are not modelled.",
        "A draw of the curve may be positive: the simulator should treat this as a response not told apart from "
        "zero, not as evidence that dearer fuel raises capacity.",
        "Capacity is system available seat miles as reported; mainline and regional are not separated."]
    return _clean({
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": quarter_end(last_quarter),
        "as_of_detail": {"last_capacity_quarter": quarter_label(last_quarter), "curve_estimated_through": quarter_end(fit_last),
                         "fuel_daily_last_print": market_data.load_fuel_daily(fuel_path)[0][-1].isoformat()},
        "inputs": [{"path": _shown(p), "sha256": _sha(p)} for p in inputs],
        "seed": seed, "interval_level": LEVEL, "test_split": "computed once" if evaluate_test else "withheld",
        "headline": line, "plain_summary": " ".join(parts), "unit": UNIT, "verdict": verdict(curve),
        "verdict_other_specification": verdict(other_curve),
        "primary_spec": {"id": spec_id(primary_id),
                         "description": f"Year-over-year capacity growth (percent) on year-over-year changes in the "
                                        f"quarterly average spot jet fuel price (steps of 10%) at lags 0 to {MAX_LAG}, "
                                        f"with {text[primary_id]}; fit years; pandemic-touched quarters excluded.",
                         "equation": "growth[t] = a + sum_{j=0..4} b[j] * fuel change[t-j]"
                                     + (" + c * t" if primary_id == "trend" else "")
                                     + " + e[t]; cumulative response at k = b[0] + ... + b[k]",
                         "units": UNIT, "hac_lag": HAC_LAG, **describe(primary),
                         "selection": {"rule": "lower BIC on the fit years", "bic": {name: fit["bic"] for name, fit in specs.items()}},
                         "standard_errors": "classical, scaled up by the largest ratio of Newey-West to classical variance "
                                            "among the cumulative responses when that ratio exceeds one; Student t intervals"},
        "curve": curve,
        "curve_definition": "p10/p50/p90 are quantiles of curve_draws; estimate, se, lo and hi are the regression's "
                            "cumulative response, its standard error and its 90% Student t interval; lag_coef is the "
                            "single quarter's coefficient.",
        "curve_draws": np.round(draws, 4),
        "curve_draws_method": {"n_draws": n_draws, "seed": seed, "df": primary["df"],
                               "method": "Joint draws of the five lag coefficients from a multivariate Student t centred "
                                         "on the estimates; each row is the running sum of one draw, in the unit above. "
                                         "Not clipped: a draw may be positive or non-monotone."},
        "united_points": points,
        "united_points_definition": "capacity_growth_pct is year-over-year growth in available seat miles; fuel_change_pct "
                                    "is the year-over-year change in the quarterly average Gulf Coast spot price.",
        "delta_curve": delta_curve, "pooled": pooled, "refit_through_latest": refit,
        "by_split": by_split, "n": primary["n"], "variants": variants, "limits": limits,
        "variants_tried": [f"{v['id']}: {v['description']} n = {v['n']}; after {MAX_LAG} quarters "
                           f"{_range(v['curve'][-1])}." for v in variants]
                          + ([f"refit_through_latest: {refit['description']} n = {refit['n']}; after {MAX_LAG} quarters "
                              f"{_range(refit['curve'][-1])}.",
                              f"delta: {delta_curve['description']} n = {delta_curve['n']}; after {MAX_LAG} quarters "
                              f"{_range(delta_curve['curve'][-1])}.",
                              f"pooled: {pooled['description']} {pooled['n_rows']} rows; after {MAX_LAG} quarters "
                              f"{_range(pooled['curve'][-1])}."]
                             + ([f"pooled_without_trend: as pooled, without United's trend; after {MAX_LAG} quarters "
                                 f"{_range(pooled['without_trend']['curve'][-1])}."] if pooled["without_trend"] else [])
                             if evaluate_test else [])})


REQUIRED = ("generated_at", "as_of", "inputs", "headline", "plain_summary", "unit", "primary_spec", "curve",
            "curve_draws", "united_points", "delta_curve", "by_split", "n", "limits", "variants_tried")


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate(payload: dict) -> None:
    """Shape checks the builder runs before writing; raises ValueError naming the first problem."""
    missing = [key for key in REQUIRED if key not in payload]
    if missing:
        raise ValueError(f"capacity_response.json lacks {missing}")
    if payload.get("fixture"):
        raise ValueError("capacity_response.json must not be a fixture")
    json.dumps(payload, allow_nan=False)
    if not all(set(item) >= {"path", "sha256"} and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in payload["inputs"]):
        raise ValueError("inputs must list path and sha256")
    curve = payload["curve"]
    if [row.get("quarters_after") for row in curve] != list(range(MAX_LAG + 1)):
        raise ValueError("curve must list quarters_after 0..4 in order")
    for row in curve:
        if not all(_finite(row.get(key)) for key in ("p10", "p50", "p90", "estimate", "lo", "hi")):
            raise ValueError("curve needs finite p10, p50, p90, estimate, lo and hi")
        if not (row["p10"] <= row["p50"] <= row["p90"] and row["lo"] <= row["estimate"] <= row["hi"]):
            raise ValueError("curve quantiles or interval are out of order")
    draws = payload["curve_draws"]
    if len(draws) < 500 or not all(len(row) == len(curve) and all(_finite(v) for v in row) for row in draws):
        raise ValueError("curve_draws needs at least 500 finite rows, one value per quarter")
    if not payload["headline"] or not payload["plain_summary"] or not payload["limits"] or not payload["variants_tried"]:
        raise ValueError("headline, plain_summary, limits and variants_tried must not be empty")
    if "10%" not in payload["unit"]:
        raise ValueError("unit must state the 10% fuel step")
    if not isinstance(payload["n"], int) or payload["n"] < 1 or not payload["united_points"]:
        raise ValueError("n and united_points are required")
    for name in ("fit", "select", "test"):
        if name not in payload["by_split"]:
            raise ValueError(f"by_split.{name} is missing")
