"""Peer panel: the fare clock's recovery measurement pooled over United, Delta and American.

Same definition as United's fare clock (app/lab/fare_clock.py): the year-over-year
change in revenue per available seat mile (cents) on the year-over-year change in
fuel cost per seat mile (cents) in the same quarter and the four before it, with
year-over-year capacity growth as the control. Here every carrier gets its own
intercept and the three carriers share the response.

All three carriers face the same fuel price in the same quarter, so rows of one
quarter are not independent. Every range here resamples whole quarters (moving
blocks of four consecutive quarters, every carrier's row for a drawn quarter kept
together), and the analytic errors are clustered by quarter.

This module reads tracked files only: data/lab/peer_quarterly.csv (written by
scripts/lab/build_peer_panel.py from the local filing and traffic stores),
app/static/data/fare_clock.json (pandemic window, United-only result) and
app/static/data/demand_signal.json (traveller growth for the 2022 variant).
The smooth curve, the rho grid and the half-life are the fare clock's own.
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

from app.lab import fare_clock as fc
from app.lab import stats

ROOT = Path(__file__).resolve().parents[2]
PANEL_CSV = ROOT / "data" / "lab" / "peer_quarterly.csv"
MANIFEST_JSON = ROOT / "data" / "lab" / "manifests" / "peer_panel.json"
FARE_CLOCK_JSON = ROOT / "app" / "static" / "data" / "fare_clock.json"
DEMAND_JSON = ROOT / "app" / "static" / "data" / "demand_signal.json"
OUTPUT = ROOT / "app" / "static" / "data" / "peer_recovery.json"

CARRIERS = ("UAL", "DAL", "AAL")
NAMES = {"UAL": "United", "DAL": "Delta", "AAL": "American"}
VALUE_COLUMNS = ("total_revenue_musd", "fuel_expense_musd", "asm_m", "fuel_scope_factor")
SOURCE_COLUMNS = ("revenue_source", "fuel_source", "asm_source")
COLUMNS = ("carrier", "period_end") + VALUE_COLUMNS + SOURCE_COLUMNS
HORIZON = fc.HORIZON            # quarters 0..4 after the shock
REACH = 4 + (HORIZON - 1)       # levels behind one row: a year-over-year change and its four lags
BLOCK = 4                       # consecutive quarters per bootstrap block: year-over-year changes overlap by three
SEED = fc.SEED
N_DRAWS = 1000
MIN_CARRIER_ROWS = 8            # a carrier's resample with fewer rows is not fitted
OOS_FIRST_YEAR = 2024
MANAGEMENT_PATH = (0.45, 0.85, 1.0, 1.0, 1.0)
VARIANT_YEARS = (2022, 2023)
WIDE_ONE_YEAR = 0.50            # a 90% range of the one-year share wider than this makes the headline neutral
WIDE_HALF_LIFE = 4.0            # a 90% range of the half-life wider than this many quarters: speed not pinned down
LEVELS, LEVEL_NAMES = (0.05, 0.1, 0.5, 0.9, 0.95), ("p05", "p10", "p50", "p90", "p95")


# ------------------------------------------------------------------ small helpers

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
    if k < len(values):
        out[k:] = values[:len(values) - k]
    return out


def _spread(values) -> dict:
    return dict(zip(LEVEL_NAMES, stats.quantiles(np.asarray(values, dtype=float), LEVELS)))


# ------------------------------------------------------------------ the tracked panel

def load_panel(path: Path = PANEL_CSV) -> dict:
    """{carrier: table} from the tracked CSV, each on a regular quarterly grid; blank cells and absent quarters are NaN."""
    with open(path, newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or any(column not in rows[0] for column in COLUMNS):
        raise ValueError(f"{Path(path).name} lacks the columns the peer panel needs")
    panel = {}
    for carrier in sorted({r["carrier"] for r in rows}, key=lambda c: (CARRIERS.index(c) if c in CARRIERS else 99, c)):
        mine = [r for r in rows if r["carrier"] == carrier]
        index = [fc.quarter_index(date.fromisoformat(r["period_end"])) for r in mine]
        if len(set(index)) != len(index) or any(fc.quarter_end(i) != r["period_end"] for i, r in zip(index, mine)):
            raise ValueError(f"{Path(path).name}: {carrier} needs one row per calendar quarter end")
        quarters = np.arange(min(index), max(index) + 1)
        table = {"carrier": carrier, "q": quarters, "year": quarters // 4,
                 "sources": {column: sorted({r[column] for r in mine if r[column]}) for column in SOURCE_COLUMNS}}
        for column in VALUE_COLUMNS:
            values = np.full(len(quarters), np.nan)
            for i, row in zip(index, mine):
                if row[column].strip():
                    values[i - quarters[0]] = float(row[column])
            table[column] = values
        panel[carrier] = table
    return panel


def carrier_panel(table: dict, scale_fuel: bool = True) -> dict:
    """Unit revenue and fuel cost per seat mile (cents), their year-over-year changes and the lags of the fuel change.

    With `scale_fuel` a fuel figure that covers mainline flying only is multiplied by
    the row's fuel_scope_factor, which brings it to the consolidated scope of the
    revenue beside it (American before 2020; 1 everywhere else).
    """
    asm = table["asm_m"]
    fuel = table["fuel_expense_musd"] * (table["fuel_scope_factor"] if scale_fuel else 1.0)
    unit_rev, fuel_cpasm = 100.0 * table["total_revenue_musd"] / asm, 100.0 * fuel / asm
    d_fuel = fuel_cpasm - _lagged(fuel_cpasm, 4)
    return {"carrier": table["carrier"], "q": table["q"], "year": table["year"], "unit_rev": unit_rev,
            "fuel_cpasm": fuel_cpasm, "d_rev": unit_rev - _lagged(unit_rev, 4), "d_fuel": d_fuel,
            "cap": 100.0 * (asm / _lagged(asm, 4) - 1.0),
            "lags": np.column_stack([_lagged(d_fuel, j) for j in range(HORIZON)])}


def stack(panel: dict, scale_fuel: bool = True) -> dict:
    """Every carrier's quarters in one set of arrays; `valid` marks rows with the outcome, all five lags and capacity."""
    parts = [carrier_panel(table, scale_fuel) for table in panel.values()]
    join = lambda key: np.concatenate([p[key] for p in parts])
    data = {"carriers": [p["carrier"] for p in parts],
            "carrier": np.concatenate([np.full(len(p["q"]), i) for i, p in enumerate(parts)]),
            "q": join("q"), "year": join("year"), "y": join("d_rev"), "lags": join("lags"), "cap": join("cap")}
    data["valid"] = np.isfinite(data["y"]) & np.isfinite(data["lags"]).all(axis=1) & np.isfinite(data["cap"])
    return data


