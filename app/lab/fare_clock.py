"""Fare clock: what share of a jet-fuel cost shock comes back through revenue, and how fast.

Two measurements, both chosen before the locked test years were scored.

United, quarterly (the recovery curve). Everything is in cents per available seat
mile, so a coefficient is a share: the year-over-year change in unit revenue is
regressed on the year-over-year change in fuel cost per seat mile in the same
quarter and the four before it, with year-over-year capacity growth as the one
control. The running sum of the lag coefficients through quarter k is the share
of a lasting fuel cost change that unit revenue has made up k quarters later.

Industry, monthly. The monthly log change in real airfares (CPI airline fares over
CPI all items) is regressed on the current and lagged monthly log changes in jet
fuel. That gives an elasticity, not a share; dividing by fuel cost as a share of
revenue converts it, and is done only with a sourced share.

Splits: fit 2013-2019, select 2020-2023, locked test 2024-2026. The primary
curve is estimated on the fit split alone. Lags are positional and built with
stats.lag_matrix, so no row uses a value later than its own period. Statistics
come from app.lab.stats; nothing is re-implemented here.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np

from app.lab import market_data, stats

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "data" / "lab"
UAL_CSV = LAB / "ual_quarterly.csv"
DAL_CSV = LAB / "dal_quarterly.csv"
DAL_CHECK_JSON = LAB / "dal_check.json"
DEMAND_JSON = ROOT / "app" / "static" / "data" / "demand_signal.json"
LITERATURE_JSON = LAB / "literature.json"
OUTPUT = ROOT / "app" / "static" / "data" / "fare_clock.json"

SPLITS = {"fit": (2013, 2019), "select": (2020, 2023), "test": (2024, 2026)}
PANDEMIC_Q = (2020 * 4, 2021 * 4 + 3)          # 2020Q1..2021Q4 as year*4 + quarter-1
PANDEMIC_M = (2020 * 12 + 2, 2021 * 12 + 11)   # 2020-03..2021-12 as year*12 + month-1
FARE_SA, FARE_NSA, ALL_ITEMS = "CUSR0000SETG01", "CUUR0000SETG01", "CUSR0000SA0"
MIN_FUEL_PRINTS = 10        # a month with fewer daily prints has no monthly average
SHORT_FUEL_PRINTS = 15      # months below this are listed in the output
LEVEL = 0.90                # two-sided level of every `lo`/`hi` interval
HORIZON = 5                 # quarters 0..4 after the shock
QUARTERLY_HAC_LAG = 4       # year-over-year changes overlap by three quarters, so at least 3
SEED = 20260715
STANDARD_ERRORS = ("Standard errors come from a cautious covariance: the classical one plus the positive part of "
                   "(Newey-West minus classical), which is at least as large as both for every coefficient and every "
                   "sum of coefficients. Newey-West errors are biased downward in short samples and can fall below "
                   "the classical ones; this never reports the smaller. Intervals use Student t with the residual "
                   "degrees of freedom.")
N_DRAWS = 1000
N_BOOT = 2000
PROMISE_DATE = date(2026, 7, 15)
ONSET_WINDOW, ONSET_JUMP = 60, 0.25
CORE_RULES = ("costs_only", "full_immediate", "management", "fitted")
# Used only when the literature file is absent: docs/lab_contract.md, 8-K of 15 July 2026.
PROMISE_FALLBACK = {"2026Q3": (0.8, 0.9), "2026Q4": (1.0, 1.0)}
UAL_COLUMNS = ("asm_m", "total_revenue_musd", "passenger_revenue_musd", "trasm_cents", "prasm_cents",
               "fuel_gallons_m", "fuel_price_usd_gal", "fuel_expense_musd")
# Delta's adjusted columns stand in for the GAAP ones: its GAAP revenue and fuel carry third-party refinery sales.
DAL_ADJUSTED = {"trasm_cents": "adj_trasm_cents", "total_revenue_musd": "adj_total_revenue_musd",
                "fuel_price_usd_gal": "adj_fuel_price_usd_gal", "fuel_expense_musd": "adj_fuel_expense_musd"}
DEMAND_SPEC = {"id": "industry_real_sa_lags0_12_tsa_demand_control", "y": "dlog_real_sa", "max_lag": 12,
               "years": (2023, SPLITS["test"][1]), "pandemic": "exclude",
               "text": "Lags 0 to 12 with a demand control: the one-month change in the year-over-year growth of TSA "
                       "traveller counts. The control starts in 2020, inside the pandemic, so the months used are "
                       "those from 2023 with no March 2020 to December 2021 month behind them: February 2023 on, "
                       "mostly the locked test years. A variant only; computed once, with the test split, next to "
                       "the same regression without the control on the same months."}
PUBLISHED = (  # (literature entry id, start of the number's label); values are read from the file, never typed here
    ("dong-2024-thesis-ch1", "Legacy carriers, same-quarter fare change for a 10% industry-wide"),
    ("dong-2024-thesis-ch1", "Legacy carriers, four-quarter cumulative fare change for a 10% increase"),
    ("dong-2024-thesis-ch1", "Low-cost carriers, same-quarter fare change"),
    ("dong-2024-thesis-ch1", "Low-cost carriers, four-quarter cumulative fare change"),
    ("dong-atallah-galdo-2025-jatm", "Legacy-carrier airfare change"),
    ("gayle-lin-2021-ei", "Mean crude oil to airfare pass-through elasticity"),
    ("gayle-lin-2021-ei", "Mean elasticity with the influence of hedging set to zero"),
    ("chuang-2020-eb", "Same-quarter fare change per one-dollar positive"),
    ("chuang-2020-eb", "Same-quarter fare change per one-dollar negative"),
    ("chuang-2020-eb", "Long-run pass-through, low end"),
    ("chuang-2020-eb", "Long-run pass-through, high end"),
    ("wadud-2015-tra", "Implied air fare elasticity to fuel price, fuel rising above"),
    ("wadud-2015-tra", "Implied air fare elasticity to fuel price, fuel recovering below"),
    ("jiang-que-2022-afl", "Median ratio of price change to fuel cost change, quarters when fuel cost rises"),
    ("jiang-que-2022-afl", "Median ratio of price change to fuel cost change, quarters when fuel cost falls"),
    ("koopmans-lieshout-2016-jatm", "Upper bound on pass-through of a firm-specific cost change"),
    ("koopmans-lieshout-2016-jatm", "Lower bound on pass-through of a sector-wide cost change"),
    ("ubs-2026-03-ual-pass-through-45", "UBS assumed fuel pass-through rate for United"),
    ("ubs-2026-03-investing-com-range", "UBS assumed fuel pass-through rates across its airline coverage, low"),
    ("ubs-2026-03-investing-com-range", "UBS assumed fuel pass-through rates across its airline coverage, high"),
)


# ------------------------------------------------------------------ small helpers

def split_of(year: int) -> str | None:
    return next((name for name, (first, last) in SPLITS.items() if first <= year <= last), None)


def month_index(day: date) -> int:
    return day.year * 12 + day.month - 1


def month_label(index: int) -> str:
    return f"{index // 12}-{index % 12 + 1:02d}"


def quarter_index(day: date) -> int:
    return day.year * 4 + (day.month - 1) // 3


def quarter_label(index: int) -> str:
    return f"{index // 4}Q{index % 4 + 1}"


def quarter_end(index: int) -> str:
    month = 3 * (index % 4) + 3
    return date(index // 4, month, 31 if month in (3, 12) else 30).isoformat()


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _shown(path: Path) -> str:
    path = Path(path).resolve()
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)


def _clean(value, places: int = 6):
    """JSON-ready copy: numpy scalars and arrays to Python, floats rounded, non-finite to None."""
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


def _pct(share: float) -> str:
    return f"{100 * share:.0f}%"


def _lagged(values: np.ndarray, k: int) -> np.ndarray:
    """values[t-k] at position t; the first k positions are NaN."""
    out = np.full(len(values), np.nan)
    out[k:] = values[:len(values) - k]
    return out


# ------------------------------------------------------------------ regression core

@dataclass(frozen=True, eq=False)
class LagFit:
    """OLS of y[t] on a constant, lags of one or more series and controls.

    `cov` is the cautious covariance (see cautious_cov): at least as large as both
    the classical and the Newey-West covariance for every combination of
    coefficients, so no interval here is narrower than either would give.
    """
    fit: stats.OLSResult
    cov: np.ndarray        # the covariance used for every interval and draw
    cov_hac: np.ndarray    # Newey-West, scaled by n/(n-k)
    hac_lag: int
    rows: np.ndarray       # positions on the caller's regular grid
    blocks: tuple          # one array of coefficient positions per lagged series, in lag order
    design: np.ndarray     # regressors for every grid position (NaN where unavailable), no constant
    dropped_controls: int  # control columns that were all zero on the kept rows

    def cumulative(self, block: int = 0, level: float = LEVEL) -> dict:
        out = stats.cumulative_lag_effects(self.fit.beta, self.cov, self.blocks[block])
        lo, hi = stats.conf_int(out["cumulative"], out["se"], level, df=self.fit.df_resid)
        return {"cumulative": out["cumulative"], "se": out["se"], "lo": lo, "hi": hi,
                "se_newey_west": stats.cumulative_lag_effects(self.fit.beta, self.cov_hac, self.blocks[block])["se"],
                "se_classical": stats.cumulative_lag_effects(self.fit.beta, self.fit.cov, self.blocks[block])["se"]}

    def predict(self, rows=None) -> np.ndarray:
        design = self.design if rows is None else self.design[rows]
        return self.fit.beta[0] + design @ self.fit.beta[1:]

    def describe(self) -> dict:
        return {"n": self.fit.n, "k": self.fit.k, "df_resid": self.fit.df_resid, "r2": self.fit.r2,
                "hac_lag": self.hac_lag, "condition_number": self.fit.cond}


def cautious_cov(classical: np.ndarray, hac: np.ndarray) -> np.ndarray:
    """A covariance at least as large as both inputs in every direction.

    classical + the positive part of (hac - classical). For any weights w,
    w'Vw >= max(w'classical w, w'hac w). Newey-West errors are biased downward in
    short samples and can fall below the classical ones; this never reports the
    smaller of the two for any coefficient or any sum of coefficients.
    """
    gap = hac - classical
    values, vectors = np.linalg.eigh((gap + gap.T) / 2.0)
    return classical + (vectors * np.clip(values, 0.0, None)) @ vectors.T


def lag_regression(y, series, max_lag: int, mask=None, controls=None, hac_lag: int | None = None) -> LagFit:
    """y[t] on a constant, lags 0..max_lag of each series and controls[t].

    Everything sits on one regular grid. A row is used when y, every lag and every
    control are finite and `mask` allows it. Row t uses series values at t-max_lag..t
    only (stats.lag_matrix). Kept rows that are not adjacent on the grid are treated
    as adjacent by the Newey-West weights. Intervals use the cautious covariance.
    """
    y = np.asarray(y, dtype=float)
    n = len(y)
    columns, blocks, cursor, valid = [], [], 1, np.isfinite(y)
    for values in series:
        matrix, kept = stats.lag_matrix(values, max_lag)
        full = np.full((n, max_lag + 1), np.nan)
        full[kept] = matrix
        columns.append(full)
        valid &= np.isfinite(full).all(axis=1)
        blocks.append(np.arange(cursor, cursor + max_lag + 1))
        cursor += max_lag + 1
    dropped = 0
    if mask is not None:
        valid &= np.asarray(mask, dtype=bool)
    if controls is not None:
        controls = np.asarray(controls, dtype=float).reshape(n, -1)
        valid &= np.isfinite(controls).all(axis=1)
        live = np.any(controls[valid] != 0, axis=0)
        dropped = int((~live).sum())
        columns.append(controls[:, live])
    design = np.column_stack(columns)
    rows = np.flatnonzero(valid)
    fit = stats.ols(design[rows], y[rows])
    if fit.rank_deficient or fit.df_resid <= 0:
        raise ValueError(f"Regression is not identified: n={fit.n}, k={fit.k}, rank={fit.rank}")
    lag = stats.nw_lag_default(fit.n) if hac_lag is None else min(int(hac_lag), fit.n - 1)
    robust = stats.newey_west(fit, lag, small_sample=True)
    return LagFit(fit, cautious_cov(fit.cov, robust.cov), robust.cov, robust.lag, rows, tuple(blocks), design, dropped)


def curve_draws(beta, cov, n_draws: int, seed, df: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Joint draws of the lag coefficients and of their running sums (the recovery curve).

    Normal draws with the given covariance; with `df` each draw is divided by
    sqrt(chi2(df)/df), which makes the draws multivariate Student t. Returns
    (coefficient draws, cumulative draws); the second is the cumulative sum of the first.
    """
    beta, cov = np.asarray(beta, dtype=float), np.asarray(cov, dtype=float)
    if beta.ndim != 1 or cov.shape != (len(beta), len(beta)) or not np.isfinite(cov).all():
        raise ValueError("beta and cov do not conform")
    rng = np.random.default_rng(seed)
    values, vectors = np.linalg.eigh((cov + cov.T) / 2.0)
    root = vectors * np.sqrt(np.clip(values, 0.0, None))
    shocks = rng.standard_normal((n_draws, len(beta))) @ root.T
    if df is not None:
        shocks = shocks / np.sqrt(rng.chisquare(df, size=(n_draws, 1)) / df)
    coefficients = beta + shocks
    return coefficients, np.cumsum(coefficients, axis=1)


# ------------------------------------------------------------------ part 1: industry, monthly

def monthly_fuel(dates, values) -> dict[int, tuple[float, int]]:
    """Mean of the daily prints in each calendar month and the number of prints."""
    total, count = {}, {}
    for day, value in zip(dates, values):
        key = month_index(day)
        total[key] = total.get(key, 0.0) + float(value)
        count[key] = count.get(key, 0) + 1
    return {key: (total[key] / count[key], count[key]) for key in sorted(total)}


