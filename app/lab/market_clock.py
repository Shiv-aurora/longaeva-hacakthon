"""The market clock: how fast United's share price reacts to jet-fuel moves, and what happens next.

Timing discipline (every function in this module follows it)
------------------------------------------------------------
Grid. One row per US equity session (the dates SPY printed). A series that did
not print on a session is NaN there. Nothing is forward filled or interpolated.

Returns. r[t] = log(close[t] / close[t-1]), dated on the later close.

What is known when:

- HO=F (front-month NY Harbor ULSD future, the tradable fuel proxy) settles at
  14:30 New York time, before the 16:00 equity close. Its value on session t is
  treated as known at the equity close of t. A signal built from HO=F through
  session t is therefore paired only with equity returns of sessions t+1, t+2,
  ... (close of t onward) in every predictive test, never with session t itself.
  Caveat: the price file holds Yahoo's daily "close" for HO=F, and Yahoo does not
  document whether that is the 14:30 settlement or a later last trade. The
  primary lagged regression is therefore also computed with one more session
  skipped (returns from the close of t+1), and the trading rule is tried both
  ways. The lead-lag correlations with the spot series are reported as a check.
- The EIA Gulf Coast jet-fuel spot print for session t is published later. It is
  treated as known one business day later: a signal built from EIA prints
  through t is paired with equity returns of sessions t+2, t+3, ... The file is
  the current vintage, not what was first published.
- Same-session regressions (fuel return of t against equity return of t) are
  descriptive co-movement. They are not tradable and no trade is chosen on them.
- Betas. excess[t] = r_asset[t] - beta[t] * r_SPY[t], where beta[t] comes from
  sessions t-252 .. t-1 only (stats.rolling_beta).
- Events. Session t is a crossing when |trailing fuel change at t| exceeds a
  quantile of the same quantity over sessions strictly before t. An event is the
  first crossing after `quiet_days` sessions without one. The primary overlap
  rule walks forward and drops an event when a kept event lies within the event
  window span before it; it never looks forward. The market-model beta is
  estimated on a window that ends before the event window starts.
- Splits. fit 2013-2019, select 2020-2023, test 2024 onward. A row or event
  belongs to a split only if its whole outcome window ends inside that split, so
  nothing measured in fit or select touches a later split (boundary rows are
  purged and counted). `explore` only ever receives the panel cut at the end of
  the select split; `build` writes the frozen specification, then computes the
  test split once, and checks that the fit and select numbers are unchanged by
  the presence of the test data.

Contract rolls. HO=F is a continuous front-month series: on the first session of
each calendar month its "return" compares two different contracts. That return
is treated as missing (not as a fuel move) in the primary specification; the raw
series is kept as a variant. Roll dates are inferred from the contract calendar
because Yahoo does not publish them.

Statistics come from app.lab.stats; this module arranges data and timing. Its one
addition is the forward-walking overlap rule in thin().
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass, replace
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import zlib

import numpy as np

from app.lab import stats
from app.lab.market_data import FUEL, FUEL_CSV, PRICES_CSV, load_fuel_daily, load_prices

MARKET, TARGET, HO = "SPY", "UAL", "HO=F"
PEERS = ("DAL", "AAL", "LUV", "JETS")
AIRLINES = (TARGET,) + PEERS
NAMES = {"UAL": "United", "DAL": "Delta", "AAL": "American", "LUV": "Southwest", "JETS": "the JETS airline fund"}
SPLITS = {"fit": ("2013-01-01", "2019-12-31"), "select": ("2020-01-01", "2023-12-31"), "test": ("2024-01-01", None)}
HORIZONS = (1, 2, 5, 10, 20, 60)
HEADLINE_HORIZONS = (20, 60)
BETA_WINDOW = 252
LEVEL = 0.95
MIN_OBS = 30
MIN_EVENTS = 5       # fewer events than this are reported but never worded as an effect
ROLLING_WINDOW, ROLLING_STEP = 504, 21
COST_BPS = 5.0
RULE_HOLDS, RULE_DELAYS, RULE_EVENTS = (5, 10, 20, 60), (0, 1), ("all", "thinned", "strict")
MIN_RULE_TRADES = 10
DSR_BAR = 0.95       # deflated Sharpe ratio a rule must reach to clear the haircut for the number of variants tried
DEFAULT_SEED = 20261010
SIGNAL_LABEL = {"ho_1d": "HO=F one-session return", "ho_5d": "HO=F trailing 5-session change",
                "ho_20d": "HO=F trailing 20-session change", "eia_1d": "EIA spot one-session return",
                "eia_5d": "EIA spot trailing 5-session change", "eia_20d": "EIA spot trailing 20-session change"}
TIMING = [
    "Grid: one row per US equity session; a series that did not print is missing there and is never filled.",
    "HO=F on session t is treated as known at the equity close of t and is paired with equity returns of "
    "sessions t+1 onward in every predictive test.",
    "Yahoo does not document whether its HO=F close is the 14:30 settlement or a later trade, so the primary lagged "
    "regression is also computed with one more session skipped and the trading rule is tried with both entries.",
    "The EIA spot print for session t is treated as known one business day later and is paired with equity "
    "returns of sessions t+2 onward. It is the current vintage of the series.",
    "Same-session regressions are descriptive co-movement, not tradable.",
    "Beta for session t uses sessions t-252 to t-1 only.",
    "Event thresholds use only sessions before the event; market-model windows end before the event window.",
    "A row or event belongs to a split only if its whole outcome window ends inside that split.",
    "The first session of each month is a contract roll for HO=F; its return is treated as missing.",
]


# ---------------------------------------------------------------- data

@dataclass(frozen=True, eq=False)
class Panel:
    """Levels on the equity-session grid; NaN where a source did not print."""
    dates: tuple
    levels: dict

    def upto(self, end: date) -> "Panel":
        n = bisect_right(self.dates, end)
        return Panel(self.dates[:n], {k: v[:n].copy() for k, v in self.levels.items()})


def load_panel(fuel_path: Path = FUEL_CSV, prices_path: Path = PRICES_CSV) -> Panel:
    grid, _ = load_prices(MARKET, "adj_close", prices_path)
    levels = {}
    for name in (MARKET,) + AIRLINES + (HO, FUEL):
        days, values = load_fuel_daily(fuel_path) if name == FUEL else load_prices(name, "adj_close", prices_path)
        lookup = dict(zip(days, values))
        levels[name] = np.array([lookup.get(day, np.nan) for day in grid], dtype=float)
    return Panel(tuple(grid), levels)


def log_returns(level) -> np.ndarray:
    """r[t] = log(level[t] / level[t-1]); NaN when either print is missing."""
    level = np.asarray(level, dtype=float)
    out = np.full(len(level), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        out[1:] = np.log(level[1:] / level[:-1])
    out[~np.isfinite(out)] = np.nan
    return out


@dataclass(frozen=True, eq=False)
class FuelMoves:
    one_day: np.ndarray   # one-session log return; NaN across a missing print or a contract roll
    step: np.ndarray      # log change since the previous print; 0 on a roll or where there is no print
    observed: np.ndarray  # the source printed on this session

    def trailing(self, k: int) -> np.ndarray:
        """Sum of the last k session steps, known at session t. NaN when the source
        did not print on t or fewer than k sessions of history exist."""
        n, out = len(self.step), np.full(len(self.step), np.nan)
        seen = np.flatnonzero(self.observed)
        if not len(seen):
            return out
        total = np.cumsum(self.step)
        rows = np.arange(seen[0] + k, n)
        rows = rows[self.observed[rows]]
        out[rows] = total[rows] - total[rows - k]
        return out


def fuel_moves(level, dates, skip_roll: bool) -> FuelMoves:
    """Session-by-session fuel moves from a level series with gaps. With skip_roll,
    the first print of each calendar month is a contract roll: it contributes no
    move and becomes the base for the next session."""
    level = np.asarray(level, dtype=float)
    n = len(level)
    one, step = np.full(n, np.nan), np.zeros(n)
    observed = np.isfinite(level) & (level > 0)
    previous, roll_pending = -1, False
    for t in range(n):
        if skip_roll and t > 0 and dates[t].month != dates[t - 1].month:
            roll_pending = True
        if not observed[t]:
            continue
        if previous >= 0 and not roll_pending:
            step[t] = math.log(level[t] / level[previous])
            if previous == t - 1:
                one[t] = step[t]
        previous, roll_pending = t, False
    return FuelMoves(one, step, observed)


def net_of_market(moves: FuelMoves, market_ret, window: int = BETA_WINDOW) -> FuelMoves:
    """The fuel moves minus beta x the SPY return over the same sessions, beta of
    the one-session fuel return on SPY from the previous `window` sessions only."""
    market = np.where(np.isfinite(market_ret), market_ret, 0.0)
    beta = stats.rolling_beta(moves.one_day, market_ret, window, min_obs=3 * window // 4)["beta"]
    total = np.cumsum(market)
    one, step, observed, previous = np.full(len(market), np.nan), np.zeros(len(market)), moves.observed.copy(), -1
    for t in range(len(market)):
        if not moves.observed[t]:
            continue
        if not math.isfinite(beta[t]):
            observed[t] = False
        elif previous >= 0 and (moves.step[t] != 0.0 or math.isfinite(moves.one_day[t])):   # not a roll session
            step[t] = moves.step[t] - beta[t] * (total[t] - total[previous])
            if math.isfinite(moves.one_day[t]):
                one[t] = step[t]
        previous = t
    return FuelMoves(one, step, observed)


def excess_returns(asset_ret, market_ret, window: int = BETA_WINDOW) -> tuple:
    """(excess, beta): excess[t] = asset[t] - beta[t] * market[t], beta[t] from t-window .. t-1."""
    beta = stats.rolling_beta(asset_ret, market_ret, window)["beta"]
    return np.asarray(asset_ret, dtype=float) - beta * np.asarray(market_ret, dtype=float), beta


def forward_sum(x, h: int, skip: int = 0) -> np.ndarray:
    """out[t] = x[t+1+skip] + ... + x[t+skip+h]; NaN when a term is missing or past the sample."""
    x = np.asarray(x, dtype=float)
    n, out = len(x), np.full(len(x), np.nan)
    good = np.isfinite(x)
    total = np.concatenate([[0.0], np.cumsum(np.where(good, x, 0.0))])
    missing = np.concatenate([[0], np.cumsum(~good)])
    lo = np.arange(n) + 1 + skip
    hi = lo + h
    rows = np.flatnonzero(hi <= n)
    rows = rows[missing[hi[rows]] == missing[lo[rows]]]
    out[rows] = total[hi[rows]] - total[lo[rows]]
    return out


def split_bounds(dates, splits=SPLITS) -> dict:
    """First and last grid index of each split; a split with no sessions is left out."""
    out = {}
    for name, (start, end) in splits.items():
        first = bisect_left(dates, date.fromisoformat(start))
        last = (len(dates) if end is None else bisect_right(dates, date.fromisoformat(end))) - 1
        if first <= last:
            out[name] = (first, last)
    return out


def rows_in(n: int, bound: tuple, reach: int = 0) -> np.ndarray:
    """Rows t inside the split whose outcome window, ending at t + reach, also ends inside it."""
    rows = np.zeros(n, dtype=bool)
    rows[bound[0]:max(bound[0], bound[1] - reach + 1)] = True
    return rows


def _seed(base: int, *labels) -> int:
    return zlib.crc32("|".join(str(v) for v in (base,) + labels).encode())


# ---------------------------------------------------------------- regressions

def lagged_hac_lag(n: int, horizon: int, multiple: int = 2) -> int:
    """HAC lag for an h-session overlapping target: at least `multiple` x h, never below the default."""
    return max(stats.nw_lag_default(n), multiple * horizon)


def slope(x, y, rows, hac_lag: int | None = None, level: float = LEVEL) -> dict:
    """OLS slope of y on x (with a constant) over `rows`, Newey-West errors, normal interval."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    keep = np.asarray(rows, dtype=bool) & np.isfinite(x) & np.isfinite(y)
    n = int(keep.sum())
    lag = stats.nw_lag_default(max(n, 1)) if hac_lag is None else int(hac_lag)
    out = {"coef": None, "se": None, "t_nw": None, "ci": None, "p": None, "r2": None, "n": n, "hac_lag": lag}
    if n < max(MIN_OBS, 2 * lag + 2):
        return out
    fit = stats.ols(x[keep], y[keep])
    if fit.rank_deficient:
        return out
    robust = stats.newey_west(fit, lag)
    lo, hi = stats.conf_int(fit.beta[1], robust.se[1], level)
    out.update(coef=float(fit.beta[1]), se=float(robust.se[1]), t_nw=float(robust.t[1]), ci=[lo, hi],
               p=float(stats.p_value(robust.t[1])), r2=float(fit.r2))
    return out


