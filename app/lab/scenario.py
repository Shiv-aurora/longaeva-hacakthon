"""Run business scenarios under pricing responses on the same simulated worlds.

`run_scenario(request)` is what POST /api/lab/scenario returns. It drives the monthly
simulator (app/monthly.py) once per response (costs only, management's stated path,
the measured recovery curve) with identical fuel and demand paths, so every
difference between modes is caused by the response. `run_grid(request)` is POST
/api/lab/grid: one metric against a threshold over two drivers. `compare(run_a,
run_b)` subtracts two runs, `baseline_fuel_usd_gal()` states the reference fuel
price, `today_request()` builds a request from the latest observations, and
`validate_response` checks a scenario result against the shape below.

Starting point: United's Q2 2026 business as reported by 16 July 2026 (the latest
reported quarter until 20 October 2026). Fuel moves after that date are not in the
no-shock reference. The engine projects from August 2026; the two months before
the first whole quarter are simulated but not reported, and every shock starts in
October 2026, so reported periods are whole fiscal quarters from 2026Q4 and quarter
k of the recovery curve is the k-th reported quarter after the shock starts.

Scenario request (all fields optional; unknown fields are rejected):
  fuel_shock_pct       percent change in the price United pays per gallon against the
                       reference returned by baseline_fuel_usd_gal(); -50 to 80.
  fuel_usd_gal         the same shock stated as a price level (0.5x to 1.8x the reference).
                       Give at most one of the two. Neither means no fuel shock.
  fuel_shock_months    months the shock is held, 1 to 24 (default 6). The simulator has
                       one shock window: capacity and demand changes use the same months,
                       and all of them then fade by half every two months.
  capacity_change_pct  -90 to 0 (capacity cuts only).
  demand_shock_pct     -80 to 100.
  delivery_delay       {"aircraft": n, "months": m}: n mainline aircraft due to enter the
                       fleet inside the shock window arrive m months late.
  capacity_response    "none" (default) or "measured": add the capacity change that
                       followed past fuel moves (app/static/data/capacity_response.json).
  demand_monthly_log_sd  monthly demand noise (log standard deviation), 0 to 0.1. Default: the engine's
                       assumed value. app/static/data/range_check.json tests that value against
                       United's history; `demand_noise()` states the value and what the test found.
  horizon_months       12 to 22 reported months (default 12).
  seed                 0 to 1,000,000,000 (default 1).
  paths                200 to 20,000 simulated worlds (default DEFAULT_PATHS).

Scenario response:
  {"status": "ok", "run_id", "elapsed_ms", "cached", "request", "paths", "as_of", "fuel",
   "modes": {mode: {"status": "ok", "quarters": [...], "prob": {...}, "next_12m": {...}, "bridge": [...]}
             | {"status": "not_computed", "reason"}},
   "bridge_per_share", "response_used", "capacity_response", "per_share", "monte_carlo",
   "inputs", "model", "assumptions"}
or {"status": "invalid", "errors": [...]}. Nothing is raised for a bad request. A result
computed with a response whose source kind is "synthetic" also carries "synthetic_response": true.

Differences from the draft in docs/lab_contract.md:
- Request gains `fuel_usd_gal`, `capacity_response`, `demand_monthly_log_sd` and `paths`.
  `fuel_shock_pct` is defined on the price United pays (one reference price), not on the spot
  price the engine uses.
- `{"status": "not_computed"}` is reported per mode, so the other modes still run. The
  top-level status stays "ok". `{"status": "invalid", "errors"}` is new.
- Quarter rows gain `months`, `complete`, `operating_income_change_musd`,
  `fare_change_pct` and `no_shock` (the paired reference). `eps_change` is null when no
  sourced tax rate and share count are available; `per_share` says why.
- `prob` gains `fcf_negative_12m_no_shock`. `next_12m` holds the 12-month totals.
- Every computed mode carries a `bridge`; the top-level `bridge_per_share` is the measured
  mode's (null until it is computed). Steps gain `operating_income_musd` and a "demand"
  step, values are means across paths so they add up to "net", and "profit sharing" has
  null values because the simulator does not model it.
- `response_used` is keyed by mode (three curves are used, not one).
- New top-level fields: `cached`, `request`, `paths`, `as_of`, `fuel`, `capacity_response`,
  `per_share`, `monte_carlo`, `inputs`, `model`. `monte_carlo.demand_noise` carries the demand
  noise used, whether it is the engine's assumption or was asked for, and what the range
  check found: the coverage of the assumed value, the fitted value that was rejected, and
  how much of the range's width comes from fuel noise.

Grid request: {"x": {"driver", "values"}, "y": {"driver", "values"}, "metric", "period",
"threshold", "direction", "mode", "base"}. Drivers: GRID_DRIVERS. `recovery_share` sets
every quarter's recovery share to the grid value on all paths, replacing the mode's
curve. Metrics: fcf_12m_musd, operating_income_12m_musd, operating_margin (period = a
reported quarter, default the first), eps_change (period = a quarter or "12m", the
default). `base` is a scenario request for everything that is not a driver. At most
GRID_CELLS cells. Grid response: the contract's fields plus `run_id`, `cached`, `period`,
`x`, `y`, `base`, `boundary_method`, `prob_definition`, `monte_carlo`, `response_used`,
`capacity_response` and `notes`. Cells are listed by y, then x. The path count is lowered,
by a fixed rule in cells and months, so that a full grid stays within a few seconds.

Capacity response (approximation): the engine takes one capacity path per rollout, so
the sampled response is applied to CAPACITY_GROUPS groups of paths ranked by the size of
their draw, each with its group's mean monthly cut. It responds to the scenario's fuel
shock only, not to simulated fuel noise; a change in year-over-year capacity growth is
read as the same percentage change in capacity against plan; draws that would add
capacity are set to zero because the engine models cuts only.

Per-share conversion: change in operating income x (1 - tax rate) / diluted shares.
Neither data/ual_q2_2026.json nor data/monthly_anchors.json carries a tax rate or a
diluted share count (checked 2026-10-09), so both come from data/lab/ual_quarterly.csv:
the latest quarter filed on or before the information cutoff, tax rate = 1 - net
income / pretax income of that quarter. Without that file the per-share fields are null.

`run_id` is the SHA-256 of the canonical request together with the identities of
every input (evidence, fuel history, each response's draws, per-share inputs, code).
The same request, seed and inputs always give the same run_id and the same numbers;
only `elapsed_ms` and `cached` vary. Results are kept in a small in-memory cache.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import csv
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import threading
from time import perf_counter

import numpy as np

from ..data import FUEL_CSV, load_fuel
from ..evidence import ANCHORS, available_records, digest, load_snapshot
from ..fleet import capacity_paths
from ..monthly import (RECOVERY_MODE, VERSION, MonthlyScenario, _rollout, baseline, month_sequence, shock_weights,
                       simulate_recovery)
from . import response as R

ROOT = Path(__file__).resolve().parents[2]
QUARTERLY_CSV = ROOT / "data" / "lab" / "ual_quarterly.csv"
FUEL_DAILY = ROOT / "data" / "lab" / "fuel_daily.csv"
COST_CLOCK = ROOT / "app" / "static" / "data" / "cost_clock.json"
DEMAND_SIGNAL = ROOT / "app" / "static" / "data" / "demand_signal.json"
RANGE_CHECK = ROOT / "app" / "static" / "data" / "range_check.json"
INFORMATION_CUTOFF = "2026-07-16"
DEFAULT_PATHS = 10000
MODES = ("costs_only", "management", "measured")
BATCHES = 20
CACHE_SIZE = 64
ENGINE_FUEL_PCT = (-60, 100)
ENGINE_MONTHS = 24
BOUNDS = {"fuel_shock_pct": (-50, 80), "fuel_shock_months": (1, 24), "capacity_change_pct": (-90, 0),
          "demand_shock_pct": (-80, 100), "horizon_months": (12, 22), "seed": (0, 1_000_000_000),
          "paths": (200, 20000), "delivery_delay.aircraft": (0, 1000), "delivery_delay.months": (0, 24),
          "demand_monthly_log_sd": (0, .1)}
INTEGERS = {"fuel_shock_months", "horizon_months", "seed", "paths", "delivery_delay.aircraft", "delivery_delay.months"}
DEFAULTS = {"fuel_shock_months": 6, "capacity_change_pct": 0.0, "demand_shock_pct": 0.0,
            "horizon_months": 12, "seed": 1, "paths": DEFAULT_PATHS}
FIELDS = ("fuel_shock_pct", "fuel_usd_gal", "fuel_shock_months", "capacity_change_pct", "demand_shock_pct",
          "delivery_delay", "capacity_response", "demand_monthly_log_sd", "horizon_months", "seed", "paths")
BRIDGE_STEPS = ("fuel cost", "fare recovery", "capacity", "demand", "profit sharing", "net")
CAPACITY_GROUPS = 8
GRID_FIELDS = ("x", "y", "metric", "period", "threshold", "direction", "mode", "base")
GRID_DRIVERS = ("fuel_shock_pct", "fuel_usd_gal", "demand_shock_pct", "capacity_change_pct", "delivery_delay_months", "recovery_share")
GRID_METRICS = {"fcf_12m_musd": 1, "operating_income_12m_musd": 1, "operating_margin": 4, "eps_change": 3}  # rounding digits
GRID_CELLS = 81
GRID_PATH_MONTHS = 7_000_000  # cells x paths x engine months a grid may spend
GRID_MIN_PATHS = 500
RECOVERY_SHARE_BOUNDS = (-.5, 1.5)
CODE_FILES = ("app/monthly.py", "app/fleet.py", "app/evidence.py", "app/data.py", "app/cash_bridge.py",
              "app/lab/response.py", "app/lab/scenario.py")
CODE_SHA256 = hashlib.sha256(b"".join((ROOT / name).read_bytes() for name in CODE_FILES)).hexdigest()

_cache: OrderedDict[str, dict] = OrderedDict()
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _file(path: Path) -> dict:
    return {"path": path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _lead_months(cutoff: str) -> int:
    first = int(month_sequence(cutoff, 1)[0][5:])
    return (3 - (first - 1) % 3) % 3


def demand_noise(path: Path | None = None) -> dict:
    """The engine's assumed monthly demand noise and, once the range check is built, what that check found about it."""
    path = RANGE_CHECK if path is None else Path(path)
    engine = MonthlyScenario.demand_monthly_log_sd
    name = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name
    block = _built(path).get("runner")
    assumed = {"monthly_log_sd": engine, "source": "engine_default", "engine_default": engine}
    if not isinstance(block, dict) or not isinstance(block.get("statement"), str):
        return {**assumed, "check": None, "reason": f"{name} {'does not carry a usable check' if path.is_file() else 'has not been built'}"}
    if block.get("demand_monthly_log_sd") != engine:
        return {**assumed, "check": None, "reason": f"{name} tested a different value ({block.get('demand_monthly_log_sd')!r})"}
    return {**assumed, "check": {**block, "path": name}}