def pandemic_window(path: Path = FARE_CLOCK_JSON) -> dict:
    """The pandemic window the fare clock excludes, read from its output; its module constant if the file does not say."""
    window, source = tuple(fc.PANDEMIC_Q), "app/lab/fare_clock.py PANDEMIC_Q (fare_clock.json did not state a window)"
    if Path(path).is_file():
        spec = json.loads(Path(path).read_text()).get("primary_spec") or {}
        match = re.search(r"(\d{4})Q([1-4])\s*-\s*(\d{4})Q([1-4]) level", str(spec.get("sample", "")))
        if match:
            a, b, c, d = (int(v) for v in match.groups())
            window, source = (a * 4 + b - 1, c * 4 + d - 1), f"{_shown(path)} primary_spec.sample"
    return {"first": int(window[0]), "last": int(window[1]), "source": source,
            "label": f"{fc.quarter_label(int(window[0]))}-{fc.quarter_label(int(window[1]))}"}


def touched(quarters: np.ndarray, window: dict, reach: int = REACH) -> np.ndarray:
    """Quarters with any level from `reach` quarters back to the quarter itself inside the window (fare clock's rule)."""
    return (quarters >= window["first"]) & (quarters - reach <= window["last"])


# ------------------------------------------------------------------ regression with quarter clusters

def base_matrix(data: dict, rows: np.ndarray, extra=None) -> np.ndarray:
    """One intercept per carrier present on the rows, capacity growth, and any extra controls."""
    codes = data["carrier"][rows]
    columns = [(codes[:, None] == np.unique(codes)[None, :]).astype(float), data["cap"][rows][:, None]]
    if extra is not None:
        columns.append(np.asarray(extra, dtype=float).reshape(len(data["q"]), -1)[rows])
    return np.column_stack(columns)


def cluster_cov(X: np.ndarray, resid: np.ndarray, clusters: np.ndarray, hac_lag: int = 0) -> np.ndarray:
    """Sandwich covariance with the scores summed within each cluster (here: a calendar quarter).

    hac_lag 0 is the usual one-way cluster-robust covariance (Liang and Zeger, 1986).
    hac_lag L adds Bartlett-weighted cross products between clusters 1..L quarters
    apart (Driscoll and Kraay, 1998), for outcomes that overlap in time. Scaled by
    G/(G-1) x (n-1)/(n-k) with G clusters.
    """
    X, resid, clusters = np.asarray(X, dtype=float), np.asarray(resid, dtype=float), np.asarray(clusters)
    n, k = X.shape
    labels = np.unique(clusters)
    if len(labels) < 2 or n <= k:
        raise ValueError("Clustered covariance needs at least two clusters and more rows than coefficients")
    scores = np.array([(X[clusters == g] * resid[clusters == g, None]).sum(axis=0) for g in labels])
    meat = scores.T @ scores
    position = {int(g): i for i, g in enumerate(labels)}
    for lag in range(1, int(hac_lag) + 1):
        pairs = [(i, position[int(g) - lag]) for g, i in position.items() if int(g) - lag in position]
        if pairs:
            cross = scores[[i for i, _ in pairs]].T @ scores[[j for _, j in pairs]]
            meat = meat + (1.0 - lag / (hac_lag + 1.0)) * (cross + cross.T)
    bread = np.linalg.pinv(X.T @ X)
    return len(labels) / (len(labels) - 1.0) * (n - 1.0) / (n - k) * bread @ meat @ bread


def lag_regression(data: dict, rows: np.ndarray) -> dict:
    """The five-lag regression pooled over carriers, with naive and quarter-clustered errors for the running sums."""
    y, quarters = data["y"][rows], data["q"][rows]
    base = base_matrix(data, rows)
    X = np.column_stack([base, data["lags"][rows]])
    fit = stats.ols(X, y, add_const=False)
    if fit.rank_deficient or fit.df_resid <= 0:
        raise ValueError(f"Pooled lag regression is not identified: n={fit.n}, k={fit.k}, rank={fit.rank}")
    cols = np.arange(base.shape[1], base.shape[1] + HORIZON)
    covs = {"naive": fit.cov, "cluster_quarter": cluster_cov(X, fit.resid, quarters),
            "cluster_quarter_hac4": cluster_cov(X, fit.resid, quarters, hac_lag=BLOCK)}
    n_quarters = len(np.unique(quarters))
    cumulative = np.cumsum(fit.beta[cols])
    se = {}
    for name, cov in covs.items():
        sub = cov[np.ix_(cols, cols)]
        se[name] = np.array([math.sqrt(max(float(sub[:k + 1, :k + 1].sum()), 0.0)) for k in range(HORIZON)])
    used = np.maximum.reduce(list(se.values()))
    lo, hi = stats.conf_int(cumulative, used, fc.LEVEL, df=n_quarters - 1)
    within = y - base[:, :-1] @ np.linalg.lstsq(base[:, :-1], y, rcond=None)[0]
    design_effect = float((used[-1] / se["naive"][-1]) ** 2)
    return {"n_rows": fit.n, "n_quarters": n_quarters, "k": fit.k, "df_resid": fit.df_resid,
            "r2_within_carrier": 1.0 - float(fit.resid @ fit.resid) / float(within @ within),
            "capacity_coef_cents_per_point": fit.beta[base.shape[1] - 1], "condition_number": fit.cond,
            "curve": [{"quarters_after": k, "estimate": cumulative[k], "se": used[k], "lo": lo[k], "hi": hi[k],
                       "se_naive": se["naive"][k], "se_cluster_quarter": se["cluster_quarter"][k],
                       "se_cluster_quarter_hac4": se["cluster_quarter_hac4"][k]} for k in range(HORIZON)],
            "design_effect_one_year_share": design_effect,
            "n_quarters_effective_analytic": min(float(n_quarters), fit.n / design_effect),
            "interval": f"{fc.LEVEL:.0%} Student t with (quarters - 1) degrees of freedom on the largest of the three errors"}


# ------------------------------------------------------------------ the smooth two-parameter curve