def up_down(x, y, rows, level: float = LEVEL) -> dict:
    """Separate slopes for rises and falls: y on max(x, 0) and min(x, 0), Newey-West errors."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    keep = np.asarray(rows, dtype=bool) & np.isfinite(x) & np.isfinite(y)
    n = int(keep.sum())
    out = {"up": None, "down": None, "up_minus_down": None, "n": n, "n_up": int((x[keep] > 0).sum()),
           "n_down": int((x[keep] < 0).sum())}
    if n < MIN_OBS:
        return out
    design = np.column_stack([np.maximum(x[keep], 0.0), np.minimum(x[keep], 0.0)])
    fit = stats.ols(design, y[keep])
    if fit.rank_deficient:
        return out
    robust = stats.newey_west(fit)
    for key, col in (("up", 1), ("down", 2)):
        lo, hi = stats.conf_int(fit.beta[col], robust.se[col], level)
        out[key] = {"coef": float(fit.beta[col]), "se": float(robust.se[col]), "t_nw": float(robust.t[col]), "ci": [lo, hi]}
    gap = stats.linear_combination(fit.beta, robust.cov, [0.0, 1.0, -1.0])
    lo, hi = stats.conf_int(gap["estimate"], gap["se"], level)
    out["up_minus_down"] = {"coef": gap["estimate"], "se": gap["se"], "t_nw": gap["t"], "ci": [lo, hi],
                            "p": float(stats.p_value(gap["t"]))}
    return out


def _slope_only(x, y) -> float:
    dx = x - x.mean()
    return float(dx @ (y - y.mean()) / (dx @ dx))


def block_interval(x, y, rows, block_len: int, n_boot: int, seed: int, level: float = LEVEL):
    """Moving-block bootstrap percentile interval for the slope; None when the sample is shorter than 3 blocks."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    keep = np.asarray(rows, dtype=bool) & np.isfinite(x) & np.isfinite(y)
    if keep.sum() < max(MIN_OBS, 3 * block_len):
        return None
    lo, hi = stats.percentile_interval(stats.block_bootstrap(_slope_only, (x[keep], y[keep]), block_len, n_boot, seed), level)
    return [lo, hi]


def lead_lag(ho, eia, first: int, last: int) -> dict:
    """Correlations of one-session HO=F and EIA spot returns at lags -1, 0, +1 over
    sessions first..last. If the spot print of session t correlates with HO=F of
    t+1 more than HO=F of t does with the spot of t+1, the spot print reflects
    news that the HO=F close of the same session does not yet show."""
    def corr(a, b):
        keep = np.isfinite(a) & np.isfinite(b)
        return {"corr": float(np.corrcoef(a[keep], b[keep])[0, 1]) if keep.sum() >= MIN_OBS else None, "n": int(keep.sum())}
    ho, eia = np.asarray(ho, dtype=float)[first:last + 1], np.asarray(eia, dtype=float)[first:last + 1]
    return {"same_session": corr(ho, eia), "ho_then_spot_next_session": corr(ho[:-1], eia[1:]),
            "spot_then_ho_next_session": corr(eia[:-1], ho[1:])}


def rolling_same_day(excess, fuel_ret, dates, first: int, last: int, window: int = ROLLING_WINDOW,
                     step: int = ROLLING_STEP) -> list:
    """Same-session slope over trailing `window`-session windows ending every `step` sessions."""
    out = []
    for end in range(first + window - 1, last + 1, step):
        rows = np.zeros(len(dates), dtype=bool)
        rows[end - window + 1:end + 1] = True
        fit = slope(fuel_ret, excess, rows)
        if fit["coef"] is not None:
            out.append({"start": dates[end - window + 1].isoformat(), "end": dates[end].isoformat(), "beta": fit["coef"],
                        "lo": fit["ci"][0], "hi": fit["ci"][1], "t_nw": fit["t_nw"], "n": fit["n"]})
    return out


# ---------------------------------------------------------------- events

@dataclass(frozen=True)
class EventSpec:
    fuel: str = HO                 # HO=F, or FUEL for the EIA spot (then known one session later)
    skip_roll: bool = True
    change_days: int = 5
    quantile: float = 0.95
    threshold_window: int | None = None   # None: expanding; otherwise the last N sessions before t
    min_history: int = 252
    quiet_days: int = 5
    overlap: str = "greedy"               # see thin(); or one of stats.OVERLAP_RULES
    thin_within_side: bool = False
    k: int = 5
    h: int = 60
    est_len: int = 250
    est_exclude: str = "none"             # or "event_windows": drop sessions inside any event window from estimation
    abnormal: str = "beta_only"           # market model without its intercept; or "market_model" with it


@dataclass(frozen=True, eq=False)
class EventSet:
    signal: np.ndarray       # trailing fuel change as known at each session
    threshold: np.ndarray    # quantile of |signal| over earlier sessions only
    crossing: np.ndarray     # |signal| > threshold
    episodes: np.ndarray     # first crossing after quiet_days without one (grid indices)
    side: np.ndarray         # +1 fuel rise, -1 fuel fall, per episode
    kept: np.ndarray         # survives the overlap rule
    overlapping: np.ndarray  # another episode lies within k + h sessions


def event_threshold(signal, quantile: float, min_history: int, window: int | None = None) -> np.ndarray:
    """threshold[t] = quantile of |signal| over sessions before t (all of them, or the last `window`)."""
    size = np.abs(np.asarray(signal, dtype=float))
    out = np.full(len(size), np.nan)
    for t in range(len(size)):
        past = size[(0 if window is None else max(0, t - window)):t]
        past = past[np.isfinite(past)]
        if len(past) >= min_history:
            out[t] = np.quantile(past, quantile)
    return out


def thin(events, span: int, rule: str) -> dict:
    """Overlap rules. "greedy" (the primary rule, not in stats.py) walks forward in
    time and keeps an event unless a kept event lies within `span` sessions before
    it, so the windows of kept events never intersect and no later event is
    consulted. The other rules are stats.thin_events: "drop_later" is stricter
    (an earlier event within span removes this one whether or not it was kept),
    "drop_all" conditions on later events, "flag" keeps everything."""
    if rule != "greedy":
        return stats.thin_events(events, span, rule)
    flagged = stats.thin_events(events, span, "flag")
    kept = []
    for e in flagged["events"].tolist():
        if not kept or e - kept[-1] > span:
            kept.append(e)
    kept = np.array(kept, dtype=int)
    return {"events": flagged["events"], "overlapping": flagged["overlapping"], "kept": kept,
            "dropped": np.setdiff1d(flagged["events"], kept)}


def define_events(signal, spec: EventSpec, threshold=None) -> EventSet:
    signal = np.asarray(signal, dtype=float)
    if threshold is None:
        threshold = event_threshold(signal, spec.quantile, spec.min_history, spec.threshold_window)
    with np.errstate(invalid="ignore"):
        crossing = np.isfinite(signal) & np.isfinite(threshold) & (np.abs(signal) > threshold)
    episodes = np.array([t for t in np.flatnonzero(crossing) if not crossing[max(0, t - spec.quiet_days):t].any()], dtype=int)
    side = np.sign(signal[episodes]).astype(int)
    kept, overlapping = np.zeros(len(episodes), dtype=bool), np.zeros(len(episodes), dtype=bool)
    groups = [side == 1, side == -1] if spec.thin_within_side else [np.ones(len(episodes), dtype=bool)]
    for group in groups:
        thinned = thin(episodes[group], spec.k + spec.h, spec.overlap)
        kept[group] = np.isin(episodes[group], thinned["kept"])
        overlapping[group] = thinned["overlapping"]
    return EventSet(signal, threshold, crossing, episodes, side, kept, overlapping)


def _mean_stats(values, seed: int, n_boot: int, level: float = LEVEL) -> dict:
    """Mean over events with a t interval (df = n - 1) and a bootstrap interval over events.
    `interval` is the wider of the two; with few events both undercover."""
    values = np.asarray(values, dtype=float)
    n = len(values)
    out = {"mean": float(values.mean()) if n else None, "n": n, "se": None, "t": None, "ci_t": None, "ci_boot": None,
           "interval": None, "share_positive": float((values > 0).mean()) if n else None}
    if n < 2 or not values.std(ddof=1) > 0:
        return out
    se = float(values.std(ddof=1) / math.sqrt(n))
    t_lo, t_hi = stats.conf_int(out["mean"], se, level, df=n - 1)
    b_lo, b_hi = stats.percentile_interval(stats.pairs_bootstrap(np.mean, values, n_boot, seed), level)
    out.update(se=se, t=out["mean"] / se, ci_t=[t_lo, t_hi], ci_boot=[b_lo, b_hi], interval=[min(t_lo, b_lo), max(t_hi, b_hi)])
    return out


