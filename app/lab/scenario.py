"""Run one business scenario under three pricing responses on the same simulated worlds.

`run_scenario(request)` is what POST /api/lab/scenario returns. It drives the monthly
simulator (app/monthly.py) once per response (costs only, management's promise, the
measured recovery curve) with identical fuel and demand paths, so every difference
between modes is caused by the response. `compare(run_a, run_b)` subtracts two runs,
`baseline_fuel_usd_gal()` states the reference fuel price, `validate_response` checks
a result against the shape below.

Starting point: United's Q2 2026 business as reported by 16 July 2026 (the latest
reported quarter until 20 October 2026). Fuel moves after that date are not in the
no-shock reference. The engine projects from August 2026; the two months before
the first whole quarter are simulated but not reported, and every shock starts in
October 2026, so reported periods are whole fiscal quarters from 2026Q4 and quarter
k of the recovery curve is the k-th reported quarter after the shock starts.

Request (all fields optional; unknown fields are rejected):
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
  horizon_months       12 to 22 reported months (default 12).
  seed                 0 to 1,000,000,000 (default 1).
  paths                200 to 20,000 simulated worlds (default DEFAULT_PATHS).

Response:
  {"status": "ok", "run_id", "elapsed_ms", "cached", "request", "paths", "as_of", "fuel",
   "modes": {mode: {"status": "ok", "quarters": [...], "prob": {...}, "next_12m": {...}, "bridge": [...]}
             | {"status": "not_computed", "reason"}},
   "bridge_per_share", "response_used", "per_share", "monte_carlo", "inputs", "model", "assumptions"}
or {"status": "invalid", "errors": [...]}. Nothing is raised for a bad request. A result
computed with a response whose source kind is "synthetic" also carries "synthetic_response": true.

Differences from the draft in docs/lab_contract.md:
- Request gains `fuel_usd_gal` and `paths`. `fuel_shock_pct` is defined on the price
  United pays (one reference price), not on the spot price the engine uses internally.
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
- New top-level fields: `cached`, `request`, `paths`, `as_of`, `fuel`, `per_share`,
  `monte_carlo`, `inputs`, `model`.

Per-share conversion: change in operating income x (1 - tax rate) / diluted shares.
Neither data/ual_q2_2026.json nor data/monthly_anchors.json carries a tax rate or a
diluted share count (checked 2026-10-10), so both come from data/lab/ual_quarterly.csv:
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
from ..monthly import VERSION, MonthlyScenario, baseline, month_sequence, simulate_recovery
from . import response as R

ROOT = Path(__file__).resolve().parents[2]
QUARTERLY_CSV = ROOT / "data" / "lab" / "ual_quarterly.csv"
META = ROOT / "app" / "static" / "data" / "meta.json"
INFORMATION_CUTOFF = "2026-07-16"
DEFAULT_PATHS = 10000
MODES = ("costs_only", "management", "measured")
BATCHES = 20
CACHE_SIZE = 64
ENGINE_FUEL_PCT = (-60, 100)
ENGINE_MONTHS = 24
BOUNDS = {"fuel_shock_pct": (-50, 80), "fuel_shock_months": (1, 24), "capacity_change_pct": (-90, 0),
          "demand_shock_pct": (-80, 100), "horizon_months": (12, 22), "seed": (0, 1_000_000_000),
          "paths": (200, 20000), "delivery_delay.aircraft": (0, 1000), "delivery_delay.months": (0, 24)}
INTEGERS = {"fuel_shock_months", "horizon_months", "seed", "paths", "delivery_delay.aircraft", "delivery_delay.months"}
DEFAULTS = {"fuel_shock_months": 6, "capacity_change_pct": 0.0, "demand_shock_pct": 0.0,
            "horizon_months": 12, "seed": 1, "paths": DEFAULT_PATHS}
FIELDS = ("fuel_shock_pct", "fuel_usd_gal", "fuel_shock_months", "capacity_change_pct", "demand_shock_pct",
          "delivery_delay", "horizon_months", "seed", "paths")
BRIDGE_STEPS = ("fuel cost", "fare recovery", "capacity", "demand", "profit sharing", "net")
# United, 8-K of 15 July 2026, exhibit 99.2. First stated quarter is treated as the
# quarter the shock starts. Superseded by app/static/data/meta.json `promises` once built.
STATED_PROMISE = ({"quarters_after": 0, "low": .8, "high": .9}, {"quarters_after": 1, "low": 1.0, "high": 1.0})
PROMISE_SOURCE = {
    "accession": "0000100517-26-000135", "exhibit": "EX-99.2", "filed_on": "2026-07-15",
    "url": "https://www.sec.gov/Archives/edgar/data/100517/000010051726000135/ual_erx06302026xex992.htm",
    "path": "data/lab_raw/sec/0000100517-26-000135/ual_erx06302026xex992.htm",
    "sha256": "c4cc6beb1994c66370a93b8cf63191f0a18113125c0d9b7053bcfaa7dc570f9a",
    "quote": "The Company expects to recover 80% to 90% of the fuel price increase in the third quarter and 100% of the fuel price increase by the fourth quarter of 2026."}
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


def _context() -> dict:
    snapshot, fuel = load_snapshot(), load_fuel()
    known = [r for r in fuel if r["date"] <= INFORMATION_CUTOFF]
    return {"snapshot": snapshot, "fuel": fuel, "baseline": baseline(snapshot, INFORMATION_CUTOFF),
            "spot": known[-1], "latest": fuel[-1], "lead": _lead_months(INFORMATION_CUTOFF)}


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


def _management() -> R.Response:
    if not META.is_file():
        return R.management(STATED_PROMISE, source=PROMISE_SOURCE)
    try:
        meta = json.loads(META.read_bytes())
        promises = sorted(meta["promises"], key=lambda p: p["period"])
        if meta.get("fixture") or not promises:
            raise ValueError("no promises")
        return R.management(promises_in_order(promises), source={**_file(META), "periods": [p["period"] for p in promises],
                                                                "evidence_ids": [i for p in promises for i in p.get("evidence_ids", [])]})
    except (KeyError, TypeError, ValueError) as error:
        return R.management(STATED_PROMISE, source={**PROMISE_SOURCE, "meta_json_not_used": f"{type(error).__name__}: {error}"})


def promises_in_order(promises: list[dict]) -> list[dict]:
    """Calendar-period promises to quarters after the shock: the first stated period is quarter 0."""
    return [{"quarters_after": i, "low": p["recovery_low"], "high": p["recovery_high"]} for i, p in enumerate(promises)]


def default_responses(skip=()) -> dict:
    """Mode name to Response, or to the error explaining why the mode cannot run yet."""
    modes = {}
    if "costs_only" not in skip:
        modes["costs_only"] = R.costs_only()
    if "management" not in skip:
        modes["management"] = _management()
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


def _validate(request: dict) -> tuple[dict, list[str], bool]:
    """Canonical request, problems, and whether the fuel fields are usable for the range check."""
    errors: list[str] = []
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
    periods: dict[str, list[int]] = {}
    for i, month in enumerate(months):
        if i >= lead:
            periods.setdefault(f"{month[:4]}Q{(int(month[5:]) - 1) // 3 + 1}", []).append(i)

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


def _assumptions(fuel: dict, per_share: dict, start: str) -> list[str]:
    engine = MonthlyScenario()
    return [
        f"Starting point: United's Q2 2026 business as reported by {INFORMATION_CUTOFF} (four markets, fleet plan, costs, cash). "
        "Quarterly totals are spread evenly over months; no seasonality is modelled. Fuel moves after that date are not in the no-shock reference.",
        f"Fuel: the reference is the {fuel['reference_usd_gal']:.2f} US dollars a gallon United paid in Q2 2026. The shock moves that price to "
        f"{fuel['level_usd_gal']:.2f} on the central path for {fuel['held_months']} months from {start}, then {fuel['after_hold']}. "
        "Simulated paths still vary around it: monthly fuel and demand noise use pre-cutoff fuel volatility and assumed persistence and correlation.",
        "Response: every monthly change in the fuel price starts its own clock, and the recovery curve gives the share of that change, "
        "times gallons burned, that comes back as revenue by quarter after the change. Fuel price falls are passed back with the same curve, "
        "so a temporary shock is under-recovered while it builds and over-recovered while it fades.",
        "The recovery share is defined on revenue net of volume effects: no fare-demand elasticity, pass-through cap or fare-step limit is applied on top of it. "
        "`fare_change_pct` shows the average fare change that the recovery implies.",
        "One recovery curve is drawn per simulated path from the response's joint draws; management's ranges are sampled uniformly. "
        "The first quarter management stated is treated as the quarter the shock starts.",
        "Every mode, the no-shock reference and every bridge step use the same simulated fuel and demand paths, so differences are caused by the shock and the response alone.",
        "Capacity and demand changes and the delivery-delay window use the same months as the fuel shock. Capacity can only be cut. "
        f"{engine.nonfuel_fixed_fraction:.0%} of non-fuel cost is assumed fixed when capacity changes; that share is an assumption, not a fit, "
        "and a cut can raise simulated profit when empty seats absorb it.",
        f"Free cash flow moves with after-tax operating income ({engine.marginal_cash_tax_fraction:.0%} assumed cash tax) around the Q2 2026 cash run rate; capital spending stays at that run rate. "
        "No financing, hedging, working-capital timing or profit sharing is modelled.",
        "Bridge steps are means across paths over the first 12 reported months, applied in the order fuel cost, fare recovery, capacity (with delivery delays), demand; they add up to net.",
        ("Per share: change in operating income x (1 - tax rate) / diluted shares, with the tax rate and share count of the quarter ended "
         f"{per_share['source']['period_end']}. Interest, profit sharing and share-count changes are held fixed."
         if per_share["available"] else "Per-share fields are null: " + per_share["reason"] + "."),
        "Probabilities are shares of simulated paths under these assumptions, not calibrated event probabilities. Ranges exclude parameter and model uncertainty other than the recovery curve's. "
        "Months more than 12 after the cutoff are exploratory in the engine's model card."]


def run_scenario(request: dict, response_modes: dict | None = None) -> dict:
    """Simulate the request under every response mode. `response_modes` overrides or adds modes by name."""
    started = perf_counter()
    if not isinstance(request, dict):
        return {"status": "invalid", "errors": ["request must be a JSON object with any of: " + ", ".join(FIELDS)]}
    req, errors, fuel_ok = _validate(request)
    c = _context()
    if fuel_ok:
        fuel, engine_pct = _fuel(req, c, errors)
    start = month_sequence(INFORMATION_CUTOFF, c["lead"] + 1)[-1]
    aircraft, delay_months = req["delivery_delay"]["aircraft"], req["delivery_delay"]["months"]
    eligible = _eligible_aircraft(c, req)
    if aircraft > eligible:
        errors.append(f"delivery_delay.aircraft must be at most {eligible}: the fleet plan has {eligible} mainline aircraft "
                      f"entering in the {req['fuel_shock_months']} months from {start} (the shock window set by fuel_shock_months)")
    if errors:
        return {"status": "invalid", "errors": errors}
    modes = {**default_responses(skip=response_modes or ()), **(response_modes or {})}
    modes = {name: modes[name] for name in (*MODES, *(n for n in modes if n not in MODES))}
    ready = {name: m for name, m in modes.items() if isinstance(m, R.Response)}
    absent = {name: str(m) if isinstance(m, Exception) else "no response was supplied" for name, m in modes.items() if name not in ready}
    per_share = per_share_inputs()
    inputs = [_file(ANCHORS), _file(FUEL_CSV)] + [{"path": m.source["path"], "sha256": m.source["sha256"]}
                                                  for m in ready.values() if {"path", "sha256"} <= set(m.source)]
    if per_share["available"]:
        inputs.append({k: per_share["source"][k] for k in ("path", "sha256")})
    identity = {"request": req, "information_cutoff": INFORMATION_CUTOFF, "code_sha256": CODE_SHA256,
                "evidence": digest(c["snapshot"]), "fuel": digest(c["fuel"]),
                "responses": {name: ready[name].fingerprint if name in ready else "not_computed: " + absent[name] for name in modes},
                "per_share": [per_share["tax_rate"], per_share["diluted_shares_m"]]}
    run_id = digest(identity)
    with _cache_lock:
        hit = _cache.get(run_id)
        if hit is not None:
            _cache.move_to_end(run_id)
            return {**deepcopy(hit), "cached": True, "elapsed_ms": round((perf_counter() - started) * 1000)}
    lead = c["lead"]
    s = MonthlyScenario(information_cutoff=INFORMATION_CUTOFF, horizon_months=lead + req["horizon_months"],
                        paths=req["paths"], seed=req["seed"], fuel_shock_pct=engine_pct,
                        demand_shock_pct=req["demand_shock_pct"], capacity_shock_pct=req["capacity_change_pct"],
                        delivery_delay_months=delay_months, delivery_aircraft_count=aircraft or None,
                        onset_month=lead + 1, duration_months=req["fuel_shock_months"])
    capacity_on, demand_on = req["capacity_change_pct"] != 0 or aircraft > 0, req["demand_shock_pct"] != 0
    cases = {}
    if capacity_on or demand_on:
        cases["fuel_only"] = replace(s, demand_shock_pct=0, capacity_shock_pct=0, delivery_delay_months=0, delivery_aircraft_count=None)
    if capacity_on and demand_on:
        cases["fuel_capacity"] = replace(s, demand_shock_pct=0)
    try:
        raw = simulate_recovery(s, ready, snapshot=c["snapshot"], fuel=c["fuel"], cases=cases)
    except ValueError as error:
        return {"status": "invalid", "errors": [str(error)]}
    output, errors_by_mode = {}, {}
    for name in modes:
        if name in ready:
            output[name], errors_by_mode[name] = _mode(raw["responses"][name], raw["months"], lead, per_share, capacity_on, demand_on)
        else:
            output[name] = {"status": "not_computed", "reason": absent[name]}
    measured = output.get("measured", {})
    result = {
        "status": "ok", "run_id": run_id, "elapsed_ms": 0, "cached": False, "request": req, "paths": req["paths"],
        "as_of": {"information_cutoff": INFORMATION_CUTOFF, "fuel_last_observation": raw["fuel_last_observation"],
                  "shock_starts": raw["months"][lead], "months": raw["months"][lead:],
                  "unreported_lead_months": raw["months"][:lead]},
        "fuel": fuel, "modes": output,
        "bridge_per_share": measured.get("bridge"),
        "response_used": {name: m.describe() for name, m in ready.items()},
        "per_share": per_share,
        "monte_carlo": {"paths": req["paths"], "seed": req["seed"],
                        "method": f"standard error by batch means: the statistic on {BATCHES} disjoint groups of paths, spread / sqrt({BATCHES})",
                        "standard_error": errors_by_mode},
        "inputs": inputs, "model": {"engine_version": VERSION, "code_sha256": CODE_SHA256, "code_files": list(CODE_FILES)},
        "assumptions": _assumptions(fuel, per_share, start)}
    if any(m.source.get("kind") == "synthetic" for m in ready.values()):
        result["synthetic_response"] = True
    result["elapsed_ms"] = round((perf_counter() - started) * 1000)
    with _cache_lock:
        _cache[run_id] = deepcopy(result)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return result


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
           "response_used", "per_share", "monte_carlo", "inputs", "model", "assumptions"}
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