def smooth_profile(y: np.ndarray, lags: np.ndarray, base: np.ndarray) -> tuple[float, float]:
    """Least-squares (L, rho) with `base` partialled out; the fare clock's profile with any set of controls.

    For each rho on the fare clock's grid the fuel term is the weighted lag sum with
    weights (1 - rho) rho^j; L given rho is a ratio of two inner products. A base
    column that is all zero on a resample (a carrier that was not drawn) is harmless.
    """
    residual = lambda v: v - base @ np.linalg.lstsq(base, v, rcond=None)[0]
    weights = (1.0 - fc.RHO_GRID)[:, None] * fc.RHO_GRID[:, None] ** np.arange(lags.shape[1])
    z, r = residual(lags @ weights.T), residual(y)
    zz, zy = np.einsum("ij,ij->j", z, z), z.T @ r
    if len(y) <= np.linalg.matrix_rank(base) + 2 or not np.all(zz > 1e-12):
        return math.nan, math.nan
    best = int(np.argmin(r @ r - zy ** 2 / zz))
    return float(zy[best] / zz[best]), float(fc.RHO_GRID[best])


def quarter_resamples(quarters: np.ndarray, n_boot: int, seed, block: int = BLOCK) -> list[np.ndarray]:
    """Row positions for each resample: moving blocks of consecutive distinct quarters, all carriers of a quarter together."""
    labels = np.unique(quarters)
    members = [np.flatnonzero(quarters == g) for g in labels]
    return [np.concatenate([members[i] for i in index])
            for index in stats.block_indices(len(labels), min(block, len(labels)), n_boot, seed)]


def fit_resamples(y, lags, base, resamples, keep=None, min_rows: int = 0) -> np.ndarray:
    """(L, rho) on each resample, NaN where it cannot be fitted; `keep` restricts every resample to a subset of rows."""
    out = np.full((len(resamples), 2), np.nan)
    for b, index in enumerate(resamples):
        if keep is not None:
            index = index[keep[index]]
        if len(index) >= max(min_rows, 1):
            out[b] = smooth_profile(y[index], lags[index], base[index])
    return out


def summarise(long_run: float, rho: float, pairs: np.ndarray, n_draws: int | None = None) -> dict:
    """Point estimate, percentile ranges and the curve from resampled (L, rho) pairs; draws are the resampled curves."""
    pairs = pairs[np.isfinite(pairs).all(axis=1)]
    if n_draws is not None:
        pairs = pairs[:n_draws]
    if len(pairs) < 100:
        raise ValueError("Too few usable bootstrap resamples for the smooth curve")
    draws = np.array([fc.smooth_curve(a, b) for a, b in pairs])
    estimate, curve_q = fc.smooth_curve(long_run, rho), stats.quantiles(draws, LEVELS)
    return {"L": long_run, "rho": rho, "half_life_quarters": fc.half_life(rho),
            "L_range": _spread(pairs[:, 0]), "rho_range": _spread(pairs[:, 1]),
            "half_life_range": _spread([fc.half_life(b) for b in pairs[:, 1]]),
            "one_year_share": {"estimate": estimate[-1], **_spread(draws[:, -1])},
            "curve": [{"quarters_after": k, "p10": curve_q[1][k], "p50": curve_q[2][k], "p90": curve_q[3][k],
                       "estimate": estimate[k], "p05": curve_q[0][k], "p95": curve_q[4][k]} for k in range(HORIZON)],
            "share_of_resamples_at_rho_upper_bound": float(np.mean(pairs[:, 1] >= fc.RHO_GRID[-1])),
            "n_resamples": len(pairs), "draws": draws}


def smooth_fit(data: dict, rows: np.ndarray, seed: int, n_draws: int = N_DRAWS, extra=None) -> dict:
    """The partial-adjustment curve pooled over the carriers on `rows`, with ranges from a bootstrap over quarters.

    Also resamples single rows as if they were independent, to measure how much
    wider the honest range is (the design effect) and the effective number of
    independent quarters behind the one-year share.
    """
    y, x, quarters, base = data["y"][rows], data["lags"][rows], data["q"][rows], base_matrix(data, rows, extra)
    long_run, rho = smooth_profile(y, x, base)
    if not math.isfinite(long_run):
        raise ValueError("The smooth curve is not identified on these rows")
    n_boot = int(1.5 * n_draws) + 100
    resamples = quarter_resamples(quarters, n_boot, seed)
    out = summarise(long_run, rho, fit_resamples(y, x, base, resamples), n_draws)
    weights = (1.0 - rho) * rho ** np.arange(HORIZON)
    final = stats.ols(np.column_stack([base, x @ weights]), y, add_const=False)
    naive = fit_resamples(y, x, base, list(np.random.default_rng(seed + 1).integers(0, len(y), size=(n_draws, len(y)))))
    naive = naive[np.isfinite(naive).all(axis=1)]
    naive_q = _spread([fc.smooth_curve(a, b)[-1] for a, b in naive])
    honest_width = out["one_year_share"]["p95"] - out["one_year_share"]["p05"]
    naive_width = naive_q["p95"] - naive_q["p05"]
    design_effect = (honest_width / naive_width) ** 2 if naive_width > 0 else math.nan
    n_quarters = len(np.unique(quarters))
    out.update({
        "n_rows": len(y), "n_quarters": n_quarters, "n_calendar_years": len(np.unique(data["year"][rows])),
        "rows_by_carrier": {data["carriers"][int(c)]: int((data["carrier"][rows] == c).sum())
                            for c in np.unique(data["carrier"][rows])},
        "first_quarter": fc.quarter_label(int(quarters.min())), "last_quarter": fc.quarter_label(int(quarters.max())),
        "capacity_coef_cents_per_point": final.beta[np.unique(data["carrier"][rows]).size],
        "one_year_share_range_if_rows_were_independent": naive_q,
        "design_effect": design_effect,
        "n_quarters_effective": (min(float(n_quarters), len(y) / design_effect)
                                 if math.isfinite(design_effect) and design_effect > 0 else float(n_quarters)),
        "resamples": resamples, "y": y, "x": x})
    return out


