"""Cost clock: how fast and how fully spot jet fuel reaches an airline's reported price per gallon.

Two specifications, both a regression of the reported quarterly average price
per gallon on averages of the daily Gulf Coast spot price:

  same_quarter   reported = a + b * mean(spot over the quarter)
  lagged_window  reported = a + b0 * mean(spot over the quarter)
                              + b1 * mean(spot over the same window moved back 30 days)

The second lets the last weeks of the previous quarter matter (inventory and
contract lags). Its slope is b0 + b1 and its mean lag in days is 30 * b1 / (b0 + b1).

Splits: fit 2013-2019, select 2020-2023, locked test 2024-2026. The
specification is chosen on fit and select (estimated on fit, scored on select);
the estimation window is chosen by walking forward through select; the chosen
model is then estimated through 2023 and frozen. Pandemic quarters (2020Q1 to
2021Q4) are left out of estimation and of the headline scores.

predicted_price_paid turns a daily spot path into a predictive range for a
quarter's price paid. It uses no print after its as-of date and refuses a model
estimated on reports published on or after that date. Statistics come from
app.lab.stats.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
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
LITERATURE_JSON = LAB / "literature.json"
DAL_CHECK_JSON = LAB / "dal_check.json"
OUTPUT = ROOT / "app" / "static" / "data" / "cost_clock.json"

SPLITS = {"fit": (2013, 2019), "select": (2020, 2023), "test": (2024, 2026)}
PANDEMIC = ("2020Q1", "2021Q4")    # the same window the fare clock leaves out
LEVEL = 0.90                       # two-sided level of every interval; ranges are P10 to P90
SEED = 20260715
N_DRAWS = 4000
LAG_WINDOW_DAYS = 30
VOL_WINDOW = 250                   # daily log changes behind the random-walk volatility
MIN_PRINTS = 40                    # a quarter-long window with fewer prints has no average
TARGET = "2026Q3"
# Specification -> how far back (days) each quarter-long averaging window is moved; "prev" is the previous quarter.
SPECS = {"same_quarter": (0,), "lagged_window": (0, LAG_WINDOW_DAYS), "previous_quarter": (0, "prev"),
         **{f"shift_{days}": (days,) for days in (7, 14, 21, 30, 45)}}
CANDIDATES = ("same_quarter", "lagged_window")
WINDOW_STARTS = (2013, 2018)
MATCH_TOLERANCE = 0.05             # USD per gallon: how close two price moves must be to be called a match
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


# ------------------------------------------------------------------ quarters and windows

def quarter_index(quarter: str) -> int:
    return int(quarter[:4]) * 4 + int(quarter[5]) - 1


def quarter_label(index: int) -> str:
    return f"{index // 4}Q{index % 4 + 1}"


def quarter_bounds(quarter: str) -> tuple[date, date]:
    year, q = int(quarter[:4]), int(quarter[5])
    following = date(year + (q == 4), 3 * q % 12 + 1, 1)
    return date(year, 3 * q - 2, 1), following - timedelta(days=1)


def quarter_of(day: date) -> str:
    return f"{day.year}Q{(day.month + 2) // 3}"


def split_of(quarter: str) -> str | None:
    year = int(quarter[:4])
    return next((name for name, (first, last) in SPLITS.items() if first <= year <= last), None)


def in_pandemic(quarter: str) -> bool:
    return quarter_index(PANDEMIC[0]) <= quarter_index(quarter) <= quarter_index(PANDEMIC[1])


def windows(spec: str, quarter: str) -> list[tuple[date, date]]:
    """The averaging windows of a specification for one quarter, in coefficient order."""
    first, last = quarter_bounds(quarter)
    out = []
    for shift in SPECS[spec]:
        if shift == "prev":
            out.append(quarter_bounds(quarter_label(quarter_index(quarter) - 1)))
        else:
            out.append((first - timedelta(days=shift), last - timedelta(days=shift)))
    return out


def spec_lags(spec: str) -> np.ndarray:
    return np.array([91.0 if shift == "prev" else float(shift) for shift in SPECS[spec]])


def _day(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _ordinals(dates) -> np.ndarray:
    return np.array([d.toordinal() for d in dates], dtype=int)


def window_mean(ords: np.ndarray, values: np.ndarray, start: date, end: date) -> tuple[float, int]:
    """Mean of the prints dated start..end and their count; (nan, 0) for an empty window."""
    pick = (ords >= start.toordinal()) & (ords <= end.toordinal())
    count = int(pick.sum())
    return (float(values[pick].mean()), count) if count else (math.nan, 0)


def design(spec: str, quarters, ords: np.ndarray, values: np.ndarray) -> np.ndarray:
    """One row per quarter, one column per window; NaN where a window has fewer than MIN_PRINTS prints."""
    out = np.full((len(quarters), len(SPECS[spec])), np.nan)
    for i, quarter in enumerate(quarters):
        for j, (start, end) in enumerate(windows(spec, quarter)):
            mean, count = window_mean(ords, values, start, end)
            out[i, j] = mean if count >= MIN_PRINTS else np.nan
    return out


# ------------------------------------------------------------------ inputs

def united_rows(path: Path = UAL_CSV) -> list[dict]:
    """United's quarters with a reported price: quarter, filed_on, price, and the columns the checks need."""
    return [{**row, "price": row["fuel_price_usd_gal"]} for row in filings.load_ual_quarterly(path)
            if row["fuel_price_usd_gal"] is not None]