def _context() -> dict:
    snapshot, fuel = load_snapshot(), load_fuel()
    known = [r for r in fuel if r["date"] <= INFORMATION_CUTOFF]
    return {"snapshot": snapshot, "fuel": fuel, "baseline": baseline(snapshot, INFORMATION_CUTOFF),
            "spot": known[-1], "latest": fuel[-1], "lead": _lead_months(INFORMATION_CUTOFF), "noise": demand_noise()}


def baseline_fuel_usd_gal() -> dict:
    """The simulator's reference fuel price, where it comes from, and how to convert a shock."""
    c = _context()
    reference = c["baseline"]["paid_fuel_usd_per_gallon"]
    records = [r for r in available_records(c["snapshot"], INFORMATION_CUTOFF)
               if r["entity"] == "UAL" and r["variable"] in ("fuel_expense", "fuel_gallons")]
    urls = {s["source_id"]: s["url"] for s in c["snapshot"]["sources"]}
    return {"usd_gal": round(reference, 4),
            "definition": "Average price United paid per gallon in the latest reported quarter (fuel expense / gallons). "
                          "The simulator carries it forward as the no-shock reference.",
            "period": [records[0]["effective_start"], records[0]["effective_end"]],
            "source": {**_file(ANCHORS), "record_ids": [r["record_id"] for r in records],
                       "values": {r["variable"]: r["value"] for r in records},
                       "published_on": max(r["published_on"] for r in records),
                       "urls": sorted({urls[r["source_id"]] for r in records})},
            "information_cutoff": INFORMATION_CUTOFF,
            "conversion": "fuel_usd_gal = usd_gal * (1 + fuel_shock_pct / 100)",
            "market": {**_file(FUEL_CSV), "series": "EIA Gulf Coast kerosene-type jet fuel spot",
                       "spot_at_cutoff": {"date": c["spot"]["date"], "usd_gal": c["spot"]["usd_per_gallon"]},
                       "latest_spot": {"date": c["latest"]["date"], "usd_gal": c["latest"]["usd_per_gallon"]},
                       "reference_moved_to_latest_spot_usd_gal": round(reference + c["latest"]["usd_per_gallon"] - c["spot"]["usd_per_gallon"], 4),
                       "rule": "The simulator moves United's price dollar for dollar with this spot price from the cutoff. "
                               "Spot moves after the cutoff are not in the reference; pass a level in fuel_usd_gal to include them."}}


def per_share_inputs(path: Path | None = None) -> dict:
    """Tax rate and diluted share count from the latest quarter filed by the cutoff, or the reason they are missing."""
    path = QUARTERLY_CSV if path is None else Path(path)
    missing = {"available": False, "tax_rate": None, "diluted_shares_m": None, "source": None}
    if not path.is_file():
        return {**missing, "reason": "data/lab/ual_quarterly.csv has not been built; data/ual_q2_2026.json and "
                                     "data/monthly_anchors.json carry neither a tax rate nor a diluted share count"}
    with path.open(newline="", encoding="utf-8") as stream:
        rows = [row for row in csv.DictReader(stream) if (row.get("filed_on") or "9999") <= INFORMATION_CUTOFF]
    for row in sorted(rows, key=lambda r: r.get("period_end") or "", reverse=True):
        try:
            pretax, net, shares = (float(row[k]) for k in ("pretax_income_musd", "net_income_musd", "diluted_shares_m"))
        except (KeyError, TypeError, ValueError):
            continue
        if pretax <= 0 or shares <= 0 or not 0 <= 1 - net / pretax < .6:
            continue
        return {"available": True, "tax_rate": round(1 - net / pretax, 6), "diluted_shares_m": shares,
                "method": "tax rate = 1 - net income / pretax income of the same quarter; diluted weighted-average shares as reported",
                "source": {**_file(path), "period_end": row["period_end"], "filed_on": row["filed_on"],
                           "accession": row.get("accession"), "pretax_income_musd": pretax, "net_income_musd": net}}
    return {**missing, "reason": f"No quarter filed by {INFORMATION_CUTOFF} in {path.name} has positive pretax income, net income and diluted shares"}


def default_responses(skip=()) -> dict:
    """Mode name to Response, or to the error explaining why the mode cannot run yet."""
    modes = {}
    if "costs_only" not in skip:
        modes["costs_only"] = R.costs_only()
    if "management" not in skip:
        modes["management"] = R.management()
    if "measured" not in skip:
        try:
            modes["measured"] = R.measured()
        except (R.NotComputed, ValueError, TypeError, KeyError) as error:
            modes["measured"] = error
    return modes


def _number(errors: list, name: str, value, default=None):
    low, high = BOUNDS[name]
    if value is None:
        return default
    whole = name in INTEGERS
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        errors.append(f"{name} must be {'a whole number' if whole else 'a number'} between {low} and {high}; got {value!r}")
        return default
    if whole and float(value) != int(value):
        errors.append(f"{name} must be a whole number between {low} and {high}; got {value!r}")
        return default
    if not low <= value <= high:
        errors.append(f"{name} must be between {low} and {high}; got {value!r}")
        return default
    return int(value) if whole else float(value)