def by_carrier(data: dict, rows: np.ndarray, pooled: dict, n_draws: int) -> dict:
    """Each carrier's own curve, fitted on the same quarter resamples as the pooled one, and the gaps between carriers."""
    y, x, codes = pooled["y"], pooled["x"], data["carrier"][rows]
    base = np.column_stack([np.ones(len(y)), data["cap"][rows]])
    table, shares = [], {}
    for code in np.unique(codes):
        name, keep = data["carriers"][int(code)], codes == code
        entry = {"carrier": name, "name": NAMES.get(name, name), "n_rows": int(keep.sum()),
                 "first_quarter": fc.quarter_label(int(data["q"][rows][keep].min())),
                 "last_quarter": fc.quarter_label(int(data["q"][rows][keep].max()))}
        long_run, rho = smooth_profile(y[keep], x[keep], base[keep])
        pairs = fit_resamples(y, x, base, pooled["resamples"], keep, MIN_CARRIER_ROWS)[:n_draws]
        usable = np.isfinite(pairs).all(axis=1)
        if not math.isfinite(long_run) or usable.sum() < 100:
            table.append({**entry, "status": "not fitted: too few quarters", "one_year_share": None})
            continue
        fit = summarise(long_run, rho, pairs)
        shares[name] = np.where(usable, [fc.smooth_curve(a, b)[-1] if u else np.nan
                                         for (a, b), u in zip(pairs, usable)], np.nan)
        width = fit["one_year_share"]["p95"] - fit["one_year_share"]["p05"]
        table.append({**entry, "status": "fitted", "L": fit["L"], "rho": fit["rho"],
                      "half_life_quarters": fit["half_life_quarters"], "half_life_range": fit["half_life_range"],
                      "one_year_share": fit["one_year_share"], "range_width_p05_p95": width,
                      "wide": bool(is_wide(fit["one_year_share"])),
                      "range_includes_zero": bool(fit["one_year_share"]["p05"] <= 0.0 <= fit["one_year_share"]["p95"]),
                      "n_resamples": fit["n_resamples"],
                      "share_of_resamples_at_rho_upper_bound": fit["share_of_resamples_at_rho_upper_bound"]})
    gaps, names = [], list(shares)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            both = np.isfinite(shares[a]) & np.isfinite(shares[b])
            if both.sum() < 100:
                continue
            spread = _spread(shares[b][both] - shares[a][both])
            point = (next(r for r in table if r["carrier"] == b)["one_year_share"]["estimate"]
                     - next(r for r in table if r["carrier"] == a)["one_year_share"]["estimate"])
            gaps.append({"pair": f"{NAMES.get(b, b)} minus {NAMES.get(a, a)}", "estimate": point, **spread,
                         "n_resamples": int(both.sum()), "range_excludes_zero": bool(spread["p05"] > 0 or spread["p95"] < 0)})
    return {"table": table, "differences_in_one_year_share": gaps,
            "method": "Each carrier's own two-parameter curve (its own intercept and capacity coefficient), fitted on "
                      "the same quarter resamples as the pooled curve so the gaps between carriers respect the "
                      f"common fuel shock. A resample with fewer than {MIN_CARRIER_ROWS} rows for a carrier is not "
                      "fitted for that carrier. Gaps are differences in the one-year share with p05 to p95 ranges."}


# ------------------------------------------------------------------ variant, out of sample, comparison

def demand_growth(path: Path, quarters: np.ndarray) -> np.ndarray:
    """Year-over-year growth in TSA travellers (percent) for each quarter; NaN where the file has no complete figure."""
    growth = {}
    if Path(path).is_file():
        for row in json.loads(Path(path).read_text()).get("quarterly") or []:
            if row.get("complete") and row.get("yoy") is not None:
                growth[int(row["period"][:4]) * 4 + int(row["period"][5]) - 1] = 100.0 * float(row["yoy"])
    return np.array([growth.get(int(q), np.nan) for q in quarters])


def variant_with_2022(data: dict, clean: np.ndarray, demand_path: Path, seed: int, n_draws: int) -> dict:
    """The pooled curve with the 2022-2023 quarters added back and a traveller-growth control. A variant, not the result."""
    label = (f"VARIANT, not the headline: the {VARIANT_YEARS[0]}-{VARIANT_YEARS[1]} quarters, which the main estimate "
             "drops because 2020-2021 levels sit behind their year-over-year changes, are added back with a demand "
             "control.")
    growth = demand_growth(demand_path, data["q"])
    era = (data["q"] >= VARIANT_YEARS[0] * 4).astype(float)
    added = data["valid"] & (data["year"] >= VARIANT_YEARS[0]) & (data["year"] <= VARIANT_YEARS[1])
    rows = np.flatnonzero((clean | added) & (np.isfinite(growth) | (era == 0)))
    n_added = int(added[rows].sum())
    if not n_added:
        return {"label": label, "status": "not computed", "reason": "no 2022-2023 rows with a traveller-growth figure"}
    control = np.column_stack([np.where(era == 1, np.nan_to_num(growth), 0.0), era])
    fit = smooth_fit(data, rows, seed, n_draws, extra=control)
    plain = smooth_fit(data, rows, seed, n_draws, extra=era)
    base = base_matrix(data, rows, control)
    weights = (1.0 - fit["rho"]) * fit["rho"] ** np.arange(HORIZON)
    final = stats.ols(np.column_stack([base, fit["x"] @ weights]), fit["y"], add_const=False)
    public = lambda f: {k: f[k] for k in ("L", "rho", "half_life_quarters", "L_range", "half_life_range",
                                          "one_year_share", "curve", "n_rows", "n_quarters", "n_quarters_effective",
                                          "rows_by_carrier", "share_of_resamples_at_rho_upper_bound")}
    return {"label": label, "status": "computed", **public(fit), "rows_added": n_added,
            "demand_coef_cents_per_point_of_traveller_growth": final.beta[base.shape[1] - 2],
            "same_rows_without_demand_control": public(plain),
            "controls": "Year-over-year growth in TSA checkpoint travellers (percent) for quarters from "
                        f"{VARIANT_YEARS[0]}Q1 on, and an indicator for those quarters: the traveller series starts in "
                        "2019, so it has no year-over-year figure for 2015-2019 and the indicator keeps the missing "
                        "figures from acting as zeros. Carrier intercepts and capacity growth as in the main estimate.",
            "caveat": "The added quarters compare 2022 with 2021 and carry lagged fuel changes measured against 2020: "
                      "the rebound from the pandemic and the 2022 fuel spike happened together, and one national "
                      "traveller count cannot fully separate them."}