def study(asset_ret, market_ret, events, spec: EventSpec, est_exclude, dates, seed: int, n_boot: int,
          level: float = LEVEL, with_path: bool = True) -> dict:
    """Event study for a fixed list of events (the overlap rule has already been
    applied). Paths cumulate abnormal returns from day -k; `post` cumulates from
    day +1, after the fuel move was known."""
    events = np.asarray(events, dtype=int)
    out = {"n_events": 0, "n_input": int(len(events)), "dropped": [], "event_dates": [], "path": [], "signal_window": None,
           "day0": None, "post": {str(h): None for h in HEADLINE_HORIZONS}}
    if not len(events):
        return out
    res = stats.event_study(asset_ret, market_ret, events, spec.k, spec.h, overlap="flag", seed=seed, est_len=spec.est_len,
                            est_exclude=est_exclude, n_boot=n_boot, level=level)
    out["dropped"] = [{"date": dates[d["index"]].isoformat(), "reason": d["reason"]} for d in res.dropped]
    out["n_events"] = res.n_events
    if not res.n_events:
        return out
    k = spec.k
    if spec.abnormal == "beta_only":
        ar = res.ar + res.alpha[:, None]   # r - beta x market: the intercept is put back
        car = np.cumsum(ar, axis=1)
        mean, lo, hi = car.mean(axis=0), np.full(car.shape[1], np.nan), np.full(car.shape[1], np.nan)
        if res.n_events >= 2:
            lo, hi = stats.percentile_interval(stats.pairs_bootstrap(lambda c: c.mean(axis=0), car, n_boot, seed), level)
    else:
        ar, car, mean, lo, hi = res.ar, res.car, res.mean_car, res.lo, res.hi
    out["event_dates"] = [dates[e].isoformat() for e in res.events]
    if with_path:
        out["path"] = [{"day": int(d), "mean": float(m), "lo": None if math.isnan(a) else float(a),
                        "hi": None if math.isnan(b) else float(b)} for d, m, a, b in zip(res.days, mean, lo, hi)]
    out["signal_window"] = _mean_stats(car[:, k] - car[:, 0], _seed(seed, "signal_window"), n_boot, level)
    out["day0"] = _mean_stats(ar[:, k], _seed(seed, "day0"), n_boot, level)
    for h in HEADLINE_HORIZONS:
        if h <= spec.h:
            out["post"][str(h)] = _mean_stats(car[:, k + h] - car[:, k], _seed(seed, "post", h), n_boot, level)
    return out


# ---------------------------------------------------------------- trading rule

@dataclass(frozen=True)
class RuleSpec:
    side: str          # "up": trade after fuel-rise events; "down": after fuel-fall events; "both"
    bet: str           # "rebound": long after a rise, short after a fall; "drift": short after a rise, long after a fall
    hold: int          # sessions held
    delay: int         # 0: enter at the close of the event session; 1: one session later
    events: str = "all"  # "all": every event; "thinned": kept by the primary overlap rule; "strict": kept by stats "drop_later"
    cost_bps: float = COST_BPS

    @property
    def label(self) -> str:
        return f"{self.bet} bet after {self.side} events ({self.events}), hold {self.hold}, entry +{self.delay}"

    def position(self, event_side: int) -> int:
        return event_side if self.bet == "rebound" else -event_side


def rule_returns(asset_ret, market_ret, beta, events, sides, limits, rule: RuleSpec, dates) -> dict:
    """Daily returns of the rule on the whole grid (zero when flat).

    For an event at session e the position is held over sessions e+1+delay ..
    e+delay+hold. Each day earns position x (R_asset - beta x R_SPY) in simple
    returns with the past-only beta of that day. Costs: cost_bps of traded
    notional on both legs, (1 + |beta|) x cost_bps, charged on the first and on
    the last day held. A trade is taken only if it can be closed by `limits`
    (the last session of the event's split) and no position is open."""
    simple_a, simple_m = np.expm1(asset_ret), np.expm1(market_ret)
    n = len(simple_a)
    net, gross, trades, busy_until = np.zeros(n), np.zeros(n), [], -1
    cost = rule.cost_bps / 1e4
    for e, side, limit in zip(np.asarray(events, dtype=int), np.asarray(sides, dtype=int), np.asarray(limits, dtype=int)):
        first, last = e + 1 + rule.delay, e + rule.delay + rule.hold
        if last > limit or last >= n or e < busy_until:
            continue
        days = np.arange(first, last + 1)
        daily = rule.position(int(side)) * (simple_a[days] - beta[days] * simple_m[days])
        if not np.isfinite(daily).all():
            continue
        gross[days] = daily
        net[days] = daily
        net[first] -= cost * (1.0 + abs(beta[first]))
        net[last] -= cost * (1.0 + abs(beta[last]))
        trades.append({"event": dates[e].isoformat(), "entry": dates[first - 1].isoformat(), "exit": dates[last].isoformat(),
                       "position": "long" if rule.position(int(side)) > 0 else "short", "index": int(e),
                       "gross": float(gross[days].sum()), "net": float(net[days].sum())})
        busy_until = last
    return {"net": net, "gross": gross, "trades": trades}


def rule_stats(run: dict, bound: tuple) -> dict:
    first, last = bound
    net, gross = run["net"][first:last + 1], run["gross"][first:last + 1]
    trades = [t for t in run["trades"] if first <= t["index"] <= last]
    out = {"n_trades": len(trades), "n_days": int(len(net)), "days_in_market": int((gross != 0).sum()), "sharpe_ann": None,
           "se_ann": None, "ci": None, "sharpe_ann_gross": None, "mean_trade_net": None, "mean_trade_gross": None,
           "hit_rate": None, "total_net": float(net.sum())}
    if not trades:
        return out
    s, g = stats.sharpe_stats(net), stats.sharpe_stats(gross)
    out.update(mean_trade_net=float(np.mean([t["net"] for t in trades])), mean_trade_gross=float(np.mean([t["gross"] for t in trades])),
               hit_rate=float(np.mean([t["net"] > 0 for t in trades])))
    if math.isfinite(s.sharpe_ann) and math.isfinite(s.se_ann):
        out.update(sharpe_ann=s.sharpe_ann, se_ann=s.se_ann, ci=list(stats.conf_int(s.sharpe_ann, s.se_ann, LEVEL)),
                   sharpe_ann_gross=g.sharpe_ann if math.isfinite(g.sharpe_ann) else None)
    return out


def rule_grid() -> list:
    return [RuleSpec(side, bet, hold, delay, events) for events in RULE_EVENTS for side in ("up", "down", "both")
            for bet in ("rebound", "drift") for hold in RULE_HOLDS for delay in RULE_DELAYS]


def choose_rule(rows: list, min_trades: int = MIN_RULE_TRADES) -> int | None:
    """Index of the variant with the highest net annualised Sharpe on the select
    split among those with at least `min_trades` trades there."""
    scored = [(row["select"]["sharpe_ann"], -i) for i, row in enumerate(rows)
              if row["select"]["sharpe_ann"] is not None and row["select"]["n_trades"] >= min_trades]
    return -max(scored)[1] if scored else None


# ---------------------------------------------------------------- prepared series

@dataclass(frozen=True, eq=False)
class Prepared:
    dates: tuple
    ret: dict       # log returns by name
    excess: dict    # ticker -> excess return
    beta: dict      # ticker -> past-only beta
    same_day: dict  # "ho" / "eia" -> one-session fuel return (descriptive use only)
    signals: dict   # name -> (array as observed at t, sessions to wait before it is known)


def prepare(panel: Panel, beta_window: int = BETA_WINDOW, skip_roll: bool = True, tickers=AIRLINES,
            fuel_net_of_market: bool = False) -> Prepared:
    ret = {name: log_returns(panel.levels[name]) for name in (MARKET,) + tuple(tickers)}
    excess, beta = {}, {}
    for ticker in tickers:
        excess[ticker], beta[ticker] = excess_returns(ret[ticker], ret[MARKET], beta_window)
    ho, eia = fuel_moves(panel.levels[HO], panel.dates, skip_roll), fuel_moves(panel.levels[FUEL], panel.dates, False)
    if fuel_net_of_market:
        ho = net_of_market(ho, ret[MARKET], beta_window)
    signals = {"ho_1d": (ho.one_day, 0), "ho_5d": (ho.trailing(5), 0), "ho_20d": (ho.trailing(20), 0),
               "eia_1d": (eia.one_day, 1), "eia_5d": (eia.trailing(5), 1), "eia_20d": (eia.trailing(20), 1)}
    return Prepared(panel.dates, ret, excess, beta, {"ho": ho.one_day, "eia": eia.one_day}, signals)


def events_for(panel: Panel, spec: EventSpec, cache: dict | None = None) -> EventSet:
    """Events of a specification. `cache` reuses the signal and threshold between
    specifications that differ only in the overlap rule or the abnormal-return model."""
    key = (spec.fuel, spec.skip_roll, spec.change_days, spec.quantile, spec.threshold_window, spec.min_history)
    if cache is None or key not in cache:
        if spec.fuel == HO:
            signal = fuel_moves(panel.levels[HO], panel.dates, spec.skip_roll).trailing(spec.change_days)
        else:   # the EIA print for session t is known one session later
            raw = fuel_moves(panel.levels[FUEL], panel.dates, False).trailing(spec.change_days)
            signal = np.full(len(raw), np.nan)
            signal[1:] = raw[:-1]
        made = (signal, event_threshold(signal, spec.quantile, spec.min_history, spec.threshold_window))
        if cache is None:
            return define_events(made[0], spec, made[1])
        cache[key] = made
    return define_events(cache[key][0], spec, cache[key][1])


def rule_event_sets(panel: Panel, spec: EventSpec, cache: dict | None = None) -> dict:
    """Which events each family of rule variants may trade: name -> (EventSet, tradable mask)."""
    primary, strict = events_for(panel, spec, cache), events_for(panel, replace(spec, overlap="drop_later"), cache)
    return {"all": (primary, np.ones(len(primary.episodes), dtype=bool)), "thinned": (primary, primary.kept),
            "strict": (strict, strict.kept)}