def _dlog(levels: np.ndarray) -> np.ndarray:
    out = np.full(len(levels), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        out[1:] = np.log(levels[1:]) - np.log(levels[:-1])
    return out


def industry_panel(cpi_path: Path = market_data.CPI_CSV, fuel_path: Path = market_data.FUEL_CSV) -> dict:
    """Monthly grid of fuel averages and CPI indexes with their one-month log changes.

    A month the source did not publish stays missing, and so do the two changes that touch it.
    """
    cpi = {sid: {month_index(d): v for d, v in zip(*market_data.load_cpi(sid, cpi_path))}
           for sid in (FARE_SA, FARE_NSA, ALL_ITEMS)}
    fuel_dates, fuel_values = market_data.load_fuel_daily(fuel_path)
    fuel = monthly_fuel(fuel_dates, fuel_values)
    first = min(min(fuel), *(min(s) for s in cpi.values()))
    last = max(max(s) for s in cpi.values())
    months = np.arange(first, last + 1)
    level = {sid: np.array([cpi[sid].get(m, np.nan) for m in months]) for sid in cpi}
    prints = np.array([fuel.get(m, (np.nan, 0))[1] for m in months])
    fuel_level = np.array([fuel[m][0] if m in fuel and fuel[m][1] >= MIN_FUEL_PRINTS else np.nan for m in months])
    dlog_fuel = _dlog(fuel_level)
    return {"months": months, "year": months // 12, "fuel": fuel_level, "prints": prints, "dlog_fuel": dlog_fuel,
            "dlog_fuel_up": np.where(np.isfinite(dlog_fuel), np.maximum(dlog_fuel, 0.0), np.nan),
            "dlog_fuel_down": np.where(np.isfinite(dlog_fuel), np.minimum(dlog_fuel, 0.0), np.nan),
            "dlog_real_sa": _dlog(level[FARE_SA] / level[ALL_ITEMS]),
            "dlog_real_nsa": _dlog(level[FARE_NSA] / level[ALL_ITEMS]),
            "dlog_nominal_sa": _dlog(level[FARE_SA]),
            "missing_cpi_months": [month_label(m) for m, v in zip(months, level[FARE_SA]) if not np.isfinite(v)],
            "short_fuel_months": [{"month": month_label(m), "n_prints": int(p)}
                                  for m, p in zip(months, prints) if p < SHORT_FUEL_PRINTS],
            "last_fuel_print": max(d for d in fuel_dates if month_index(d) <= last).isoformat(),
            "last_cpi_month": month_label(int(last))}


INDUSTRY_SPECS = (
    {"id": "industry_real_sa_lags0_12_fit", "y": "dlog_real_sa", "max_lag": 12, "years": (2013, 2019),
     "text": "Monthly log change in real airfares (CUSR0000SETG01 over CUSR0000SA0) on log changes in monthly "
             "average jet fuel, lags 0 to 12, 2013-2019. Primary industry specification."},
    {"id": "industry_real_sa_lags0_18_fit", "y": "dlog_real_sa", "max_lag": 18, "years": (2013, 2019),
     "text": "As the primary industry specification with lags 0 to 18."},
    {"id": "industry_real_sa_lags0_12_2013_2023_no_pandemic_handling", "y": "dlog_real_sa", "max_lag": 12,
     "years": (2013, 2023), "text": "Lags 0 to 12 on 2013-2023 with the pandemic months left in and no indicators."},
    {"id": "industry_real_sa_lags0_12_2013_2023_excluding_pandemic", "y": "dlog_real_sa", "max_lag": 12,
     "years": (2013, 2023), "pandemic": "exclude",
     "text": "Lags 0 to 12 on 2013-2023, dropping every month whose fare change or any of whose fuel lags falls in "
             "March 2020 to December 2021."},
    {"id": "industry_real_sa_lags0_12_2013_2023_pandemic_indicators", "y": "dlog_real_sa", "max_lag": 12,
     "years": (2013, 2023), "pandemic": "indicators",
     "text": "Lags 0 to 12 on 2013-2023 with one indicator for each month from March 2020 to December 2021; later "
             "months keep pandemic-period fuel changes among their lags."},
    {"id": "industry_real_nsa_month_indicators_lags0_12_fit", "y": "dlog_real_nsa", "max_lag": 12,
     "years": (2013, 2019), "month_indicators": True,
     "text": "Not seasonally adjusted airfares (CUUR0000SETG01) with month-of-year indicators, lags 0 to 12, "
             "2013-2019: a check on the seasonally adjusted series."},
    {"id": "industry_nominal_sa_lags0_12_fit", "y": "dlog_nominal_sa", "max_lag": 12, "years": (2013, 2019),
     "text": "Nominal seasonally adjusted airfares (not deflated), lags 0 to 12, 2013-2019."},
    {"id": "industry_asymmetric_lags0_12_fit", "y": "dlog_real_sa", "max_lag": 12, "years": (2013, 2019),
     "asymmetric": True,
     "text": "Separate lag polynomials for fuel increases and fuel decreases, lags 0 to 12, 2013-2019. Primary "
             "asymmetry test."},
    {"id": "industry_asymmetric_lags0_12_2013_2023_excluding_pandemic", "y": "dlog_real_sa", "max_lag": 12,
     "years": (2013, 2023), "pandemic": "exclude", "asymmetric": True,
     "text": "The asymmetry regression on 2013-2023 excluding pandemic-touched months."},
)


def industry_fit(panel: dict, spec: dict, extra_control=None, extra_mask=None) -> LagFit:
    months, max_lag = panel["months"], spec["max_lag"]
    first, last = spec["years"]
    mask = (panel["year"] >= first) & (panel["year"] <= last)
    if extra_mask is not None:
        mask &= extra_mask
    in_window = (months >= PANDEMIC_M[0]) & (months <= PANDEMIC_M[1])
    controls = []
    if spec.get("pandemic") == "exclude":  # the change at t uses levels t-1..t; lag max_lag reaches level t-max_lag-1
        mask &= ~((months >= PANDEMIC_M[0]) & (months - max_lag - 1 <= PANDEMIC_M[1]))
    elif spec.get("pandemic") == "indicators":
        controls += [(months == m).astype(float) for m in months[in_window & mask]]
    if spec.get("month_indicators"):
        controls += [((months % 12) == m).astype(float) for m in range(1, 12)]
    if extra_control is not None:
        controls.append(np.asarray(extra_control, dtype=float))
    series = ([panel["dlog_fuel_up"], panel["dlog_fuel_down"]] if spec.get("asymmetric") else [panel["dlog_fuel"]])
    return lag_regression(panel[spec["y"]], series, max_lag, mask,
                          np.column_stack(controls) if controls else None)


def _lag_rows(fit: LagFit, block: int = 0) -> list[dict]:
    cum = fit.cumulative(block)
    return [{"lag_months": k, "coef": cum["cumulative"][k], "lo": cum["lo"][k], "hi": cum["hi"][k],
             "se": cum["se"][k], "se_newey_west": cum["se_newey_west"][k], "se_classical": cum["se_classical"][k],
             "lag_coef": fit.fit.beta[fit.blocks[block][k]]}
            for k in range(len(fit.blocks[block]))]


def asymmetry_test(fit: LagFit, horizon: int) -> dict:
    """Cumulative response through `horizon` to increases and to decreases, and a test that they are equal."""
    up, down = fit.cumulative(0), fit.cumulative(1)
    weights = np.zeros(fit.fit.k)
    weights[fit.blocks[0][:horizon + 1]], weights[fit.blocks[1][:horizon + 1]] = 1.0, -1.0
    gap = stats.linear_combination(fit.fit.beta, fit.cov, weights)
    return {"horizon_months": horizon, "up": up["cumulative"][horizon], "up_lo": up["lo"][horizon],
            "up_hi": up["hi"][horizon], "down": down["cumulative"][horizon], "down_lo": down["lo"][horizon],
            "down_hi": down["hi"][horizon], "difference": gap["estimate"], "difference_se": gap["se"],
            "p_value": stats.p_value(gap["t"], fit.fit.df_resid)}


def demand_control(path: Path, months: np.ndarray) -> np.ndarray | None:
    """One-month change in the year-over-year growth of TSA travellers on the monthly grid; None without the file.

    Only complete months with a year-over-year figure are used; the rest are missing.
    """
    if not Path(path).is_file():
        return None
    rows = json.loads(Path(path).read_text()).get("monthly_control") or []
    growth = {int(r["month"][:4]) * 12 + int(r["month"][5:7]) - 1: float(r["yoy"])
              for r in rows if r.get("complete") and r.get("yoy") is not None}
    level = np.array([growth.get(int(m), np.nan) for m in months])
    return level - _lagged(level, 1)


def demand_variant(panel: dict, control: np.ndarray) -> dict:
    """The industry regression with and without the TSA demand control, on the same months."""
    entry = {"id": DEMAND_SPEC["id"], "description": DEMAND_SPEC["text"], "includes_test_years": True}
    usable = np.isfinite(control)
    try:
        with_control = industry_fit(panel, DEMAND_SPEC, extra_control=control, extra_mask=usable)
        without = industry_fit(panel, DEMAND_SPEC, extra_mask=usable)
    except ValueError as error:
        return {**entry, "cumulative": None, "reason": str(error)}
    pick = lambda fit: [_lag_rows(fit)[k] for k in (0, 3, 6, 12)]
    position = with_control.fit.k - 1
    return {**entry, **with_control.describe(),
            "first_month": month_label(int(panel["months"][with_control.rows[0]])),
            "last_month": month_label(int(panel["months"][with_control.rows[-1]])),
            "cumulative": pick(with_control), "cumulative_without_control_same_months": pick(without),
            "demand_coef": with_control.fit.beta[position],
            "demand_coef_se": float(np.sqrt(with_control.cov[position, position])),
            "demand_coef_unit": "log points of real fare per unit change in traveller growth (1.0 = 100 points)"}


def industry_block(panel: dict, evaluate_test: bool, fuel_share: dict | None, demand: np.ndarray | None = None) -> dict:
    fits = {spec["id"]: industry_fit(panel, spec) for spec in INDUSTRY_SPECS}
    primary_spec = INDUSTRY_SPECS[0]
    primary = fits[primary_spec["id"]]
    months, y = panel["months"], panel[primary_spec["y"]]
    variants = []
    for spec in INDUSTRY_SPECS:
        fit = fits[spec["id"]]
        entry = {"id": spec["id"], "description": spec["text"], **fit.describe(),
                 "first_month": month_label(int(months[fit.rows[0]])),
                 "last_month": month_label(int(months[fit.rows[-1]]))}
        if spec.get("asymmetric"):
            entry["asymmetry"] = [asymmetry_test(fit, h) for h in (3, 6, 12)]
        else:
            rows = _lag_rows(fit)
            entry["cumulative"] = [rows[k] for k in (0, 3, 6, 12, 18) if k < len(rows)]
        variants.append(entry)
    if demand is not None:
        variants.append(demand_variant(panel, demand) if evaluate_test else
                        {"id": DEMAND_SPEC["id"], "description": DEMAND_SPEC["text"], "cumulative": None,
                         "reason": "withheld: its months lie in the test years"})

    # How the fit-split coefficients do outside the fit split, against "fares ignore fuel" (the fit-split mean).
    predicted, flat = primary.predict(), float(np.mean(y[primary.rows]))
    usable = np.isfinite(predicted) & np.isfinite(y)
    touched = (months >= PANDEMIC_M[0]) & (months - primary_spec["max_lag"] - 1 <= PANDEMIC_M[1])
    by_split = {}
    for name, (first, last) in SPLITS.items():
        if name == "test" and not evaluate_test:
            by_split[name] = {"status": "withheld: the test split is computed once, for the final build"}
            continue
        in_split = usable & (panel["year"] >= first) & (panel["year"] <= last)
        subsets = {"all": in_split}
        if name == "select":
            subsets["excluding_pandemic_touched"] = in_split & ~touched
        by_split[name] = {"in_sample": name == "fit"}
        for label, rows in subsets.items():
            scores = {"n": int(rows.sum())}
            if rows.any():
                scores.update(mae_fitted=stats.mae(predicted[rows], y[rows]),
                              mae_no_response=stats.mae(np.full(int(rows.sum()), flat), y[rows]))
            by_split[name][label] = scores

    lag_rows = _lag_rows(primary)
    converted = None
    if fuel_share is not None:
        share = fuel_share["mean_of_quarterly_ratios"]
        converted = [{"lag_months": r["lag_months"], "share": r["coef"] / share, "lo": r["lo"] / share,
                      "hi": r["hi"] / share} for r in lag_rows]
    conversion = {
        "formula": "recovery share through lag k = cumulative fare elasticity through lag k / (fuel expense / total "
                   "revenue): full recovery of a fuel cost change needs revenue to change by that share times the "
                   "percentage change in fuel",
        "fuel_share": fuel_share, "recovery_share": converted,
        "reason_null": None if fuel_share is not None else
        "data/lab/ual_quarterly.csv is absent, so there is no sourced fuel share of revenue; the elasticities are "
        "published unconverted",
        "caveat": "The share is United's, used for an industry price index; the interval carries the elasticity's "
                  "uncertainty only. The elasticity is of real fares to the nominal fuel price."}
    return {"primary_id": primary_spec["id"], "fit": primary, "fits": fits, "lag_rows": lag_rows,
            "variants": variants, "by_split": by_split, "conversion": conversion,
            "asymmetry": asymmetry_test(fits["industry_asymmetric_lags0_12_fit"], 12),
            "lags18_at_12": _lag_rows(fits["industry_real_sa_lags0_18_fit"])[12]}


# ------------------------------------------------------------------ part 2: United, quarterly

def load_ual_quarterly(path: Path = UAL_CSV, columns: tuple = UAL_COLUMNS) -> dict:
    """An earnings-release table on a regular quarterly grid; blank cells and absent quarters are NaN."""
    with open(path, newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or any(c not in rows[0] for c in ("period_end",) + tuple(columns)):
        raise ValueError(f"{Path(path).name} lacks the columns the fare clock needs")
    index = [quarter_index(date.fromisoformat(r["period_end"])) for r in rows]
    if len(set(index)) != len(index) or any(quarter_end(i) != r["period_end"] for i, r in zip(index, rows)):
        raise ValueError(f"{Path(path).name} needs one row per calendar quarter end")
    quarters = np.arange(min(index), max(index) + 1)
    table = {"q": quarters, "year": quarters // 4,
             "absent_quarters": [quarter_label(int(q)) for q in quarters if q not in set(index)]}
    for column in columns:
        values = np.full(len(quarters), np.nan)
        for i, row in zip(index, rows):
            if row[column].strip():
                values[i - quarters[0]] = float(row[column])
        table[column] = values
    return table


def load_delta_quarterly(path: Path = DAL_CSV) -> dict:
    """Delta's table under United's column names, with Delta's adjusted figures in place of the GAAP ones."""
    table = load_ual_quarterly(path, UAL_COLUMNS + tuple(DAL_ADJUSTED.values()))
    for name, adjusted in DAL_ADJUSTED.items():
        table[name] = table[adjusted]
    return table


def delta_look(path: Path, rules: dict, seed: int) -> dict:
    """United's primary specification on Delta, and United's curve scored on Delta's quarters."""
    table = load_delta_quarterly(path)
    panel = united_panel(table)
    quarters = panel["q"]
    have = np.isfinite(panel["unit_rev"]) & np.isfinite(panel["fuel_cpasm"])
    out = {"measures": "Delta's adjusted TRASM (excludes third-party refinery sales) and adjusted average fuel price "
                       "per gallon times gallons, over ASM, as printed in its releases; GAAP revenue and fuel carry "
                       "the refinery's third-party sales and are not used.",
           "first_quarter_with_adjusted_figures": quarter_label(int(quarters[have][0])),
           "last_quarter": quarter_label(int(quarters[have][-1])), "curve": None}
    try:
        fit = recovery_fit(panel, years=(SPLITS["fit"][0], SPLITS["test"][1]), pandemic="exclude")
    except ValueError as error:
        out["reason"] = str(error)
    else:
        curve = padded_curve(fit)
        used = [quarter_label(int(q)) for q in quarters[fit.rows]]
        out.update(fit.describe(), quarters=used,
                   curve=[{"quarters_after": k, "estimate": curve["cumulative"][k], "se": curve["se"][k],
                           "lo": curve["lo"][k], "hi": curve["hi"][k]} for k in range(HORIZON)])
        years = panel["year"][fit.rows]
        early = [label for label, year in zip(used, years) if year <= SPLITS["fit"][1]]
        late = [label for label, year in zip(used, years) if year >= SPLITS["test"][0]]
        out["quarters_in_fit_split"], out["quarters_in_test_split"] = early, late
        out["split_note"] = (
            f"Delta's table starts in {quarter_label(int(quarters[0]))} and its adjusted unit revenue in "
            f"{out['first_quarter_with_adjusted_figures']}. With year-over-year changes and four lags, "
            f"{len(early)} of its quarters fall in the fit split ({', '.join(early) or 'none'}), too few to fit "
            f"anything, and none in the select split is free of 2020Q1-2021Q4 levels. The other {len(late)} usable "
            f"quarters ({late[0]} to {late[-1]}) are in the locked test years. So Delta cannot be split: this curve "
            f"is an in-sample estimate on {fit.fit.n} quarters with {fit.fit.k} coefficients, descriptive only, "
            "computed once after United's specification was fixed, and not used for United's curve, draws or scores.")
    scores = score_rules(panel, rules, np.array([], dtype=int), seed, True, baseline="none")
    out["united_rules_on_delta"] = {
        "note": "United's fitted curve (2015-2019 United data) and the rival rules applied to Delta's year-over-year "
                "changes, fuel term only because Delta has no fit-split quarters to estimate a baseline from. For "
                "the fitted curve this is out of sample: another airline, a later period.",
        "h1": scores["h1"]["test"], "h4": scores["h4"]["test"]}
    return out


def united_panel(table: dict, revenue: str = "total", fuel: str = "price_x_gallons") -> dict:
    """Unit revenue and fuel cost per available seat mile (cents) with their year-over-year changes.

    Unit revenue is the figure printed in the release (TRASM or PRASM), or revenue
    over seat miles where the release prints none. Fuel cost is the reported average
    price per gallon times gallons consumed, or reported fuel expense.
    """
    asm = table["asm_m"]
    reported, dollars = ((table["trasm_cents"], table["total_revenue_musd"]) if revenue == "total"
                         else (table["prasm_cents"], table["passenger_revenue_musd"]))
    computed = 100.0 * dollars / asm
    unit_rev = np.where(np.isfinite(reported), reported, computed)
    fuel_cost = (table["fuel_price_usd_gal"] * table["fuel_gallons_m"] if fuel == "price_x_gallons"
                 else table["fuel_expense_musd"])
    fuel_cpasm = 100.0 * fuel_cost / asm
    return {"q": table["q"], "year": table["year"], "asm": asm, "unit_rev": unit_rev, "fuel_cpasm": fuel_cpasm,
            "fuel_cost_musd": fuel_cost, "d_rev": unit_rev - _lagged(unit_rev, 4),
            "d_fuel": fuel_cpasm - _lagged(fuel_cpasm, 4), "cap": 100.0 * (asm / _lagged(asm, 4) - 1.0),
            "computed_unit_rev": computed, "reported_unit_rev": reported}


def pandemic_touched(quarters: np.ndarray, reach: int) -> np.ndarray:
    """Quarters for which any level from `reach` quarters back to the quarter itself lies in the pandemic window."""
    return (quarters >= PANDEMIC_Q[0]) & (quarters - reach <= PANDEMIC_Q[1])


def recovery_fit(panel: dict, years=SPLITS["fit"], max_lag: int = 4, control: bool = True, transform: str = "yoy",
                 pandemic: str = "none", extra=None, drop_year: int | None = None,
                 hac_lag: int = QUARTERLY_HAC_LAG) -> LagFit:
    """Change in unit revenue on the change in fuel cost per seat mile at lags 0..max_lag (all in cents).

    transform "yoy" uses year-over-year changes; "qoq_sd" uses the quarter-on-quarter
    change of the year-over-year change. `control` adds year-over-year capacity growth
    (percent), transformed the same way. pandemic "exclude" drops every row with any
    level in 2020Q1-2021Q4; "indicators" adds one indicator per quarter whose own
    unit-revenue change uses such a level.
    """
    y, x, cap, own = panel["d_rev"], panel["d_fuel"], panel["cap"], 4
    if transform == "qoq_sd":
        y, x, cap, own = y - _lagged(y, 1), x - _lagged(x, 1), cap - _lagged(cap, 1), 5
    elif transform != "yoy":
        raise ValueError("transform must be 'yoy' or 'qoq_sd'")
    quarters = panel["q"]
    mask = (panel["year"] >= years[0]) & (panel["year"] <= years[1])
    if drop_year is not None:
        mask &= panel["year"] != drop_year
    controls = [cap] if control else []
    if pandemic == "exclude":
        mask &= ~pandemic_touched(quarters, own + max_lag)
    elif pandemic == "indicators":
        controls += [(quarters == q).astype(float) for q in quarters[pandemic_touched(quarters, own) & mask]]
    elif pandemic != "none":
        raise ValueError("pandemic must be 'none', 'exclude' or 'indicators'")
    if extra is not None:
        controls.append(np.asarray(extra, dtype=float))
    return lag_regression(y, [x], max_lag, mask, np.column_stack(controls) if controls else None, hac_lag)


def padded_curve(fit: LagFit) -> dict:
    """Cumulative shares for quarters 0..4; a shorter lag polynomial holds its last value."""
    pad = lambda v: np.concatenate([v, np.repeat(v[-1], HORIZON - len(v))])[:HORIZON]
    return {key: pad(np.asarray(value)) for key, value in fit.cumulative().items()}


def fuel_term(d_fuel, cumulative) -> np.ndarray:
    """Unit-revenue change a response rule assigns to fuel: sum over j of increment[j] * d_fuel[t-j].

    `cumulative` is the rule's recovery curve (share recovered j quarters after the
    shock); its first differences are the lag weights. NaN where a lag is missing.
    """
    increments = np.diff(np.concatenate([[0.0], np.asarray(cumulative, dtype=float)]))
    matrix, kept = stats.lag_matrix(d_fuel, len(increments) - 1)
    out = np.full(len(np.asarray(d_fuel)), np.nan)
    out[kept] = matrix @ increments
    return out


def rule_predictions(panel: dict, cumulative, fit_rows, baseline: str = "fit_split") -> dict:
    """Predicted year-over-year change in unit revenue under one response rule, given realised fuel cost.

    h4 (four quarters ahead of the year-ago level): baseline + fuel term.
    h1 (one quarter ahead of last quarter's reported change): last quarter's actual
    year-over-year change plus the change in the baseline and in the fuel term.
    baseline "fit_split": a constant and a capacity-growth coefficient estimated on
    the fit rows with the fuel response held at the rule; "none": fuel term only.
    """
    term, cap, actual = fuel_term(panel["d_fuel"], cumulative), panel["cap"], panel["d_rev"]
    intercept = slope = 0.0
    if baseline == "fit_split":
        intercept, slope = stats.ols(cap[fit_rows], (actual - term)[fit_rows]).beta
    elif baseline != "none":
        raise ValueError("baseline must be 'fit_split' or 'none'")
    h4 = intercept + slope * cap + term
    h1 = _lagged(actual, 1) + slope * (cap - _lagged(cap, 1)) + term - _lagged(term, 1)
    return {"h4": h4, "h1": h1, "intercept": float(intercept), "capacity_coef": float(slope), "fuel_term": term}


def score_rules(panel: dict, rules: dict, fit_rows, seed: int, evaluate_test: bool,
                baseline: str = "fit_split") -> dict:
    """Mean absolute error of each rule by split and horizon, and fitted-minus-rival paired differences."""
    predictions = {name: rule_predictions(panel, curve, fit_rows, baseline) for name, curve in rules.items()}
    actual, quarters, out = panel["d_rev"], panel["q"], {}
    for h, (key, reach, block) in enumerate((("h1", 9, 2), ("h4", 8, 4))):
        errors = {name: np.abs(actual - p[key]) for name, p in predictions.items()}
        usable = np.all([np.isfinite(e) for e in errors.values()], axis=0)
        touched = pandemic_touched(quarters, reach)
        subsets = {"fit": usable & np.isin(np.arange(len(quarters)), fit_rows)}
        for name in ("select", "test"):
            first, last = SPLITS[name]
            subsets[name] = usable & (panel["year"] >= first) & (panel["year"] <= last)
        subsets["select_excluding_pandemic"] = subsets["select"] & ~touched
        left_out = [quarter_label(int(q)) for q in quarters[subsets["test"] & touched]]
        subsets["test"] = subsets["test"] & ~touched   # the test never scores a quarter with a pandemic level behind it
        out[key] = {}
        for s, (name, rows) in enumerate(subsets.items()):
            if name == "test" and not evaluate_test:
                out[key][name] = {"status": "withheld: the test split is computed once, for the final build"}
                continue
            n = int(rows.sum())
            entry = {"n": n, "quarters": [quarter_label(int(q)) for q in quarters[rows]], "in_sample": name == "fit",
                     "mae_cents_per_asm": {r: (float(errors[r][rows].mean()) if n else None) for r in rules}}
            if n:
                core = {r: entry["mae_cents_per_asm"][r] for r in CORE_RULES}
                ranked = sorted(core, key=core.get)
                if name == "test":
                    entry["left_out_pandemic_touched"] = left_out
                entry["lowest_mae_core_rule"] = ranked[0]
                entry["margin_to_next_core_rule"] = core[ranked[1]] - core[ranked[0]]
                entry["fitted_minus_rival_abs_error"] = {
                    r: stats.paired_loss_diff(errors["fitted"][rows], errors[r][rows], N_BOOT,
                                              seed + 100 * h + 10 * s + i, LEVEL, block_len=min(block, n))
                    for i, r in enumerate(rules) if r != "fitted"}
            out[key][name] = entry
    out["baselines"] = {name: {"intercept_cents": p["intercept"], "capacity_coef_cents_per_point": p["capacity_coef"]}
                        for name, p in predictions.items()}
    return out


def leave_one_year_out_mae(panel: dict, score_rows=None, **spec) -> float:
    """Mean absolute error on the fit split when each calendar year is predicted from the others.

    Errors are averaged over `score_rows` (default: the rows the specification
    uses), so specifications with different samples can be compared on common rows.
    The target is the specification's own dependent variable.
    """
    full = recovery_fit(panel, **spec)
    y = panel["d_rev"] - _lagged(panel["d_rev"], 1) if spec.get("transform") == "qoq_sd" else panel["d_rev"]
    scored = full.rows if score_rows is None else np.intersect1d(full.rows, score_rows)
    errors = []
    for year in np.unique(panel["year"][scored]):
        held = scored[panel["year"][scored] == year]
        errors.extend(np.abs(y[held] - recovery_fit(panel, drop_year=int(year), **spec).predict(held)))
    return float(np.mean(errors))


def bootstrap_curve(fit: LagFit, seed: int, block_len: int = 4, n_boot: int = N_BOOT) -> dict:
    """Moving-block bootstrap of the rows, as a check on the covariance-based draws."""
    design, y, cols = fit.design[fit.rows], fit.fit.resid + fit.fit.fitted, fit.blocks[0]

    def stat(yb, xb):
        refit = stats.ols(xb, yb)
        if refit.rank_deficient or refit.cond > 1e8:
            return np.full(HORIZON, np.nan)
        cum = np.cumsum(refit.beta[cols])
        return np.concatenate([cum, np.repeat(cum[-1], HORIZON - len(cum))])[:HORIZON]

    draws = stats.block_bootstrap(stat, (y, design), min(block_len, len(y)), n_boot, seed)
    good = draws[np.isfinite(draws).all(axis=1)]
    out = {"method": f"moving-block bootstrap of the estimation rows, block length {min(block_len, len(y))}",
           "n_boot": n_boot, "unusable_resamples": int(len(draws) - len(good)), "curve": None}
    if len(good) >= 100:
        q = stats.quantiles(good, (0.1, 0.5, 0.9))
        out["curve"] = [{"quarters_after": k, "p10": q[0][k], "p50": q[1][k], "p90": q[2][k]} for k in range(HORIZON)]
    return out


RHO_GRID = np.round(np.arange(0.0, 0.951, 0.01), 2)
SMOOTH_METHOD = (
    "Partial adjustment: recovery(k) = L x (1 - rho^(k+1)) for k = 0..4 quarters after the shock. L is the share "
    "recovered in the long run and rho the part of the remaining gap left open each quarter; the half-life, "
    "ln(0.5)/ln(rho), counts quarters from the start of the shock quarter until half of L is in. Same data and "
    "definition as the five-lag regression: year-over-year change in TRASM on a constant, year-over-year capacity "
    "growth and L times the weighted sum of the year-over-year fuel cost changes at lags 0 to 4 with weights "
    "(1 - rho) rho^j. Least squares: rho on a grid from 0 to 0.95 in steps of 0.01, L in closed form given rho. "
    "Uncertainty: a moving-block bootstrap over quarters (blocks of four consecutive estimation rows, each row "
    "carrying its own lags), L and rho re-estimated on every resample; ranges are percentiles of the resamples and "
    "the draws are the resampled curves. Where the estimation rows are not contiguous (2019Q4 is followed by "
    "2024Q1) a block can straddle the gap. The curve stops at quarter 4, where the share is L x (1 - rho^5).")
SMOOTH_POST_HOC = (
    "This two-parameter form was chosen AFTER the locked test years had been seen, because the pre-specified "
    "five-lag curve was not monotone and lost the test. Its test-split score is therefore descriptive, not a clean "
    "test. The clean test of it is the dated forward forecast for the next quarter.")


def smooth_curve(long_run: float, rho: float) -> np.ndarray:
    """Share recovered 0..4 quarters after the shock under partial adjustment."""
    return long_run * (1.0 - rho ** np.arange(1, HORIZON + 1))


def half_life(rho: float) -> float:
    """Quarters from the start of the shock quarter until half the long-run share is in; 0 when rho is 0."""
    return 0.0 if rho <= 0.0 else math.log(0.5) / math.log(rho)


def smooth_profile(y: np.ndarray, lags: np.ndarray, cap: np.ndarray) -> tuple[float, float]:
    """Least-squares (L, rho): for each rho on the grid, y on a constant, capacity growth and the weighted lag sum.

    The constant and capacity are partialled out once; L given rho is then a ratio
    of two inner products, so the whole grid is one matrix product. Returns NaNs
    when the resample cannot identify the fit.
    """
    base = np.column_stack([np.ones(len(y)), cap])
    residual = lambda v: v - base @ np.linalg.lstsq(base, v, rcond=None)[0]
    weights = (1.0 - RHO_GRID)[:, None] * RHO_GRID[:, None] ** np.arange(lags.shape[1])
    z, r = residual(lags @ weights.T), residual(y)
    zz, zy = np.einsum("ij,ij->j", z, z), z.T @ r
    if not np.all(zz > 1e-12):
        return math.nan, math.nan
    best = int(np.argmin(r @ r - zy ** 2 / zz))
    return float(zy[best] / zz[best]), float(RHO_GRID[best])


def smooth_fit(panel: dict, lags: np.ndarray, rows: np.ndarray, seed: int, n_draws: int) -> dict:
    """The partial-adjustment curve on the given rows, with block-bootstrap ranges and joint draws of the curve."""
    y, x, cap, quarters = panel["d_rev"][rows], lags[rows], panel["cap"][rows], panel["q"][rows]
    long_run, rho = smooth_profile(y, x, cap)
    weights = (1.0 - rho) * rho ** np.arange(x.shape[1])
    final = stats.ols(np.column_stack([x @ weights, cap]), y)
    resampled = []
    for index in stats.block_indices(len(y), min(4, len(y)), int(1.5 * n_draws) + 100, seed):
        pair = smooth_profile(y[index], x[index], cap[index])
        if math.isfinite(pair[0]):
            resampled.append(pair)
        if len(resampled) == n_draws:
            break
    if len(resampled) < n_draws:
        raise ValueError("Too few usable bootstrap resamples for the smooth curve")
    pairs = np.array(resampled)
    draws = np.array([smooth_curve(a, b) for a, b in pairs])
    levels, names = (0.05, 0.1, 0.5, 0.9, 0.95), ("p05", "p10", "p50", "p90", "p95")
    spread = lambda values: dict(zip(names, stats.quantiles(values, levels)))
    curve_q, estimate = stats.quantiles(draws, levels), smooth_curve(long_run, rho)
    long_q, year_q = spread(pairs[:, 0]), spread(draws[:, -1])
    correlation = float(np.corrcoef(pairs.T)[0, 1]) if pairs[:, 1].std() > 0 and pairs[:, 0].std() > 0 else 0.0
    at_bound = float(np.mean(pairs[:, 1] >= RHO_GRID[-1]))
    width_ratio = (long_q["p95"] - long_q["p05"]) / max(year_q["p95"] - year_q["p05"], 1e-9)
    return {"L": long_run, "rho": rho, "half_life_quarters": half_life(rho), "n": len(y),
            "first_quarter": quarter_label(int(quarters[0])), "last_quarter": quarter_label(int(quarters[-1])),
            "curve_estimate": estimate, "intercept_cents": final.beta[0],
            "capacity_coef_cents_per_point": final.beta[2], "r2": final.r2,
            "L_range": long_q, "rho_range": spread(pairs[:, 1]),
            "half_life_range": spread(np.array([half_life(b) for b in pairs[:, 1]])),
            "one_year_share": {"estimate": estimate[-1], **year_q},
            "curve": [{"quarters_after": k, "p10": curve_q[1][k], "p50": curve_q[2][k], "p90": curve_q[3][k],
                       "estimate": estimate[k], "p05": curve_q[0][k], "p95": curve_q[4][k]} for k in range(HORIZON)],
            "ridge": {"correlation_L_rho": correlation, "share_of_resamples_at_rho_upper_bound": at_bound,
                      "L_range_width_over_one_year_share_width": width_ratio,
                      "poorly_separated": bool(at_bound >= 0.05 or width_ratio >= 1.5),
                      "rule": "poorly separated when 5% or more of resamples sit at the upper bound of rho, or the "
                              "range of L is at least 1.5 times as wide as the range of the one-year share"},
            "n_resamples": n_draws, "draws": draws}


def expanding_fits(panel: dict, lags: np.ndarray, rows: np.ndarray, last_as_of: int, first_as_of: int = 2019 * 4,
                   min_rows: int = 8) -> list[dict]:
    """For each quarter, the smooth curve fitted on the rows dated before it, so a replay never sees its own future."""
    out = []
    for q in range(first_as_of, last_as_of + 1):
        known = rows[panel["q"][rows] < q]
        if len(known) < min_rows:
            continue
        long_run, rho = smooth_profile(panel["d_rev"][known], lags[known], panel["cap"][known])
        out.append({"as_of_period": quarter_label(q), "L": long_run, "rho": rho,
                    "half_life_quarters": half_life(rho), "curve_p50": smooth_curve(long_run, rho), "n": len(known),
                    "fitted_through": quarter_label(int(panel["q"][known[-1]]))})
    return out


UNITED_SPECS = (
    {"id": "united_yoy_trasm_lags0_4_capacity_fit", "kw": {},
     "text": "Year-over-year change in TRASM (cents) on year-over-year change in fuel cost per ASM (cents, price per "
             "gallon times gallons over ASM) at lags 0 to 4, controlling for year-over-year capacity growth; fit "
             "split only. Primary."},
    {"id": "united_yoy_trasm_lags0_4_no_control_fit", "kw": {"control": False},
     "text": "As primary without the capacity control, so capacity changes made in response to fuel count as recovery."},
    {"id": "united_yoy_trasm_lags0_2_capacity_fit", "kw": {"max_lag": 2},
     "text": "As primary with lags 0 to 2; the share is held at its two-quarter value afterwards."},
    {"id": "united_qoq_sd_trasm_lags0_4_capacity_fit", "kw": {"transform": "qoq_sd"},
     "text": "Quarter-on-quarter change of the year-over-year changes (seasonally differenced), lags 0 to 4, "
             "capacity control differenced the same way."},
    {"id": "united_yoy_prasm_lags0_4_capacity_fit", "kw": {}, "revenue": "passenger", "asc606": True,
     "text": "As primary with passenger unit revenue (PRASM) and an indicator for the four quarters of 2018, whose "
             "year-over-year change straddles the accounting change that moved ancillary fees into passenger revenue."},
    {"id": "united_yoy_trasm_lags0_4_capacity_fit_reported_fuel_expense", "kw": {}, "fuel": "expense",
     "text": "As primary with reported fuel expense per ASM in place of price per gallon times gallons."},
    {"id": "united_yoy_trasm_lags0_4_capacity_2013_2023_excluding_pandemic",
     "kw": {"years": (2013, 2023), "pandemic": "exclude"},
     "text": "As primary on 2013-2023, dropping every quarter with any level in 2020Q1-2021Q4 behind its "
             "year-over-year changes or their four lags."},
    {"id": "united_yoy_trasm_lags0_4_capacity_2013_2023_pandemic_indicators",
     "kw": {"years": (2013, 2023), "pandemic": "indicators"},
     "text": "As primary on 2013-2023 with one indicator per quarter from 2020Q1 to 2022Q4 (quarters whose own "
             "year-over-year change uses a 2020Q1-2021Q4 level); the 2023 quarters stay in, with pandemic-based "
             "fuel changes among their lags."},
    {"id": "united_yoy_trasm_lags0_4_capacity_2013_2023_no_pandemic_handling", "kw": {"years": (2013, 2023)},
     "text": "As primary on 2013-2023 with the pandemic quarters left in and no indicators."},
)


def _united_fit(table: dict, spec: dict) -> tuple[dict, LagFit]:
    panel = united_panel(table, spec.get("revenue", "total"),
                         "fuel_expense" if spec.get("fuel") == "expense" else "price_x_gallons")
    extra = (panel["year"] == 2018).astype(float) if spec.get("asc606") else None
    return panel, recovery_fit(panel, extra=extra, **spec["kw"])


def fuel_share_of_revenue(table: dict, years=SPLITS["fit"]) -> dict:
    """Reported fuel expense over total revenue, by quarter, for converting an elasticity to a share."""
    rows = (table["year"] >= years[0]) & (table["year"] <= years[1])
    rows &= np.isfinite(table["fuel_expense_musd"]) & np.isfinite(table["total_revenue_musd"])
    ratio = table["fuel_expense_musd"][rows] / table["total_revenue_musd"][rows]
    out = {"source": "data/lab/ual_quarterly.csv: fuel_expense_musd / total_revenue_musd", "years": list(years),
           "n_quarters": int(rows.sum()), "mean_of_quarterly_ratios": float(ratio.mean()),
           "min": float(ratio.min()), "max": float(ratio.max()),
           "ratio_of_sums": float(table["fuel_expense_musd"][rows].sum() / table["total_revenue_musd"][rows].sum())}
    passenger = table["passenger_revenue_musd"][rows]
    if np.isfinite(passenger).all():
        out["mean_share_of_passenger_revenue"] = float((table["fuel_expense_musd"][rows] / passenger).mean())
    return out


# ------------------------------------------------------------------ management's statements

def load_literature(path: Path = LITERATURE_JSON) -> dict:
    """Verified literature entries by id; empty when the file is absent."""
    if not Path(path).is_file():
        return {}
    return {e["id"]: e for e in json.loads(Path(path).read_text())["entries"] if e.get("verified") is True}


def management_statements(literature: dict) -> list[dict]:
    """What United said about recovering the 2026 fuel increase, by calendar quarter, with sources."""
    sources = (("ual-2026q1-investor-update", "april_2026", "guidance assumption"),
               ("ual-2026q2-investor-update", "july_2026", "expectation"),
               ("ual-2026q2-earnings-release", "july_2026", "reported outcome"))
    pattern = re.compile(r"^(?:Assumed|Expected) recovery,? (?:by )?(\d{4}Q[1-4])(?:, (low|high))?$")
    found = {}
    for entry_id, path, kind in sources:
        entry = literature.get(entry_id)
        for number in (entry or {}).get("numbers", []):
            label, match = number["label"], pattern.match(number["label"])
            if kind == "reported outcome":
                if not label.startswith("Share of the second-quarter fuel expense increase recovered"):
                    continue
                period, side = "2026Q2", None
            elif match:
                period, side = match.groups()
            else:
                continue
            item = found.setdefault((path, period, kind), {
                "path": path, "period": period, "low": None, "high": None, "kind": kind, "source_id": entry_id,
                "url": entry.get("url"), "quote": number["quote"]})
            share = float(number["value"]) / 100.0
            for key in (("low", "high") if side is None else (side,)):
                item[key] = share
    stated = [s for s in found.values() if s["low"] is not None and s["high"] is not None]
    if not any(s["path"] == "july_2026" and s["kind"] == "expectation" for s in stated):
        stated += [{"path": "july_2026", "period": period, "low": low, "high": high, "kind": "expectation",
                    "source_id": None, "url": None, "quote": None,
                    "note": "literature file absent: range as stated in docs/lab_contract.md (8-K, 15 July 2026)"}
                   for period, (low, high) in PROMISE_FALLBACK.items()]
    return sorted(stated, key=lambda s: (s["path"], s["period"], s["kind"]))


def shock_onset(dates, values, before: date = PROMISE_DATE) -> dict | None:
    """First print in the year before the promise that is over 25% above the mean of the 60 prints before it."""
    values = np.asarray(values, dtype=float)
    for i in range(ONSET_WINDOW, len(dates)):
        if not (before.replace(year=before.year - 1) <= dates[i] <= before):
            continue
        trailing = float(values[i - ONSET_WINDOW:i].mean())
        if values[i] > (1.0 + ONSET_JUMP) * trailing:
            onset_q = quarter_index(dates[i])
            in_quarter = [d for d in dates if quarter_index(d) == onset_q]
            after = sum(d >= dates[i] for d in in_quarter)
            return {"rule": f"first daily print in the 12 months before {before.isoformat()} more than "
                            f"{_pct(ONSET_JUMP)} above the mean of the previous {ONSET_WINDOW} prints",
                    "date": dates[i].isoformat(), "usd_per_gallon": float(values[i]),
                    "trailing_mean_usd_per_gallon": trailing, "onset_quarter": quarter_label(onset_q),
                    "prints_in_onset_quarter": len(in_quarter), "prints_from_onset": int(after),
                    "first_full_quarter": quarter_label(onset_q + (0 if after >= 0.9 * len(in_quarter) else 1))}
    return None


def management_paths(statements: list[dict], onset: dict | None) -> dict:
    """Each stated path by calendar quarter, and mapped onto quarters after the shock."""
    paths = {}
    for name in sorted({s["path"] for s in statements}):
        stated = [s for s in statements if s["path"] == name]
        entry = {"stated": stated, "mapped": None, "mapped_midpoints": None}
        if onset is not None:
            zero = int(onset["first_full_quarter"][:4]) * 4 + int(onset["first_full_quarter"][-1]) - 1
            by_k = {}
            for s in stated:
                k = int(s["period"][:4]) * 4 + int(s["period"][-1]) - 1 - zero
                if 0 <= k < HORIZON and (k not in by_k or s["kind"] != "reported outcome"):
                    by_k[k] = s
            if 0 in by_k:
                mapped, last = [], None
                for k in range(HORIZON):
                    last = by_k.get(k, last)
                    mapped.append({"quarters_after": k, "low": last["low"], "high": last["high"],
                                   "period": last["period"], "basis": last["kind"] if k in by_k else
                                   "not stated: last stated range held"})
                entry["mapped"] = mapped
                entry["mapped_midpoints"] = [round((m["low"] + m["high"]) / 2.0, 6) for m in mapped]
        paths[name] = entry
    return paths


def promise_rule(statements: list[dict]) -> list[float]:
    """The July promise read as a generic rule: the first promised quarter is the quarter of the shock."""
    promised = sorted((s for s in statements if s["path"] == "july_2026" and s["kind"] == "expectation"),
                      key=lambda s: s["period"])
    mids = [round((s["low"] + s["high"]) / 2.0, 6) for s in promised]
    return (mids + [mids[-1]] * HORIZON)[:HORIZON]


def published_estimates(literature: dict) -> tuple[list[dict], list[str]]:
    """Published numbers from verified literature entries, in the unit each source used.

    `value` is a share of the cost change only where the source states one (in
    percent); otherwise it is None and the number sits in `value_as_published`
    with its unit. Nothing is converted.
    """
    out, missing = [], []
    for entry_id, prefix in PUBLISHED:
        entry = literature.get(entry_id)
        number = next((n for n in (entry or {}).get("numbers", []) if n["label"].startswith(prefix)), None)
        if number is None:
            missing.append(f"{entry_id}: {prefix}")
            continue
        is_share = "percent of the cost change" in number["unit"] or "pass-through rate" in number["label"]
        out.append({"label": f"{entry['authors']} ({entry['year']}): {number['label']}",
                    "value": float(number["value"]) / 100.0 if is_share else None,
                    "value_as_published": number["value"], "unit": number["unit"], "stated_as_share_of_cost": is_share,
                    "url": entry.get("url"), "entry_id": entry_id, "quote": number["quote"],
                    "quality": entry.get("quality"), "kind": entry.get("kind")})
    return out, missing


def quarter_accounts(table: dict, panel: dict, fit: LagFit, draws: np.ndarray, period: str,
                     stated_share: dict | None) -> dict:
    """One quarter, year over year: raw ratios, the baseline that would make United's statement true,
    and what the fitted model assigns to fuel. Every input is from ual_quarterly.csv."""
    t = int(np.flatnonzero(table["q"] == int(period[:4]) * 4 + int(period[-1]) - 1)[0])
    revenue, fuel, asm = table["total_revenue_musd"], table["fuel_expense_musd"], table["asm_m"]
    d_revenue, d_fuel_usd = revenue[t] - revenue[t - 4], fuel[t] - fuel[t - 4]
    capacity = asm[t] / asm[t - 4] - 1.0
    d_rev, d_fuel = panel["d_rev"][t], panel["d_fuel"][t]
    beta = fit.fit.beta
    lags = np.array([panel["d_fuel"][t - j] for j in range(len(fit.blocks[0]))])
    baseline = beta[0] + fit.design[t, len(fit.blocks[0]):] @ beta[1 + len(fit.blocks[0]):]
    attributed = float(lags @ beta[fit.blocks[0]])
    attributed_draws = (draws[:, :len(lags)] @ lags) if draws is not None else None
    q = lambda v: None if attributed_draws is None else dict(zip(("p10", "p50", "p90"), stats.quantiles(v)))
    out = {"period": period,
           "inputs": {"total_revenue_musd": [revenue[t - 4], revenue[t]], "fuel_expense_musd": [fuel[t - 4], fuel[t]],
                      "asm_m": [asm[t - 4], asm[t]], "unit_revenue_cents": [panel["unit_rev"][t - 4], panel["unit_rev"][t]],
                      "fuel_cost_per_asm_cents": [panel["fuel_cpasm"][t - 4], panel["fuel_cpasm"][t]],
                      "order": "year-ago quarter, quarter"},
           "raw": {"revenue_change_musd": d_revenue, "fuel_expense_change_musd": d_fuel_usd,
                   "revenue_change_over_fuel_expense_change": d_revenue / d_fuel_usd,
                   "unit_revenue_change_cents": d_rev, "fuel_cost_per_asm_change_cents": d_fuel,
                   "unit_revenue_change_over_fuel_cost_per_asm_change": d_rev / d_fuel,
                   "capacity_growth": capacity, "revenue_growth": d_revenue / revenue[t - 4],
                   "note": "No baseline is taken off: all revenue growth is set against the fuel increase."},
           "implied_by_statement": None,
           "implied_by_statement_note": "United stated no recovered share for this quarter, so there is no baseline "
                                        "to solve for.",
           "fitted_model": {
               "fuel_attributed_unit_revenue_cents": attributed,
               "fuel_attributed_revenue_musd": attributed * asm[t] / 100.0,
               "share_of_fuel_cost_per_asm_change": attributed / d_fuel,
               "share_of_fuel_cost_per_asm_change_range": q(attributed_draws / d_fuel) if attributed_draws is not None else None,
               "share_of_fuel_expense_change": attributed * asm[t] / 100.0 / d_fuel_usd,
               "baseline_unit_revenue_change_cents": float(baseline),
               "unexplained_unit_revenue_change_cents": float(d_rev - baseline - attributed),
               "realised_share_after_fitted_baseline": float((d_rev - baseline) / d_fuel),
               "note": "fuel-attributed = sum of the fitted lag coefficients times this and the previous four "
                       "quarters' fuel cost changes; baseline = fitted constant plus capacity effect; "
                       "realised share = (actual unit revenue change - baseline) / fuel cost change, which "
                       "also carries everything the model does not explain"}}
    if stated_share is not None:
        share = (stated_share["low"] + stated_share["high"]) / 2.0
        growth = (d_revenue - share * d_fuel_usd) / revenue[t - 4]
        out["implied_by_statement_note"] = None
        out["implied_by_statement"] = {
            "label": "IMPLIED, not disclosed by United: the baseline year-over-year revenue growth that makes "
                     "(revenue change - baseline) / fuel expense change equal the stated share",
            "stated_share": share, "source_id": stated_share["source_id"], "quote": stated_share["quote"],
            "implied_baseline_revenue_growth": growth,
            "implied_baseline_revenue_change_musd": growth * revenue[t - 4],
            "implied_baseline_unit_revenue_growth_at_actual_capacity": (1.0 + growth) / (1.0 + capacity) - 1.0}
    return out


# ------------------------------------------------------------------ wording generated from the numbers

def verdict(lo: float, hi: float, promise_low: float, promise_high: float) -> str:
    """Where an interval sits against zero and against a promised range."""
    zero_inside, promise_overlaps = lo <= 0.0 <= hi, lo <= promise_high and hi >= promise_low
    if zero_inside and promise_overlaps:
        return "neutral"
    if promise_overlaps:
        return "consistent_with_promise"
    if hi < promise_low:
        return "below_promise_zero_not_excluded" if zero_inside else ("below_zero" if hi < 0 else "partial")
    return "above_promise"


def united_headline(curve: list[dict], n: int) -> str:
    """The measured behaviour in one sentence; neutral when the one-year range cannot tell a response from none."""
    now, year = curve[0], curve[-1]
    stem = (f"After a fuel cost shock, United's unit revenue has made up {_pct(now['estimate'])} of the cost in the "
            f"same quarter and {_pct(year['estimate'])} a year later ({int(100 * LEVEL)}% ranges {_pct(now['lo'])} "
            f"to {_pct(now['hi'])} and {_pct(year['lo'])} to {_pct(year['hi'])}, {n} quarters)")
    lo, hi = year["lo"], year["hi"]
    if lo <= 0.0 <= hi:
        tail = ("the range is too wide to tell full recovery from none" if hi >= 1.0
                else "the range is too wide to tell a revenue response from none")
    elif hi < 0.0:
        tail = "in this history unit revenue moved against fuel cost"
    elif lo > 1.0:
        tail = "more than the full cost came back within a year"
    elif hi >= 1.0:
        tail = "a response is clear, and full recovery within a year is inside the range"
    else:
        tail = "a response is clear, and the range stops short of the full cost within a year"
    return f"{stem}: {tail}."


def promise_line(point: float, lo: float, hi: float, promise_low: float, promise_high: float, n: int,
                 quarters_after: int, period: str) -> str:
    """The fitted share next to a stated promise; neutral when the range holds both the promise and zero."""
    when = {0: "In the quarter of a fuel cost shock", 1: "One quarter after a fuel cost shock"}.get(
        quarters_after, f"{quarters_after} quarters after a fuel cost shock")
    promise = _pct(promise_low) if promise_low == promise_high else f"{_pct(promise_low)} to {_pct(promise_high)}"
    stem = (f"{when} the fitted share is {_pct(point)} ({int(100 * LEVEL)}% range {_pct(lo)} to {_pct(hi)}, "
            f"{n} quarters)")
    tail = {"neutral": f"the range is too wide to tell the {promise} management stated for {period} from no "
                       "recovery at all",
            "consistent_with_promise": f"that is consistent with the {promise} management stated for {period}, and "
                                       "no recovery is outside the range",
            "below_promise_zero_not_excluded": f"that is below the {promise} management stated for {period}, and "
                                               "the range does not rule out no recovery",
            "below_zero": f"that is below the {promise} management stated for {period}: in this history unit "
                          "revenue moved against fuel cost",
            "partial": f"more than nothing, less than the {promise} management stated for {period}",
            "above_promise": f"that is more than the {promise} management stated for {period}",
            }[verdict(lo, hi, promise_low, promise_high)]
    return f"{stem}: {tail}."


def industry_headline(lag_row: dict, n: int) -> str:
    stem = (f"Industry fares: a 10% rise in jet fuel goes with a {10 * lag_row['coef']:+.2f}% change in real "
            f"airfares over {lag_row['lag_months']} months ({int(100 * LEVEL)}% range "
            f"{10 * lag_row['lo']:+.2f}% to {10 * lag_row['hi']:+.2f}%, {n} months)")
    if lag_row["lo"] <= 0.0 <= lag_row["hi"]:
        return f"{stem}: the range includes zero, so this history cannot tell a fare response from none."
    return f"{stem}: {'fares rose with fuel' if lag_row['coef'] > 0 else 'fares moved against fuel'}."


# ------------------------------------------------------------------ the build

def _united_block(table: dict, statements: list[dict], paths: dict, seed: int, n_draws: int,
                  evaluate_test: bool) -> dict:
    primary_spec = UNITED_SPECS[0]
    panel, primary = _united_fit(table, primary_spec)
    quarters, n_lags = panel["q"], len(primary.blocks[0])
    curve = padded_curve(primary)
    coefficient_draws, draws = curve_draws(primary.fit.beta[primary.blocks[0]],
                                           primary.cov[np.ix_(primary.blocks[0], primary.blocks[0])],
                                           n_draws, seed, df=primary.fit.df_resid)
    quantile = stats.quantiles(draws, (0.05, 0.1, 0.5, 0.9, 0.95))
    curve_rows = [{"quarters_after": k, "p10": quantile[1][k], "p50": quantile[2][k], "p90": quantile[3][k],
                   "estimate": curve["cumulative"][k], "se": curve["se"][k], "lo": curve["lo"][k],
                   "hi": curve["hi"][k], "p05": quantile[0][k], "p95": quantile[4][k],
                   "se_newey_west": curve["se_newey_west"][k], "se_classical": curve["se_classical"][k],
                   "lag_coef": primary.fit.beta[primary.blocks[0][k]]} for k in range(HORIZON)]

    rules = {"costs_only": [0.0] * HORIZON, "full_immediate": [1.0] * HORIZON,
             "management": promise_rule(statements), "fitted": list(curve["cumulative"])}
    for name, entry in paths.items():
        if entry["mapped_midpoints"] is not None:
            rules[f"management_{name}_mapped"] = entry["mapped_midpoints"]

    # The smooth two-parameter curve. Its rule is added last, so every earlier score keeps its bootstrap seed.
    lag_columns = primary.design[:, :n_lags]
    clean_rows = (recovery_fit(panel, years=(SPLITS["fit"][0], SPLITS["test"][1]), pandemic="exclude").rows
                  if evaluate_test else primary.rows)
    smooth = {"fit_only": smooth_fit(panel, lag_columns, primary.rows, seed + 6, n_draws),
              "through_latest": smooth_fit(panel, lag_columns, clean_rows, seed + 7, n_draws) if evaluate_test else None,
              "expanding": expanding_fits(panel, lag_columns, clean_rows,
                                          int(quarters[-1]) + 1 if evaluate_test else SPLITS["select"][1] * 4 + 3)}
    rules["smooth_fit_only"] = list(smooth["fit_only"]["curve_estimate"])
    # The same table with the smooth curve in the fitted seat, for its own ranking and paired differences.
    seated = {**{r: rules[r] for r in CORE_RULES if r != "fitted"}, "fitted": rules["smooth_fit_only"],
              "prespecified_five_lag": rules["fitted"]}
    smooth_scores = score_rules(panel, seated, primary.rows, seed + 8, evaluate_test)

    scores = score_rules(panel, rules, primary.rows, seed + 1, evaluate_test)
    fuel_only = score_rules(panel, rules, primary.rows, seed + 2, evaluate_test, baseline="none")

    variants = []
    for spec in UNITED_SPECS:
        v_panel, fit = _united_fit(table, spec)
        v_curve = padded_curve(fit)
        v_scores = score_rules(panel, {**{r: rules[r] for r in CORE_RULES}, "fitted": list(v_curve["cumulative"])},
                               primary.rows, seed + 3, False)
        entry = {"id": spec["id"], "description": spec["text"], **fit.describe(),
                 "first_quarter": quarter_label(int(v_panel["q"][fit.rows[0]])),
                 "last_quarter": quarter_label(int(v_panel["q"][fit.rows[-1]])),
                 "same_rows_as_primary": bool(np.array_equal(fit.rows, primary.rows)),
                 "curve": [{"quarters_after": k, "estimate": v_curve["cumulative"][k], "se": v_curve["se"][k],
                            "lo": v_curve["lo"][k], "hi": v_curve["hi"][k]} for k in range(HORIZON)],
                 "select_scored_on": "primary TRASM target and fuel measure; select rows are in sample for "
                                     "variants estimated on 2013-2023",
                 "select_mae_h4": v_scores["h4"]["select"]["mae_cents_per_asm"]["fitted"],
                 "select_mae_h1": v_scores["h1"]["select"]["mae_cents_per_asm"]["fitted"],
                 "select_n_h4": v_scores["h4"]["select"]["n"]}
        if spec["kw"].get("years", SPLITS["fit"]) == SPLITS["fit"] and not spec.get("asc606"):
            entry["fit_leave_one_year_out_mae"] = leave_one_year_out_mae(v_panel, primary.rows, **spec["kw"])
            entry["fit_leave_one_year_out_target"] = (
                "quarterly change of the year-over-year change in TRASM: not comparable with the others"
                if spec["kw"].get("transform") == "qoq_sd" else "year-over-year change in TRASM, primary rows")
        variants.append(entry)

    stated_q2 = next((s for s in statements if s["kind"] == "reported outcome" and s["period"] == "2026Q2"), None)
    accounts = None
    if evaluate_test:
        accounts = []
        for period, stated in (("2026Q2", stated_q2), ("2026Q1", None)):
            index = int(period[:4]) * 4 + int(period[-1]) - 1
            if index in quarters and np.isfinite(panel["d_rev"][quarters == index]).all() \
                    and np.isfinite(primary.design[quarters == index]).all():
                accounts.append(quarter_accounts(table, panel, primary, coefficient_draws, period, stated))

    refit = None
    if evaluate_test:  # the primary specification on every quarter with no pandemic level behind it, test years included
        latest = recovery_fit(panel, years=(SPLITS["fit"][0], SPLITS["test"][1]), pandemic="exclude")
        l_curve = padded_curve(latest)
        refit = {"note": "The primary specification re-estimated on every quarter through the latest with no "
                         "2020Q1-2021Q4 level behind it. It includes the test years, was computed once together "
                         "with the test scores, and is not used for the curve, the draws or any score.",
                 **latest.describe(), "quarters": [quarter_label(int(q)) for q in quarters[latest.rows]],
                 "curve": [{"quarters_after": k, "estimate": l_curve["cumulative"][k], "se": l_curve["se"][k],
                            "lo": l_curve["lo"][k], "hi": l_curve["hi"][k]} for k in range(HORIZON)]}

    in_fit = np.zeros(len(quarters), dtype=bool)
    in_fit[primary.rows] = True
    points = [{"period_end": quarter_end(int(q)), "fuel_shock": panel["d_fuel"][i],
               "revenue_response": panel["d_rev"][i], "split": split_of(int(q) // 4),
               "capacity_growth_pct": panel["cap"][i], "in_estimation": bool(in_fit[i]),
               "pandemic_touched": bool(pandemic_touched(quarters[i:i + 1], 4)[0])}
              for i, q in enumerate(quarters)
              if np.isfinite(panel["d_fuel"][i]) and np.isfinite(panel["d_rev"][i]) and split_of(int(q) // 4)
              and (evaluate_test or split_of(int(q) // 4) != "test")]

    both = np.isfinite(panel["computed_unit_rev"]) & np.isfinite(panel["reported_unit_rev"])
    expense = table["fuel_expense_musd"]
    fuel_gap = np.abs(panel["fuel_cost_musd"] / expense - 1.0)
    checks = {"quarters": int(np.isfinite(panel["unit_rev"]).sum()),
              "first_quarter": quarter_label(int(quarters[0])), "last_quarter": quarter_label(int(quarters[-1])),
              "absent_quarters": table["absent_quarters"],
              "unit_revenue_from_release_table": int(np.isfinite(panel["reported_unit_rev"]).sum()),
              "unit_revenue_computed_from_revenue_over_asm": int((~np.isfinite(panel["reported_unit_rev"])
                                                                  & np.isfinite(panel["computed_unit_rev"])).sum()),
              "max_abs_gap_reported_vs_computed_trasm_cents":
                  float(np.max(np.abs(panel["computed_unit_rev"] - panel["reported_unit_rev"])[both])),
              "max_rel_gap_price_x_gallons_vs_fuel_expense": float(np.nanmax(fuel_gap)),
              "quarters_with_fuel_gap_over_2pct": [quarter_label(int(q)) for q, g in zip(quarters, fuel_gap) if g > 0.02],
              "fit_split_fuel_shock_range_cents": [float(np.min(panel["d_fuel"][primary.rows])),
                                                   float(np.max(panel["d_fuel"][primary.rows]))]}
    return {"panel": panel, "fit": primary, "curve_rows": curve_rows, "draws": draws,
            "coefficient_draws": coefficient_draws, "rules": rules, "scores": scores, "fuel_only": fuel_only,
            "variants": variants, "accounts": accounts, "points": points, "checks": checks, "stated_q2": stated_q2,
            "n_lags": n_lags, "refit": refit, "bootstrap": bootstrap_curve(primary, seed + 4),
            "smooth": smooth, "smooth_scores": smooth_scores, "lag_columns": lag_columns}


def _oos_summary(scores: dict, evaluate_test: bool) -> dict:
    """Who has the lowest error on select and on test, by horizon, with the fitted-minus-best-rival difference."""
    out = {}
    for split in ("select", "select_excluding_pandemic", "test"):
        for horizon in ("h1", "h4"):
            entry = scores[horizon][split]
            if "status" in entry or not entry["n"]:
                out[f"{split}_{horizon}"] = {"n": entry.get("n", 0), "winner": None,
                                             "note": entry.get("status", "no quarters qualify")}
                continue
            core = {r: entry["mae_cents_per_asm"][r] for r in CORE_RULES}
            rival = min((r for r in CORE_RULES if r != "fitted"), key=core.get)
            diff = entry["fitted_minus_rival_abs_error"][rival]
            out[f"{split}_{horizon}"] = {
                "n": entry["n"], "winner": entry["lowest_mae_core_rule"],
                "margin_cents_per_asm": entry["margin_to_next_core_rule"], "mae_cents_per_asm": core,
                "best_rival": rival, "fitted_minus_best_rival": diff,
                "distinguishable": bool(diff["n"] >= 2 and (diff["hi"] < 0 or diff["lo"] > 0))}
    return out


def delta_september_quarter(path: Path) -> dict | None:
    """Delta's September 2026 quarter against its July guidance, as recorded in the Delta check file."""
    if not Path(path).is_file():
        return None
    check = json.loads(Path(path).read_text()).get("guidance_vs_actual_sep_quarter") or {}
    try:
        fuel, eps, unit = check["fuel_price_usd_gal"], check["eps"], check["unit_revenue"]
        return {"fuel_assumed_usd_gal": fuel["assumed_all_in"], "fuel_actual_adj_usd_gal": fuel["actual_adj"],
                "fuel_actual_minus_assumed": fuel["actual_adj_minus_assumed"],
                "adj_trasm_yoy_pct": unit["adj_trasm_yoy_stated_pct_sep_quarter"],
                "eps_guided_low": eps["guided_low"], "eps_guided_high": eps["guided_high"],
                "eps_actual_adj": eps["actual_adj"], "eps_position": eps["position"],
                "evidence_ids": fuel["evidence_ids"] + unit["evidence_ids"] + eps["evidence_ids"]}
    except (KeyError, TypeError):
        return None


def build(seed: int = SEED, n_draws: int = N_DRAWS, cpi_path: Path = market_data.CPI_CSV,
          fuel_path: Path = market_data.FUEL_CSV, ual_path: Path = UAL_CSV,
          literature_path: Path = LITERATURE_JSON, evaluate_test: bool = True,
          generated_at: str | None = None, dal_path: Path = DAL_CSV, dal_check_path: Path = DAL_CHECK_JSON,
          demand_path: Path = DEMAND_JSON) -> dict:
    """The fare clock output. Deterministic for fixed inputs, seed and generated_at.

    Runs the industry part alone, with the United fields null and the reason
    stated, when the United quarterly file is absent. The Delta look, the Delta
    September-quarter note and the demand-control variant are each skipped when
    their file is absent.
    """
    if n_draws < 500:
        raise ValueError("The recovery curve needs at least 500 joint draws")
    cpi_path, fuel_path, ual_path, literature_path, dal_path, dal_check_path, demand_path = map(
        Path, (cpi_path, fuel_path, ual_path, literature_path, dal_path, dal_check_path, demand_path))
    panel = industry_panel(cpi_path, fuel_path)
    fuel_dates, fuel_values = market_data.load_fuel_daily(fuel_path)
    literature = load_literature(literature_path)
    table = load_ual_quarterly(ual_path) if ual_path.is_file() else None
    fuel_share = fuel_share_of_revenue(table) if table is not None else None
    industry = industry_block(panel, evaluate_test, fuel_share, demand_control(demand_path, panel["months"]))
    delta_q3 = delta_september_quarter(dal_check_path)
    statements = management_statements(literature)
    onset = shock_onset(fuel_dates, fuel_values)
    paths = management_paths(statements, onset)
    promised = sorted((s for s in statements if s["path"] == "july_2026" and s["kind"] == "expectation"),
                      key=lambda s: s["period"])
    united = _united_block(table, statements, paths, seed, n_draws, evaluate_test) if table is not None else None
    published, published_missing = published_estimates(literature)
    ind_fit, row12 = industry["fit"], industry["lag_rows"][12]
    asym = industry["asymmetry"]
    asym_scale = fuel_share["mean_of_quarterly_ratios"] if fuel_share else 1.0

    limits = [
        "Co-movement, not a causal pass-through: fuel prices and travel demand rise and fall together with the "
        "economy, so part of any measured response is demand, not airlines passing on cost. Nothing here "
        "identifies the causal share.",
        "The industry measure is the consumer price index for airline fares: fares paid by consumers, not airline "
        "revenue per seat mile. It leaves out most ancillary revenue, and the seasonally adjusted series is the "
        "current vintage, revised annually, not the figures first published.",
        "The industry result is an elasticity of fares to fuel, not a recovery share. Converting it needs fuel "
        "cost as a share of revenue, and the share used is United's, not the industry's.",
        f"October 2025 has no CPI print in the source ({', '.join(panel['missing_cpi_months']) or 'none missing'}); "
        "the two monthly changes that touch a missing month are left out, not interpolated.",
        "The pandemic breaks every airline series. Monthly: March 2020 to December 2021; quarterly: 2020Q1 to "
        "2021Q4 (the brief says roughly 2020Q2; the collapse began in March 2020, inside 2020Q1). The primary "
        "estimates use the fit split, which ends in 2019. With year-over-year changes and four lags, every "
        "quarter from 2020Q1 to 2023Q4 has a pandemic-quarter level somewhere behind it, so the select split "
        "cannot discriminate between specifications for the quarterly curve.",
        "Timing inside a quarter is not measured. The curve is estimated on year-over-year changes in quarterly "
        "averages, so quarter 0 is the response to a change in the quarter's average fuel cost, however that change "
        "was spread over the quarter in the fitting years. It does not say what happens when fuel jumps late in a "
        "quarter, after most of that quarter's seats are sold: the jump moves the quarter's average only a little, "
        "the curve applies its quarter-0 share to that small change, and the rest of the jump arrives as next "
        "quarter's shock. It cannot separate fuel that has been high for a while from fuel that moved after "
        "guidance was set, and the simulator's monthly reading of the curve (one step per quarter) is not a "
        "within-quarter path."
        + (f" Delta's September 2026 quarter is the live example (data/lab/dal_check.json): fuel came in at "
           f"${delta_q3['fuel_actual_adj_usd_gal']:.2f} a gallon against ${delta_q3['fuel_assumed_usd_gal']:.2f} "
           f"assumed in July (+${delta_q3['fuel_actual_minus_assumed']:.2f}), adjusted unit revenue was up "
           f"{delta_q3['adj_trasm_yoy_pct']:g}% year over year, and adjusted earnings per share were "
           f"${delta_q3['eps_actual_adj']:.2f} against a guided ${delta_q3['eps_guided_low']:.2f} to "
           f"${delta_q3['eps_guided_high']:.2f}." if delta_q3 else ""),
    ]
    variants_tried = [f"{v['id']}: {v['description']}" for v in industry["variants"]]
    inputs = [{"path": _shown(p), "sha256": _sha(p)} for p in (cpi_path, fuel_path)
              + ((literature_path,) if literature else ()) + ((demand_path,) if demand_path.is_file() else ())
              + ((dal_check_path,) if delta_q3 else ())]
    as_of = {"fuel_daily_last_print_in_monthly_panel": panel["last_fuel_print"],
             "cpi_last_month": panel["last_cpi_month"]}
    payload = {
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": panel["last_fuel_print"], "as_of_detail": as_of, "inputs": inputs, "seed": seed,
        "interval_level": LEVEL,
        "headline": industry_headline(row12, ind_fit.fit.n), "plain_summary": None,
        "recovery_definition": None, "primary_spec": None,
        "curve": None, "curve_draws": None, "curve_draws_method": None,
        "rivals": {"costs_only": [0] * HORIZON, "full_immediate": [1] * HORIZON,
                   "management": [{"period": s["period"], "low": s["low"], "high": s["high"], "path": s["path"],
                                   "source_id": s["source_id"], "url": s["url"], "quote": s["quote"]}
                                  for s in promised],
                   "management_note": "The contract's field: the July 2026 promise by calendar quarter. These are "
                                      "statements about one specific shock in named quarters, not a generic curve "
                                      "by quarters after a shock. Both stated paths (April and July 2026), with "
                                      "sources and a mapping onto quarters after the shock, are in management_paths.",
                   "management_paths": {
                       "paths": paths, "shock_onset": onset,
                       "mapping": "quarters_after 0 is the first quarter that sits almost wholly (at least 90% of "
                                  "its daily prints) after the onset of the fuel increase on the daily jet fuel "
                                  "series; later calendar quarters follow in order, and quarters after the last "
                                  "stated one hold its range.",
                       "assumption": "The onset quarter itself carries only part of the increase and is treated as "
                                     "before the shock; a share stated or reported for a calendar quarter is taken "
                                     "as the share that many quarters after a shock of any size and sign."},
                   "management_rule_for_scoring": {
                       "curve": promise_rule(statements),
                       "note": "The July promise read as a generic rule, as the project brief specifies: the "
                               "midpoint of the first promised quarter's range in the quarter of the shock, the "
                               "next promised value from the following quarter."},
                   "published": published,
                   "published_note": "Verified entries of data/lab/literature.json only. value is a share of the "
                                     "cost change (a fraction) only where the source itself states one; otherwise "
                                     "value is null and the number is in value_as_published with its unit. "
                                     "Elasticities and cents-per-dollar figures are not converted to recovery "
                                     "shares: the sources do not give the fuel share of fares the conversion needs. "
                                     "The UBS figures are secondary reports of a bank note; Koopmans and Lieshout "
                                     "give bounds in words (below half, above half), not point estimates.",
                   "published_not_found": published_missing},
        "united_points": None,
        "united_status": {"computed": False, "reason": f"{_shown(ual_path)} is absent: only the industry part ran"},
        "industry_monthly": industry["lag_rows"],
        "industry_monthly_definition": "coef is the cumulative elasticity of real airfares to jet fuel through that "
                                       "lag (the sum of the lag coefficients 0..lag), lo and hi its 90% interval; "
                                       "lag_coef is the single lag's coefficient. " + STANDARD_ERRORS,
        "industry": {"primary_id": industry["primary_id"], **ind_fit.describe(),
                     "sample": "2013-01 to 2019-12",
                     "elasticity_at_months": {str(k): industry["lag_rows"][k] for k in (3, 6, 12)},
                     "lags_0_18_variant_at_12_months": industry["lags18_at_12"],
                     "by_split": industry["by_split"], "recovery_share_conversion": industry["conversion"],
                     "variants": industry["variants"], "fuel_months_short_of_prints": panel["short_fuel_months"],
                     "min_prints_for_a_monthly_average": MIN_FUEL_PRINTS,
                     "missing_cpi_months": panel["missing_cpi_months"]},
        "asymmetry": {
            "up": asym["up"] / asym_scale, "down": asym["down"] / asym_scale, "p_value": asym["p_value"],
            "unit": ("share of a fuel cost change made up through industry fares within 12 months: the fare "
                     "elasticity divided by United's fuel expense over revenue, 2013-2019" if fuel_share else
                     "12-month elasticity of real airfares to jet fuel (no sourced fuel share to convert it)"),
            "fuel_share_used": fuel_share and fuel_share["mean_of_quarterly_ratios"],
            "up_lo": asym["up_lo"] / asym_scale, "up_hi": asym["up_hi"] / asym_scale,
            "down_lo": asym["down_lo"] / asym_scale, "down_hi": asym["down_hi"] / asym_scale,
            "horizon_months": asym["horizon_months"], "up_elasticity": asym["up"], "down_elasticity": asym["down"],
            "difference_elasticity": asym["difference"], "difference_se_elasticity": asym["difference_se"],
            "definition": "Cumulative 12-month response of real industry airfares to fuel increases (up) and to "
                          "fuel decreases (down), from separate lag polynomials on the fit split; p_value tests "
                          "that the two are equal (cautious covariance, Student t). Equal values mean a symmetric "
                          "response. Monthly industry fares, not United's unit revenue."},
        "by_split": {name: {**industry_flat(industry["by_split"][name]), "industry": industry["by_split"][name]}
                     for name in SPLITS},
        "by_split_note": "Top-level numbers per split are average misses. With United's curve: cents per ASM for "
                         "the year-over-year change in TRASM, four quarters ahead unless the name says one quarter "
                         "ahead. Without it: percentage points of the monthly change in real industry fares. The "
                         "fitting period is in sample.",
        "n": None, "n_industry_months": ind_fit.fit.n,
        "variants_tried": variants_tried, "limits": limits,
        "oos_test": None, "management_comparison": None, "delta_look": None,
        "delta_september_quarter": delta_q3,
        "test_split": ("computed once, for the specifications fixed beforehand" if evaluate_test
                       else "withheld in this build"),
    }
    summary = [industry_headline(row12, ind_fit.fit.n),
               f"Fuel increases and decreases: the 12-month elasticities are {asym['up']:.3f} and {asym['down']:.3f}; "
               f"the test that they are equal gives p = {asym['p_value']:.2f}."]
    if united is None:
        payload["recovery_definition"] = (
            "Not yet measured for United: the quarterly file is absent. The industry figures are elasticities of "
            "real consumer airfares to the jet fuel price, not recovery shares.")
        payload["primary_spec"] = {"united": None, "industry": INDUSTRY_SPECS[0]["text"]}
        payload["plain_summary"] = _sentences(summary + ["United's recovery curve has not been computed."])
        return _clean(payload)

    fit, rows, quarters = united["fit"], united["curve_rows"], united["panel"]["q"]
    first_q, last_q = quarter_label(int(quarters[fit.rows[0]])), quarter_label(int(quarters[fit.rows[-1]]))
    n = fit.fit.n
    first_promise, k_promise = promised[0], 0
    if onset is not None:  # quarters between the first full quarter of the shock and the first promised quarter
        index = lambda label: int(label[:4]) * 4 + int(label[-1]) - 1
        k_promise = min(max(index(first_promise["period"]) - index(onset["first_full_quarter"]), 0), HORIZON - 1)
    headline = united_headline(rows, n)
    against_promise = promise_line(rows[k_promise]["estimate"], rows[k_promise]["lo"], rows[k_promise]["hi"],
                                   first_promise["low"], first_promise["high"], n, k_promise, first_promise["period"])
    oos = _oos_summary(united["scores"], evaluate_test)
    history_headline = headline
    locked = oos["test_h4"]
    if locked["winner"] is not None:
        stated = united["rules"]["management"]
        words = {"costs_only": "a no-response rule", "full_immediate": "a full-immediate-recovery rule",
                 "management": f"a fixed rule of {_pct(stated[0])} recovered in the same quarter and "
                               f"{_pct(stated[1])} afterwards"}
        errors = locked["mae_cents_per_asm"]
        if locked["winner"] == "fitted":
            headline += (f" On the {locked['n']} locked test quarters this curve had the lowest average miss: "
                         f"{errors['fitted']:.2f} cents per seat mile against {errors[locked['best_rival']]:.2f} for "
                         f"{words[locked['best_rival']]}"
                         + ("." if locked["distinguishable"] else ", a gap too small to tell from chance."))
        else:
            headline += (f" On the {locked['n']} locked test quarters it lost to {words[locked['winner']]}: an average "
                         f"miss of {errors['fitted']:.2f} cents per seat mile against {errors[locked['winner']]:.2f}"
                         + ("." if locked["distinguishable"] else ", a gap too small to tell from chance."))
    payload.update({
        "headline": headline,
        "headline_history_only": history_headline,
        "promise_comparison": {
            "line": against_promise, "quarters_after": k_promise, "period": first_promise["period"],
            "promise_low": first_promise["low"], "promise_high": first_promise["high"],
            "verdict": verdict(rows[k_promise]["lo"], rows[k_promise]["hi"], first_promise["low"],
                               first_promise["high"]),
            "note": "One comparison line, not the headline. The promise is for a named calendar quarter of one "
                    "shock; quarters_after comes from the mapping in rivals.management_paths."},
        "industry_headline": industry_headline(row12, ind_fit.fit.n),
        "n": n,
        "recovery_definition": (
            "Recovery share k quarters after a fuel cost shock = the change in United's total revenue per available "
            "seat mile (cents) in that quarter per one-cent lasting change in fuel cost per available seat mile, at "
            "given capacity growth. It is the running sum through lag k of the coefficients from regressing the "
            "year-over-year change in TRASM on the year-over-year changes in fuel cost per ASM (average price per "
            "gallon times gallons, over ASM) at lags 0 to 4, with year-over-year capacity growth as a control. It "
            "is a share in that quarter, not a running total of dollars, and after quarter 4 it is held. A share of "
            "1 means unit revenue is up by as much as unit fuel cost. How this relates to United's measure: United "
            "says it 'recovered' a share of the year-over-year increase in fuel expense but discloses no formula. "
            "The raw ratio of its revenue change to its fuel expense change does not reproduce its figure, so its "
            "measure must be (revenue change minus a baseline revenue change that would have happened without the "
            "fuel move) divided by the fuel expense change, with a baseline it does not disclose. Ours has the same "
            "form with a stated baseline: the fitted constant plus the fitted effect of capacity growth, estimated "
            "on 2013-2019. The two baselines need not agree, so our share and United's are not the same number."),
        "primary_spec": {
            "id": UNITED_SPECS[0]["id"], "description": UNITED_SPECS[0]["text"],
            "equation": "d4(TRASM)[t] = a + sum_{j=0..4} b[j] * d4(fuel cost per ASM)[t-j] + c * capacity growth[t] "
                        "+ e[t]; recovery share at k = b[0] + ... + b[k]",
            "units": "cents per available seat mile; capacity growth in percent year over year",
            "sample": f"{first_q} to {last_q}", **fit.describe(),
            "intercept_cents": fit.fit.beta[0], "capacity_coef_cents_per_point": fit.fit.beta[1 + united["n_lags"]],
            "standard_errors": STANDARD_ERRORS + f" Newey-West lag for the quarterly regressions: {fit.hac_lag} "
                               "(year-over-year changes overlap by three quarters).",
            "pandemic_handling": "Primary: estimated on the fit split only, which ends in 2019Q4, so no pandemic "
                                 "quarter enters as an observation or behind a change. Variants: 2013-2023 "
                                 "excluding pandemic-touched quarters, and 2013-2023 with quarter indicators.",
            "why_primary": "It is the specification named in the project brief and was fixed before any select or "
                           "test number was computed. Fit-only estimation keeps both later splits out of the "
                           "coefficients. The capacity control matches the simulator, which moves capacity "
                           "separately. No variant was promoted on select-split scores, because every select "
                           "quarter is pandemic-touched.",
            "industry": INDUSTRY_SPECS[0]["text"]},
        "curve": rows,
        "curve_definition": "p10/p50/p90 (and p05/p95) are quantiles of curve_draws; estimate is the regression's "
                            "cumulative share, se its standard error, lo/hi its 90% Student t interval; "
                            "se_newey_west and se_classical are shown for comparison; lag_coef is the single "
                            "quarter's coefficient.",
        "curve_draws": np.round(united["draws"], 4),
        "curve_draws_method": {
            "n_draws": int(len(united["draws"])), "seed": seed,
            "method": "Joint draws of the five lag coefficients from a multivariate Student t centred on the "
                      "estimates, with the cautious covariance and the regression's residual degrees of freedom; "
                      "each row of curve_draws is the running sum of one coefficient draw. Not clipped and not "
                      "smoothed: a draw may be negative, above 1 or non-monotone.",
            "why": f"Covariance route rather than a block bootstrap: with {n} quarters and {fit.fit.k} coefficients "
                   f"a block bootstrap resamples only {-(-n // 4)} blocks of four quarters and leans on a handful "
                   "of them; the covariance route uses every quarter once and keeps the correlation between "
                   "quarters. Student t rather than normal because the covariance is itself estimated from few "
                   "quarters. " + STANDARD_ERRORS + " The block bootstrap is reported beside it as a check.",
            "df": fit.fit.df_resid, "block_bootstrap_check": united["bootstrap"]},
        "united_points": united["points"],
        "united_points_definition": "fuel_shock and revenue_response are year-over-year changes in fuel cost per ASM "
                                    "and in TRASM, both in cents per ASM.",
        "united_status": {"computed": True, "reason": None},
        "united": {"data_checks": united["checks"], "variants": united["variants"], "selection_note": None,
                   "refit_all_clean_quarters_through_latest": united["refit"]},
        "oos_test": {
            "definition": "Predicted year-over-year change in TRASM (cents) under each response rule, given the "
                          "fuel cost per ASM and capacity that actually occurred; this tests the response, not a "
                          "fuel forecast. h4: four quarters ahead of the year-ago level (baseline plus the rule's "
                          "fuel term). h1: one quarter ahead of the last reported quarter (last quarter's actual "
                          "year-over-year change plus the change in baseline and fuel term). Each rule gets its own "
                          "constant and capacity coefficient, estimated on the fit split with the fuel response "
                          "held at the rule, so the rules differ only in the fuel response. A test quarter with a "
                          "2020Q1-2021Q4 level anywhere behind its prediction is left out and listed.",
            "rules": united["rules"],
            "rule_notes": {
                "costs_only": "no revenue response", "full_immediate": "full recovery in the quarter of the shock",
                "management": "the July 2026 promise read as a generic rule, as the project brief specifies: the "
                              "midpoint of the first promised quarter's range in the quarter of the shock, the next "
                              "promised value afterwards",
                "fitted": "the primary curve (point estimates), estimated on the fit split",
                "management_*_mapped": "the stated calendar-quarter path mapped onto quarters after the shock; see "
                                       "rivals.management_paths"},
            "scores": united["scores"], "summary": oos,
            "fuel_term_only_variant": {"note": "The same comparison with no baseline: prediction = the rule's fuel "
                                               "term alone.", "scores": united["fuel_only"],
                                       "summary": _oos_summary(united["fuel_only"], evaluate_test)},
            "interval_note": "fitted_minus_rival_abs_error is the mean of |fitted error| - |rival error| over the "
                             "same quarters (negative favours the fitted curve) with a 90% moving-block bootstrap "
                             "interval; with this few quarters the interval undercovers and is descriptive only."},
        "management_comparison": None,
    })
    payload["as_of_detail"].update(ual_last_quarter=quarter_end(int(quarters[-1])),
                                   curve_estimated_through=quarter_end(int(quarters[fit.rows[-1]])))
    payload["as_of"] = max(panel["last_fuel_print"], quarter_end(int(quarters[-1])))
    payload["inputs"] += [{"path": _shown(ual_path), "sha256": _sha(ual_path)}]
    payload["delta_look"] = None
    if evaluate_test and dal_path.is_file():
        delta = delta_look(dal_path, united["rules"], seed + 5)
        delta["united_curve"] = [{"quarters_after": r["quarters_after"], "estimate": r["estimate"], "lo": r["lo"],
                                  "hi": r["hi"]} for r in rows]
        payload["delta_look"] = delta
        payload["inputs"] += [{"path": _shown(dal_path), "sha256": _sha(dal_path)}]
        payload["as_of_detail"]["delta_last_quarter"] = quarter_end(
            int(delta["last_quarter"][:4]) * 4 + int(delta["last_quarter"][-1]) - 1)
        payload["as_of"] = max(payload["as_of"], payload["as_of_detail"]["delta_last_quarter"])
        payload["variants_tried"].append(
            "delta_look_primary_spec_on_delta: United's primary specification on Delta's adjusted figures, estimated "
            "in sample on Delta's test-period quarters (it has no fit-split quarters), plus United's rules scored "
            "on Delta. A second look, not used for the curve.")
    payload["variants_tried"] += [f"{v['id']}: {v['description']}" for v in united["variants"]] + [
        "united_oos_fuel_term_only: the out-of-sample comparison with no baseline (prediction = fuel term alone).",
        "united_curve_block_bootstrap: moving-block bootstrap of the estimation rows as a check on the "
        "covariance-based draws."]

    # by_split: the four core rules on each split, both horizons.
    for name in SPLITS:
        entry = {}
        for horizon in ("h1", "h4"):
            scored = united["scores"][horizon][name]
            entry[horizon] = scored if "status" in scored else {
                "n": scored["n"], "in_sample": scored["in_sample"], "mae_cents_per_asm": scored["mae_cents_per_asm"],
                "lowest_mae_core_rule": scored.get("lowest_mae_core_rule")}
        if name == "select":
            entry["excluding_pandemic_touched_n"] = {h: united["scores"][h]["select_excluding_pandemic"]["n"]
                                                     for h in ("h1", "h4")}
        flat, labels = {}, {"fitted": "fitted_curve", "costs_only": "no_response", "full_immediate": "full_immediate",
                            "management": "management_path"}
        for horizon, prefix in (("h4", "average_miss_"), ("h1", "one_quarter_ahead_miss_")):
            scored = united["scores"][horizon][name]
            if "status" in scored:
                flat["status"] = scored["status"]
                continue
            flat["n_quarters" if horizon == "h4" else "n_quarters_one_quarter_ahead"] = scored["n"]
            flat.update({prefix + labels[r]: scored["mae_cents_per_asm"][r] for r in labels})
        payload["by_split"][name] = {**flat, "united": entry, "industry": payload["by_split"][name]["industry"]}

    # Did any fit-split variant beat the primary on both the fit-split cross-validation and the select split?
    primary_variant = united["variants"][0]
    better = [v["id"] for v in united["variants"][1:]
              if v.get("fit_leave_one_year_out_mae") is not None
              and "not comparable" not in v["fit_leave_one_year_out_target"]
              and v["fit_leave_one_year_out_mae"] < primary_variant["fit_leave_one_year_out_mae"]
              and v["select_mae_h4"] is not None and v["select_mae_h4"] < primary_variant["select_mae_h4"]]
    payload["united"]["selection_note"] = (
        "The primary was fixed in advance. Fit-split leave-one-year-out error and select-split error are reported "
        "for every variant. " + (f"Variants with lower error than the primary on both: {', '.join(better)}. They "
                                 "were not promoted: the select split is pandemic-touched throughout."
                                 if better else "No variant has lower error than the primary on both."))

    shock_range = united["checks"]["fit_split_fuel_shock_range_cents"]
    estimates = [r["estimate"] for r in rows]
    latest_shock = united["panel"]["d_fuel"][-1]
    payload["limits"] += [
        f"Small sample: the primary curve rests on {n} quarters ({first_q} to {last_q}) and {fit.fit.k} "
        "coefficients. The ranges use the larger of the classical and Newey-West errors and Student t, and are "
        "still more likely too narrow than too wide: they assume the specification is right."
        + ("" if all(a <= b for a, b in zip(estimates, estimates[1:])) else
           " The quarter-by-quarter shape is not smooth (" + ", ".join(_pct(e) for e in estimates) + " for quarters "
           "0 to 4): the fuel changes at neighbouring lags are highly correlated, so single quarters trade off "
           "against each other. The draws carry that trade-off; the share after a year is the best-determined "
           "number and the path to it is not."),
        "One fuel cycle: the fit split is dominated by the 2014-2016 fall in fuel. Year-over-year fuel cost changes "
        f"in it run from {shock_range[0]:.2f} to {shock_range[1]:.2f} cents per ASM"
        + (f"; the latest quarter's change, {latest_shock:+.2f}, is "
           f"{'inside' if shock_range[0] <= latest_shock <= shock_range[1] else 'outside'} that range"
           if evaluate_test else "")
        + ". The curve assumes a linear response that is the same for increases and decreases and for any size of "
          "shock; the monthly industry test of asymmetry is the only check on that.",
        "Hedging: United hedged fuel in the early 2010s and later stopped. Fuel cost here is the reported average "
        "price per gallon times gallons, and in the early sample that price includes hedge gains and losses where "
        "the release does not separate them, so the early 'shock' is partly a hedging result, not the market price.",
        "The capacity control removes any recovery that works through capacity cuts made because of fuel. "
        "Management's own account ties recovery to capacity. The no-control variant shows the difference.",
        "United does not define 'recover'. Our share uses a fitted baseline (constant plus capacity effect, "
        "2013-2019); United's baseline is undisclosed. Comparing the two compares definitions.",
        "Figures are as first printed in each quarter's earnings release. Year-over-year changes can straddle "
        "restatements and accounting changes; the 2018 change moved ancillary fees into passenger revenue, which is "
        "why the PRASM variant carries a 2018 indicator. TRASM is little affected.",
        "The lag polynomial stops at four quarters; anything recovered later is not measured and the share is held "
        "at its four-quarter value.",
        "Management's promise is stated for calendar quarters of one shock. Mapping it onto 'quarters after a "
        f"shock' takes {onset['first_full_quarter'] if onset else 'the first promised quarter'} as the first full "
        "quarter on the daily fuel series and is an assumption.",
    ]
    summary = [history_headline, against_promise,
               f"Fitted on {n} quarters ({first_q} to {last_q}), the share made up is {_pct(rows[0]['p50'])} in the "
               f"quarter of the shock (P10 {_pct(rows[0]['p10'])}, P90 {_pct(rows[0]['p90'])}), "
               f"{_pct(rows[1]['p50'])} one quarter later (P10 {_pct(rows[1]['p10'])}, P90 {_pct(rows[1]['p90'])}) "
               f"and {_pct(rows[4]['p50'])} after a year (P10 {_pct(rows[4]['p10'])}, P90 {_pct(rows[4]['p90'])})."]
    names = {"costs_only": "no response", "full_immediate": "full immediate recovery",
             "management": "management's path", "fitted": "the pre-specified five-lag curve"}
    for horizon, words in (("h4", "four quarters ahead"), ("h1", "one quarter ahead")):
        result = oos[f"test_{horizon}"]
        if result["winner"] is None:
            continue
        diff, errors = result["fitted_minus_best_rival"], result["mae_cents_per_asm"]
        spread = (f", {int(100 * LEVEL)}% range {diff['lo']:+.2f} to {diff['hi']:+.2f}"
                  f"{'' if result['distinguishable'] else ', which includes zero'}" if diff["n"] >= 2 else "")
        summary.append(
            f"Locked test, {words} (n = {result['n']} quarters): mean absolute error in cents per ASM is "
            + ", ".join(f"{errors[r]:.2f} for {names[r]}" for r in CORE_RULES)
            + f"; lowest is {names[result['winner']]}. The pre-specified curve minus {names[result['best_rival']]}: "
              f"{diff['mean_diff']:+.2f}{spread}.")
    summary.append(f"The select split (2020-2023) has {oos['select_excluding_pandemic_h4']['n']} quarters free of "
                   "pandemic-period levels, so its scores are reported but decide nothing.")
    if united["accounts"]:
        q2 = united["accounts"][0]
        implied = q2["implied_by_statement"]
        model = q2["fitted_model"]
        text = (f"For {q2['period']} the raw ratio of United's revenue change to its fuel expense change is "
                f"{q2['raw']['revenue_change_over_fuel_expense_change']:.2f}; the fitted model assigns "
                f"{_pct(model['share_of_fuel_cost_per_asm_change'])} of the fuel cost change per ASM to recovery")
        if implied:
            text += (f"; United said it recovered about {_pct(implied['stated_share'])}, which implies an undisclosed "
                     f"baseline revenue growth of {100 * implied['implied_baseline_revenue_growth']:.1f}%")
        summary.append(text + ". This compares definitions; it does not validate either.")
        payload["management_comparison"] = {
            "label": "A comparison of definitions, not a validation. United states a recovered share without a "
                     "formula; data/lab/literature.json records that no definition was found in the release, the "
                     "investor update, the call or the 10-Q excerpts.",
            "united_measure_as_read": "(revenue change - a baseline revenue change that would have happened without "
                                      "the fuel move) / fuel expense change, year over year; baseline undisclosed.",
            "statement": united["stated_q2"],
            "quarters": united["accounts"]}
    refit = united["refit"]
    if refit:
        then, now = refit["curve"][0], refit["curve"][-1]
        moved = (f"Not stable across periods. Re-estimated on all {refit['n']} quarters through "
                 f"{refit['quarters'][-1]} with no pandemic level behind them (test years included; not used for the "
                 f"curve), the same-quarter share is {_pct(then['estimate'])} ({_pct(then['lo'])} to "
                 f"{_pct(then['hi'])}) against {_pct(rows[0]['estimate'])} on the fit split, and the one-year share "
                 f"{_pct(now['estimate'])} ({_pct(now['lo'])} to {_pct(now['hi'])}) against "
                 f"{_pct(rows[-1]['estimate'])}.")
        payload["limits"].append(moved + " A curve fitted on 2015-2019 may understate how fast United now reprices.")
        summary.append(moved)
    delta = payload["delta_look"]
    if delta and delta["curve"]:
        first, last = delta["curve"][0], delta["curve"][-1]
        on_delta = delta["united_rules_on_delta"]["h4"]
        summary.append(
            f"Second look, Delta (in sample on {delta['n']} quarters, {len(delta['quarters_in_test_split'])} of them "
            f"in the test years, {delta['k']} coefficients): "
            f"{_pct(first['estimate'])} in the same quarter ({_pct(first['lo'])} to {_pct(first['hi'])}) and "
            f"{_pct(last['estimate'])} a year later ({_pct(last['lo'])} to {_pct(last['hi'])}). United's rules on "
            f"Delta's quarters, four quarters ahead, fuel term only (n = {on_delta['n']}): "
            + ", ".join(f"{on_delta['mae_cents_per_asm'][r]:.2f} for {names[r]}" for r in CORE_RULES)
            + f"; lowest is {names[on_delta['lowest_mae_core_rule']]}.")
    summary += [industry_headline(row12, ind_fit.fit.n), summary_asym(asym)]
    _publish_smooth(payload, united, summary, evaluate_test, seed, first_promise, k_promise)
    return _clean(payload)


def _sentences(parts: list[str]) -> list[str]:
    """One sentence per item: the parts are split where a full stop is followed by a capital letter."""
    return [piece.strip() for part in parts for piece in re.split(r"(?<=[.])\s+(?=[A-Z])", part) if piece.strip()]


def smooth_headline(fit: dict) -> str:
    """The number that holds up (the share made up after a year, as the simulator's draws have it) with its range and
    the single best fit beside it; neutral when the range reaches zero. The speed is said to be open, not quoted."""
    year = fit["one_year_share"]
    spread = f"{int(100 * LEVEL)}% range {_pct(year['p05'])} to {_pct(year['p95'])}"
    if year["p05"] <= 0.0:
        return (f"United's history cannot tell a revenue response to fuel cost from none: the share made up after a "
                f"year is {_pct(year['p50'])} ({spread}).")
    return (f"A year after a fuel cost change, United has made up about {_pct(year['p50'])} of it through revenue "
            f"({spread}; best single fit {_pct(year['estimate'])}); the speed is not pinned down.")


def _smooth_public(fit: dict | None) -> dict | None:
    return None if fit is None else {k: v for k, v in fit.items() if k != "draws"}


def _publish_smooth(payload: dict, united: dict, summary: list[str], evaluate_test: bool, seed: int,
                    first_promise: dict, k_promise: int) -> None:
    """Make the smooth curve the simulator-facing one and keep the pre-specified curve, untouched, beside it."""
    smooth = united["smooth"]
    lead_name = "through_latest" if smooth["through_latest"] is not None else "fit_only"
    lead, alone = smooth[lead_name], smooth["fit_only"]
    rows = lead["curve"]
    payload["curve_unrestricted"] = payload["curve"]
    payload["curve_unrestricted_draws"] = payload["curve_draws"]
    payload["curve_unrestricted_draws_method"] = payload["curve_draws_method"]
    payload["curve_unrestricted_note"] = (
        "The pre-specified curve: five free lag coefficients on the fit split. It was fixed before the test years "
        "were scored and it lost that test; its scores in oos_test are unchanged. It is kept for the record and is "
        "no longer what the simulator reads.")
    payload["curve"], payload["curve_draws"] = rows, np.round(lead["draws"], 4)
    payload["curve_definition"] = (
        f"The smooth partial-adjustment curve, {lead_name.replace('_', ' ')}: p10/p50/p90 (and p05/p95) are "
        "percentiles of curve_draws, estimate is the least-squares curve L x (1 - rho^(k+1)).")
    payload["curve_draws_method"] = {
        "n_draws": int(len(lead["draws"])), "seed": seed, "method": SMOOTH_METHOD,
        "why": "A bootstrap rather than a covariance: the curve is non-linear in rho, and resampling quarters "
               "carries the trade-off between L and rho into the draws. Every draw is monotone by construction.",
        "which": lead_name}
    payload["n_unrestricted"], payload["n"] = payload["n"], lead["n"]
    payload["headline_prespecified"] = payload["headline"]
    payload["headline"] = smooth_headline(lead)
    payload["promise_comparison_prespecified"] = payload["promise_comparison"]
    at = rows[k_promise]
    payload["promise_comparison"] = {
        "line": promise_line(at["estimate"], at["p05"], at["p95"], first_promise["low"], first_promise["high"],
                             lead["n"], k_promise, first_promise["period"]),
        "quarters_after": k_promise, "period": first_promise["period"], "promise_low": first_promise["low"],
        "promise_high": first_promise["high"],
        "verdict": verdict(at["p05"], at["p95"], first_promise["low"], first_promise["high"]),
        "note": "One comparison line, not the headline, from the smooth curve the simulator reads. The promise is "
                "for a named calendar quarter of one shock; quarters_after comes from rivals.management_paths."}
    payload["primary_spec"] = {
        "id": f"united_smooth_partial_adjustment_{lead_name}",
        "description": "Two-parameter partial-adjustment recovery curve on the same data and definition as the "
                       "five-lag regression, with the capacity control; the curve the simulator and the forward "
                       "test read.",
        "equation": "d4(TRASM)[t] = a + L * sum_{j=0..4} (1 - rho) rho^j * d4(fuel cost per ASM)[t-j] + c * capacity "
                    "growth[t] + e[t]; recovery share at k = L * (1 - rho^(k+1))",
        "sample": f"{lead['first_quarter']} to {lead['last_quarter']}, no quarter with a 2020Q1-2021Q4 level behind it",
        "n": lead["n"], "L": lead["L"], "rho": lead["rho"], "half_life_quarters": lead["half_life_quarters"],
        "chosen_after_test_split_seen": True, "post_hoc_note": SMOOTH_POST_HOC,
        "why_through_latest": "A forward-looking simulation may use everything known today. Replays of past "
                              "quarters must use smooth.expanding, which never sees its own future.",
        "prespecified": payload["primary_spec"]}
    payload["recovery_definition"] = (
        "The published curve is recovery(k) = L x (1 - rho^(k+1)): L is the share of a lasting fuel cost change that "
        "unit revenue makes up in the long run, rho how much of the remaining gap stays open each quarter. It is "
        "fitted to the same quantities as before. " + payload["recovery_definition"].replace(
            "It is the running sum through lag k of the coefficients from regressing",
            "In the pre-specified version (curve_unrestricted) it is the running sum through lag k of the "
            "coefficients from regressing"))

    def relabel(entry: dict) -> dict:
        if "status" in entry:
            return entry
        swap = lambda d: {("smooth_fit_only" if k == "fitted" else k): v for k, v in d.items()}
        out = {k: v for k, v in entry.items() if k not in ("fitted_minus_rival_abs_error", "lowest_mae_core_rule",
                                                           "margin_to_next_core_rule", "mae_cents_per_asm")}
        out["mae_cents_per_asm"] = swap(entry["mae_cents_per_asm"])
        if entry["n"]:
            best = entry["lowest_mae_core_rule"]
            out["lowest_mae_among_smooth_and_the_three_rules"] = "smooth_fit_only" if best == "fitted" else best
            out["smooth_minus_rival_abs_error"] = entry["fitted_minus_rival_abs_error"]
        return out

    scored = united["smooth_scores"]
    payload["oos_test"]["smooth_fit_only"] = {
        "note": SMOOTH_POST_HOC + " Scored exactly as the other rules: fitted on the fit split only, its own "
                "constant and capacity coefficient from the fit split.",
        "curve": alone["curve_estimate"], "descriptive_not_a_clean_test": True,
        "scores": {h: {split: relabel(scored[h][split]) for split in ("fit", "select", "test")} for h in ("h1", "h4")}}
    payload["oos_test"]["rule_notes"]["smooth_fit_only"] = (
        "the two-parameter partial-adjustment curve fitted on the fit split; chosen after the test split had been "
        "seen, so descriptive only")
    payload["smooth"] = {
        "fit_only": {**_smooth_public(alone), "label": "Fit split alone; comparable with the pre-specified curve."},
        "through_latest": smooth["through_latest"] and {
            **_smooth_public(smooth["through_latest"]),
            "label": "Every quarter through the latest with no pandemic level behind it; what a forward-looking "
                     "simulation should use. It includes the test years."},
        "through_latest_status": None if smooth["through_latest"] is not None else "withheld: it uses the test years",
        "expanding": smooth["expanding"],
        "expanding_note": "One entry per quarter: the curve fitted on quarters dated before as_of_period with no "
                          "pandemic level behind them (all published by the time guidance for as_of_period is "
                          "given). curve_p50 is the least-squares curve; no draws are stored per entry. Use these "
                          "for any replay of a past quarter.",
        "half_life_quarters": {name: smooth[name] and {"estimate": smooth[name]["half_life_quarters"],
                                                       **smooth[name]["half_life_range"]}
                               for name in ("fit_only", "through_latest")},
        "method": SMOOTH_METHOD, "post_hoc_note": SMOOTH_POST_HOC, "published_as_curve": lead_name}
    for split in SPLITS:
        entry = scored["h4"][split]
        if "status" not in entry and entry["n"]:
            payload["by_split"][split]["average_miss_smooth_curve_chosen_after_test"] = entry["mae_cents_per_asm"]["fitted"]
    payload["variants_tried"] += [
        "united_smooth_partial_adjustment_fit_only: recovery(k) = L(1 - rho^(k+1)) on the fit split. Chosen after the "
        "test split had been seen.",
        "united_smooth_partial_adjustment_through_latest: the same on every non-pandemic quarter to the latest; the "
        "published curve. Chosen after the test split had been seen.",
        "united_smooth_partial_adjustment_expanding: the same re-fitted before each quarter from 2019Q1, for replays."]

    year, ridge = lead["one_year_share"], lead["ridge"]
    payload["limits"] += [
        SMOOTH_POST_HOC,
        "The smooth curve rises steadily by construction. It cannot show an overshoot or a give-back, and it is "
        "cut at quarter 4, where the share is L x (1 - rho^5), below L when adjustment is slow."]
    if ridge["poorly_separated"] or alone["ridge"]["poorly_separated"]:
        payload["limits"].append(
            "L and rho are poorly separated: a high long-run share reached slowly fits about as well as a lower one "
            f"reached fast. In the bootstrap for the published curve their correlation is "
            f"{ridge['correlation_L_rho']:.2f}, {_pct(ridge['share_of_resamples_at_rho_upper_bound'])} of resamples "
            f"sit at the upper bound of rho, and the range of L is "
            f"{ridge['L_range_width_over_one_year_share_width']:.1f} times as wide as the range of the one-year "
            f"share. The well-determined number is the share after a year: {_pct(year['estimate'])} "
            f"({int(100 * LEVEL)}% range {_pct(year['p05'])} to {_pct(year['p95'])}). Read L and the half-life "
            "together, not one at a time.")

    fragile = None
    if lead_name == "through_latest" and len(smooth["expanding"]) >= 2:
        before, after = smooth["expanding"][-2], smooth["expanding"][-1]
        fragile = (
            f"The speed of the published curve rests on one quarter. Fitted through {before['fitted_through']} the "
            f"half-life is {before['half_life_quarters']:.1f} quarters (rho {before['rho']:.2f}, L "
            f"{_pct(before['L'])}); adding {after['fitted_through']} alone makes it "
            f"{after['half_life_quarters']:.1f} (rho {after['rho']:.2f}, L {_pct(after['L'])}). Bootstrap resamples "
            f"that leave that quarter out give slow curves, so the middle of the draws ({_pct(rows[0]['p50'])} in "
            f"the shock quarter, {_pct(rows[-1]['p50'])} after a year) is slower than the least-squares curve "
            f"({_pct(rows[0]['estimate'])} and {_pct(rows[-1]['estimate'])}). The simulator samples the draws.")
        payload["limits"].append(fragile)
        payload["smooth"]["through_latest"]["rests_on_latest_quarter"] = {
            "fitted_before_it": before, "fitted_with_it": after, "note": fragile}

    describe = lambda f: (f"L = {_pct(f['L'])} ({_pct(f['L_range']['p05'])} to {_pct(f['L_range']['p95'])}), rho = "
                          f"{f['rho']:.2f} ({f['rho_range']['p05']:.2f} to {f['rho_range']['p95']:.2f}), half-life "
                          f"{f['half_life_quarters']:.1f} quarters ({f['half_life_range']['p05']:.1f} to "
                          f"{f['half_life_range']['p95']:.1f}), one-year share {_pct(f['one_year_share']['estimate'])} "
                          f"({_pct(f['one_year_share']['p05'])} to {_pct(f['one_year_share']['p95'])})")
    lead_text = [payload["headline"],
                 f"The curve the simulator reads ({lead_name.replace('_', ' ')}, {lead['n']} quarters to "
                 f"{lead['last_quarter']}, {int(100 * LEVEL)}% ranges): {describe(lead)}.",
                 "By quarter after the shock its middle estimate is "
                 + ", ".join(f"{_pct(r['p50'])} ({_pct(r['p10'])} to {_pct(r['p90'])})" for r in rows)
                 + ", with P10 to P90 in brackets."]
    if lead_name != "fit_only":
        lead_text.append(f"On the fit split alone ({alone['n']} quarters): {describe(alone)}.")
    test = scored["h4"]["test"]
    if "status" not in test and test["n"]:
        errors = test["mae_cents_per_asm"]
        lead_text.append(
            f"Rival comparison on the {test['n']} locked test quarters, four quarters ahead, average miss in cents "
            f"per ASM: {errors['fitted']:.2f} for the smooth curve fitted on the fit split, "
            f"{errors['costs_only']:.2f} for no response, {errors['full_immediate']:.2f} for full immediate recovery, "
            f"{errors['management']:.2f} for management's path and {errors['prespecified_five_lag']:.2f} for the "
            "pre-specified five-lag curve.")
    lead_text += ([fragile] if fragile else []) + [SMOOTH_POST_HOC, payload["promise_comparison"]["line"],
                                                    "The pre-specified result stands and follows."]
    payload["plain_summary"] = _sentences(lead_text + summary)


def industry_flat(scores: dict) -> dict:
    """Flat per-split numbers for the industry regression, in percentage points of the monthly fare change."""
    if "status" in scores:
        return {"status": scores["status"]}
    every = scores["all"]
    out = {"n_months": every["n"]}
    if "mae_fitted" in every:
        out.update(average_miss_fitted_pct=100.0 * every["mae_fitted"],
                   average_miss_no_response_pct=100.0 * every["mae_no_response"])
    return out


def summary_asym(asym: dict) -> str:
    return (f"Fuel increases and decreases: the 12-month fare elasticities are {asym['up']:.3f} and "
            f"{asym['down']:.3f}; the test that they are equal gives p = {asym['p_value']:.2f}.")


REQUIRED = ("generated_at", "as_of", "inputs", "headline", "plain_summary", "recovery_definition", "primary_spec",
            "curve", "curve_draws", "rivals", "united_points", "industry_monthly", "asymmetry", "by_split", "n",
            "variants_tried", "limits", "oos_test", "management_comparison")


def validate(payload: dict) -> None:
    """Raise ValueError unless the payload has the contract's shape. United fields may be null only together."""
    def need(condition, message):
        if not condition:
            raise ValueError(f"fare_clock.json: {message}")

    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    need(isinstance(payload, dict) and not payload.get("fixture"), "must be an object and never a fixture")
    need(not [k for k in REQUIRED if k not in payload], f"missing keys {[k for k in REQUIRED if k not in payload]}")
    json.dumps(payload, allow_nan=False)
    for key in ("generated_at", "as_of", "headline", "recovery_definition"):
        need(isinstance(payload[key], str) and payload[key], f"{key} must be a non-empty string")
    need(isinstance(payload["plain_summary"], list) and payload["plain_summary"]
         and all(isinstance(v, str) and v for v in payload["plain_summary"]),
         "plain_summary must be a non-empty list of sentences")
    need(payload["inputs"] and all(re.fullmatch(r"[0-9a-f]{64}", i.get("sha256", "")) and i.get("path")
                                   for i in payload["inputs"]), "inputs need a path and a SHA-256 each")
    rows = payload["industry_monthly"]
    need(isinstance(rows, list) and [r.get("lag_months") for r in rows] == list(range(len(rows))) and len(rows) >= 13,
         "industry_monthly must list lags 0..12 in order")
    need(all(number(r["coef"]) and number(r["lo"]) and number(r["hi"]) and r["lo"] <= r["coef"] <= r["hi"]
             for r in rows), "industry_monthly needs lo <= coef <= hi")
    need(all(number(payload["asymmetry"].get(k)) for k in ("up", "down", "p_value"))
         and 0.0 <= payload["asymmetry"]["p_value"] <= 1.0, "asymmetry needs up, down and a p_value in [0, 1]")
    need(set(payload["by_split"]) == set(SPLITS), "by_split needs fit, select and test")
    for key in ("variants_tried", "limits"):
        need(isinstance(payload[key], list) and payload[key] and all(isinstance(v, str) and v for v in payload[key]),
             f"{key} must be a non-empty list of strings")
    rivals = payload["rivals"]
    need(rivals.get("costs_only") == [0] * HORIZON and isinstance(rivals.get("management"), list)
         and isinstance(rivals.get("published"), list), "rivals needs costs_only, management and published")
    need(all(number(m.get("low")) and number(m.get("high")) and m["low"] <= m["high"] and m.get("period")
             for m in rivals["management"]), "each management rival needs a period and low <= high")
    need(all(p.get("label") and "value" in p and "url" in p for p in rivals["published"]),
         "each published rival needs a label, value and url")
    united = ("curve", "curve_draws", "united_points", "oos_test", "primary_spec")
    if payload["curve"] is None:
        need(all(payload[k] is None or k == "primary_spec" for k in united) and payload["n"] is None
             and payload["management_comparison"] is None,
             "without a curve the United fields must all be null")
        need(not payload.get("united_status", {}).get("computed") and payload["united_status"].get("reason"),
             "a missing curve needs united_status.reason")
        return
    curve, draws = payload["curve"], payload["curve_draws"]
    need([r.get("quarters_after") for r in curve] == list(range(HORIZON)), "curve must list quarters_after 0..4")
    need(all(number(r[k]) for r in curve for k in ("p10", "p50", "p90")) and all(r["p10"] <= r["p50"] <= r["p90"]
                                                                               for r in curve),
         "curve needs finite p10 <= p50 <= p90")
    need(isinstance(draws, list) and len(draws) >= 500 and all(len(d) == HORIZON and all(number(v) for v in d)
                                                              for d in draws),
         "curve_draws needs at least 500 rows of five finite shares")
    need(isinstance(payload["n"], int) and payload["n"] > 0, "n must be the number of quarters behind the curve")
    need(isinstance(payload["united_points"], list) and all(
        p.get("split") in SPLITS and number(p["fuel_shock"]) and number(p["revenue_response"]) and p.get("period_end")
        for p in payload["united_points"]), "united_points need period_end, fuel_shock, revenue_response and split")
    need(isinstance(payload["oos_test"], dict) and isinstance(payload["primary_spec"], dict), "oos_test and primary_spec")
    steps = [[b - a for a, b in zip(d, d[1:])] for d in draws]
    need(all(min(step) >= -1e-9 or max(step) <= 1e-9 for step in steps),
         "every published draw must be monotone across quarters")
    smooth = payload.get("smooth")
    need(isinstance(smooth, dict) and isinstance(smooth.get("fit_only"), dict) and isinstance(smooth.get("expanding"), list)
         and "method" in smooth and "half_life_quarters" in smooth, "smooth needs fit_only, expanding, method, half-life")
    need(all({"as_of_period", "L", "rho", "curve_p50", "n"} <= set(e) and len(e["curve_p50"]) == HORIZON
             for e in smooth["expanding"]), "each expanding entry needs as_of_period, L, rho, curve_p50 and n")
    need(isinstance(payload.get("curve_unrestricted"), list) and isinstance(payload.get("curve_unrestricted_draws"), list),
         "the pre-specified curve and its draws must stay in the file")