def out_of_sample(data: dict, clean: np.ndarray) -> dict:
    """The pooled smooth curve fitted on quarters before 2024 and scored on 2024 onwards, per carrier, against fixed rules."""
    train = np.flatnonzero(clean & (data["year"] < OOS_FIRST_YEAR))
    test = np.flatnonzero(clean & (data["year"] >= OOS_FIRST_YEAR))
    long_run, rho = smooth_profile(data["y"][train], data["lags"][train], base_matrix(data, train))
    if not math.isfinite(long_run) or not len(test):
        return {"status": "not computed", "reason": "too few quarters before or after the cut"}
    rules = {"no_response": np.zeros(HORIZON), "full_immediate": np.ones(HORIZON),
             "management_path": np.array(MANAGEMENT_PATH), "pooled_smooth": fc.smooth_curve(long_run, rho)}
    errors, plain, baselines = {}, {}, {}
    for name, curve in rules.items():
        term = data["lags"] @ np.diff(np.concatenate([[0.0], curve]))
        intercept, slope = stats.ols(data["cap"][train], (data["y"] - term)[train]).beta
        errors[name] = np.abs(data["y"] - (intercept + slope * data["cap"] + term))
        plain[name] = np.abs(data["y"] - term)
        baselines[name] = {"intercept_cents": intercept, "capacity_coef_cents_per_point": slope}

    def score(rows):
        mae = {name: float(errors[name][rows].mean()) for name in rules}
        return {"n": len(rows), "quarters": [fc.quarter_label(int(q)) for q in np.unique(data["q"][rows])],
                "mae_cents_per_asm": mae, "lowest_mae": min(mae, key=mae.get),
                "pooled_smooth_minus_best_rival": mae["pooled_smooth"] - min(v for k, v in mae.items() if k != "pooled_smooth"),
                "mae_cents_per_asm_fuel_term_only": {name: float(plain[name][rows].mean()) for name in rules}}

    carriers = [{"carrier": data["carriers"][int(c)], "name": NAMES.get(data["carriers"][int(c)], ""),
                 **score(test[data["carrier"][test] == c])} for c in np.unique(data["carrier"][test])]
    return {"status": "computed", "fitted_on": f"clean quarters before {OOS_FIRST_YEAR}", "n_rows_fitted": len(train),
            "n_quarters_fitted": len(np.unique(data["q"][train])),
            "rows_fitted_by_carrier": {data["carriers"][int(c)]: int((data["carrier"][train] == c).sum())
                                       for c in np.unique(data["carrier"][train])},
            "L": long_run, "rho": rho, "half_life_quarters": fc.half_life(rho), "rules": rules,
            "baselines": baselines, "by_carrier": carriers, "all_carriers": score(test),
            "definition": "Predicted year-over-year change in revenue per ASM (cents) under each rule, given the fuel "
                          "cost per ASM and capacity that actually occurred: a constant and a capacity coefficient "
                          f"common to all carriers, estimated on the quarters before {OOS_FIRST_YEAR} with the rule's "
                          "fuel response held fixed, plus the rule's fuel term. One constant, not one per carrier, "
                          "because Delta has a single usable quarter before the cut. mae is the mean absolute error "
                          "in cents per ASM over n carrier-quarters; the fuel-term-only figures drop the baseline.",
            "management_path_note": "0.45 in the quarter of the shock, 0.85 one quarter later, 1.0 afterwards, as "
                                    "specified for this comparison.",
            "chosen_after_test_seen": True,
            "note": "This specification (the two-parameter curve, pooled over carriers) was chosen AFTER the "
                    "United-only test on these same years had been seen. The scores are descriptive, not a clean test."}


def united_only(path: Path = FARE_CLOCK_JSON) -> dict | None:
    """United's own one-year share and half-life as published by the fare clock; None when the file has none."""
    if not Path(path).is_file():
        return None
    payload = json.loads(Path(path).read_text())
    smooth = (payload.get("smooth") or {}).get("through_latest") or {}
    share = smooth.get("one_year_share")
    if isinstance(share, dict) and all(isinstance(share.get(k), (int, float)) for k in ("estimate", "p05", "p95")):
        return {"one_year_share": share, "half_life_quarters": smooth.get("half_life_quarters"),
                "half_life_range": smooth.get("half_life_range"), "L": smooth.get("L"), "n": smooth.get("n"),
                "field": "smooth.through_latest", "path": _shown(path), "sha256": _sha(path)}
    curve = payload.get("curve")
    if isinstance(curve, list) and len(curve) == HORIZON and all(k in curve[-1] for k in ("p05", "p95")):
        last = curve[-1]
        return {"one_year_share": {k: last.get(k) for k in ("estimate",) + LEVEL_NAMES}, "half_life_quarters": None,
                "half_life_range": None, "L": None, "n": payload.get("n"), "field": "curve[4]",
                "path": _shown(path), "sha256": _sha(path)}
    return None


def comparison(pooled: dict, carriers: dict, united: dict | None) -> dict:
    """Pooled against United alone: the fare clock's published figure, and United refitted here by the same method."""
    share = pooled["one_year_share"]
    width = share["p95"] - share["p05"]
    here = next((r for r in carriers["table"] if r["carrier"] == "UAL" and r["one_year_share"]), None)
    out = {"one_year_share_united_only": None, "one_year_share_pooled": share["estimate"],
           "one_year_share_pooled_range": {k: share[k] for k in LEVEL_NAMES},
           "range_width_united_only": None, "range_width_pooled": width, "range": "p05 to p95 (90%)",
           "range_width_pooled_p10_p90": share["p90"] - share["p10"],
           "half_life_quarters_pooled": pooled["half_life_quarters"], "half_life_range_pooled": pooled["half_life_range"],
           "united_only_refitted_here": None, "narrowed": None, "width_change_share": None}
    if here:
        out["united_only_refitted_here"] = {
            "one_year_share": here["one_year_share"], "range_width": here["range_width_p05_p95"],
            "half_life_quarters": here["half_life_quarters"], "half_life_range": here["half_life_range"],
            "n_rows": here["n_rows"], "pooled_width_over_this": width / here["range_width_p05_p95"],
            "note": "United alone from the tracked panel (revenue over ASM and reported fuel expense over ASM), on the "
                    "same quarter resamples as the pooled curve: the like-for-like comparison."}
    if united is None:
        out["reason"] = "fare_clock.json has no United-only smooth curve"
        return out
    theirs = united["one_year_share"]
    out.update({"one_year_share_united_only": theirs["estimate"],
                "one_year_share_united_only_range": {k: theirs.get(k) for k in LEVEL_NAMES},
                "range_width_united_only": theirs["p95"] - theirs["p05"],
                "half_life_quarters_united_only": united["half_life_quarters"],
                "half_life_range_united_only": united["half_life_range"], "n_united_only": united["n"],
                "source": {k: united[k] for k in ("path", "sha256", "field")}})
    if isinstance(theirs.get("p10"), (int, float)) and isinstance(theirs.get("p90"), (int, float)):
        out["range_width_united_only_p10_p90"] = theirs["p90"] - theirs["p10"]
    out["narrowed"] = bool(width < out["range_width_united_only"])
    out["width_change_share"] = width / out["range_width_united_only"] - 1.0
    return out