def samples_for(dates, splits=SPLITS, with_all: bool = True) -> dict:
    bounds = split_bounds(dates, splits)
    if with_all and bounds:
        bounds["all"] = (min(b[0] for b in bounds.values()), max(b[1] for b in bounds.values()))
    return bounds


# ---------------------------------------------------------------- the analyses

def analyse_same_day(prep: Prepared, ticker: str, samples: dict) -> dict:
    n, out = len(prep.dates), {}
    for name, bound in samples.items():
        rows = rows_in(n, bound)
        out[name] = {"ho": slope(prep.same_day["ho"], prep.excess[ticker], rows),
                     "eia": slope(prep.same_day["eia"], prep.excess[ticker], rows),
                     "ho_up_down": up_down(prep.same_day["ho"], prep.excess[ticker], rows),
                     "start": prep.dates[bound[0]].isoformat(), "end": prep.dates[bound[1]].isoformat()}
    return out


def analyse_lagged(prep: Prepared, ticker: str, samples: dict, signal: str, horizons=HORIZONS, extra_skip: int = 0,
                   lag_multiple: int = 2, block: tuple | None = None) -> dict:
    """Cumulative excess return over the next h sessions on a fuel signal, by sample.
    block = (n_boot, seed) adds a moving-block bootstrap interval with block 2h + 1."""
    values, wait = prep.signals[signal]
    n, out = len(prep.dates), {}
    for name, bound in samples.items():
        rows_out = []
        for h in horizons:
            skip = wait + extra_skip
            target = forward_sum(prep.excess[ticker], h, skip)
            rows = rows_in(n, bound, skip + h)
            fit = slope(values, target, rows, lagged_hac_lag(int((rows & np.isfinite(values) & np.isfinite(target)).sum() or 1),
                                                             h, lag_multiple))
            fit["horizon_days"] = h
            if block is not None:
                fit["ci_block"] = block_interval(values, target, rows, 2 * h + 1, block[0], _seed(block[1], ticker, signal, name, h))
            fit["interval"] = decision_interval(fit)
            rows_out.append(fit)
        out[name] = rows_out
    return out


def decision_interval(fit: dict):
    """The interval the wording is decided on: the widest of those computed."""
    spans = [fit.get(key) for key in ("ci", "ci_block", "ci_t", "ci_boot") if fit.get(key)]
    return [min(s[0] for s in spans), max(s[1] for s in spans)] if spans else None


def estimation_mask(n: int, events: EventSet, spec: EventSpec):
    """Sessions never used to estimate the market model (None: no exclusion)."""
    return stats.event_window_mask(n, events.episodes, spec.k, spec.h) if spec.est_exclude == "event_windows" else None


def event_members(events: EventSet, bound: tuple, h: int, side: int, kept_only: bool) -> np.ndarray:
    """Episodes of one side whose event day lies in the sample and whose window ends inside it."""
    e = events.episodes
    pick = (events.side == side) & (e >= bound[0]) & (e + h <= bound[1])
    return e[pick & events.kept] if kept_only else e[pick]


def analyse_events(prep: Prepared, ticker: str, samples: dict, events: EventSet, spec: EventSpec, seed: int, n_boot: int,
                   rules=("thinned", "flagged"), abnormal=("beta_only", "market_model"), paths=True) -> dict:
    """side -> overlap rule -> abnormal-return model -> sample -> study()."""
    exclude = estimation_mask(len(prep.dates), events, spec)
    out = {}
    for side_name, side in (("up", 1), ("down", -1)):
        out[side_name] = {}
        for rule in rules:
            out[side_name][rule] = {}
            for model in abnormal:
                cell = {}
                for name, bound in samples.items():
                    members = event_members(events, bound, spec.h, side, rule == "thinned")
                    cell[name] = study(prep.ret[ticker], prep.ret[MARKET], members, replace(spec, abnormal=model), exclude,
                                       prep.dates, _seed(seed, ticker, side_name, rule, model, name), n_boot,
                                       with_path=paths and model == spec.abnormal)
                out[side_name][rule][model] = cell
    return out


def event_counts(events: EventSet, samples: dict, h: int) -> dict:
    out = {}
    for name, bound in samples.items():
        inside = (events.episodes >= bound[0]) & (events.episodes <= bound[1])
        whole = inside & (events.episodes + h <= bound[1])
        out[name] = {"crossing_sessions": int(events.crossing[bound[0]:bound[1] + 1].sum()),
                     "episodes": int(inside.sum()), "episodes_window_inside": int(whole.sum()),
                     "purged_at_boundary": int((inside & ~whole).sum()),
                     "flagged": {"up": int((whole & (events.side == 1)).sum()), "down": int((whole & (events.side == -1)).sum())},
                     "thinned": {"up": int((whole & events.kept & (events.side == 1)).sum()),
                                 "down": int((whole & events.kept & (events.side == -1)).sum())}}
    return out


def run_rule(prep: Prepared, ticker: str, event_sets: dict, samples: dict, rule: RuleSpec) -> dict:
    """The rule on the events of its family and side, in time order, one position
    at a time. A trade must close inside its event's split."""
    events, tradable = event_sets[rule.events]
    wanted = {"up": (1,), "down": (-1,), "both": (1, -1)}[rule.side]
    splits = {k: v for k, v in samples.items() if k != "all"}
    picked, sides, limits = [], [], []
    for e, side in zip(events.episodes[tradable], events.side[tradable]):
        for bound in splits.values():
            if side in wanted and bound[0] <= e <= bound[1]:
                picked.append(int(e))
                sides.append(int(side))
                limits.append(bound[1])
    run = rule_returns(prep.ret[ticker], prep.ret[MARKET], prep.beta[ticker], picked, sides, limits, rule, prep.dates)
    return {"run": run, **{name: rule_stats(run, bound) for name, bound in samples.items()}}


def compute(panel: Panel, samples: dict, spec: EventSpec, seed: int, n_boot_events: int, n_boot_block: int,
            tickers=AIRLINES, target: str = TARGET, rule: RuleSpec | None = None) -> dict:
    """Everything reported for the frozen specification and its declared secondary
    displays, for the given samples. Called once with fit and select (panel cut
    at the end of select) and once with every sample."""
    prep = prepare(panel, tickers=tickers)
    events = events_for(panel, spec)
    out = {"same_day": {}, "lagged": {}, "events": {}, "event_counts": event_counts(events, samples, spec.h)}
    for ticker in tickers:
        main = ticker == target
        out["same_day"][ticker] = analyse_same_day(prep, ticker, samples)
        out["lagged"][ticker] = {"ho_5d": analyse_lagged(prep, ticker, samples, "ho_5d",
                                                         block=(n_boot_block, seed) if main else None)}
        if main:
            for signal in ("ho_1d", "ho_20d", "eia_1d", "eia_5d", "eia_20d"):
                out["lagged"][ticker][signal] = analyse_lagged(prep, ticker, samples, signal)
            out["lagged"][ticker]["ho_5d_skip1"] = analyse_lagged(prep, ticker, samples, "ho_5d", extra_skip=1)
        out["events"][ticker] = analyse_events(prep, ticker, samples, events, spec, seed, n_boot_events,
                                               rules=("thinned", "flagged") if main else ("thinned",),
                                               abnormal=("beta_only", "market_model") if main else (spec.abnormal,), paths=main)
    out["event_list"] = [{"date": prep.dates[e].isoformat(), "side": "up" if s > 0 else "down", "change": float(events.signal[e]),
                          "threshold": float(events.threshold[e]), "kept": bool(k), "overlapping": bool(o)}
                         for e, s, k, o in zip(events.episodes, events.side, events.kept, events.overlapping)
                         if any(b[0] <= e <= b[1] for b in samples.values())]
    known = events.threshold[np.isfinite(events.threshold)]
    out["threshold_latest"] = float(known[-1]) if len(known) else None
    if rule is not None:
        out["rule"] = run_rule(prep, target, rule_event_sets(panel, spec), samples, rule)
    out["rolling"] = None
    if "all" in samples:
        first, last = samples["all"]
        out["rolling"] = rolling_same_day(prep.excess[target], prep.same_day["ho"], prep.dates, first, last)
    return out


# ---------------------------------------------------------------- variants (fit and select only)

def _brief(fit: dict | None) -> dict | None:
    if not fit:
        return None
    keys = ("coef", "t_nw", "ci", "n", "hac_lag", "mean", "t", "interval", "ci_block")
    return {k: fit[k] for k in keys if k in fit}


def _pick(rows: list, horizon: int) -> dict:
    return next(r for r in rows if r["horizon_days"] == horizon)