def _validate(request: dict, noise: dict | None = None) -> tuple[dict, list[str], bool]:
    """Canonical request, problems, and whether the fuel fields are usable for the range check."""
    errors: list[str] = []
    noise = demand_noise() if noise is None else noise
    unknown = sorted(set(request) - set(FIELDS), key=str)
    if unknown:
        errors.append(f"unknown field(s) {unknown}; allowed fields are {list(FIELDS)}")
    canonical, fuel_ok = {}, True
    for name in ("fuel_shock_pct", "fuel_usd_gal"):
        value = request.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            errors.append(f"{name} must be a number; got {value!r}")
            value, fuel_ok = None, False
        canonical[name] = None if value is None else float(value)
    if canonical["fuel_shock_pct"] is not None and canonical["fuel_usd_gal"] is not None:
        errors.append("give fuel_shock_pct or fuel_usd_gal, not both")
        fuel_ok = False
    if canonical["fuel_shock_pct"] is None and canonical["fuel_usd_gal"] is None:
        canonical["fuel_shock_pct"] = 0.0
    for name in ("fuel_shock_months", "capacity_change_pct", "demand_shock_pct", "horizon_months", "seed", "paths"):
        canonical[name] = _number(errors, name, request.get(name), DEFAULTS[name])
    delay = request.get("delivery_delay")
    if delay is None:
        delay = {}
    if not isinstance(delay, dict) or set(delay) - {"aircraft", "months"}:
        errors.append('delivery_delay must be an object like {"aircraft": 10, "months": 6}')
        delay = {}
    canonical["delivery_delay"] = {key: _number(errors, "delivery_delay." + key, delay.get(key), 0) for key in ("aircraft", "months")}
    if (canonical["delivery_delay"]["aircraft"] > 0) != (canonical["delivery_delay"]["months"] > 0):
        errors.append("delivery_delay needs both aircraft and months above zero, or both zero")
    canonical["capacity_response"] = request.get("capacity_response") or "none"
    if canonical["capacity_response"] not in ("none", "measured"):
        errors.append(f'capacity_response must be "none" or "measured"; got {canonical["capacity_response"]!r}')
        canonical["capacity_response"] = "none"
    canonical["demand_monthly_log_sd"] = _number(errors, "demand_monthly_log_sd", request.get("demand_monthly_log_sd"), noise["monthly_log_sd"])
    return canonical, errors, fuel_ok


def _fuel(req: dict, c: dict, errors: list[str]) -> tuple[dict, float]:
    """The shock as a level and a percentage, and as the engine's percent of the spot price."""
    reference, spot = c["baseline"]["paid_fuel_usd_per_gallon"], c["spot"]["usd_per_gallon"]
    low = max(BOUNDS["fuel_shock_pct"][0], ENGINE_FUEL_PCT[0] * spot / reference)
    high = min(BOUNDS["fuel_shock_pct"][1], ENGINE_FUEL_PCT[1] * spot / reference)
    if req["fuel_usd_gal"] is not None:
        level = req["fuel_usd_gal"]
        bounds = reference * (1 + low / 100), reference * (1 + high / 100)
        if not bounds[0] <= level <= bounds[1]:
            errors.append(f"fuel_usd_gal must be between {math.ceil(bounds[0] * 100) / 100:.2f} and {math.floor(bounds[1] * 100) / 100:.2f} "
                          f"US dollars per gallon ({low:g}% to {high:g}% around the {reference:.2f} reference); got {level!r}")
        pct = (level / reference - 1) * 100
    else:
        pct = req["fuel_shock_pct"]
        if not low <= pct <= high:
            errors.append(f"fuel_shock_pct must be between {low:g} and {high:g} "
                          f"(percent change in the {reference:.2f} US dollars per gallon reference); got {pct!r}")
        level = reference * (1 + pct / 100)
    return {"reference_usd_gal": round(reference, 4), "level_usd_gal": round(level, 4), "shock_pct": round(pct, 4),
            "shock_usd_gal": round(level - reference, 4), "held_months": req["fuel_shock_months"],
            "after_hold": f"fades by half every {MonthlyScenario.recovery_half_life_months:g} months",
            "given_as": "fuel_usd_gal" if req["fuel_usd_gal"] is not None else "fuel_shock_pct"}, (level - reference) / spot * 100


def _eligible_aircraft(c: dict, req: dict) -> int:
    months = month_sequence(INFORMATION_CUTOFF, ENGINE_MONTHS)
    window = months[c["lead"]:c["lead"] + req["fuel_shock_months"]]
    return sum(e["aircraft"] for e in c["baseline"]["fleet_plan"]["events"]
               if e["kind"] == "entry" and e["group"] != "regional" and e["month"] in window)


def _checked(request: dict, c: dict) -> tuple[dict, dict | None, float, list[str]]:
    """Canonical request, its fuel block, the engine's fuel percent, and every problem found."""
    req, errors, fuel_ok = _validate(request, c["noise"])
    fuel, engine_pct = _fuel(req, c, errors) if fuel_ok else (None, 0.0)
    eligible = _eligible_aircraft(c, req)
    if req["delivery_delay"]["aircraft"] > eligible:
        start = month_sequence(INFORMATION_CUTOFF, c["lead"] + 1)[-1]
        errors.append(f"delivery_delay.aircraft must be at most {eligible}: the fleet plan has {eligible} mainline aircraft "
                      f"entering in the {req['fuel_shock_months']} months from {start} (the shock window set by fuel_shock_months)")
    return req, fuel, engine_pct, errors


def _engine_scenario(req: dict, c: dict, engine_pct: float, paths: int | None = None) -> MonthlyScenario:
    aircraft = req["delivery_delay"]["aircraft"]
    return MonthlyScenario(information_cutoff=INFORMATION_CUTOFF, horizon_months=c["lead"] + req["horizon_months"],
                           paths=req["paths"] if paths is None else paths, seed=req["seed"], fuel_shock_pct=engine_pct,
                           demand_shock_pct=req["demand_shock_pct"], capacity_shock_pct=req["capacity_change_pct"],
                           delivery_delay_months=req["delivery_delay"]["months"], delivery_aircraft_count=aircraft or None,
                           onset_month=c["lead"] + 1, duration_months=req["fuel_shock_months"],
                           demand_monthly_log_sd=req["demand_monthly_log_sd"])


def _periods(months: list[str], lead: int) -> dict[str, list[int]]:
    """Reported fiscal quarters and the engine month indexes inside each."""
    periods: dict[str, list[int]] = {}
    for i, month in enumerate(months):
        if i >= lead:
            periods.setdefault(f"{month[:4]}Q{(int(month[5:]) - 1) // 3 + 1}", []).append(i)
    return periods


def _worlds(s: MonthlyScenario, c: dict) -> dict:
    """The fuel and demand worlds simulate_monthly would draw for s, with fleet paths kept per case."""
    raw = simulate_recovery(s, {}, snapshot=c["snapshot"], fuel=c["fuel"])
    raw["fleets"] = {}
    return raw


def _roll(raw: dict, case: MonthlyScenario, recovery: np.ndarray, shocked: bool = True, groups: list | None = None) -> dict:
    """One rollout of case on the shared worlds. `groups` applies a monthly capacity cut per group of paths."""
    b = raw["baseline_inputs"]
    key = (case.delivery_delay_months, case.delivery_aircraft_count)
    if key not in raw["fleets"]:
        raw["fleets"][key] = capacity_paths(b, case, b["fleet_plan"], raw["months"], shock_weights(case))
    fleet, spot = raw["fleets"][key], raw["spot_usd_per_gallon"]
    if groups is None:
        return _rollout(b, case, raw["fuel_world"], raw["demand_world"], spot, shocked, RECOVERY_MODE, fleet, recovery)
    out: dict[str, np.ndarray] = {}
    for index, change in groups:
        cut = {**fleet, "intervention_components": fleet["intervention_components"] * (1 + change / 100)[:, None, None]}
        part = _rollout(b, replace(case, paths=len(index)), raw["fuel_world"][index], raw["demand_world"][index],
                        spot, True, RECOVERY_MODE, cut, recovery[index])
        for name, values in part.items():
            out.setdefault(name, np.empty((case.paths, *values.shape[1:])))[index] = values
    return out


def _capacity(req: dict):
    """The measured capacity response when the request asks for it: a Response, the reason it is missing, or None."""
    if req["capacity_response"] != "measured":
        return None
    try:
        return R.capacity()
    except (R.NotComputed, ValueError, TypeError, KeyError) as error:
        return error


def _capacity_groups(response: R.Response, s: MonthlyScenario, shock_pct: float, lead: int) -> tuple[list, np.ndarray]:
    """Sampled capacity response as monthly percent cuts: CAPACITY_GROUPS (paths, cut) pairs and the per-path cuts.

    Each change in the central fuel shock starts its own clock, as for fares. Paths are ranked by
    their mean cut over the first 12 reported months and each group gets its mean cut.
    """
    by_age = R.monthly_curve(response.sample(s.paths, s.seed, R.CAPACITY_STREAM), s.horizon_months)
    steps = np.diff(shock_weights(s), prepend=0) * shock_pct / 10
    change = np.clip(np.stack([by_age[:, t::-1] @ steps[:t + 1] for t in range(s.horizon_months)], axis=1), -90, 0)
    order = np.argsort(change[:, lead:lead + 12].mean(axis=1), kind="stable")
    return [(index, change[index].mean(axis=0)) for index in np.array_split(order, CAPACITY_GROUPS)], change