# ------------------------------------------------------------------ words, generated from the numbers

def is_wide(share: dict) -> bool:
    """A one-year share whose 90% range is wider than WIDE_ONE_YEAR or reaches zero cannot carry a claim."""
    return bool(share["p95"] - share["p05"] > WIDE_ONE_YEAR or share["p05"] <= 0.0)


def speed_unclear(fit: dict) -> bool:
    spread = fit["half_life_range"]
    return bool(spread["p95"] - spread["p05"] > WIDE_HALF_LIFE or fit["share_of_resamples_at_rho_upper_bound"] >= 0.05)


def headline(fit: dict, names: list[str]) -> str:
    """One sentence from the pooled fit; neutral when the range of the one-year share is wide."""
    share, who = fit["one_year_share"], ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1]
    size = (f"{fit['n_rows']} carrier-quarters from {fit['n_quarters']} distinct quarters, worth about "
            f"{fit['n_quarters_effective']:.0f} independent ones")
    if share["p05"] <= 0.0:
        return (f"Pooling {who} cannot tell a revenue response to a fuel cost shock from none: a year later the 90% "
                f"range of the share made up runs from {_pct(share['p05'])} to {_pct(share['p95'])} ({size}).")
    if is_wide(share):
        return (f"Pooling {who} shows a revenue response to a fuel cost shock but does not pin down its size: a year "
                f"later unit revenue has made up between {_pct(share['p05'])} and {_pct(share['p95'])} of the cost "
                f"(90% range; {size}).")
    life = fit["half_life_range"]
    speed = (f"how fast is not pinned down (half of the long-run share within {life['p05']:.1f} to {life['p95']:.1f} "
             "quarters)" if speed_unclear(fit) else
             f"half of the long-run share arrives within {fit['half_life_quarters']:.1f} quarters "
             f"({life['p05']:.1f} to {life['p95']:.1f})")
    return (f"Across {who}, unit revenue has made up {_pct(share['estimate'])} of a fuel cost shock a year later "
            f"(90% range {_pct(share['p05'])} to {_pct(share['p95'])}; {size}); {speed}.")


def plain_summary(payload: dict) -> list[str]:
    pooled, compare, out = payload["pooled"], payload["comparison_with_united_only"], [payload["headline"]]
    out.append(f"The {pooled['n_rows']} rows come from {pooled['n_quarters']} distinct quarters in "
               f"{pooled['n_calendar_years']} calendar years; because all carriers face the same fuel price, they count "
               f"for about {pooled['n_quarters_effective']:.0f} independent quarters.")
    life = pooled["half_life_range"]
    out.append(f"Least-squares estimate: {_pct(pooled['one_year_share']['estimate'])} after a year and "
               f"{_pct(pooled['L'])} in the long run, half of that within {pooled['half_life_quarters']:.1f} quarters "
               f"(90% range {life['p05']:.1f} to {life['p95']:.1f}"
               f"{'; the speed is not pinned down' if speed_unclear(pooled) else ''}).")
    if compare["one_year_share_united_only"] is not None:
        theirs = compare["one_year_share_united_only_range"]
        change = compare["width_change_share"]
        out.append(f"United alone, as published: {_pct(compare['one_year_share_united_only'])} after a year "
                   f"({_pct(theirs['p05'])} to {_pct(theirs['p95'])}). Pooled: {_pct(compare['one_year_share_pooled'])} "
                   f"({_pct(pooled['one_year_share']['p05'])} to {_pct(pooled['one_year_share']['p95'])}).")
        out.append(f"The pooled 90% range is {abs(100 * change):.0f}% {'narrower' if change < 0 else 'wider'} than "
                   f"United's ({100 * compare['range_width_pooled']:.0f} against "
                   f"{100 * compare['range_width_united_only']:.0f} points).")
    here = compare["united_only_refitted_here"]
    if here:
        ratio = here["pooled_width_over_this"]
        out.append(f"Like for like (United refitted here on the same resamples): the pooled range is "
                   f"{abs(100 * (ratio - 1)):.0f}% {'narrower' if ratio < 1 else 'wider'}.")
    for row in payload["by_carrier"]["table"]:
        share = row["one_year_share"]
        out.append(f"{row['name']} alone: not fitted ({row['n_rows']} quarters)." if not share else
                   f"{row['name']} alone: {_pct(share['estimate'])} after a year ({_pct(share['p05'])} to "
                   f"{_pct(share['p95'])}, {row['n_rows']} quarters"
                   f"{', which cannot be told from no response' if row['range_includes_zero'] else ''}).")
    gaps = payload["by_carrier"]["differences_in_one_year_share"]
    if gaps:
        clear = [g["pair"] for g in gaps if g["range_excludes_zero"]]
        out.append("Carriers differ beyond the noise for: " + "; ".join(clear) + "." if clear else
                   "No gap between two carriers is distinguishable from zero.")
    variant = payload["variant_with_2022"]
    if variant.get("status") == "computed":
        share = variant["one_year_share"]
        out.append(f"Variant with 2022-2023 added back and a traveller-growth control: {_pct(share['estimate'])} after "
                   f"a year ({_pct(share['p05'])} to {_pct(share['p95'])}, {variant['n_rows']} rows).")
    oos = payload["oos"]
    if oos.get("status") == "computed":
        for row in oos["by_carrier"]:
            mae = row["mae_cents_per_asm"]
            out.append(f"Out of sample {row['name']} ({row['n']} quarters from {OOS_FIRST_YEAR}): average miss "
                       f"{mae['pooled_smooth']:.2f} cents per ASM for the pooled curve, {mae['no_response']:.2f} for no "
                       f"response, {mae['full_immediate']:.2f} for full immediate recovery, "
                       f"{mae['management_path']:.2f} for management's path.")
        out.append("That comparison was chosen after the United-only test on the same years had been seen.")
    out.append("Co-movement, not proof of cause: fuel prices and travel demand move together.")
    return out