def explore(panel: Panel, splits: dict, spec: EventSpec, seed: int, n_boot_events: int, target: str = TARGET) -> dict:
    """Every variant that was computed, on fit and select only, plus the choice of
    trading rule. `panel` must already be cut at the end of the select split."""
    samples = {k: v for k, v in samples_for(panel.dates, splits, with_all=False).items() if k in ("fit", "select")}
    if panel.dates[-1] > date.fromisoformat(splits["select"][1]):
        raise ValueError("explore() must not be given data after the select split")
    variants, n = [], len(panel.dates)
    base = prepare(panel, tickers=(target,))

    def regression_variant(vid: str, family: str, text: str, prep: Prepared, lag_multiple: int = 2, extra_skip: int = 0,
                           signal: str = "ho_5d"):
        lagged = analyse_lagged(prep, target, samples, signal, HEADLINE_HORIZONS, extra_skip, lag_multiple)
        same = analyse_same_day(prep, target, samples)
        variants.append({"id": vid, "family": family, "description": text, "test": "not computed",
                         **{s: {"same_day": _brief(same[s]["ho"]), "lagged": {str(h): _brief(_pick(lagged[s], h))
                                                                              for h in HEADLINE_HORIZONS}} for s in samples}})

    regression_variant("primary", "primary", "primary specification", base)
    for window in (126, 504):
        regression_variant(f"beta_window_{window}", "beta window", f"beta from the previous {window} sessions instead of {BETA_WINDOW}",
                           prepare(panel, window, tickers=(target,)))
    raw = prepare(panel, skip_roll=False, tickers=(target,))
    regression_variant("ho_raw_rolls", "fuel proxy", "HO=F with contract-roll sessions kept as fuel moves", raw)
    regression_variant("ho_net_of_market", "fuel proxy", "HO=F move net of its own past-only beta to SPY",
                       prepare(panel, tickers=(target,), fuel_net_of_market=True))
    regression_variant("hac_lag_h", "HAC lag", "HAC lag equal to the horizon (the minimum) instead of twice the horizon",
                       base, lag_multiple=1)
    regression_variant("skip_one_session", "timing", "one more session skipped before returns start", base, extra_skip=1)
    for signal in ("ho_1d", "ho_20d", "eia_1d", "eia_5d", "eia_20d"):
        regression_variant(f"signal_{signal}", "lagged signal", f"lagged signal: {SIGNAL_LABEL[signal]}", base, signal=signal)
    eia_same = analyse_same_day(base, target, samples)
    for v in variants:   # the same-session number shown for the EIA variants is the EIA one
        if v["id"].startswith("signal_eia"):
            for s in samples:
                v[s]["same_day"] = _brief(eia_same[s]["eia"])

    bases = [("", spec),
             ("second development pass (market-model intercept kept): ", replace(spec, abnormal="market_model")),
             ("first development pass (strict thinning, estimation without event-window sessions, intercept kept): ",
              replace(spec, overlap="drop_later", est_exclude="event_windows", abnormal="market_model"))]
    changes = [
        ("primary", "as specified", {}),
        ("flag", "overlapping events kept and flagged (no thinning)", {"overlap": "flag"}),
        ("greedy", "thinning by the primary rule (kept events at least 66 sessions apart)", {"overlap": "greedy"}),
        ("drop_later", "strict thinning: dropped when any earlier event, kept or not, lies within 65 sessions", {"overlap": "drop_later"}),
        ("drop_all", "every event with a neighbour within 65 sessions dropped (conditions on later events, so not usable as "
                     "a signal)", {"overlap": "drop_all"}),
        ("thin_within_side", "thinning applied to rises and falls separately", {"thin_within_side": True}),
        ("est_exclude", "market model estimated without sessions inside any event window", {"est_exclude": "event_windows"}),
        ("est_all", "market model estimated on all 250 prior sessions", {"est_exclude": "none"}),
        ("q90", "threshold at the 90th percentile", {"quantile": 0.90}),
        ("q975", "threshold at the 97.5th percentile", {"quantile": 0.975}),
        ("20d", "20-session fuel change instead of 5", {"change_days": 20, "quiet_days": 20}),
        ("rolling_756", "threshold from the previous 756 sessions instead of all earlier sessions", {"threshold_window": 756}),
        ("est_120", "market model estimated on 120 sessions instead of 250", {"est_len": 120}),
        ("intercept", "abnormal return with the market-model intercept estimated before the event", {"abnormal": "market_model"}),
        ("no_intercept", "abnormal return without the market-model intercept", {"abnormal": "beta_only"}),
        ("ho_raw_rolls", "events from HO=F with contract-roll sessions kept", {"skip_roll": False}),
        ("eia", "events from the EIA spot series, dated one session later", {"fuel": FUEL}),
    ]
    cache, seen = {}, set()
    for b, (prefix, start) in enumerate(bases):
        for cid, text, change in changes:
            variant = replace(start, **change)
            if variant in seen:
                continue
            seen.add(variant)
            vid = "event_primary" if variant == spec else f"event_{'' if not b else f'pass{3 - b}_'}{cid}"
            events = events_for(panel, variant, cache)
            exclude = estimation_mask(n, events, variant)
            row = {"id": vid, "family": "event definition", "description": prefix + text, "test": "not computed",
                   "differs": {k: v for k, v in asdict(variant).items() if v != getattr(spec, k)}}
            for s, bound in samples.items():
                row[s] = {}
                for side_name, side in (("up", 1), ("down", -1)):
                    members = event_members(events, bound, variant.h, side, kept_only=True)
                    got = study(base.ret[target], base.ret[MARKET], members, variant, exclude, base.dates,
                                _seed(seed, "variant", vid, side_name, s), n_boot_events, with_path=False)
                    row[s][side_name] = {"n_events": got["n_events"], **{f"post_{h}": _brief(got["post"][str(h)])
                                                                         for h in HEADLINE_HORIZONS}}
            variants.append(row)

    rules, event_sets = [], rule_event_sets(panel, spec, cache)
    keep = ("n_trades", "sharpe_ann", "se_ann", "sharpe_ann_gross", "mean_trade_net", "hit_rate")
    for rule in rule_grid():
        got = run_rule(base, target, event_sets, samples, rule)
        rules.append({"id": "rule: " + rule.label, "family": "trading rule", "description": rule.label, "rule": asdict(rule),
                      "test": "not computed", **{s: {k: got[s][k] for k in keep} for s in samples}})
    chosen = choose_rule(rules)
    for row in variants:
        if row["id"] in ("primary", "event_primary"):
            row["test"] = "computed once, after freezing"
    if chosen is not None:
        rules[chosen]["test"] = "computed once, after freezing"
    sharpes = [r["select"]["sharpe_ann"] for r in rules if r["select"]["sharpe_ann"] is not None]
    span = (min(b[0] for b in samples.values()), max(b[1] for b in samples.values()))
    return {"variants": variants + rules, "chosen_rule": None if chosen is None else rules[chosen]["rule"],
            "timing_check": lead_lag(base.same_day["ho"], base.same_day["eia"], *span),
            "n_rule_variants": len(rules), "n_rule_variants_eligible": sum(r["select"]["n_trades"] >= MIN_RULE_TRADES for r in rules),
            "select_sharpe_var_ann": float(np.var(sharpes, ddof=1)) if len(sharpes) > 1 else None,
            "samples": {k: [panel.dates[v[0]].isoformat(), panel.dates[v[1]].isoformat()] for k, v in samples.items()}}


# ---------------------------------------------------------------- wording (generated from numbers only)

def spans_zero(interval) -> bool:
    """True unless both ends are finite and on the same side of zero."""
    if not interval or any(v is None or not math.isfinite(v) for v in interval):
        return True
    return not (interval[0] > 0 or interval[1] < 0)


def _pct(value: float, digits: int = 1) -> str:
    return f"{100 * value:.{digits}f}%"


def _signed(value: float, digits: int = 1) -> str:
    return f"{100 * value:+.{digits}f}%"


def _range(interval, scale: float = 1.0) -> str:
    return f"{_signed(interval[0] * scale)} to {_signed(interval[1] * scale)}"


def classify_after(fit: dict | None) -> str:
    """under_reaction: after a fuel rise the stock keeps falling (negative, interval excludes zero).
    over_reaction: it rises back (positive, interval excludes zero). Otherwise neither. A mean
    over fewer than MIN_EVENTS events is too_few whatever its interval."""
    value = None if not fit else fit.get("coef", fit.get("mean"))
    if value is None:
        return "not_computed"
    if "mean" in fit and fit.get("n", 0) < MIN_EVENTS:
        return "too_few"
    if spans_zero(fit.get("interval") or fit.get("ci")):
        return "neither"
    return "under_reaction" if value < 0 else "over_reaction"


def overall_verdict(labels: dict) -> str:
    found = {v for v in labels.values() if v in ("under_reaction", "over_reaction")}
    if not found:
        return "not_computed" if all(v == "not_computed" for v in labels.values()) else "neither"
    return found.pop() if len(found) == 1 else "mixed"


def same_day_sentence(fit: dict, name: str, period: str) -> str:
    if fit.get("coef") is None:
        return f"The same-day link between heating-oil futures and {name}'s market-hedged share return was not computed for {period}."
    move, lo_hi = fit["coef"] * 0.10, _range(fit["ci"], 0.10)
    if spans_zero(fit["ci"]):
        return (f"The same-day link between heating-oil futures and {name}'s market-hedged share return in {period} is not "
                f"distinguishable from zero ({_signed(move)} per 10% fuel rise, 95% interval {lo_hi}, n = {fit['n']} sessions).")
    return (f"In {period} a 10% one-day rise in heating-oil futures went with a {_pct(abs(move))} "
            f"{'fall' if move < 0 else 'rise'} in {name}'s market-hedged share return on the same day "
            f"(95% interval {lo_hi}, n = {fit['n']} sessions).")


def after_sentence(fits: dict, same_day: dict, name: str, period: str) -> str:
    """fits: horizon -> lagged regression row (coef per unit of the trailing 5-session fuel change)."""
    computed = {h: f for h, f in fits.items() if f and f.get("coef") is not None}
    if not computed:
        return f"The after-effect of a fuel move on {name}'s share return was not computed for {period}."
    parts = [f"{h} sessions: {_signed(f['coef'] * 0.10)}, 95% interval {_range(f['interval'], 0.10)}" for h, f in computed.items()]
    labels = {h: classify_after(f) for h, f in computed.items()}
    detail = "; ".join(parts) + f"; n = {min(f['n'] for f in computed.values())} to {max(f['n'] for f in computed.values())} overlapping sessions"
    lead = f"After a 10% five-session rise in heating-oil futures in {period}, {name}'s market-hedged return"
    if all(v == "neither" for v in labels.values()):
        spans = " and ".join(str(h) for h in computed)
        return (f"{lead} over the next {spans} sessions is not distinguishable from zero: neither a continued fall nor a "
                f"rebound ({detail}).")
    initial_fall = same_day.get("coef") is not None and not spans_zero(same_day["ci"]) and same_day["coef"] < 0
    phrases = []
    for h, label in labels.items():
        if label == "under_reaction":
            phrases.append(f"was negative over the next {h} sessions" + (" (a continued fall: under-reaction)" if initial_fall else ""))
        elif label == "over_reaction":
            phrases.append(f"was positive over the next {h} sessions" + (" (a rebound: over-reaction)" if initial_fall else ""))
        else:
            phrases.append(f"is not distinguishable from zero over the next {h} sessions")
    return f"{lead} {' and '.join(phrases)} ({detail})."


def event_sentence(up: dict, counts: dict, name: str, period: str, horizon: int = 60) -> str:
    n = up["n_events"]
    lead = (f"Large fuel rises (five-session heating-oil change above its own past 95th percentile, first of each cluster): "
            f"{n} event{'s' if n != 1 else ''} in {period} with non-overlapping windows, {counts['flagged']['up']} before thinning")
    post = up["post"].get(str(horizon))
    if not post or post.get("interval") is None or n < MIN_EVENTS:
        return f"{lead}; too few to judge what {name}'s shares did over the next {horizon} sessions."
    body = f"{_signed(post['mean'])} on average, 95% interval {_range(post['interval'])}"
    if spans_zero(post["interval"]):
        return f"{lead}; {name}'s abnormal return over the next {horizon} sessions is not distinguishable from zero ({body})."
    return (f"{lead}; {name}'s abnormal return over the next {horizon} sessions was {body} "
            f"({'continued fall' if post['mean'] < 0 else 'rebound'}).")