def _quantiles(values: np.ndarray, digits: int) -> dict:
    p10, p50, p90 = np.quantile(values, [.1, .5, .9])
    return {"p10": round(float(p10), digits) + 0.0, "p50": round(float(p50), digits) + 0.0, "p90": round(float(p90), digits) + 0.0}


def _standard_error(statistic, *arrays) -> float:
    """Batch means: the statistic on BATCHES disjoint groups of paths; its spread / sqrt(BATCHES)."""
    parts = [np.array_split(a, BATCHES) for a in arrays]
    estimates = [statistic(*(p[i] for p in parts)) for i in range(BATCHES)]
    return float(np.std(estimates, ddof=1) / math.sqrt(BATCHES))


def _recovered(roll: dict) -> tuple[np.ndarray, np.ndarray]:
    """Dollars recovered through fares and the passenger revenue before them, per path and month."""
    change = roll["market_yield_change_pct"] / 100
    before = roll["market_revenue_musd"] / (1 + change)
    return (before * change).sum(axis=2), before.sum(axis=2)


def _mode(r: dict, months: list[str], lead: int, per_share: dict, capacity_on: bool, demand_on: bool) -> tuple[dict, dict]:
    control, shocked = r["control"], r["intervention"]
    factor = (1 - per_share["tax_rate"]) / per_share["diluted_shares_m"] if per_share["available"] else None
    periods = _periods(months, lead)

    def total(roll, name, index):
        return roll[name][:, index].sum(axis=1)

    recovered, before = _recovered(shocked)
    recovered_reference, _ = _recovered(control)
    quarters = []
    for period, index in periods.items():
        income, reference_income = total(shocked, "operating_income_musd", index), total(control, "operating_income_musd", index)
        change = income - reference_income
        quarters.append({
            "period": period, "months": [months[i] for i in index], "complete": len(index) == 3,
            "operating_income_musd": _quantiles(income, 1),
            "operating_margin": _quantiles(income / total(shocked, "revenue_musd", index), 4),
            "fcf_musd": _quantiles(total(shocked, "free_cash_flow_musd", index), 1),
            "operating_income_change_musd": _quantiles(change, 1),
            "eps_change": None if factor is None else _quantiles(change * factor, 3),
            "fare_change_pct": _quantiles(100 * (recovered[:, index] - recovered_reference[:, index]).sum(axis=1) / before[:, index].sum(axis=1), 2),
            "no_shock": {"operating_income_musd": _quantiles(reference_income, 1),
                         "operating_margin": _quantiles(reference_income / total(control, "revenue_musd", index), 4),
                         "fcf_musd": _quantiles(total(control, "free_cash_flow_musd", index), 1)}})
    year = list(range(lead, lead + 12))
    income_change = total(shocked, "operating_income_musd", year) - total(control, "operating_income_musd", year)
    cash, reference_cash = total(shocked, "free_cash_flow_musd", year), total(control, "free_cash_flow_musd", year)
    fuel_only = r.get("fuel_only", shocked)
    fuel_capacity = r.get("fuel_capacity", shocked if capacity_on and not demand_on else fuel_only)
    steps = {"fuel cost": total(control, "fuel_expense_musd", year) - total(fuel_only, "fuel_expense_musd", year),
             "fare recovery": total(fuel_only, "revenue_musd", year) - total(control, "revenue_musd", year),
             "capacity": total(fuel_capacity, "operating_income_musd", year) - total(fuel_only, "operating_income_musd", year),
             "demand": total(shocked, "operating_income_musd", year) - total(fuel_capacity, "operating_income_musd", year),
             "profit sharing": None, "net": income_change}
    bridge = []
    for step in BRIDGE_STEPS:
        mean = None if steps[step] is None else float(steps[step].mean())
        bridge.append({"step": step, "value": None if mean is None or factor is None else round(mean * factor, 3),
                       "operating_income_musd": None if mean is None else round(mean, 1)})
    output = {"status": "ok", "quarters": quarters,
              "prob": {"fcf_negative_12m": round(float((cash < 0).mean()), 4),
                       "fcf_negative_12m_no_shock": round(float((reference_cash < 0).mean()), 4)},
              "next_12m": {"months": [months[i] for i in year],
                           "operating_income_musd": _quantiles(total(shocked, "operating_income_musd", year), 1),
                           "operating_income_change_musd": _quantiles(income_change, 1),
                           "eps_change": None if factor is None else _quantiles(income_change * factor, 3),
                           "fcf_musd": _quantiles(cash, 1)},
              "bridge": bridge}
    error = {"operating_income_change_12m_musd_p50": round(_standard_error(np.median, income_change), 2),
             "fcf_12m_musd_p50": round(_standard_error(np.median, cash), 2),
             "fcf_negative_12m": round(_standard_error(lambda x: float((x < 0).mean()), cash), 4),
             "eps_change_12m_p50": None if factor is None else round(_standard_error(np.median, income_change * factor), 4)}
    return output, error


def _noise_used(req: dict, noise: dict) -> dict:
    """What a result says about demand noise: the value used, whose it is, and the range check behind the assumed value."""
    value, engine, check = req["demand_monthly_log_sd"], noise["engine_default"], noise["check"]
    if value != engine:
        note = (f"Demand noise is set by the request at {value:.2%} a month, not the assumed {engine:.0%}."
                + (f" The range check in {check['path']} applies to the assumed value only." if check else ""))
    elif check:
        note = check["statement"]
    else:
        note = (f"Demand noise is an assumed {engine:.0%} a month and has not been checked against history: {noise['reason']}. "
                "Fuel noise uses the volatility of the last trading days before the cutoff.")
    return {"monthly_log_sd": value, "source": "engine_default" if value == engine else "request", "engine_default": engine,
            "check": check, "note": note}


def _assumptions(fuel: dict, per_share: dict, start: str, capacity: dict, noise: dict) -> list[str]:
    engine = MonthlyScenario()
    return [
        f"Starting point: United's Q2 2026 business as reported by {INFORMATION_CUTOFF} (four markets, fleet plan, costs, cash). "
        "Quarterly totals are spread evenly over months; no seasonality is modeled. Fuel moves after that date are not in the no-shock reference.",
        f"Fuel: the reference is the {fuel['reference_usd_gal']:.2f} US dollars a gallon United paid in Q2 2026. The shock moves that price to "
        f"{fuel['level_usd_gal']:.2f} on the central path for {fuel['held_months']} months from {start}, then {fuel['after_hold']}. "
        "Simulated paths still vary around it; the persistence of fuel and demand noise and their correlation are assumed. " + noise["note"],
        "Response: every monthly change in the fuel price starts its own clock, and the recovery curve gives the share of that change, "
        "times gallons burned, that comes back as revenue by quarter after the change.",
        "Fare response is symmetric: a falling fuel price is given back at the same speed. A temporary shock is therefore "
        "under-recovered while it builds and over-recovered while it fades.",
        "The recovery share is defined on revenue net of volume effects: no fare-demand elasticity, pass-through cap or fare-step limit is applied on top of it. "
        "`fare_change_pct` shows the average fare change that the recovery implies.",
        "One recovery curve is drawn per simulated path from the response's joint draws; management's ranges are sampled uniformly.",
        "Management's path is United's own statements about one shock. " + R.MANAGEMENT_MAPPING,
        "Every mode, the no-shock reference and every bridge step use the same simulated fuel and demand paths, so differences are caused by the shock and the response alone.",
        "Capacity and demand changes and the delivery-delay window use the same months as the fuel shock. Capacity can only be cut. "
        f"{engine.nonfuel_fixed_fraction:.0%} of non-fuel cost is assumed fixed when capacity changes; that share is an assumption, not a fit, "
        "and a cut can raise simulated profit when empty seats absorb it.",
        *([capacity["approximation"]] if capacity["status"] == "ok" else
          ["The measured capacity response was requested but is not built, so capacity follows the plan: " + capacity["reason"] + "."]
          if capacity["status"] == "not_computed" else []),
        f"Free cash flow moves with after-tax operating income ({engine.marginal_cash_tax_fraction:.0%} assumed cash tax) around the Q2 2026 cash run rate; capital spending stays at that run rate. "
        "No financing, hedging, working-capital timing or profit sharing is modeled.",
        "Bridge steps are means across paths over the first 12 reported months, applied in the order fuel cost, fare recovery, "
        "capacity (with delivery delays and any measured capacity response), demand; they add up to net.",
        ("Per share: change in operating income x (1 - tax rate) / diluted shares, with the tax rate and share count of the quarter ended "
         f"{per_share['source']['period_end']}. Interest, profit sharing and share-count changes are held fixed."
         if per_share["available"] else "Per-share fields are null: " + per_share["reason"] + "."),
        "Probabilities are shares of simulated paths under these assumptions, not calibrated event probabilities. Ranges exclude parameter and model uncertainty other than the recovery curve's. "
        "Months more than 12 after the cutoff are exploratory in the engine's model card."]