LIMITS = (
    "Co-movement, not causation: fuel prices and travel demand rise and fall together with the economy, so part of "
    "any measured response is demand and not airlines passing on cost. Pooling carriers adds rows, not new fuel "
    "shocks: every carrier sees the same fuel price in the same quarter.",
    "Scope differs between carriers. United: consolidated revenue, fuel expense and capacity from its earnings "
    "releases. Delta: the adjusted total revenue (third-party refinery sales removed) and adjusted fuel expense "
    "printed in its releases, not the GAAP figures. American: consolidated revenue and fuel from SEC XBRL facts over mainline "
    "capacity from BTS traffic filings.",
    "American's capacity base is narrower than its revenue: BTS seat miles are for the American (and, to mid-2015, US "
    "Airways) operating certificates and leave out regional partners and some international flying, while revenue "
    "and fuel are consolidated. Its cents-per-ASM figures are therefore too high in level; the recovery share is a "
    "ratio of two such figures and is distorted only as far as the mainline share of capacity moves.",
    "American's fuel figure covers mainline flying only before 2020 (as first filed) and all flying from 2020 (later "
    "filings restate the line to a higher figure, which matches regional fuel having been moved into it). The earlier figures are multiplied by one factor, the fiscal "
    "2019 fuel expense as restated over the same year as first filed, which assumes the regional share of fuel was "
    "constant from 2014 to 2019. The estimate without that factor is listed under variants_tried.",
    "American enters from 2014Q1, the first full quarter after its merger with US Airways, so its first usable "
    "row is 2016Q1.",
    "Delta owns a refinery. Its adjusted figures remove third-party refinery sales, but the refinery still changes "
    "the fuel price Delta pays relative to the market, so its fuel shock is not the same shock as the others'.",
    "Delta's adjusted figures start in 2018, so it contributes one quarter before 2020 and the rest from 2024: it "
    "adds almost nothing to the earlier period and its own curve rests on very few quarters.",
    "Hedging in the early years: reported fuel expense includes the results of fuel hedges, so the fuel cost a "
    "carrier reported after the 2014 fall in oil prices did not move one for one with the market price, and not by "
    "the same amount at each carrier. Hedge results are not separated here.",
    "The clean sample is 2015-2019 and 2024 onwards; the largest fuel spike before 2026 (2022) is outside it and "
    "enters only in the labelled variant.",
    "Year-over-year changes overlap by three quarters. The bootstrap uses blocks of four quarters, which with about "
    "thirty distinct quarters means roughly eight independent blocks; the ranges are probably still too narrow.",
    "L (the long-run share) and rho (the speed) trade off against each other; the one-year share is the better "
    "determined quantity and the half-life the weaker one.",
)


# ------------------------------------------------------------------ build and validate

def build(panel_path: Path = PANEL_CSV, fare_clock_path: Path = FARE_CLOCK_JSON, demand_path: Path = DEMAND_JSON,
          seed: int = SEED, n_draws: int = N_DRAWS, generated_at: str | None = None) -> dict:
    """The whole output for app/static/data/peer_recovery.json. Deterministic for fixed inputs, seed and timestamp."""
    if n_draws < 500:
        raise ValueError("n_draws must be at least 500")
    panel = load_panel(panel_path)
    data, window = stack(panel), pandemic_window(fare_clock_path)
    clean = data["valid"] & ~touched(data["q"], window)
    rows = np.flatnonzero(clean)
    names = [NAMES.get(c, c) for c in data["carriers"]]
    pooled = smooth_fit(data, rows, seed, n_draws)
    carriers = by_carrier(data, rows, pooled, n_draws)
    lagged = lag_regression(data, rows)
    variants = {}
    raw = stack(panel, scale_fuel=False)
    variants["american_fuel_as_first_filed"] = (
        "As the main estimate with American's pre-2020 fuel as first filed (mainline only, no scope factor)",
        smooth_fit(raw, np.flatnonzero(raw["valid"] & ~touched(raw["q"], window)), seed, n_draws))
    if "AAL" in data["carriers"] and len(data["carriers"]) > 1:
        keep = np.flatnonzero(clean & (data["carrier"] != data["carriers"].index("AAL")))
        variants["without_american"] = ("As the main estimate without American (release-based carriers only)",
                                        smooth_fit(data, keep, seed, n_draws))
    variant = variant_with_2022(data, clean, demand_path, seed, n_draws)
    oos = out_of_sample(data, clean)
    public = ("L", "rho", "half_life_quarters", "L_range", "rho_range", "half_life_range", "one_year_share", "n_rows",
              "n_quarters", "n_calendar_years", "n_quarters_effective", "design_effect", "rows_by_carrier",
              "first_quarter", "last_quarter", "capacity_coef_cents_per_point",
              "one_year_share_range_if_rows_were_independent", "share_of_resamples_at_rho_upper_bound", "n_resamples")
    inputs = [{"path": _shown(panel_path), "sha256": _sha(panel_path)}]
    inputs += [{"path": _shown(p), "sha256": _sha(p)} for p in (fare_clock_path, demand_path) if Path(p).is_file()]
    last_used = int(data["q"][rows].max())
    payload = {
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": fc.quarter_end(last_used), "inputs": inputs, "seed": seed, "interval_level": 0.9,
        "headline": headline(pooled, names), "plain_summary": [],
        "definition": {
            "recovery": "recovery(k) = L x (1 - rho^(k+1)): the share of a lasting fuel cost change that unit revenue "
                        "has made up k quarters after the shock. L is the long-run share, rho the part of the "
                        "remaining gap left open each quarter, half-life ln(0.5)/ln(rho) quarters.",
            "equation": "d4(revenue per ASM)[i,t] = a[i] + L * sum_{j=0..4} (1 - rho) rho^j * d4(fuel cost per ASM)"
                        "[i,t-j] + c * capacity growth[i,t] + e[i,t]; i a carrier, a[i] its intercept, all in cents "
                        "per available seat mile, capacity growth in percent year over year",
            "measures": "Revenue over ASM and fuel expense over ASM from data/lab/peer_quarterly.csv. United's fare "
                        "clock uses the unit revenue printed in the release and price per gallon times gallons, which "
                        "agree with these to rounding.",
            "pandemic_window": window,
            "fuel_scope_factors": {c: sorted({round(float(v), 6) for v in panel[c]["fuel_scope_factor"]
                                              if math.isfinite(v) and v != 1.0}) for c in panel},
            "pandemic_rule": f"A row is dropped when any level from {REACH} quarters back to the quarter itself lies "
                             f"in {window['label']}: the fare clock's rule.",
            "estimation": "Least squares: rho on the fare clock's grid from 0 to 0.95 in steps of 0.01, L in closed "
                          "form given rho.",
            "uncertainty": f"Moving-block bootstrap over calendar quarters: blocks of {BLOCK} consecutive distinct "
                           "quarters, with every carrier's row for a drawn quarter kept together, L and rho "
                           "re-estimated on each resample. Ranges are percentiles of the resamples; curve_draws are "
                           "the resampled curves. Rows of one quarter are never treated as independent. The clean "
                           "quarters are not contiguous (2019Q4 is followed by 2024Q1), so a block can straddle the gap.",
            "n_quarters_effective": "Rows divided by the design effect, capped at the number of distinct quarters. The "
                                    "design effect is the squared ratio of the 90% range of the one-year share under "
                                    "the quarter-block bootstrap to the same range when single rows are resampled as "
                                    "if independent.",
            "carriers": {c: {"name": NAMES.get(c, c), "sources": panel[c]["sources"],
                             "first_quarter": fc.quarter_label(int(panel[c]["q"][0])),
                             "last_quarter": fc.quarter_label(int(panel[c]["q"][-1]))} for c in panel}},
        "pooled": {**{k: pooled[k] for k in public}, "curve": pooled["curve"], "curve_draws": np.round(pooled["draws"], 4),
                   "lag_regression": {**lagged, "note": "The unrestricted five-lag regression on the same rows, with "
                                      "carrier intercepts: running sums of the lag coefficients, with classical "
                                      "errors, errors clustered by quarter, and clustered errors with four quarters "
                                      "of serial correlation. lo and hi use the largest."}},
        "by_carrier": carriers, "variant_with_2022": variant, "oos": oos,
        "comparison_with_united_only": comparison(pooled, carriers, united_only(fare_clock_path)),
        "limits": list(LIMITS),
        "variants": {key: {"description": text, **{k: fit[k] for k in ("L", "rho", "half_life_quarters", "one_year_share",
                                                                      "half_life_range", "n_rows", "n_quarters")}}
                     for key, (text, fit) in variants.items()},
        "variants_tried": [
            "pooled_smooth_carrier_intercepts_capacity_clean_quarters: the main estimate (two-parameter curve, carrier "
            "intercepts, capacity control, pandemic-touched quarters dropped, American's pre-2020 fuel brought to "
            "consolidated scope).",
            "pooled_five_lag_regression_same_rows: the unrestricted five-lag regression with quarter-clustered errors "
            "(pooled.lag_regression).",
            *[f"{key}: {text}." for key, (text, _) in variants.items()],
            "variant_with_2022: 2022-2023 quarters added back with TSA traveller growth and an indicator for quarters "
            "from 2022 (variant_with_2022), and the same rows without the traveller control.",
            "per_carrier_smooth: each carrier's own two-parameter curve on the shared quarter resamples (by_carrier).",
            "oos_pre_2024: the pooled curve fitted on quarters before 2024 and scored on 2024 onwards (oos); chosen "
            "after the United-only test had been seen."],
    }
    payload["plain_summary"] = plain_summary(payload)
    payload = _clean(payload)
    validate(payload)
    return payload