def rule_verdict(test: dict | None, deflated: dict | None) -> str:
    """no_trades, lost (Sharpe interval below zero), not_distinguishable, positive_below_bar
    (interval above zero after costs, deflated Sharpe ratio under DSR_BAR) or survives."""
    if not test or test.get("sharpe_ann") is None:
        return "no_trades"
    if spans_zero(test["ci"]):
        return "not_distinguishable"
    if test["ci"][1] < 0:
        return "lost"
    return "survives" if deflated and deflated.get("dsr") is not None and deflated["dsr"] >= DSR_BAR else "positive_below_bar"


def rule_text(rule: dict) -> str:
    rise, fall = ("long", "short") if rule["bet"] == "rebound" else ("short", "long")
    legs = {"up": f"{rise} after a large fuel rise", "down": f"{fall} after a large fuel fall",
            "both": f"{rise} after a large fuel rise and {fall} after a large fuel fall"}[rule["side"]]
    entry = "the same close" if rule["delay"] == 0 else "the next close"
    family = {"all": "", "thinned": " (thinned events)", "strict": " (strictly thinned events)"}[rule.get("events", "all")]
    return f"UAL hedged with SPY, {legs}{family}, entered at {entry} and held {rule['hold']} sessions"


def rule_sentence(rule: dict | None, by_split: dict | None, deflated: dict | None, n_variants: int, last: str = "test") -> str:
    """by_split: split -> rule_stats(); deflated: split -> stats.deflated_sharpe(). The wording is decided on `last`."""
    if not rule or not by_split or last not in by_split:
        return "No trading rule was evaluated."
    test, deflated = by_split[last], deflated or {}
    lead = (f"Trading-rule check ({rule_text(rule)}; picked from {n_variants} variants on the select split; "
            f"{rule['cost_bps']:g} basis points per side)")
    verdict = rule_verdict(test, deflated.get(last))
    if verdict == "no_trades":
        return f"{lead}: {test['n_trades']} trades in the {last} split, too few to compute a Sharpe ratio; nothing survives costs."
    dsr = "not computed" if (deflated.get(last) or {}).get("dsr") is None else f"{deflated[last]['dsr']:.2f}"
    earlier = [f"{by_split[s]['sharpe_ann']:.2f} in the {s} split" for s in ("fit", "select")
               if s != last and by_split.get(s, {}).get("sharpe_ann") is not None]
    closing = {"not_distinguishable": "nothing survives costs: the Sharpe ratio is not distinguishable from zero",
               "lost": "the rule lost money after costs: its Sharpe interval lies below zero",
               "positive_below_bar": f"the Sharpe ratio is above zero after costs on its own interval but under the bar for the best "
                                     f"of {n_variants} tries (deflated Sharpe ratio below {DSR_BAR:g})",
               "survives": f"it survives costs and the haircut for {n_variants} tries"}[verdict]
    return (f"{lead}: {last}-split annualised Sharpe {test['sharpe_ann']:.2f} with standard error {test['se_ann']:.2f} over "
            f"{test['n_trades']} trades, deflated Sharpe ratio {dsr}"
            + (f" (Sharpe {' and '.join(earlier)})" if earlier else "") + f"; {closing}.")


def make_headline(same_day: dict, after: dict, name: str, period: str) -> str:
    return same_day_sentence(same_day, name, period) + " " + after_sentence(after, same_day, name, period)


def make_plain_summary(same_day_by_split: dict, after: dict, up_events: dict, counts: dict, rule: dict | None, rule_by_split: dict | None,
                       deflated: dict | None, n_variants: int, name: str, period: str, last: str = "test") -> list:
    first = same_day_sentence(same_day_by_split[last], name, period)
    earlier = [f"{_signed(same_day_by_split[s]['coef'] * 0.10)} in the {s} split"
               + (f" ({same_day_by_split[s]['period']})" if same_day_by_split[s].get("period") else "")
               + ("" if not spans_zero(same_day_by_split[s]["ci"]) else ", not distinguishable from zero")
               for s in ("fit", "select") if s != last and same_day_by_split.get(s, {}).get("coef") is not None]
    if earlier:
        first += f" The same measure was {' and '.join(earlier)}."
    return [first, after_sentence(after, same_day_by_split[last], name, period),
            event_sentence(up_events, counts, name, period), rule_sentence(rule, rule_by_split, deflated, n_variants, last)]


def split_changes(by_split: dict, earlier: str = "select", later: str = "test") -> list:
    """What moved between two splits, for the numbers the wording rests on."""
    out = []
    for label, path, value, interval in (
            ("same-day slope", ("same_day_ual",), "beta", "ci"), ("20-session after-effect", ("lagged_ual", "20"), "coef", "interval"),
            ("60-session after-effect", ("lagged_ual", "60"), "coef", "interval"), ("rule net Sharpe", ("rule",), "sharpe_ann", "ci")):
        cells = []
        for split in (earlier, later):
            cell = by_split.get(split)
            for key in path:
                cell = None if cell is None else cell.get(key)
            cells.append(cell or {})
        a, b = (c.get(value) for c in cells)
        out.append({"measure": label, earlier: a, later: b,
                    f"{earlier}_distinguishable": not spans_zero(cells[0].get(interval)),
                    f"{later}_distinguishable": not spans_zero(cells[1].get(interval)),
                    "sign_changed": None if a is None or b is None else bool(a * b < 0)})
    return out


# ---------------------------------------------------------------- output assembly

def _clean(obj, digits: int = 6):
    """JSON-ready copy: numpy scalars to Python, floats to `digits` significant figures, NaN to None."""
    if isinstance(obj, dict):
        return {str(k): _clean(v, digits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v, digits) for v in obj]
    if isinstance(obj, np.ndarray):
        return [_clean(v, digits) for v in obj.tolist()]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return float(f"{float(obj):.{digits}g}") if math.isfinite(obj) else None
    return obj


DECISIONS = [
    "Fuel series: HO=F because it trades and is known before the equity close; the EIA spot series is shown beside it with its "
    "publication lag.",
    "Contract rolls: on the first session of a month the HO=F return compares two contracts. In fit and select those sessions "
    "show a wider gap to the spot series than other sessions, so the return is treated as missing.",
    "Overlap rule: thinning chosen before any outcome was read. The rule in stats.py (\"drop_later\") removes an event when any "
    "earlier event lies within 65 sessions, kept or not; in fit and select fuel events come in chains, so it left no fuel-rise "
    "event with a usable window in 2020-2023. The primary rule keeps an event unless a kept event lies within 65 sessions before "
    "it: windows of kept events never intersect and no later event is consulted. Both, and the flagged set, are reported.",
    "Abnormal return: the market model without its intercept (return minus beta x SPY, beta from the 250 sessions before the "
    "event window). An intercept estimated on 250 sessions and subtracted for 66 carries the share's own pre-event trend into "
    "the result; in the select split it changed the 60-session figure for fuel rises materially (the intercept variants in "
    "variants_tried show both). The intercept-free figure is also the return of the hedged position that the regressions and "
    "the trading rule use. The version with the intercept is reported beside it.",
    "Market-model estimation uses all 250 prior sessions: removing sessions inside earlier event windows left too few events.",
    "Lagged signal: the trailing 5-session HO=F change, the quantity that defines an event; chosen before results were read.",
    "Trading rule: chosen by code as the highest net Sharpe on the select split among variants with at least "
    f"{MIN_RULE_TRADES} trades there; the fit split is reported for the chosen rule and takes no part in the choice.",
]
DEVELOPMENT_LOG = [
    "Pass 1 (data to the end of the select split only): strict thinning, market model with intercept, estimation without "
    "event-window sessions, rule variants on thinned events of one side. Too few events survived. Its variants are regenerated "
    "in variants_tried under 'first development pass' and as the 'strict' rule family.",
    "Pass 2 (same data): primary thinning rule, estimation on all prior sessions, market model with intercept, rule variants on "
    "thinned events. Regenerated under 'second development pass' and as the 'thinned' rule family.",
    "Pass 3 (same data): intercept dropped from the abnormal return; rule variants on every event with one position at a "
    "time, as rebound or drift bets on rises, falls or both (the 'all' rule family); fuel move net of the market added as a "
    "variant. This is the frozen specification.",
    "No number from the test split was computed or read before the specification was frozen.",
]


def spec_document(spec: EventSpec, rule: dict | None, seed: int, n_boot_events: int, n_boot_block: int, explored: dict) -> dict:
    return {
        "target": TARGET, "peers": list(PEERS), "market": MARKET,
        "fuel_primary": "HO=F daily close, first session of each month treated as a contract roll (no return)",
        "fuel_secondary": "EIA Gulf Coast jet spot, current vintage, treated as known one business day later",
        "excess_return": f"log return minus beta x SPY log return; beta from the previous {BETA_WINDOW} sessions only",
        "same_day": "excess return on the same-session fuel return, constant included, Newey-West lag floor(4 (n/100)^(2/9)); "
                    "95% interval from the normal distribution",
        "lagged": {"signal": "ho_5d", "signal_label": SIGNAL_LABEL["ho_5d"], "horizons": list(HORIZONS),
                   "headline_horizons": list(HEADLINE_HORIZONS), "target": "sum of excess returns over sessions t+1 .. t+h",
                   "hac_lag": "max(floor(4 (n/100)^(2/9)), 2 x horizon)", "block_bootstrap_block": "2 x horizon + 1",
                   "wording_interval": "widest of the Newey-West and block-bootstrap 95% intervals"},
        "event": {**asdict(spec),
                  "definition": "session t is a crossing when |trailing change| exceeds the quantile of the same quantity over all "
                                "sessions before t; an event is the first crossing after quiet_days sessions without one",
                  "overlap_rule": "an event is kept unless a kept event lies within k + h sessions before it; rises and falls are "
                                  "thinned together and reported separately",
                  "overlap_span": spec.k + spec.h, "window": [-spec.k, spec.h],
                  "estimation": "beta from the market model on the est_len sessions ending the session before the event window",
                  "abnormal_return": "return minus beta x SPY return (no intercept)",
                  "post_event": "cumulative abnormal return over days +1 .. +h, after the fuel move was known",
                  "wording_interval": "widest of the t (df = n - 1) and event-bootstrap 95% intervals"},
        "level": LEVEL, "seed": seed, "n_boot_events": n_boot_events, "n_boot_block": n_boot_block,
        "trading_rule": {"chosen": rule, "chosen_by": "highest net annualised Sharpe on the select split among the variants with at "
                                                      f"least {MIN_RULE_TRADES} trades there",
                         "n_variants": explored["n_rule_variants"], "n_variants_eligible": explored["n_rule_variants_eligible"],
                         "cost": f"{COST_BPS:g} basis points of traded notional per side on both legs: (1 + |beta|) x cost at entry "
                                 "and again at exit",
                         "position": "+/-(UAL - beta x SPY), simple daily returns, past-only beta, zero when flat; rebound = long "
                                     "after a fuel rise and short after a fall, drift = the reverse",
                         "events": "all = every event (first crossing of a cluster); thinned = kept by the primary overlap rule; "
                                   "strict = kept by stats drop_later. In time order, one position at a time; a trade must close "
                                   "inside its split",
                         "survives_costs": "test-split 95% interval of the net Sharpe ratio above zero",
                         "clears_selection_haircut": f"and test-split deflated Sharpe ratio of at least {DSR_BAR:g} for "
                                                     "n_variants tries"},
        "declared_secondary": ["EIA spot as the fuel series", "lagged signals ho_1d and ho_20d and their EIA versions",
                               "one more session skipped before returns start", "overlapping events flagged instead of thinned",
                               "abnormal return with the market-model intercept", "rises and falls separately in the same-day "
                               "regression", "fuel falls as events", "the same specification for " + ", ".join(PEERS)],
        "decisions": DECISIONS, "development_log": DEVELOPMENT_LOG, "selection_data": explored["samples"],
        "timing_check": {**explored["timing_check"], "sample": "fit and select",
                         "reading": "one-session return correlations between HO=F and the EIA spot series; a larger value for "
                                    "spot_then_ho_next_session than for ho_then_spot_next_session means the spot print of a "
                                    "session carries news that the HO=F close of that session does not"},
        "units": {"same_day.beta": "excess log return per unit of same-session fuel log return (0.1 = 1% per 10% fuel move)",
                  "lagged.coef": "cumulative excess log return over the next h sessions per unit of the trailing 5-session "
                                 "fuel log change",
                  "event_study.path": "mean cumulative abnormal log return from day -5; day 0 is the event session",
                  "post": "mean cumulative abnormal log return over days +1 .. +h",
                  "trading_rule": "simple daily returns of the hedged position per unit of UAL notional; Sharpe annualised by "
                                  "sqrt(252) over every session of the split"},
    }