def _modes(response_modes: dict | None) -> tuple[dict, dict, dict]:
    """Every mode in order, those with a usable Response, and why the others are missing."""
    modes = {**default_responses(skip=response_modes or ()), **(response_modes or {})}
    modes = {name: modes[name] for name in (*MODES, *(n for n in modes if n not in MODES))}
    ready = {name: m for name, m in modes.items() if isinstance(m, R.Response)}
    absent = {name: str(m) if isinstance(m, Exception) else "no response was supplied" for name, m in modes.items() if name not in ready}
    return modes, ready, absent


def _hit(run_id: str, started: float) -> dict | None:
    with _cache_lock:
        hit = _cache.get(run_id)
        if hit is None:
            return None
        _cache.move_to_end(run_id)
        return {**deepcopy(hit), "cached": True, "elapsed_ms": round((perf_counter() - started) * 1000)}


def _keep(run_id: str, result: dict, started: float) -> dict:
    result["elapsed_ms"] = round((perf_counter() - started) * 1000)
    with _cache_lock:
        _cache[run_id] = deepcopy(result)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return result


def _capacity_block(capacity, s: MonthlyScenario, shock_pct: float, lead: int) -> tuple[dict, list | None]:
    """What the output says about the capacity response, and the path groups to roll out (None when nothing to apply)."""
    if capacity is None:
        return {"status": "not_requested"}, None
    if not isinstance(capacity, R.Response):
        return {"status": "not_computed", "reason": str(capacity)}, None
    groups, change = _capacity_groups(capacity, s, shock_pct, lead)
    periods = _periods(month_sequence(INFORMATION_CUTOFF, s.horizon_months), lead)
    block = {"status": "ok", **{k: v for k, v in capacity.describe().items() if k != "name"},
             "unit": "percentage points of capacity growth per 10% fuel price increase, by quarter after the shock",
             "capacity_change_pct": [{"period": period, **_quantiles(change[:, index].mean(axis=1), 2)} for period, index in periods.items()],
             "groups": CAPACITY_GROUPS,
             "approximation": f"Measured capacity response: one draw per path, applied as {CAPACITY_GROUPS} groups of paths ranked by the size of "
                              "their draw, each with its group's mean monthly cut. It answers the scenario's fuel shock only, not simulated fuel noise; "
                              "a change in year-over-year capacity growth is read as the same percentage change in capacity against plan; "
                              "draws that would add capacity are set to zero because the simulator models cuts only. "
                              "It comes on top of any capacity_change_pct and is drawn independently of the fare response."}
    return block, groups if change.any() else None


def run_scenario(request: dict, response_modes: dict | None = None) -> dict:
    """Simulate the request under every response mode. `response_modes` overrides or adds modes by name."""
    started = perf_counter()
    if not isinstance(request, dict):
        return {"status": "invalid", "errors": ["request must be a JSON object with any of: " + ", ".join(FIELDS)]}
    c = _context()
    req, fuel, engine_pct, errors = _checked(request, c)
    if errors:
        return {"status": "invalid", "errors": errors}
    modes, ready, absent = _modes(response_modes)
    capacity = _capacity(req)
    per_share = per_share_inputs()
    noise = _noise_used(req, c["noise"])
    cited = [*ready.values(), *([capacity] if isinstance(capacity, R.Response) else [])]
    inputs = [_file(ANCHORS), _file(FUEL_CSV)] + [{"path": m.source["path"], "sha256": m.source["sha256"]}
                                                  for m in cited if {"path", "sha256"} <= set(m.source)]
    if per_share["available"]:
        inputs.append({k: per_share["source"][k] for k in ("path", "sha256")})
    identity = {"request": req, "demand_noise": [noise["note"], noise["check"]],
                "information_cutoff": INFORMATION_CUTOFF, "code_sha256": CODE_SHA256,
                "evidence": digest(c["snapshot"]), "fuel": digest(c["fuel"]),
                "responses": {name: ready[name].fingerprint if name in ready else "not_computed: " + absent[name] for name in modes},
                "capacity": capacity.fingerprint if isinstance(capacity, R.Response) else None if capacity is None else str(capacity),
                "per_share": [per_share["tax_rate"], per_share["diluted_shares_m"]]}
    run_id = digest(identity)
    hit = _hit(run_id, started)
    if hit is not None:
        return hit
    lead = c["lead"]
    s = _engine_scenario(req, c, engine_pct)
    capacity_used, groups = _capacity_block(capacity, s, fuel["shock_pct"], lead)
    capacity_on = req["capacity_change_pct"] != 0 or req["delivery_delay"]["aircraft"] > 0 or groups is not None
    demand_on = req["demand_shock_pct"] != 0
    fuel_only = replace(s, demand_shock_pct=0, capacity_shock_pct=0, delivery_delay_months=0, delivery_aircraft_count=None)
    output, errors_by_mode = {}, {}
    try:
        raw = _worlds(s, c)
        for name in modes:
            if name not in ready:
                output[name] = {"status": "not_computed", "reason": absent[name]}
                continue
            recovery = ready[name].monthly_shares(s.paths, s.horizon_months, s.seed)
            rolls = {"control": _roll(raw, s, recovery, shocked=False), "intervention": _roll(raw, s, recovery, groups=groups)}
            if capacity_on or demand_on:
                rolls["fuel_only"] = _roll(raw, fuel_only, recovery)
            if capacity_on and demand_on:
                rolls["fuel_capacity"] = _roll(raw, replace(s, demand_shock_pct=0), recovery, groups=groups)
            output[name], errors_by_mode[name] = _mode(rolls, raw["months"], lead, per_share, capacity_on, demand_on)
    except ValueError as error:
        return {"status": "invalid", "errors": [str(error)]}
    result = {
        "status": "ok", "run_id": run_id, "elapsed_ms": 0, "cached": False, "request": req, "paths": req["paths"],
        "as_of": {"information_cutoff": INFORMATION_CUTOFF, "fuel_last_observation": raw["fuel_last_observation"],
                  "shock_starts": raw["months"][lead], "months": raw["months"][lead:],
                  "unreported_lead_months": raw["months"][:lead]},
        "fuel": fuel, "modes": output,
        "bridge_per_share": output.get("measured", {}).get("bridge"),
        "response_used": {name: m.describe() for name, m in ready.items()},
        "capacity_response": capacity_used,
        "per_share": per_share,
        "monte_carlo": {"paths": req["paths"], "seed": req["seed"],
                        "method": f"standard error by batch means: the statistic on {BATCHES} disjoint groups of paths, spread / sqrt({BATCHES})",
                        "standard_error": errors_by_mode, "demand_noise": noise},
        "inputs": inputs, "model": {"engine_version": VERSION, "code_sha256": CODE_SHA256, "code_files": list(CODE_FILES)},
        "assumptions": _assumptions(fuel, per_share, raw["months"][lead], capacity_used, noise)}
    if any(m.source.get("kind") == "synthetic" for m in cited):
        result["synthetic_response"] = True
    return _keep(run_id, result, started)


def _cached(run) -> dict | None:
    if isinstance(run, str):
        with _cache_lock:
            return deepcopy(_cache.get(run))
    return run if isinstance(run, dict) else None