REQUIRED = ("generated_at", "as_of", "inputs", "headline", "plain_summary", "definition", "pooled", "by_carrier",
            "variant_with_2022", "oos", "comparison_with_united_only", "limits", "variants_tried")


def validate(payload: dict) -> None:
    """Raise ValueError unless the payload has the shape the pages and the simulator read."""
    def need(condition, message):
        if not condition:
            raise ValueError(f"peer_recovery.json: {message}")

    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    need(isinstance(payload, dict) and not payload.get("fixture"), "must be an object and never a fixture")
    need(not [k for k in REQUIRED if k not in payload], f"missing keys {[k for k in REQUIRED if k not in payload]}")
    json.dumps(payload, allow_nan=False)
    for key in ("generated_at", "as_of", "headline"):
        need(isinstance(payload[key], str) and payload[key], f"{key} must be a non-empty string")
    for key in ("plain_summary", "limits", "variants_tried"):
        need(isinstance(payload[key], list) and payload[key] and all(isinstance(v, str) and v for v in payload[key]),
             f"{key} must be a non-empty list of strings")
    need(payload["inputs"] and all(re.fullmatch(r"[0-9a-f]{64}", i.get("sha256", "")) and i.get("path")
                                   for i in payload["inputs"]), "inputs need a path and a SHA-256 each")
    pooled = payload["pooled"]
    curve, draws = pooled.get("curve"), pooled.get("curve_draws")
    need(isinstance(curve, list) and [r.get("quarters_after") for r in curve] == list(range(HORIZON)),
         "pooled.curve must list quarters_after 0..4")
    need(all(number(r[k]) for r in curve for k in ("p10", "p50", "p90"))
         and all(r["p10"] <= r["p50"] <= r["p90"] for r in curve), "pooled.curve needs finite p10 <= p50 <= p90")
    need(isinstance(draws, list) and len(draws) >= 500
         and all(len(d) == HORIZON and all(number(v) for v in d) for d in draws),
         "pooled.curve_draws needs at least 500 rows of five finite shares")
    steps = [[b - a for a, b in zip(d, d[1:])] for d in draws]
    need(all(min(step) >= -1e-9 or max(step) <= 1e-9 for step in steps), "every draw must be monotone across quarters")
    need(all(number(pooled.get(k)) for k in ("L", "half_life_quarters", "n_quarters_effective"))
         and isinstance(pooled.get("n_rows"), int) and pooled["n_rows"] > 0,
         "pooled needs L, half_life_quarters, n_rows and n_quarters_effective")
    share = pooled.get("one_year_share") or {}
    need(all(number(share.get(k)) for k in ("estimate",) + LEVEL_NAMES) and share["p05"] <= share["p50"] <= share["p95"],
         "pooled.one_year_share needs an estimate and ordered percentiles")
    need(pooled["n_quarters_effective"] <= pooled["n_rows"], "effective quarters cannot exceed the rows")
    table = payload["by_carrier"].get("table")
    need(isinstance(table, list) and table and all(r.get("carrier") and isinstance(r.get("n_rows"), int) for r in table),
         "by_carrier.table needs one entry per carrier with n_rows")
    need("label" in payload["variant_with_2022"] and "VARIANT" in payload["variant_with_2022"]["label"],
         "variant_with_2022 must be labelled as a variant")
    oos = payload["oos"]
    if oos.get("status") == "computed":
        need(oos.get("chosen_after_test_seen") is True, "oos must say it was chosen after the test was seen")
        need(all(isinstance(r.get("n"), int) and all(number(v) for v in r["mae_cents_per_asm"].values())
                 for r in oos["by_carrier"]), "each oos row needs n and a mean absolute error per rule")
    compare = payload["comparison_with_united_only"]
    need(all(k in compare for k in ("one_year_share_united_only", "one_year_share_pooled", "range_width_united_only",
                                    "range_width_pooled")), "comparison_with_united_only lacks a required field")