def spec_hash(document: dict) -> str:
    return hashlib.sha256(json.dumps(_clean(document), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _same_day_row(ticker: str, cell: dict) -> dict:
    main = cell.get("all") or next(iter(cell.values()))
    row = {"ticker": ticker, "fuel": HO, "sample": "all" if "all" in cell else next(iter(cell)),
           "beta": main["ho"]["coef"], "t_nw": main["ho"]["t_nw"], "n": main["ho"]["n"], "se": main["ho"]["se"], "ci": main["ho"]["ci"],
           "r2": main["ho"]["r2"], "by_split": {}, "eia_by_split": {}, "up_down_by_split": {}}
    for name, got in cell.items():
        row["by_split"][name] = {**_as_beta(got["ho"]), "start": got["start"], "end": got["end"]}
        row["eia_by_split"][name] = _as_beta(got["eia"])
        row["up_down_by_split"][name] = got["ho_up_down"]
    return row


def _as_beta(fit: dict) -> dict:
    return {"beta": fit["coef"], "se": fit["se"], "t_nw": fit["t_nw"], "ci": fit["ci"], "p": fit["p"], "n": fit["n"], "r2": fit["r2"]}


def _lagged_rows(cell: dict, signal: str, ticker: str) -> list:
    main_name = "all" if "all" in cell else next(iter(cell))
    rows = []
    for i, fit in enumerate(cell[main_name]):
        rows.append({"horizon_days": fit["horizon_days"], "coef": fit["coef"], "t_nw": fit["t_nw"], "se": fit["se"], "ci": fit["ci"],
                     "n": fit["n"], "hac_lag": fit["hac_lag"], "signal": signal, "ticker": ticker, "sample": main_name,
                     "by_split": {name: {k: v for k, v in rows_[i].items() if k != "horizon_days"} for name, rows_ in cell.items()}})
    return rows


def variant_strings(explored: dict) -> list:
    out = []
    for v in explored["variants"]:
        if v["family"] == "trading rule":
            bits = [f"{s} net Sharpe {'n/a' if v[s]['sharpe_ann'] is None else format(v[s]['sharpe_ann'], '.2f')} ({v[s]['n_trades']} trades)"
                    for s in ("fit", "select") if s in v]
        elif v["family"] == "event definition":
            bits = []
            for s in ("fit", "select"):
                if s in v:
                    up, down = v[s]["up"], v[s]["down"]
                    bits.append(f"{s} rises n={up['n_events']} post-60 {_fmt_mean(up['post_60'])}, falls n={down['n_events']} "
                                f"post-60 {_fmt_mean(down['post_60'])}")
        else:
            bits = [f"{s} same-day {_fmt_coef(v[s]['same_day'])}, 20d {_fmt_coef(v[s]['lagged']['20'])}, 60d {_fmt_coef(v[s]['lagged']['60'])}"
                    for s in ("fit", "select") if s in v]
        tail = " Test split not computed." if v["test"] == "not computed" else " Chosen: test split computed once, after freezing."
        out.append(f"{v['family']}: {v['description']}. " + "; ".join(bits) + "." + tail)
    return out


def _fmt_coef(fit) -> str:
    return "n/a" if not fit or fit.get("coef") is None else f"{fit['coef']:+.3f} (t {fit['t_nw']:+.2f})"


def _fmt_mean(fit) -> str:
    if not fit or fit.get("mean") is None:
        return "n/a"
    return _signed(fit["mean"]) + ("" if fit.get("t") is None else f" (t {fit['t']:+.2f})")


def _same(a, b, path="") -> list:
    """Paths at which two cleaned structures differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [d for k in sorted(set(a) | set(b)) for d in _same(a.get(k), b.get(k), f"{path}/{k}")]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in _same(x, y, f"{path}/{i}")]
    return [] if a == b else [path]


def _only(result: dict, names) -> dict:
    """The parts of a compute() result that belong to the named samples."""
    out = {"same_day": {t: {s: v for s, v in cell.items() if s in names} for t, cell in result["same_day"].items()},
           "lagged": {t: {sig: {s: v for s, v in cell.items() if s in names} for sig, cell in sigs.items()}
                      for t, sigs in result["lagged"].items()},
           "event_counts": {s: v for s, v in result["event_counts"].items() if s in names},
           "events": {t: {side: {rule: {model: {s: v for s, v in cell.items() if s in names} for model, cell in models.items()}
                                 for rule, models in rules.items()} for side, rules in sides.items()}
                      for t, sides in result["events"].items()}}
    if "rule" in result:
        out["rule"] = {s: v for s, v in result["rule"].items() if s in names}
    return _clean(out)


def freeze(panel: Panel, splits=SPLITS, seed: int = DEFAULT_SEED, n_boot_events: int = 5000, n_boot_block: int = 2000,
           spec: EventSpec = EventSpec()) -> dict:
    """Stage 1. Uses data up to the end of the select split only. Returns the frozen
    specification, every variant tried, and the fit and select results."""
    cut = panel.upto(date.fromisoformat(splits["select"][1]))
    explored = explore(cut, splits, spec, seed, n_boot_events)
    rule = explored["chosen_rule"]
    document = spec_document(spec, rule, seed, n_boot_events, n_boot_block, explored)
    samples = {k: v for k, v in samples_for(cut.dates, splits, with_all=False).items() if k in ("fit", "select")}
    early = compute(cut, samples, spec, seed, n_boot_events, n_boot_block, rule=None if rule is None else RuleSpec(**rule))
    return {"spec": _clean(document), "spec_sha256": spec_hash(document), "explored": explored, "early": early,
            "selection_data_end": cut.dates[-1].isoformat(), "event_spec": spec, "seed": seed,
            "n_boot_events": n_boot_events, "n_boot_block": n_boot_block, "splits": splits}


def pending_document(frozen: dict, inputs: list, generated_at: str) -> dict:
    """What is written to disk before the test split is computed."""
    return _clean({"status": "specification frozen; test split not yet computed", "generated_at": generated_at,
                   "as_of": frozen["selection_data_end"], "inputs": inputs, "primary_spec": frozen["spec"],
                   "spec_sha256": frozen["spec_sha256"], "variants_tried": variant_strings(frozen["explored"])})


def finish(panel: Panel, frozen: dict, inputs: list, generated_at: str) -> dict:
    """Stage 2. Computes every sample once for the frozen specification and assembles the output."""
    splits, spec, seed = frozen["splits"], frozen["event_spec"], frozen["seed"]
    rule = frozen["explored"]["chosen_rule"]
    samples = samples_for(panel.dates, splits)
    full = compute(panel, samples, spec, seed, frozen["n_boot_events"], frozen["n_boot_block"],
                   rule=None if rule is None else RuleSpec(**rule))
    moved = _same(_only(full, ("fit", "select")), _only(frozen["early"], ("fit", "select")))
    if moved:
        raise RuntimeError("fit or select results changed when test data was added: " + ", ".join(moved[:5]))
    last = "test"
    if last not in samples:
        raise ValueError("the panel has no session in the test split")
    period = f"{panel.dates[samples[last][0]].year}-{panel.dates[samples[last][1]].year}"
    ual_same = {s: v["ho"] for s, v in full["same_day"][TARGET].items()}
    ual_lag = full["lagged"][TARGET]["ho_5d"]
    model = spec.abnormal
    up_events = full["events"][TARGET]["up"]["thinned"][model]
    deflated = None
    if rule is not None:
        var = frozen["explored"]["select_sharpe_var_ann"]
        deflated = {}
        for s in ("select", "test"):
            if s in samples and full["rule"][s]["sharpe_ann"] is not None and var is not None:
                first, end = samples[s]
                deflated[s] = stats.deflated_sharpe(full["rule"]["run"]["net"][first:end + 1], frozen["explored"]["n_rule_variants"],
                                                    var, trial_var_annualised=True)
    events = full["events"][TARGET]
    main_event = up_events["all"]
    threshold_now = full["threshold_latest"]
    by_split = {}
    for s, bound in samples.items():
        if s == "all":
            continue
        by_split[s] = {
            "start": panel.dates[bound[0]].isoformat(), "end": panel.dates[bound[1]].isoformat(), "n": bound[1] - bound[0] + 1,
            "same_day_ual": _as_beta(ual_same[s]),
            "lagged_ual": {str(h): {k: v for k, v in _pick(ual_lag[s], h).items() if k != "horizon_days"} for h in HEADLINE_HORIZONS},
            "events": {**full["event_counts"][s],
                       "post_up_thinned": {str(h): up_events[s]["post"][str(h)] for h in HEADLINE_HORIZONS},
                       "post_down_thinned": {str(h): events["down"]["thinned"][model][s]["post"][str(h)] for h in HEADLINE_HORIZONS},
                       "post_up_flagged": {str(h): events["up"]["flagged"][model][s]["post"][str(h)] for h in HEADLINE_HORIZONS},
                       "post_down_flagged": {str(h): events["down"]["flagged"][model][s]["post"][str(h)] for h in HEADLINE_HORIZONS}},
            "rule": None if rule is None else {**full["rule"][s], "deflated": (deflated or {}).get(s)},
        }
    peers = []
    for ticker in AIRLINES:
        lag = full["lagged"][ticker]["ho_5d"]
        ev = full["events"][ticker]
        peers.append({"ticker": ticker, "name": NAMES[ticker], "by_split": {s: {
            "same_day": _as_beta(full["same_day"][ticker][s]["ho"]),
            "lagged": {str(h): _brief(_pick(lag[s], h)) for h in HEADLINE_HORIZONS},
            "event_up": {"n_events": ev["up"]["thinned"][model][s]["n_events"],
                         **{f"post_{h}": _brief(ev["up"]["thinned"][model][s]["post"][str(h)]) for h in HEADLINE_HORIZONS}},
            "event_down": {"n_events": ev["down"]["thinned"][model][s]["n_events"],
                           **{f"post_{h}": _brief(ev["down"]["thinned"][model][s]["post"][str(h)]) for h in HEADLINE_HORIZONS}},
        } for s in samples}})
    last_print = {name: max(d for d, v in zip(panel.dates, panel.levels[name]) if math.isfinite(v)).isoformat()
                  for name in (MARKET, TARGET, HO, FUEL)}
    # Wording is generated from the rounded numbers that are stored, so it can be regenerated from the file.
    by_split, up_last, counts_last = _clean(by_split), _clean(up_events[last]), _clean(full["event_counts"][last])
    same_day = {s: {"coef": v["same_day_ual"]["beta"], "ci": v["same_day_ual"]["ci"], "n": v["same_day_ual"]["n"],
                    "period": f"{v['start'][:4]}-{v['end'][:4]}"} for s, v in by_split.items()}
    after = {h: by_split[last]["lagged_ual"][str(h)] for h in HEADLINE_HORIZONS}
    rule_by_split = None if rule is None else {s: v["rule"] for s, v in by_split.items()}
    deflated_by_split = None if rule is None else {s: v["rule"]["deflated"] for s, v in by_split.items()}
    labels = {str(h): classify_after(after[h]) for h in HEADLINE_HORIZONS}
    event_labels = {str(h): classify_after(up_last["post"][str(h)]) for h in HEADLINE_HORIZONS}
    headline = make_headline(same_day[last], after, NAMES[TARGET], period)
    summary = make_plain_summary(same_day, after, up_last, counts_last, rule, rule_by_split, deflated_by_split,
                                 frozen["explored"]["n_rule_variants"], NAMES[TARGET], period, last)
    verdict = "no_rule" if rule is None else rule_verdict(rule_by_split[last], deflated_by_split[last])
    out = {
        "generated_at": generated_at, "as_of": panel.dates[-1].isoformat(), "last_print": last_print, "inputs": inputs,
        "headline": headline, "plain_summary": summary,
        "verdict": {"label": overall_verdict(labels), "split": last, "by_horizon": labels, "event_study_by_horizon": event_labels,
                    "rule": "under_reaction if the stock keeps falling after a fuel rise, over_reaction if it rises back, "
                            "neither if the 95% interval spans zero; decided on the lagged regression in the locked split"},
        "timing": TIMING, "splits": {s: {"start": v[0], "end": v[1] or panel.dates[-1].isoformat()} for s, v in splits.items()},
        "primary_spec": frozen["spec"], "spec_sha256": frozen["spec_sha256"], "spec_frozen_before_test": True,
        "selection_data_end": frozen["selection_data_end"],
        "lock_check": "fit and select numbers are identical with and without the test data",
        "same_day": [_same_day_row(t, full["same_day"][t]) for t in AIRLINES],
        "same_day_rolling": {"ticker": TARGET, "fuel": HO, "window_days": ROLLING_WINDOW, "step_days": ROLLING_STEP,
                             "series": full["rolling"]},
        "lagged": _lagged_rows(ual_lag, "ho_5d", TARGET),
        "lagged_secondary": {sig: _lagged_rows(cell, sig, TARGET) for sig, cell in full["lagged"][TARGET].items() if sig != "ho_5d"},
        "event_study": {
            "ticker": TARGET, "side": "up", "overlap_rule": spec.overlap, "sample": "all", "level": LEVEL,
            "abnormal_return": frozen["spec"]["event"]["abnormal_return"], "alongside": "up and down hold every side, overlap rule "
            "(thinned, flagged), abnormal-return model (beta_only, market_model) and sample",
            "n_events": main_event["n_events"], "n_events_flagged": events["up"]["flagged"][model]["all"]["n_events"],
            "threshold": (f"|{spec.change_days}-session HO=F log change| above its {100 * spec.quantile:g}th percentile over all "
                          f"earlier sessions (at least {spec.min_history}); most recent threshold "
                          + ("n/a" if threshold_now is None else _pct(threshold_now))
                          + f"; first crossing after {spec.quiet_days} sessions without one; an event within {spec.k + spec.h} "
                          "sessions after a kept event is dropped"),
            "window": [-spec.k, spec.h], "path": main_event["path"], "post": main_event["post"],
            "counts": full["event_counts"], "up": events["up"], "down": events["down"], "events": full["event_list"],
        },
        "trading_rule": None if rule is None else {
            "definition": frozen["spec"]["trading_rule"], "chosen": rule, "cost_bps_per_side": COST_BPS,
            "n_variants": frozen["explored"]["n_rule_variants"], "select_sharpe_var_ann": frozen["explored"]["select_sharpe_var_ann"],
            "by_split": {s: {**{k: v for k, v in full["rule"][s].items()}, "deflated": (deflated or {}).get(s)}
                         for s in samples if s != "all"},
            "trades": [{k: v for k, v in t.items() if k != "index"} for t in full["rule"]["run"]["trades"]],
            "verdict": verdict, "survives_costs": verdict in ("positive_below_bar", "survives"),
            "clears_selection_haircut": verdict == "survives", "statement": summary[3],
            "deflated_note": "dsr is the probability that the true Sharpe exceeds the expected best Sharpe of n_variants zero-skill "
                             "rules (variance of the variants' select-split Sharpes). On select it corrects for the selection made "
                             "there; on test it is a stricter bar than psr_zero, which needs no correction."},
        "peers": peers, "by_split": by_split,
        "select_to_test": split_changes(by_split, "select", last),
        "variants_tried": variant_strings(frozen["explored"]),
        "variants_detail": [{k: v for k, v in row.items()} for row in frozen["explored"]["variants"]],
        "caveats": [
            "HO=F is NY Harbor diesel, not Gulf Coast jet fuel; it is used because it trades and settles before the equity close.",
            "HO=F is Yahoo's continuous front-month series: roll dates are inferred (first session of each month) and that "
            "session's return is dropped; Yahoo does not document whether its close is the settlement.",
            "The EIA spot series is the current vintage; the one-business-day publication lag is an assumption.",
            "A fuel move is not a pure cost shock: demand news moves fuel and airline shares the same way, supply news moves "
            "them in opposite ways, and the slopes mix the two.",
            "Overlapping h-session targets leave about n / h independent windows; intervals at 60 sessions rest on few of them.",
            "Event bands resample events as if independent and undercover when events are few.",
            "The Sharpe standard error assumes independent daily returns.",
            "Hedge rebalancing as beta drifts is not charged.",
        ],
    }
    return _clean(out)


def build(panel: Panel, inputs: list, generated_at: str, splits=SPLITS, seed: int = DEFAULT_SEED, n_boot_events: int = 5000,
          n_boot_block: int = 2000, spec: EventSpec = EventSpec(), on_frozen=None) -> dict:
    """Freeze the specification on fit and select, hand it to `on_frozen` (the
    builder writes it to disk there), then compute the test split once."""
    frozen = freeze(panel, splits, seed, n_boot_events, n_boot_block, spec)
    if on_frozen is not None:
        on_frozen(pending_document(frozen, inputs, generated_at))
    return finish(panel, frozen, inputs, generated_at)


REQUIRED_KEYS = {
    "top": ("generated_at", "as_of", "inputs", "headline", "plain_summary", "same_day", "lagged", "event_study", "by_split",
            "variants_tried", "primary_spec", "spec_sha256", "trading_rule", "peers"),
    "same_day": ("ticker", "beta", "t_nw", "n"), "lagged": ("horizon_days", "coef", "t_nw"),
    "event_study": ("n_events", "threshold", "path"), "path": ("day", "mean", "lo", "hi"), "by_split": ("fit", "select", "test"),
    "inputs": ("path", "sha256"),
}


def validate(doc: dict) -> list:
    """Problems with the documented shape; an empty list means the document conforms."""
    problems = [f"missing {k}" for k in REQUIRED_KEYS["top"] if k not in doc]
    if problems:
        return problems
    if doc.get("fixture"):
        problems.append("fixture flag set")
    for name in ("same_day", "lagged", "inputs"):
        if not isinstance(doc[name], list) or not doc[name]:
            problems.append(f"{name} must be a non-empty list")
            continue
        problems += [f"{name}[{i}] missing {k}" for i, row in enumerate(doc[name]) for k in REQUIRED_KEYS[name] if k not in row]
    problems += [f"event_study missing {k}" for k in REQUIRED_KEYS["event_study"] if k not in doc["event_study"]]
    problems += [f"path[{i}] missing {k}" for i, row in enumerate(doc["event_study"].get("path", []))
                 for k in REQUIRED_KEYS["path"] if k not in row]
    problems += [f"by_split missing {k}" for k in REQUIRED_KEYS["by_split"] if k not in doc["by_split"]]
    if not isinstance(doc["headline"], str) or not doc["headline"]:
        problems.append("headline must be a non-empty string")
    if not isinstance(doc["plain_summary"], list) or not 3 <= len(doc["plain_summary"]) <= 4:
        problems.append("plain_summary must list three or four sentences")
    if not isinstance(doc["variants_tried"], list) or not all(isinstance(v, str) for v in doc["variants_tried"]):
        problems.append("variants_tried must be a list of strings")
    return problems