def delta_rows(path: Path = DAL_CSV, field: str = "adj_fuel_price_usd_gal") -> list[dict]:
    """Delta's quarters with the adjusted price (the one its guidance is stated on)."""
    out = []
    with open(path, newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get(field):
                end = date.fromisoformat(row["period_end"])
                out.append({"quarter": quarter_of(end), "period_end": end, "filed_on": date.fromisoformat(row["filed_on"]),
                            "price": float(row[field]),
                            "fuel_gallons_m": float(row["fuel_gallons_m"]) if row.get("fuel_gallons_m") else None})
    return out


def literature_number(literature: dict, entry_id: str, label_start: str) -> dict | None:
    """One sourced number from literature.json, found by entry id and the start of its label."""
    for entry in literature.get("entries", []):
        if entry.get("id") == entry_id:
            for number in entry.get("numbers", []):
                if number.get("label", "").startswith(label_start):
                    return {"value": number["value"], "unit": number.get("unit"), "quote": number.get("quote"),
                            "source_id": entry_id, "url": entry.get("url"), "label": number["label"]}
    return None


def quoted_date(text: str) -> date | None:
    """The first 'Month D, YYYY' in a quote."""
    match = re.search(r"\b(" + "|".join(MONTHS) + r") (\d{1,2}), (\d{4})\b", text or "")
    return date(int(match.group(3)), MONTHS.index(match.group(1)) + 1, int(match.group(2))) if match else None


# ------------------------------------------------------------------ the regression

@dataclass(frozen=True, eq=False)
class CostModel:
    """reported price = beta[0] + beta[1:] . window means, with what a predictive draw needs."""
    airline: str
    spec: str
    beta: np.ndarray
    cov: np.ndarray            # classical, scaled up so no coefficient's standard error is below its Newey-West one
    sigma: float               # residual standard deviation, USD per gallon
    df: int
    n: int
    r2: float
    quarters: tuple
    last_filed_on: date        # latest publication date among the reports used
    se_classical: np.ndarray
    se_newey_west: np.ndarray

    @property
    def basis(self) -> float:
        return float(self.beta[0])

    @property
    def slope(self) -> float:
        return float(self.beta[1:].sum())

    def lag_days(self, beta=None) -> float:
        b = self.beta[1:] if beta is None else np.asarray(beta)[..., 1:]
        return b @ spec_lags(self.spec) / b.sum(axis=-1)

    def predict(self, x) -> np.ndarray:
        return self.beta[0] + np.asarray(x, dtype=float) @ self.beta[1:]

    def summary(self, level: float = LEVEL, seed: int = SEED) -> dict:
        weights = np.r_[0.0, np.ones(len(self.beta) - 1)]
        slope = stats.linear_combination(self.beta, self.cov, weights)
        lag_lo, lag_hi = 0.0, 0.0
        if len(self.beta) > 2:
            draws, _ = coefficient_draws(self, 4000, np.random.default_rng(seed))
            lag_lo, lag_hi = stats.percentile_interval(self.lag_days(draws), level)
        return {"airline": self.airline, "spec": self.spec, "equation": equation(self.spec),
                "lag_days": float(self.lag_days()), "lag_days_ci": [lag_lo, lag_hi],
                "basis_usd_gal": self.basis,
                "basis_ci": list(stats.conf_int(self.beta[0], math.sqrt(self.cov[0, 0]), level, df=self.df)),
                "slope": slope["estimate"], "ci": list(stats.conf_int(slope["estimate"], slope["se"], level, df=self.df)),
                "coefficients": self.beta[1:], "r2": self.r2, "n": self.n, "resid_sd_usd_gal": self.sigma,
                "df_resid": self.df, "sample": f"{self.quarters[0]} to {self.quarters[-1]}",
                "se_classical": self.se_classical, "se_newey_west": self.se_newey_west,
                "estimated_on_reports_published_through": self.last_filed_on.isoformat()}


def equation(spec: str) -> str:
    names = {0: "mean spot over the quarter", "prev": "mean spot over the previous quarter"}
    terms = [names.get(shift, f"mean spot over the quarter's window moved back {shift} days") for shift in SPECS[spec]]
    return "reported price per gallon = a + " + " + ".join(f"b{i} x {term}" for i, term in enumerate(terms))


def fit_cost_model(rows: list[dict], ords: np.ndarray, values: np.ndarray, spec: str, airline: str = "UAL") -> CostModel:
    """Least squares of the reported price on the specification's window means, over the rows given."""
    x = design(spec, [row["quarter"] for row in rows], ords, values)
    keep = np.isfinite(x).all(axis=1)
    used = [row for row, ok in zip(rows, keep) if ok]
    fit = stats.ols(x[keep], np.array([row["price"] for row in used], dtype=float))
    if fit.rank_deficient or fit.df_resid < 1:
        raise ValueError(f"Cost regression is not identified: n={fit.n}, k={fit.k}")
    robust = stats.newey_west(fit, None, small_sample=True)
    scale = max(1.0, float(np.max((robust.se / fit.se) ** 2)))
    return CostModel(airline, spec, fit.beta, fit.cov * scale, math.sqrt(fit.sigma2), fit.df_resid, fit.n, fit.r2,
                     tuple(row["quarter"] for row in used), max(row["filed_on"] for row in used), fit.se, robust.se)


def coefficient_draws(model: CostModel, n_draws: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Joint Student t draws of the coefficients and of one residual each (the normal linear model's predictive)."""
    values, vectors = np.linalg.eigh((model.cov + model.cov.T) / 2.0)
    root = vectors * np.sqrt(np.clip(values, 0.0, None))
    scale = np.sqrt(model.df / rng.chisquare(model.df, size=n_draws))
    beta = model.beta + (rng.standard_normal((n_draws, len(model.beta))) @ root.T) * scale[:, None]
    return beta, model.sigma * scale * rng.standard_normal(n_draws)


def errors(model: CostModel, rows: list[dict], ords: np.ndarray, values: np.ndarray) -> list[dict]:
    """Predicted minus reported for each row whose windows are complete."""
    x = design(model.spec, [row["quarter"] for row in rows], ords, values)
    return [{"quarter": row["quarter"], "reported": row["price"], "predicted": float(model.predict(xi)),
             "error": float(model.predict(xi) - row["price"])}
            for row, xi in zip(rows, x) if np.isfinite(xi).all()]


def mae(items: list[dict]) -> float | None:
    return float(np.mean([abs(item["error"]) for item in items])) if items else None


def walk_forward(rows: list[dict], ords, values, spec: str, start_year: int, keep_pandemic: bool = False) -> dict:
    """One quarter ahead through the select years: each quarter predicted from the earlier quarters since start_year."""
    pool = [row for row in rows if int(row["quarter"][:4]) >= start_year and (keep_pandemic or not in_pandemic(row["quarter"]))]
    out = []
    for i, row in enumerate(pool):
        if split_of(row["quarter"]) == "select" and i >= len(SPECS[spec]) + 4:
            out += errors(fit_cost_model(pool[:i], ords, values, spec), [row], ords, values)
    return {"start_year": start_year, "mae": mae(out), "n": len(out), "pandemic_quarters_kept": keep_pandemic}


def choose(rows: list[dict], ords, values) -> dict:
    """Specification and estimation window, from the fit and select years only."""
    through_select = [row for row in rows if split_of(row["quarter"]) in ("fit", "select")]
    fit_rows = [row for row in through_select if split_of(row["quarter"]) == "fit"]
    select_rows = [row for row in through_select if split_of(row["quarter"]) == "select"]
    clean_select = [row for row in select_rows if not in_pandemic(row["quarter"])]
    table, models = {}, {}
    for spec in SPECS:
        model = models[spec] = fit_cost_model(fit_rows, ords, values, spec)
        table[spec] = {"basis_usd_gal": model.basis, "slope": model.slope, "lag_days": float(model.lag_days()),
                       "r2": model.r2, "resid_sd_usd_gal": model.sigma, "n_fit": model.n,
                       "fit_mae": mae(errors(model, fit_rows, ords, values)),
                       "select_mae": mae(errors(model, clean_select, ords, values)), "select_n": len(clean_select),
                       "select_mae_all_quarters": mae(errors(model, select_rows, ords, values)),
                       "select_n_all_quarters": len(select_rows)}
    spec = min(CANDIDATES, key=lambda name: table[name]["select_mae"])
    walks = [walk_forward(through_select, ords, values, spec, start) for start in WINDOW_STARTS]
    kept = [walk_forward(through_select, ords, values, spec, start, keep_pandemic=True) for start in WINDOW_STARTS]
    start = min(walks, key=lambda walk: walk["mae"])["start_year"]
    return {"spec": spec, "start_year": start, "by_spec": table, "fit_split_models": models, "walk_forward": walks,
            "walk_forward_pandemic_kept": kept, "through_select": through_select,
            "rule": "Specification: the candidate with the lower mean absolute error on the select years when "
                    "estimated on the fit years. Estimation window: the start year with the lower one-quarter-ahead "
                    "error walking forward through the select years. Pandemic quarters are left out of both."}


def final_rows(rows: list[dict], start_year: int, last_year: int = SPLITS["select"][1]) -> list[dict]:
    return [row for row in rows if start_year <= int(row["quarter"][:4]) <= last_year and not in_pandemic(row["quarter"])]


@lru_cache(maxsize=4)
def _default_model(ual: str, fuel: str, stamp: tuple) -> CostModel:
    dates, values = market_data.load_fuel_daily(Path(fuel))
    rows, ords = united_rows(Path(ual)), _ordinals(dates)
    picked = choose(rows, ords, values)
    return fit_cost_model(final_rows(rows, picked["start_year"]), ords, values, picked["spec"])


def default_model(ual_path: Path = UAL_CSV, fuel_path: Path = market_data.FUEL_CSV) -> CostModel:
    """United's chosen model, estimated through 2023 from the committed files."""
    stamp = (Path(ual_path).stat().st_mtime_ns, Path(fuel_path).stat().st_mtime_ns)
    return _default_model(str(ual_path), str(fuel_path), stamp)


def model_as_of(as_of, ual_path: Path = UAL_CSV, fuel_path: Path = market_data.FUEL_CSV) -> CostModel:
    """United's chosen specification and window, re-estimated on every clean quarter reported before `as_of`.

    Unlike default_model it learns from the 2024-2026 reports: use it for a forward estimate, never to
    score those years. Each quarter's windows end before its report was published, so nothing after
    `as_of` enters.
    """
    dates, values = market_data.load_fuel_daily(Path(fuel_path))
    rows, ords = united_rows(Path(ual_path)), _ordinals(dates)
    picked = choose(rows, ords, values)
    used = [row for row in rows if int(row["quarter"][:4]) >= picked["start_year"] and not in_pandemic(row["quarter"])
            and row["filed_on"] < _day(as_of)]
    return fit_cost_model(used, ords, values, picked["spec"])


# ------------------------------------------------------------------ the predictive range

def predicted_price_paid(quarter: str, dates, values, as_of=None, model: CostModel | None = None,
                         n_draws: int = N_DRAWS, seed: int = SEED, vol_window: int = VOL_WINDOW) -> dict:
    """Predictive range for an airline's average price per gallon in `quarter`, from daily spot to date.

    `dates`, `values` is the daily spot path, oldest first; prints after `as_of` (default: the last
    print) are ignored. Weekdays after the last print used, up to the quarter's end, follow a random
    walk: each daily log change is normal with the standard deviation of the last `vol_window` log
    changes and a drift that keeps the expected price at the last print. Each draw then applies one
    joint Student t draw of the model's coefficients and one residual. `point` is the model at the
    expected path (remaining days held at the last print), with no draw. The model must have been
    estimated on reports published before the as-of date.
    """
    dates = [_day(d) for d in dates]
    values = np.asarray(values, dtype=float)
    as_of = dates[-1] if as_of is None else _day(as_of)
    keep = np.array([d <= as_of for d in dates], dtype=bool)
    dates, values = [d for d, ok in zip(dates, keep) if ok], values[keep]
    if not dates:
        raise ValueError("No spot prints on or before the as-of date")
    model = default_model() if model is None else model
    if model.last_filed_on >= as_of:
        raise ValueError(f"The model uses a report published on {model.last_filed_on}, not before the as-of date {as_of}")
    ords, last, end = _ordinals(dates), dates[-1], quarter_bounds(quarter)[1]
    future = [d for d in (last + timedelta(days=i) for i in range(1, (end - last).days + 1)) if d.weekday() < 5]
    rng = np.random.default_rng(seed)
    changes = np.diff(np.log(values[-(vol_window + 1):]))
    vol = float(changes.std(ddof=1)) if len(changes) >= 20 else math.nan
    paths = np.empty((n_draws, 0))
    if future:
        if not math.isfinite(vol):
            raise ValueError("Too few prints before the as-of date to measure daily volatility")
        steps = rng.standard_normal((n_draws, len(future))) * vol - 0.5 * vol * vol
        paths = values[-1] * np.exp(np.cumsum(steps, axis=1))
    ahead_ords = _ordinals(future)
    means, expected = np.empty((n_draws, len(SPECS[model.spec]))), []
    for j, (start, stop) in enumerate(windows(model.spec, quarter)):
        seen = (ords >= start.toordinal()) & (ords <= stop.toordinal())
        ahead = (ahead_ords >= start.toordinal()) & (ahead_ords <= stop.toordinal())
        count = int(seen.sum() + ahead.sum())
        if count < MIN_PRINTS:
            raise ValueError(f"Window {start} to {stop} has {count} prints or simulated days; at least {MIN_PRINTS} needed")
        means[:, j] = (values[seen].sum() + paths[:, ahead].sum(axis=1)) / count
        expected.append((values[seen].sum() + values[-1] * ahead.sum()) / count)
    beta, noise = coefficient_draws(model, n_draws, rng)
    price = beta[:, 0] + (means * beta[:, 1:]).sum(axis=1) + noise
    p10, p50, p90 = (float(v) for v in stats.quantiles(price))
    first = quarter_bounds(quarter)[0]
    return {"quarter": quarter, "airline": model.airline, "as_of": as_of.isoformat(), "last_print": last.isoformat(),
            "p10": p10, "p50": p50, "p90": p90, "mean": float(price.mean()), "point": float(model.predict(expected)),
            "prints_in_quarter": int(((ords >= first.toordinal()) & (ords <= end.toordinal())).sum()),
            "weekdays_remaining": len(future), "complete": not future,
            "expected_window_means_usd_gal": [float(v) for v in expected],
            "daily_volatility": vol if future else None, "n_draws": n_draws, "seed": seed, "spec": model.spec}


def _range(result: dict) -> dict:
    return {k: result[k] for k in ("as_of", "p10", "p50", "p90", "point", "weekdays_remaining", "complete")}


def position(value: float, p10: float, p90: float) -> str:
    """Where a stated number sits against a P10-P90 range."""
    return "below_range" if value < p10 else "above_range" if value > p90 else "inside_range"


# ------------------------------------------------------------------ management's arithmetic

def effective_tax_rate(rows: list[dict], quarters: int = 4) -> dict:
    """1 - net income / pre-tax income over the latest `quarters` reported quarters (GAAP, pooled)."""
    used = [row for row in rows if row.get("pretax_income_musd") is not None and row.get("net_income_musd") is not None][-quarters:]
    pretax, net = sum(row["pretax_income_musd"] for row in used), sum(row["net_income_musd"] for row in used)
    return {"rate": 1.0 - net / pretax, "quarters": [row["quarter"] for row in used], "pretax_income_musd": pretax,
            "net_income_musd": net, "definition": "1 - net income / income before income taxes, GAAP, summed over the quarters listed"}


def profit_sharing_rate(literature: dict, entry_id: str = "ual-2026q2-investor-update") -> dict | None:
    """Marginal profit-sharing share of pre-tax earnings in the top tier, read from the entry's notes."""
    notes = next((e.get("notes", "") for e in literature.get("entries", []) if e.get("id") == entry_id), "")
    patterns = {"base": r"about (\d+(?:\.\d+)?)% of adjusted pre-tax earnings", "top_tier": r"about (\d+(?:\.\d+)?)% above it",
                "above_prior_year": r"about (\d+(?:\.\d+)?)% of earnings above the prior year"}
    found = {name: re.search(pattern, notes) for name, pattern in patterns.items()}
    if not all(found.values()):
        return None
    tiers = {name: float(match.group(1)) / 100.0 for name, match in found.items()}
    return {"rate": sum(tiers.values()), "tiers": tiers, "source_id": entry_id,
            "caveat": "Read from the notes field of the literature entry, which paraphrases the investor update; "
                      "not a machine-verified quote. Assumes every tier applies at the margin."}


def management_check(literature: dict, rows: list[dict], dates, values, model: CostModel, seed: int = SEED) -> dict:
    """United's July 2026 statement that fuel added $575 million, or $1.12 a share, to the third quarter.

    Every input is read from the committed files. The implied price change is the claimed dollars over
    gallons; the per-share figure is tested with the effective tax rate of the latest four quarters and
    with United's stated profit-sharing tiers; the implied price change is set beside the move in the
    daily fuel series over the same days and beside what this module's own estimate of the quarter's
    price did between the two dates.
    """
    entry = "ual-2026q2-investor-update"
    claimed = literature_number(literature, entry, "Increase in third-quarter fuel cost since the beginning of July")
    per_share = literature_number(literature, entry, "The same increase expressed per share")
    assumed = literature_number(literature, entry, "Assumed all-in average fuel price, 2026Q3")
    curve_day = quoted_date(assumed["quote"]) if assumed else None
    if not (claimed and per_share and assumed and curve_day):
        return {"computed": False, "reason": "The July 2026 statement was not found in literature.json"}
    by_quarter = {row["quarter"]: row for row in rows}
    latest, year_ago = rows[-1], by_quarter.get(quarter_label(quarter_index(TARGET) - 4))
    gallons = {"prior_year_third_quarter": year_ago["fuel_gallons_m"], "latest_quarter": latest["fuel_gallons_m"]}
    shares, tax, sharing = latest["diluted_shares_m"], effective_tax_rate(rows), profit_sharing_rate(literature)
    month_start = date(curve_day.year, curve_day.month, 1)
    spot = [(d, float(v)) for d, v in zip(dates, values) if month_start <= d <= curve_day]
    start, end = spot[0], spot[-1]
    before = predicted_price_paid(TARGET, dates, values, as_of=start[0], model=model, seed=seed)
    after = predicted_price_paid(TARGET, dates, values, as_of=end[0], model=model, seed=seed)
    moves = {"spot_point_to_point": end[1] - start[1], "cost_clock_quarter_estimate": after["point"] - before["point"]}
    implied = {name: claimed["value"] / g for name, g in gallons.items()}
    after_tax_share = per_share["value"] * shares / claimed["value"]
    keep = (1.0 - tax["rate"]) * (1.0 - (sharing["rate"] if sharing else 0.0))
    cases = [{"gallons_assumption": name, "gallons_m": g, "price_move": move, "price_change_usd_gal": change,
              "reproduced_usd_m": g * change, "reproduced_eps_tax_only": g * change * (1.0 - tax["rate"]) / shares,
              "reproduced_eps": g * change * keep / shares}
             for name, g in gallons.items() for move, change in moves.items()]
    primary = next(c for c in cases if c["gallons_assumption"] == "prior_year_third_quarter"
                   and c["price_move"] == "cost_clock_quarter_estimate")
    implied_mid = float(np.mean(list(implied.values())))
    gap = {name: change - implied_mid for name, change in moves.items()}
    needed = 1.0 - after_tax_share / (1.0 - tax["rate"])
    eps_tax_only = claimed["value"] * (1.0 - tax["rate"]) / shares
    eps_full = claimed["value"] * keep / shares
    return {
        "computed": True, "claimed_usd_m": claimed["value"], "claimed_eps": per_share["value"],
        "reproduced_usd_m": primary["reproduced_usd_m"], "reproduced_eps": primary["reproduced_eps"],
        "inputs": {"gallons_m": primary["gallons_m"], "price_change_usd_gal": primary["price_change_usd_gal"],
                   "tax_rate": tax["rate"], "diluted_shares_m": shares,
                   "profit_sharing_rate": sharing["rate"] if sharing else None},
        "reproduced_definition": "gallons of the prior-year third quarter x the change in this module's expected "
                                 "third-quarter price paid between the first July print and the forward-curve date, "
                                 "then less profit sharing and tax, over the latest quarter's diluted shares",
        "claim": {"quote": claimed["quote"], "source_id": entry, "url": claimed["url"],
                  "assumed_price_usd_gal": assumed["value"], "assumed_price_quote": assumed["quote"],
                  "forward_curve_date": curve_day.isoformat()},
        "implied_price_change_usd_gal": implied,
        "per_share": {
            "diluted_shares_m": shares, "diluted_shares_quarter": latest["quarter"], "tax": tax,
            "after_tax_share_implied_by_claim": after_tax_share,
            "combined_rate_implied_by_claim": 1.0 - after_tax_share,
            "eps_with_tax_only": eps_tax_only, "gap_with_tax_only": eps_tax_only - per_share["value"],
            "profit_sharing_rate_needed_at_this_tax_rate": needed, "profit_sharing": sharing,
            "eps_with_tax_and_profit_sharing": eps_full if sharing else None,
            "gap_with_tax_and_profit_sharing": eps_full - per_share["value"] if sharing else None,
            "consistent": bool(sharing and abs(eps_full - per_share["value"]) <= 0.02)},
        "price_move": {
            "start": {"date": start[0].isoformat(), "spot_usd_gal": start[1], "what": "first print of the month"},
            "end": {"date": end[0].isoformat(), "spot_usd_gal": end[1], "what": "the forward-curve date United names"},
            "spot_change_usd_gal": moves["spot_point_to_point"], "spot_change_pct": 100.0 * (end[1] / start[1] - 1.0),
            "cost_clock_quarter_estimate": {"at_start": before["point"], "at_end": after["point"],
                                            "change_usd_gal": moves["cost_clock_quarter_estimate"]},
            "implied_by_claim_usd_gal": implied_mid,
            "implied_pct_of_assumed_price_before_move": 100.0 * implied_mid / (assumed["value"] - implied_mid),
            "implied_over_spot_change": implied_mid / moves["spot_point_to_point"],
            "gap_usd_gal": gap, "tolerance_usd_gal": MATCH_TOLERANCE,
            "matches": {name: bool(abs(value) <= MATCH_TOLERANCE) for name, value in gap.items()},
            "note": "United marks the quarter's average on the forward curve, which this project does not hold; "
                    "the spot series is a single day's price. A spot jump moves a quarter average by less when "
                    "the forward curve prices later months below spot."},
        "cases": cases}


# ------------------------------------------------------------------ wording generated from the numbers

def _usd(value: float) -> str:
    return f"${value:.2f}"


def _cents(value: float) -> str:
    return f"{100 * value:.0f} cents"


def headline(fit: dict, test: dict | None) -> str:
    """The cost relationship in one sentence; neutral when the slope's interval includes zero."""
    lo, hi = fit["ci"]
    sample = f"{fit['n']} quarters, {fit['sample']}"
    if lo <= 0.0 <= hi:
        return (f"No measurable link between spot jet fuel and the price per gallon {fit['airline']} reports: the "
                f"slope is {fit['slope']:.2f} with a {LEVEL:.0%} range of {lo:.2f} to {hi:.2f} ({sample}), which includes zero.")
    lag = (f" with a lag of about {fit['lag_days']:.0f} days" if fit["lag_days"] >= 1 else " in the same quarter")
    name = {"UAL": "United", "DAL": "Delta"}.get(fit["airline"], fit["airline"])
    out = (f"{name}'s price per gallon follows Gulf Coast spot jet fuel{lag}: price paid = {_usd(fit['basis_usd_gal'])} "
           f"+ {fit['slope']:.2f} x spot ({LEVEL:.0%} range for the slope {lo:.2f} to {hi:.2f}; {sample}), "
           f"off by {_cents(fit.get('in_sample_mae', fit['resid_sd_usd_gal']))} a gallon on average")
    if test and test.get("n"):
        out += f" in those years and by {_cents(test['mae'])} in the {test['n']} quarters of 2024-2026 it never saw"
    return out + "."


def era_sentence(eras: list[dict]) -> str:
    early, late = eras[0], eras[1]
    tighter = late["resid_sd_usd_gal"] < early["resid_sd_usd_gal"]
    return (f"Same fit on {early['sample']} against {late['sample']}: the lag went from {early['lag_days']:.0f} to "
            f"{late['lag_days']:.0f} days, the basis from {_usd(early['basis_usd_gal'])} to {_usd(late['basis_usd_gal'])}, "
            f"the slope from {early['slope']:.2f} to {late['slope']:.2f} and the typical miss from "
            f"{_cents(early['resid_sd_usd_gal'])} to {_cents(late['resid_sd_usd_gal'])}: the relationship "
            f"{'tightened' if tighter else 'did not tighten'} after the early, hedged years.")


WORDS = {"below_range": "below the range", "above_range": "above the range", "inside_range": "inside the range"}


def calibration_sentence(test: dict | None) -> str:
    """How the frozen model did on the test years, in the direction the numbers give."""
    if not test or not test["n"]:
        return ""
    low = sum(item["error"] < 0 for item in test["quarters"])
    side, count = ("above", low) if low * 2 >= test["n"] else ("below", test["n"] - low)
    inside = sum(item["inside_p10_p90"] for item in test["quarters"])
    verdict = "the range is too narrow" if test["inside_p10_p90"] < 0.6 else "the range held up"
    return (f"In 2024-2026 the reported price was {side} the prediction in {count} of {test['n']} quarters "
            f"(the prediction ran {_cents(abs(test['mean_error']))} {'low' if test['mean_error'] < 0 else 'high'} on average) "
            f"and inside the P10-P90 range in {inside} of {test['n']}: {verdict}.")


def prediction_sentence(block: dict) -> str:
    out = (f"For {block['quarter']} the predicted price paid is {_usd(block['p50'])} a gallon (P10 {_usd(block['p10'])}, "
           f"P90 {_usd(block['p90'])}), with the quarter's fuel prices {'complete' if block['complete'] else 'not yet complete'}")
    if block.get("management"):
        out += (f"; management assumed about {_usd(block['management']['value'])}, which is "
                f"{WORDS[block['management']['position']]}")
    out += "."
    if block.get("at_guidance_date") and block.get("management"):
        early = block["at_guidance_date"]
        out += (f" On {early['as_of']}, the date of the forward curve behind that figure, the same method gave "
                f"{_usd(early['p50'])} (P10 {_usd(early['p10'])}, P90 {_usd(early['p90'])}).")
    for item in block.get("sensitivity") or []:
        out += (f" {item['short']}: {_usd(item['p50'])} (P10 {_usd(item['p10'])}, P90 {_usd(item['p90'])})"
                + (f", management's figure {WORDS[item['management_position']]}." if item.get("management_position") else "."))
    return out


def delta_sentence(block: dict) -> str:
    if not block.get("actual"):
        return ""
    words = {"below_range": "below", "above_range": "above", "inside_range": "inside"}
    return (f"Delta check: with Delta's own basis the method gives {_usd(block['p50'])} (P10 {_usd(block['p10'])}, P90 "
            f"{_usd(block['p90'])}) for the September quarter; Delta reported {_usd(block['actual']['value'])}, "
            f"{words[block['actual']['position']]} the range, against the {_usd(block['assumed']['value'])} it assumed in July "
            f"(method off by {_cents(abs(block['method_error']))}, the assumption by {_cents(abs(block['assumption_error']))}).")


def check_sentence(check: dict) -> str:
    if not check.get("computed"):
        return ""
    share, move = check["per_share"], check["price_move"]
    low, high = sorted(check["cases"][i]["gallons_m"] for i in (0, len(check["cases"]) - 1))
    eps = (f"with the {share['tax']['rate']:.1%} tax rate and the stated profit-sharing tiers ({share['profit_sharing']['rate']:.1%}) "
           f"it comes to {_usd(share['eps_with_tax_and_profit_sharing'])}" if share["profit_sharing"] else
           "the profit-sharing tiers were not found")
    matched = move["matches"]["spot_point_to_point"]
    return (f"Management's ${check['claimed_usd_m']:.0f} million over {low:,.0f} to {high:,.0f} million gallons implies a price "
            f"change of about {_usd(move['implied_by_claim_usd_gal'])} a gallon; "
            f"tax alone at {share['tax']['rate']:.1%} gives {_usd(share['eps_with_tax_only'])} a share, not "
            f"{_usd(check['claimed_eps'])}, and {eps}. Spot moved {_usd(move['spot_change_usd_gal'])} between "
            f"{move['start']['date']} and {move['end']['date']}"
            + (", which matches." if matched else
               f", so the implied move is {move['implied_over_spot_change']:.0%} of the spot move: a gap, not a match "
               "(this module's own estimate of the quarter's price moved "
               f"{_usd(move['cost_clock_quarter_estimate']['change_usd_gal'])})."))


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


def scored(model: CostModel, rows: list[dict], dates, values, ords, seed: int) -> dict:
    """Out-of-sample errors of a frozen model with each quarter's P10-P90 range from its complete spot path."""
    items = []
    for item in errors(model, rows, ords, values):
        end = quarter_bounds(item["quarter"])[1]
        band = predicted_price_paid(item["quarter"], dates, values, as_of=end, model=model, seed=seed)
        items.append({**item, "p10": band["p10"], "p90": band["p90"],
                      "inside_p10_p90": band["p10"] <= item["reported"] <= band["p90"]})
    return {"mae": mae(items), "n": len(items), "in_sample": False,
            "mean_error": float(np.mean([i["error"] for i in items])) if items else None,
            "inside_p10_p90": float(np.mean([i["inside_p10_p90"] for i in items])) if items else None,
            "quarters": items}


def era_fit(rows, ords, values, spec: str, first: int, last: int, label: str) -> dict:
    picked = [r for r in rows if first <= int(r["quarter"][:4]) <= last and not in_pandemic(r["quarter"])]
    return {"label": label, **fit_cost_model(picked, ords, values, spec).summary()}


def build(seed: int = SEED, n_draws: int = N_DRAWS, fuel_path: Path = market_data.FUEL_CSV, ual_path: Path = UAL_CSV,
          dal_path: Path = DAL_CSV, literature_path: Path = LITERATURE_JSON, dal_check_path: Path = DAL_CHECK_JSON,
          evaluate_test: bool = True, generated_at: str | None = None) -> dict:
    """Everything cost_clock.json carries.

    `evaluate_test=False` withholds the model's errors on 2024-2026 and everything estimated from those
    years (the test scores, the refits and the Delta check). The reconciliation of management's
    arithmetic is accounting on reported figures, not a model score, and is always computed.
    """
    dates, values = market_data.load_fuel_daily(fuel_path)
    ords = _ordinals(dates)
    rows, full = united_rows(ual_path), filings.load_ual_quarterly(ual_path)
    literature = json.loads(Path(literature_path).read_text())
    picked = choose(rows, ords, values)
    spec, start = picked["spec"], picked["start_year"]
    model = fit_cost_model(final_rows(rows, start), ords, values, spec)
    fit = {**model.summary(seed=seed), "in_sample_mae": mae(errors(model, final_rows(rows, start), ords, values))}
    test_rows = [row for row in rows if split_of(row["quarter"]) == "test"]
    fit_model = picked["fit_split_models"][spec]
    by_split = {
        "fit": {"mae": picked["by_spec"][spec]["fit_mae"], "n": fit_model.n, "in_sample": True,
                "coefficients": "estimated on the fit years", "mae_same_quarter_spec": picked["by_spec"]["same_quarter"]["fit_mae"]},
        "select": {"mae": picked["by_spec"][spec]["select_mae"], "n": picked["by_spec"][spec]["select_n"], "in_sample": False,
                   "coefficients": "estimated on the fit years", "pandemic_quarters": "left out",
                   "mae_all_quarters": picked["by_spec"][spec]["select_mae_all_quarters"],
                   "n_all_quarters": picked["by_spec"][spec]["select_n_all_quarters"],
                   "mae_same_quarter_spec": picked["by_spec"]["same_quarter"]["select_mae"]},
        "test": None}
    eras = [era_fit(rows, ords, values, spec, 2013, 2016, "the early years, when United hedged"),
            era_fit(rows, ords, values, spec, 2018, SPLITS["select"][1], "2018 to the end of the select years")]
    reported_quarters = {row["quarter"] for row in rows}
    last_print = dates[-1]

    def forecast(as_of, use=model):
        return predicted_price_paid(TARGET, dates, values, as_of=as_of, model=use, n_draws=n_draws, seed=seed)

    target = forecast(last_print)
    assumed = literature_number(literature, "ual-2026q2-investor-update", "Assumed all-in average fuel price, 2026Q3")
    hedging = next((e for e in literature.get("entries", []) if e.get("id") == "ual-fy2025-10k"), None)
    curve_day = quoted_date(assumed["quote"]) if assumed else None
    first, end = quarter_bounds(TARGET)
    in_quarter = [d for d in dates if first <= d <= end]
    predicted = {
        **{k: target[k] for k in ("quarter", "as_of", "last_print", "p10", "p50", "p90", "point", "complete",
                                  "prints_in_quarter", "weekdays_remaining", "expected_window_means_usd_gal")},
        "already_reported": TARGET in reported_quarters, "n_draws": n_draws, "seed": seed,
        "model": "the frozen model in `fit` (estimated through 2023)",
        "management": None, "at_guidance_date": None, "calibration": None,
        "path": [_range(forecast(d)) for d in in_quarter],
        "path_definition": "The range as it stood on each print date of the quarter: prints to that date, a random "
                           "walk for the remaining weekdays.",
        "sensitivity": None}
    if assumed:
        gap = target["p50"] - assumed["value"]
        year_ago = quarter_label(quarter_index(TARGET) - 4)
        gallons = {"prior_year_third_quarter": next((r["fuel_gallons_m"] for r in full if r["quarter"] == year_ago), None),
                   "latest_quarter": full[-1]["fuel_gallons_m"]}
        predicted["management"] = {**assumed, "position": position(assumed["value"], target["p10"], target["p90"]),
                                   "p50_minus_management_usd_gal": gap,
                                   "fuel_expense_gap_usd_m": {k: gap * g for k, g in gallons.items() if g},
                                   "forward_curve_date": curve_day.isoformat() if curve_day else None}
        if curve_day:
            predicted["at_guidance_date"] = _range(forecast(curve_day))
    check = management_check(literature, full, dates, values, model, seed)

    delta_check, delta_fit, as_of_dates = None, None, [last_print]
    variants = [f"{name}: {equation(name)}; estimated on the fit years, select-year mean absolute error "
                f"{row['select_mae']:.3f} USD per gallon ({row['select_n']} quarters, pandemic quarters left out), "
                f"{row['select_mae_all_quarters']:.3f} on all {row['select_n_all_quarters']}."
                + (" Candidate." if name in CANDIDATES else " Listed for comparison; not a candidate.")
                for name, row in picked["by_spec"].items()]
    variants += [f"{spec} estimated from {w['start_year']}, walking forward one quarter at a time through the select "
                 f"years: mean absolute error {w['mae']:.3f} USD per gallon ({w['n']} quarters, pandemic quarters left out); "
                 f"{k['mae']:.3f} with pandemic quarters kept ({k['n']} quarters)."
                 for w, k in zip(picked["walk_forward"], picked["walk_forward_pandemic_kept"])]
    if evaluate_test:
        by_split["test"] = {**scored(model, test_rows, dates, values, ords, seed), "coefficients": "the frozen model in `fit`",
                            "mae_same_quarter_spec": mae(errors(fit_cost_model(final_rows(rows, start), ords, values, "same_quarter"),
                                                                test_rows, ords, values))}
        eras.append(era_fit(rows, ords, values, spec, 2018, SPLITS["test"][1],
                            "2018 to the latest report (includes test years; descriptive)"))
        refit = model_as_of(last_print, ual_path, fuel_path)
        all_years = fit_cost_model(final_rows(rows, SPLITS["fit"][0]), ords, values, spec)
        predicted["sensitivity"] = [
            {"id": "refit_through_latest", "short": "Refit that also learns from the 2024-2026 reports",
             "label": f"refit on every clean quarter from {start} reported before the as-of date (uses test-year "
                      "reports as training data; not the headline; cost_clock.model_as_of)", "n": refit.n, "basis_usd_gal": refit.basis,
             "slope": refit.slope, "lag_days": float(refit.lag_days()), "resid_sd_usd_gal": refit.sigma,
             **_range(forecast(last_print, refit))},
            {"id": "all_years", "short": "Fit that keeps the hedged years",
             "label": f"same specification estimated on {SPLITS['fit'][0]}-{SPLITS['select'][1]}, hedged years included",
             "n": all_years.n, "basis_usd_gal": all_years.basis, "slope": all_years.slope,
             "lag_days": float(all_years.lag_days()), "resid_sd_usd_gal": all_years.sigma,
             **_range(forecast(last_print, all_years))}]
        for item in predicted["sensitivity"]:
            item["management_position"] = position(assumed["value"], item["p10"], item["p90"]) if assumed else None
        variants += [f"{item['label']}: {TARGET} P50 {item['p50']:.2f} (P10 {item['p10']:.2f}, P90 {item['p90']:.2f})."
                     for item in predicted["sensitivity"]]
        predicted["calibration"] = {"inside_p10_p90": by_split["test"]["inside_p10_p90"], "n": by_split["test"]["n"],
                                    "mean_error_usd_gal": by_split["test"]["mean_error"],
                                    "note": calibration_sentence(by_split["test"])}
        delta_check, delta_fit, delta_day = delta_block(dal_path, dal_check_path, dates, values, ords, spec, start, seed, n_draws)
        as_of_dates.append(delta_day)
    eras = [{k: v for k, v in era.items() if k in ("label", "sample", "n", "basis_usd_gal", "basis_ci", "slope", "ci",
                                                      "lag_days", "lag_days_ci", "resid_sd_usd_gal", "r2")} for era in eras]

    x_same = design("same_quarter", [r["quarter"] for r in rows], ords, values)[:, 0]
    x_lag = design("lagged_window", [r["quarter"] for r in rows], ords, values)[:, 1]
    x_model = design(spec, [r["quarter"] for r in rows], ords, values)
    used, reported = set(model.quarters), []
    for row, same, lag, x in zip(rows, x_same, x_lag, x_model):
        quarter, split = row["quarter"], split_of(row["quarter"])
        show = np.isfinite(x).all() and (evaluate_test or split != "test")
        prints = window_mean(ords, values, *quarter_bounds(quarter))[1]
        reported.append({"period_end": row["period_end"].isoformat(), "quarter": quarter, "usd_gal": row["price"],
                         "spot_mean_usd_gal": same, "n_prints": prints, "spot_lagged_mean_usd_gal": lag,
                         "fitted_usd_gal": float(model.predict(x)) if show else None, "split": split,
                         "in_estimation": quarter in used, "pandemic": in_pandemic(quarter)})
    first_shown = quarter_bounds(rows[0]["quarter"])[0] - timedelta(days=LAG_WINDOW_DAYS)
    test = by_split["test"]
    summary = " ".join(filter(None, [
        headline(fit, test), era_sentence(eras),
        calibration_sentence(test), prediction_sentence(predicted),
        delta_sentence(delta_check) if delta_check else "", check_sentence(check)]))
    limits = [
        "One price index: Gulf Coast spot. United buys at many airports, with regional differentials, taxes and "
        "into-plane fees; the basis is their average over the estimation years and misses when they move.",
        f"The lag is inferred from quarterly averages alone ({fit['n']} quarters); daily purchases are not observed. "
        "The lag is a mix of the quarter and the quarter moved back 30 days, so it cannot exceed about a month by much.",
        "The predictive range assumes spot follows a random walk for the remaining days and ignores the forward curve "
        "that management guides on.",
        "Coefficients are frozen at the end of 2023. If United resumes hedging, or its mix of purchase locations "
        "changes, the relationship changes.",
        f"Pandemic quarters ({PANDEMIC[0]} to {PANDEMIC[1]}) are left out of estimation and of the headline errors; "
        "their errors are in `reported`.",
        f"The estimation window was chosen by walk-forward error on {picked['walk_forward'][0]['n']} select quarters; "
        "with pandemic quarters kept the result is in variants_tried and need not agree.",
        "Delta's adjusted price includes its refinery's result, which moves with refining margins; its basis is "
        "less stable than United's.",
        "The reconciliation of management's per-share figure uses a GAAP effective tax rate and profit-sharing "
        "tiers taken from a paraphrase in literature.json; United does not publish the calculation."]
    if test and delta_check:
        other = delta_check["earlier_test_quarters"]
        limits.insert(0, f"In 2024-2026 the frozen relationships ran {100 * abs(test['mean_error']):.0f} cents "
                         f"{'low' if test['mean_error'] < 0 else 'high'} for United and {100 * abs(other['mean_error']):.0f} cents "
                         f"{'low' if other['mean_error'] < 0 else 'high'} for Delta on average: what the airlines pay over Gulf "
                         "Coast spot has moved since 2018-2023, and the frozen coefficients do not know it. United's "
                         f"P10-P90 range held the reported price in {test['inside_p10_p90']:.0%} of {test['n']} test quarters.")
    payload = {
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": max(as_of_dates).isoformat(),
        "as_of_detail": {"fuel_daily_last_print": last_print.isoformat(), "ual_last_quarter": rows[-1]["quarter"],
                         "ual_last_report_published": rows[-1]["filed_on"].isoformat(),
                         "coefficients_use_reports_published_through": model.last_filed_on.isoformat()},
        "inputs": [{"path": _shown(p), "sha256": _sha(p)} for p in
                   (fuel_path, ual_path, literature_path) + ((dal_path, dal_check_path) if evaluate_test else ())],
        "seed": seed, "interval_level": LEVEL, "test_split": "computed once" if evaluate_test else "withheld",
        "headline": headline(fit, test), "plain_summary": summary,
        "daily_fuel": [[d.isoformat(), float(v)] for d, v in zip(dates, values) if d >= first_shown],
        "reported": reported, "fit": fit, "by_split": by_split,
        "selection": {"rule": picked["rule"], "chosen_spec": spec, "chosen_start_year": start,
                      "by_spec": picked["by_spec"], "walk_forward": picked["walk_forward"],
                      "walk_forward_pandemic_kept": picked["walk_forward_pandemic_kept"]},
        "eras": eras, "eras_summary": era_sentence(eras),
        "hedging_statement": hedging and {"source_id": hedging["id"], "url": hedging.get("url"),
                                          "finding": hedging.get("finding")},
        "predicted_2026q3": predicted, "delta_check": delta_check, "delta_fit": delta_fit,
        "management_check": check, "limits": limits, "variants_tried": variants,
        "units": "USD per gallon; intervals are 90% Student t; ranges are P10 to P90 of predictive draws"}
    return _clean(payload)


def delta_block(dal_path, check_path, dates, values, ords, spec: str, start: int, seed: int, n_draws: int):
    """The method on Delta's September quarter: Delta's own coefficients through 2023, then its reported price."""
    rows = delta_rows(dal_path)
    check = json.loads(Path(check_path).read_text())
    guide, actual = check["guidance_sep_quarter"], check["actuals_sep_quarter"]
    quarter = guide["period"]
    model = fit_cost_model(final_rows(rows, start), ords, values, spec, airline="DAL")
    reported_on = _day(actual["reported_on"])
    as_of = min(dates[-1], reported_on - timedelta(days=1))
    final = predicted_price_paid(quarter, dates, values, as_of=as_of, model=model, n_draws=n_draws, seed=seed)
    early = predicted_price_paid(quarter, dates, values, as_of=_day(guide["fuel_curve_as_of"]), model=model,
                                 n_draws=n_draws, seed=seed)
    price, assumed = actual["fuel_price_adj_usd_gal"], guide["fuel_price_assumed_usd_gal"]
    earlier = [r for r in rows if split_of(r["quarter"]) == "test" and quarter_index(r["quarter"]) < quarter_index(quarter)]
    block = {**_range(final), "quarter": quarter, "airline": "DAL", "price_basis": guide.get("fuel_price_basis"),
             "at_guidance_curve_date": _range(early),
             "assumed": {"value": assumed, "curve_as_of": guide["fuel_curve_as_of"], "made_on": guide["made_on"],
                         "evidence_id": guide["evidence_ids"]["fuel_price_assumed_usd_gal"]},
             "actual": {"value": price, "reported_on": actual["reported_on"],
                        "evidence_id": actual["evidence_ids"]["fuel_price_adj_usd_gal"],
                        "position": position(price, final["p10"], final["p90"])},
             "method_error": final["p50"] - price, "assumption_error": assumed - price,
             "method_closer_than_assumption": abs(final["p50"] - price) < abs(assumed - price),
             "earlier_test_quarters": scored(model, earlier, dates, values, ords, seed),
             "note": "Delta's coefficients are estimated on its own quarters through 2023 with the specification and "
                     "window chosen on United; nothing from 2024-2026 enters them. Delta published the outcome on "
                     f"{actual['reported_on']}, before this file was built: a check of the method, not a forecast."}
    return block, model.summary(seed=seed), reported_on


REQUIRED = ("generated_at", "as_of", "inputs", "headline", "plain_summary", "daily_fuel", "reported", "fit", "by_split",
            "management_check", "variants_tried", "predicted_2026q3", "delta_check", "limits")


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate(payload: dict) -> None:
    """Shape checks the builder runs before writing; raises ValueError naming the first problem."""
    missing = [key for key in REQUIRED if key not in payload]
    if missing:
        raise ValueError(f"cost_clock.json lacks {missing}")
    if payload.get("fixture"):
        raise ValueError("cost_clock.json must not be a fixture")
    json.dumps(payload, allow_nan=False)
    if not all(set(item) >= {"path", "sha256"} and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in payload["inputs"]):
        raise ValueError("inputs must list path and sha256")
    if not payload["headline"] or not payload["plain_summary"] or not payload["variants_tried"] or not payload["limits"]:
        raise ValueError("headline, plain_summary, variants_tried and limits must not be empty")
    if not payload["daily_fuel"] or not all(len(row) == 2 and _finite(row[1]) for row in payload["daily_fuel"]):
        raise ValueError("daily_fuel must be [date, price] pairs")
    if not payload["reported"] or not all(_finite(row["usd_gal"]) and row["period_end"] for row in payload["reported"]):
        raise ValueError("reported must list period_end and usd_gal")
    fit = payload["fit"]
    if not all(_finite(fit[key]) for key in ("lag_days", "basis_usd_gal", "slope", "r2", "n")) or len(fit["ci"]) != 2:
        raise ValueError("fit needs lag_days, basis_usd_gal, slope, ci, r2 and n")
    if not fit["ci"][0] <= fit["slope"] <= fit["ci"][1]:
        raise ValueError("fit.ci must bracket the slope")
    for name in ("fit", "select", "test"):
        part = payload["by_split"].get(name)
        if name != "test" and part is None:
            raise ValueError(f"by_split.{name} is missing")
        if part is not None and not (_finite(part["mae"]) and isinstance(part["n"], int)):
            raise ValueError(f"by_split.{name} needs mae and n")
    block = payload["predicted_2026q3"]
    if not block["p10"] <= block["p50"] <= block["p90"]:
        raise ValueError("predicted_2026q3 quantiles are out of order")
    check = payload["management_check"]
    figures = ("claimed_usd_m", "claimed_eps", "reproduced_usd_m", "reproduced_eps")
    if check.get("computed") and not all(_finite(check[key]) for key in figures):
        raise ValueError("management_check needs claimed and reproduced figures")