def compare(run_a, run_b) -> dict:
    """b minus a: differences of the P50s and of the probabilities, per mode and quarter.

    Each argument is a run_scenario result or the run_id of one still in the cache.
    """
    a, b = _cached(run_a), _cached(run_b)
    errors = [f"run_{label} is not an ok scenario result or a cached run_id" for label, run in (("a", a), ("b", b))
              if run is None or run.get("status") != "ok"]
    if errors:
        return {"status": "invalid", "errors": errors}

    def gap(x, y, digits):
        return None if x is None or y is None else round(y["p50"] - x["p50"], digits)

    modes = {}
    for name in a["modes"]:
        left, right = a["modes"][name], b["modes"].get(name, {})
        if left.get("status") != "ok" or right.get("status") != "ok":
            modes[name] = {"status": "not_computed"}
            continue
        later = {q["period"]: q for q in right["quarters"]}
        modes[name] = {
            "status": "ok",
            "quarters": [{"period": q["period"],
                          "operating_income_musd_p50": gap(q["operating_income_musd"], later[q["period"]]["operating_income_musd"], 1),
                          "operating_margin_p50": gap(q["operating_margin"], later[q["period"]]["operating_margin"], 4),
                          "fcf_musd_p50": gap(q["fcf_musd"], later[q["period"]]["fcf_musd"], 1),
                          "eps_change_p50": gap(q["eps_change"], later[q["period"]]["eps_change"], 3)}
                         for q in left["quarters"] if q["period"] in later],
            "next_12m": {"operating_income_musd_p50": gap(left["next_12m"]["operating_income_musd"], right["next_12m"]["operating_income_musd"], 1),
                         "fcf_musd_p50": gap(left["next_12m"]["fcf_musd"], right["next_12m"]["fcf_musd"], 1),
                         "eps_change_p50": gap(left["next_12m"]["eps_change"], right["next_12m"]["eps_change"], 3)},
            "prob": {"fcf_negative_12m": round(right["prob"]["fcf_negative_12m"] - left["prob"]["fcf_negative_12m"], 4)}}
    return {"status": "ok", "definition": "run_b minus run_a", "run_a": a["run_id"], "run_b": b["run_id"],
            "request_a": a["request"], "request_b": b["request"], "same_worlds": a["request"]["seed"] == b["request"]["seed"]
            and a["paths"] == b["paths"] and a["request"]["horizon_months"] == b["request"]["horizon_months"], "modes": modes}


def boundary(xs, ys, prob) -> list[dict]:
    """Points where prob crosses 0.5, by linear interpolation between adjacent tested points along each axis.

    prob[j][i] belongs to (xs[i], ys[j]). A tested point exactly at 0.5 is itself a boundary point.
    """
    points = set()
    for j, y in enumerate(ys):
        for i, x in enumerate(xs):
            here = prob[j][i] - .5
            if here == 0:
                points.add((float(x), float(y)))
            if i + 1 < len(xs) and here * (prob[j][i + 1] - .5) < 0:
                points.add((x + here / (prob[j][i] - prob[j][i + 1]) * (xs[i + 1] - x), float(y)))
            if j + 1 < len(ys) and here * (prob[j + 1][i] - .5) < 0:
                points.add((float(x), y + here / (prob[j][i] - prob[j + 1][i]) * (ys[j + 1] - y)))
    return [{"x": round(x, 4) + 0.0, "y": round(y, 4) + 0.0} for x, y in sorted(points)]


def _axis(errors: list[str], name: str, spec) -> tuple[str | None, list[float]]:
    if not isinstance(spec, dict) or set(spec) != {"driver", "values"}:
        errors.append(f'{name} must be an object like {{"driver": "fuel_shock_pct", "values": [0, 20, 40]}}')
        return None, []
    driver, values = spec["driver"], spec["values"]
    if driver not in GRID_DRIVERS:
        errors.append(f"{name}.driver must be one of {list(GRID_DRIVERS)}; got {driver!r}")
        driver = None
    if (not isinstance(values, list) or not values
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values)):
        errors.append(f"{name}.values must be a non-empty list of numbers")
        return driver, []
    if len(set(values)) != len(values):
        errors.append(f"{name}.values must not repeat a value")
    return driver, sorted(float(v) for v in set(values))


def _cell_request(base: dict, settings: dict) -> dict:
    request = dict(base)
    for driver, value in settings.items():
        if driver in ("fuel_shock_pct", "fuel_usd_gal"):
            request[driver] = value
        elif driver == "delivery_delay_months":
            aircraft = base["delivery_delay"]["aircraft"] if isinstance(base.get("delivery_delay"), dict) else 0
            request["delivery_delay"] = {"aircraft": aircraft if value else 0, "months": value}
        elif driver != "recovery_share":
            request[driver] = value
    return request


def run_grid(request: dict, response_modes: dict | None = None) -> dict:
    """One metric against a threshold over two drivers, every cell on the same simulated worlds."""
    started = perf_counter()
    if not isinstance(request, dict):
        return {"status": "invalid", "errors": ["request must be a JSON object with: " + ", ".join(GRID_FIELDS)]}
    errors: list[str] = []
    unknown = sorted(set(request) - set(GRID_FIELDS), key=str)
    if unknown:
        errors.append(f"unknown field(s) {unknown}; allowed fields are {list(GRID_FIELDS)}")
    (x_driver, xs), (y_driver, ys) = _axis(errors, "x", request.get("x")), _axis(errors, "y", request.get("y"))
    if x_driver is not None and (x_driver == y_driver or {x_driver, y_driver} == {"fuel_shock_pct", "fuel_usd_gal"}):
        errors.append("x and y must be different drivers, and only one of them may be a fuel driver")
    if len(xs) * len(ys) > GRID_CELLS:
        errors.append(f"the grid has {len(xs) * len(ys)} cells; the limit is {GRID_CELLS} (for example 9 by 9)")
    metric, direction = request.get("metric", "fcf_12m_musd"), request.get("direction", "below")
    threshold, mode, base = request.get("threshold", 0), request.get("mode", "measured"), request.get("base") or {}
    if metric not in GRID_METRICS:
        errors.append(f"metric must be one of {list(GRID_METRICS)}; got {metric!r}")
    if direction not in ("below", "at_or_above"):
        errors.append(f'direction must be "below" or "at_or_above"; got {direction!r}')
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
        errors.append(f"threshold must be a number; got {threshold!r}")
    if not isinstance(base, dict):
        errors.append("base must be a scenario request object")
        base = {}
    drivers = [d for d in (x_driver, y_driver) if d is not None]
    for driver in drivers:
        taken = ("fuel_shock_pct", "fuel_usd_gal") if driver in ("fuel_shock_pct", "fuel_usd_gal") else (driver,) if driver in FIELDS else ()
        if any(base.get(name) is not None for name in taken):
            errors.append(f"remove {' and '.join(n for n in taken if base.get(n) is not None)} from base: {driver} is a grid driver")
        low, high = RECOVERY_SHARE_BOUNDS
        if driver == "recovery_share" and any(not low <= v <= high for v in (xs if driver == x_driver else ys)):
            errors.append(f"recovery_share values must be between {low:g} and {high:g}")
        if driver == "delivery_delay_months" and not (isinstance(base.get("delivery_delay"), dict) and base["delivery_delay"].get("aircraft")):
            errors.append("set base.delivery_delay.aircraft above zero to vary delivery_delay_months")
    c = _context()
    checked = {}
    for y in ys:
        for x in xs:
            cell = {driver: int(value) if driver == "delivery_delay_months" and value == int(value) else value
                    for driver, value in ((x_driver, x), (y_driver, y)) if driver is not None}
            checked[x, y] = (*_checked(_cell_request(base, cell), c), cell.get("recovery_share"))
            errors.extend(e for e in checked[x, y][3] if e not in errors)
    lead = c["lead"]
    canonical = next(iter(checked.values()))[0] if checked else _validate(base, c["noise"])[0]
    periods = _periods(month_sequence(INFORMATION_CUTOFF, lead + canonical["horizon_months"]), lead)
    period = request.get("period") or ("12m" if metric != "operating_margin" else next(iter(periods)))
    allowed = ["12m"] if metric in ("fcf_12m_musd", "operating_income_12m_musd") else [*periods, *(["12m"] if metric == "eps_change" else [])]
    if metric in GRID_METRICS and period not in allowed:
        errors.append(f"period for {metric} must be one of {allowed}; got {period!r}")
    modes, ready, absent = _modes(response_modes)
    if mode not in modes:
        errors.append(f"mode must be one of {list(modes)}; got {mode!r}")
    if errors:
        return {"status": "invalid", "errors": errors[:12]}
    if "recovery_share" not in drivers and mode not in ready:
        return {"status": "not_computed", "reason": absent[mode]}
    per_share = per_share_inputs()
    if metric == "eps_change" and not per_share["available"]:
        return {"status": "not_computed", "reason": per_share["reason"]}
    capacity = _capacity(canonical)
    months = lead + canonical["horizon_months"]
    budget = GRID_PATH_MONTHS // (len(checked) * months) // 100 * 100
    paths = min(canonical["paths"], max(GRID_MIN_PATHS, budget))
    response = None if "recovery_share" in drivers else ready[mode]
    driven = set(drivers) | ({"fuel_shock_pct", "fuel_usd_gal"} if {"fuel_shock_pct", "fuel_usd_gal"} & set(drivers) else set())
    shown = {k: v for k, v in canonical.items() if k not in driven}
    if "delivery_delay_months" in drivers:
        shown["delivery_delay"] = {"aircraft": base["delivery_delay"]["aircraft"]}
    grid = {"x": {"driver": x_driver, "values": xs}, "y": {"driver": y_driver, "values": ys}, "metric": metric, "period": period,
            "threshold": threshold, "direction": direction, "mode": mode, "base": shown}
    run_id = digest({"kind": "grid", "request": grid, "paths": paths, "information_cutoff": INFORMATION_CUTOFF,
                     "code_sha256": CODE_SHA256, "evidence": digest(c["snapshot"]), "fuel": digest(c["fuel"]),
                     "response": "flat recovery_share" if response is None else response.fingerprint,
                     "capacity": capacity.fingerprint if isinstance(capacity, R.Response) else None if capacity is None else str(capacity),
                     "per_share": [per_share["tax_rate"], per_share["diluted_shares_m"]] if metric == "eps_change" else None})
    hit = _hit(run_id, started)
    if hit is not None:
        return hit
    first = next(iter(checked.values()))
    world = _engine_scenario(first[0], c, first[2], paths)
    index = list(range(lead, lead + 12)) if period == "12m" else periods[period]
    factor = (1 - per_share["tax_rate"]) / per_share["diluted_shares_m"] if metric == "eps_change" else None
    curves, cuts, capacity_used, cells, prob = {}, {}, {"status": "not_requested"}, [], []
    try:
        raw = _worlds(world, c)
        for y in ys:
            prob.append([])
            for x in xs:
                req, fuel, engine_pct, _, share = checked[x, y]
                case = _engine_scenario(req, c, engine_pct, paths)
                if share not in curves:
                    recovery = (R.flat(share) if response is None else response).monthly_shares(paths, case.horizon_months, case.seed)
                    reference = (_roll(raw, world, recovery, shocked=False)["operating_income_musd"][:, index].sum(axis=1)
                                 if factor is not None else None)
                    curves[share] = recovery, reference
                recovery, reference = curves[share]
                if capacity is not None and fuel["shock_pct"] not in cuts:
                    capacity_used, cuts[fuel["shock_pct"]] = _capacity_block(capacity, case, fuel["shock_pct"], lead)
                roll = _roll(raw, case, recovery, groups=cuts.get(fuel["shock_pct"]))
                income = roll["operating_income_musd"][:, index].sum(axis=1)
                values = (roll["free_cash_flow_musd"][:, index].sum(axis=1) if metric == "fcf_12m_musd" else income if metric == "operating_income_12m_musd"
                          else income / roll["revenue_musd"][:, index].sum(axis=1) if metric == "operating_margin" else (income - reference) * factor)
                prob[-1].append(float((values < threshold).mean() if direction == "below" else (values >= threshold).mean()))
                cells.append({"x": x, "y": y, "prob": round(prob[-1][-1], 4), "p50": round(float(np.median(values)), GRID_METRICS[metric]) + 0.0})
    except ValueError as error:
        return {"status": "invalid", "errors": [str(error)]}
    spread = [math.sqrt(p * (1 - p) / paths) for row in prob for p in row]
    result = {
        "status": "ok", "run_id": run_id, "elapsed_ms": 0, "cached": False, **grid,
        "cells": cells, "boundary": boundary(xs, ys, prob),
        "boundary_method": "Where prob crosses 0.5: linear interpolation between adjacent tested points along each axis. No runs were made between tested points.",
        "prob_definition": f"Share of simulated runs with {metric} {'below' if direction == 'below' else 'at or above'} {threshold:g} at that tested point; "
                           "it is not the probability of the event in the world.",
        "paths": paths,
        "monte_carlo": {"paths": paths, "paths_requested": canonical["paths"], "seed": canonical["seed"],
                        "prob_standard_error_max": round(max(spread), 4), "prob_standard_error_at_half": round(.5 / math.sqrt(paths), 4),
                        "method": "binomial: sqrt(prob x (1 - prob) / paths)"},
        "response_used": ({"name": "recovery_share", "note": "Every quarter's recovery share is set to the grid value on all paths; the mode's curve is not used."}
                          if response is None else response.describe()),
        "capacity_response": {k: v for k, v in capacity_used.items() if k != "capacity_change_pct"} if capacity is not None else capacity_used,
        "notes": [f"All {len(cells)} cells use the same {paths} simulated fuel and demand worlds, so differences between cells are caused by the drivers alone.",
                  f"Paths were lowered from {canonical['paths']} to {paths} to keep the grid fast." if paths < canonical["paths"] else f"Paths: {paths}.",
                  "Cells are listed by y, then x. p50 is the median of the metric in that cell."
                  + (" eps_change is against the no-shock reference on the same worlds." if metric == "eps_change" else "")]}
    if any(m.source.get("kind") == "synthetic" for m in (response, capacity) if isinstance(m, R.Response)):
        result["synthetic_response"] = True
    return _keep(run_id, result, started)


def _built(path: Path) -> dict:
    """A built output file as a dict; empty when it is absent, unreadable or a placeholder."""
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and not data.get("fixture") else {}


def _latest_fuel() -> dict | None:
    if not FUEL_DAILY.is_file():
        return None
    with FUEL_DAILY.open(newline="", encoding="utf-8") as stream:
        rows = [row for row in csv.DictReader(stream) if row.get("date") and row.get("usd_per_gallon")]
    return {"date": rows[-1]["date"], "usd_gal": float(rows[-1]["usd_per_gallon"])} if rows else None


def today_request(apply_demand: bool = False) -> dict:
    """A scenario request for what is observable now, with where each number came from.

    The model's information cutoff does not move: today's fuel enters as a level against the
    Q2 2026 reference. The demand reading is context; it becomes a demand shock only when
    apply_demand is true. `limits.delivery_delay_max_aircraft` gives, for each hold length
    from 1 to 12 months, the most aircraft a delivery delay may name (the mainline aircraft
    the fleet plan delivers in that window).
    """
    spot = _latest_fuel()
    if spot is None:
        return {"status": "not_computed", "reason": "data/lab/fuel_daily.csv has no fuel price to start from"}
    assumptions, basis = [], []
    fit = _built(COST_CLOCK).get("fit")
    usable = isinstance(fit, dict) and all(isinstance(fit.get(k), (int, float)) and not isinstance(fit.get(k), bool)
                                           and math.isfinite(fit[k]) for k in ("basis_usd_gal", "slope"))
    if usable:
        level = fit["basis_usd_gal"] + fit["slope"] * spot["usd_gal"]
        basis.append({"field": "fuel_usd_gal", "value": round(level, 3), "date": spot["date"], "source": [_file(FUEL_DAILY), _file(COST_CLOCK)],
                      "how": f"Latest Gulf Coast jet fuel print {spot['usd_gal']:.3f} converted to the price United pays with the cost clock: "
                             f"{fit['basis_usd_gal']:g} + {fit['slope']:g} x spot. This is the price paid if today's spot lasts; the clock's lag is not applied."})
    else:
        level = spot["usd_gal"]
        basis.append({"field": "fuel_usd_gal", "value": round(level, 3), "date": spot["date"], "source": [_file(FUEL_DAILY)],
                      "how": "Latest Gulf Coast jet fuel print, used as it is."})
        assumptions.append("The cost clock (app/static/data/cost_clock.json) is not built, so today's fuel is the raw spot price, "
                           "not converted to the price United pays.")
    request = {"fuel_usd_gal": round(level, 3), "fuel_shock_months": 12, "horizon_months": 12}
    basis.append({"field": "fuel_shock_months", "value": 12, "date": None, "source": [],
                  "how": "Assumption, not an observation: today's fuel price is held for all 12 reported months."})
    demand = {"status": "not_computed", "reason": "app/static/data/demand_signal.json has not been built"}
    signal = _built(DEMAND_SIGNAL)
    quarters = signal.get("quarterly") if isinstance(signal.get("quarterly"), list) else []
    latest = next((q for q in reversed(quarters) if isinstance(q, dict) and isinstance(q.get("yoy"), (int, float))), None)
    if latest is not None:
        demand = {"status": "ok", "yoy_pct": round(100 * latest["yoy"], 2), "period": latest["period"], "as_of": signal.get("as_of"),
                  "days_used": latest.get("days_used"), "days_in_period": latest.get("days_in_period"), "complete": latest.get("complete"),
                  "series": (signal.get("source") or {}).get("series"), "applied": bool(apply_demand), "source": _file(DEMAND_SIGNAL)}
        if apply_demand:
            request["demand_shock_pct"] = demand["yoy_pct"]
            basis.append({"field": "demand_shock_pct", "value": demand["yoy_pct"], "date": demand["as_of"], "source": [_file(DEMAND_SIGNAL)],
                          "how": f"{demand['period']} to date, travelers against the same days a year earlier ({demand['days_used']} days). "
                                 "Approximation: a national year-over-year change is used as United's demand shock against plan."})
        else:
            assumptions.append(f"Demand is left at plan. For context, travelers in {demand['period']} to date are {demand['yoy_pct']:+.2f}% "
                               f"against a year earlier ({demand['days_used']} days, as of {demand['as_of']}).")
    elif apply_demand:
        assumptions.append("A demand shock was asked for but no demand reading is built, so demand is left at plan.")
    c = _context()
    _, fuel, _, errors = _checked(request, c)
    if errors:
        return {"status": "invalid", "errors": errors, "request": request, "basis": basis}
    assumptions.insert(0, f"The model still starts from United's Q2 2026 business as reported by {INFORMATION_CUTOFF}; "
                          f"today's fuel enters as a level against the {fuel['reference_usd_gal']:.2f} reference ({fuel['shock_pct']:+.1f}%).")
    return {"status": "ok", "request": request, "basis": basis, "information_cutoff": INFORMATION_CUTOFF,
            "context": {"fuel": {"spot_usd_gal": spot["usd_gal"], "date": spot["date"], "converted_with_cost_clock": usable, **fuel}, "demand": demand},
            "limits": {"delivery_delay_max_aircraft": {str(months): _eligible_aircraft(c, {"fuel_shock_months": months}) for months in range(1, 13)}},
            "assumptions": assumptions}


def validate_response(result) -> list[str]:
    """Problems with a run_scenario result against the documented shape; empty when it conforms."""
    problems: list[str] = []

    def need(condition, message):
        if not condition:
            problems.append(message)

    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    def band(value, where, optional=False):
        if value is None and optional:
            return
        ok = isinstance(value, dict) and set(value) == {"p10", "p50", "p90"} and all(number(v) for v in value.values())
        need(ok and value["p10"] <= value["p50"] <= value["p90"], f"{where} must be ordered p10/p50/p90 numbers")

    if not isinstance(result, dict) or result.get("status") not in ("ok", "invalid"):
        return ["result must be an object with status ok or invalid"]
    if result["status"] == "invalid":
        need(isinstance(result.get("errors"), list) and result["errors"] and all(isinstance(e, str) for e in result["errors"]),
             "an invalid result needs a non-empty list of error strings")
        return problems
    top = {"status", "run_id", "elapsed_ms", "cached", "request", "paths", "as_of", "fuel", "modes", "bridge_per_share",
           "response_used", "capacity_response", "per_share", "monte_carlo", "inputs", "model", "assumptions"}
    need(set(result) - {"synthetic_response"} == top, f"top-level fields must be exactly {sorted(top)}")
    if problems:
        return problems
    need(isinstance(result["run_id"], str) and len(result["run_id"]) == 64, "run_id must be a SHA-256 hex string")
    need(isinstance(result["cached"], bool) and number(result["elapsed_ms"]), "cached must be a boolean and elapsed_ms a number")
    need(isinstance(result["assumptions"], list) and all(isinstance(a, str) for a in result["assumptions"]), "assumptions must be strings")
    need(all(number(result["fuel"].get(k)) for k in ("reference_usd_gal", "level_usd_gal", "shock_pct", "shock_usd_gal")), "fuel must state the level and the percentage")
    priced = result["per_share"].get("available") is True
    need(priced == number(result["per_share"].get("tax_rate")) == number(result["per_share"].get("diluted_shares_m"))
         and (priced or isinstance(result["per_share"].get("reason"), str)), "per_share must carry sourced inputs or a reason")
    need(set(MODES) <= set(result["modes"]), f"modes must include {list(MODES)}")
    need(result["capacity_response"].get("status") in ("not_requested", "not_computed", "ok"), "capacity_response needs a status")
    noise = result["monte_carlo"].get("demand_noise") if isinstance(result["monte_carlo"], dict) else None
    need(isinstance(noise, dict) and set(noise) == {"monthly_log_sd", "source", "engine_default", "check", "note"}
         and number(noise["monthly_log_sd"]) and noise["source"] in ("request", "engine_default")
         and (noise["source"] == "request") == (noise["monthly_log_sd"] != noise["engine_default"])
         and (noise["check"] is None or isinstance(noise["check"], dict)) and isinstance(noise["note"], str),
         "monte_carlo.demand_noise must state the value, whose it is and the check behind the assumed value")

    def bridge(rows, where):
        need(isinstance(rows, list) and [row.get("step") for row in rows] == list(BRIDGE_STEPS), f"{where} must list the steps {list(BRIDGE_STEPS)}")
        for row in rows if isinstance(rows, list) else []:
            modelled = row["step"] != "profit sharing"
            need(set(row) == {"step", "value", "operating_income_musd"}, f"{where} step fields")
            need(number(row["operating_income_musd"]) == modelled, f"{where} {row['step']}: operating_income_musd")
            need(number(row["value"]) == (modelled and priced), f"{where} {row['step']}: per-share value")

    for name, mode in result["modes"].items():
        if mode.get("status") == "not_computed":
            need(isinstance(mode.get("reason"), str), f"{name}: not_computed needs a reason")
            continue
        need(mode.get("status") == "ok" and set(mode) == {"status", "quarters", "prob", "next_12m", "bridge"}, f"{name}: mode fields")
        if problems:
            return problems
        need(name in result["response_used"] and len(result["response_used"][name]["curve_p50"]) == R.QUARTERS, f"{name}: response_used curve")
        need(len(mode["quarters"]) >= 4, f"{name}: at least four quarters")
        for q in mode["quarters"]:
            where = f"{name} {q.get('period')}"
            need(set(q) == {"period", "months", "complete", "operating_income_musd", "operating_margin", "fcf_musd",
                            "operating_income_change_musd", "eps_change", "fare_change_pct", "no_shock"}, f"{where}: quarter fields")
            if problems:
                return problems
            need(isinstance(q["period"], str) and len(q["period"]) == 6 and q["period"][4] == "Q", f"{where}: period like 2026Q4")
            need(q["complete"] == (len(q["months"]) == 3), f"{where}: complete flag")
            for key in ("operating_income_musd", "operating_margin", "fcf_musd", "operating_income_change_musd", "fare_change_pct"):
                band(q[key], f"{where} {key}")
            band(q["eps_change"], f"{where} eps_change", optional=not priced)
            for key in ("operating_income_musd", "operating_margin", "fcf_musd"):
                band(q["no_shock"].get(key), f"{where} no_shock {key}")
        for key in ("fcf_negative_12m", "fcf_negative_12m_no_shock"):
            need(number(mode["prob"].get(key)) and 0 <= mode["prob"][key] <= 1, f"{name}: prob.{key} must be in [0, 1]")
        for key in ("operating_income_musd", "operating_income_change_musd", "fcf_musd"):
            band(mode["next_12m"].get(key), f"{name} next_12m {key}")
        band(mode["next_12m"].get("eps_change"), f"{name} next_12m eps_change", optional=not priced)
        bridge(mode["bridge"], f"{name} bridge")
    if result["modes"]["measured"].get("status") == "ok":
        bridge(result["bridge_per_share"], "bridge_per_share")
    else:
        need(result["bridge_per_share"] is None, "bridge_per_share must be null until the measured mode is computed")
    return problems
