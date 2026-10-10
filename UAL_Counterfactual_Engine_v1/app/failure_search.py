"""Bounded joint-driver scenario discovery using common worlds, not event odds."""
from dataclasses import asdict, dataclass, replace
import hashlib
import math
from pathlib import Path
import platform
from time import perf_counter

import numpy as np

from .data import fuel_volatility, load_fuel
from .evidence import digest, evidence_manifest, load_snapshot
from .fleet import capacity_paths
from .monthly import MonthlyScenario, VERSION as MODEL_VERSION, _rollout, _world, baseline, month_sequence, shock_weights, summary

VERSION = "joint-failure-search-0.1.0"
DRIVERS = {"fuel_shock_pct", "demand_shock_pct", "capacity_shock_pct", "aircraft_unavailable_pct", "regional_contract_shock_pct", "delivery_delay_months"}


@dataclass(frozen=True)
class SearchAxis:
    driver: str
    low: float
    high: float
    points: int = 5

    def values(self) -> list:
        if self.driver not in DRIVERS:
            raise ValueError("Unsupported search driver")
        if isinstance(self.points, bool) or not isinstance(self.points, int) or not 2 <= self.points <= 7:
            raise ValueError("Axis points must be an integer from 2 to 7")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (self.low, self.high)) or self.low >= self.high:
            raise ValueError("Axis limits must be finite and low must be less than high")
        values = np.linspace(self.low, self.high, self.points).tolist()
        if self.driver == "delivery_delay_months":
            if not float(self.low).is_integer() or not float(self.high).is_integer():
                raise ValueError("Delivery delay limits must be whole months")
            values = [int(round(v)) for v in values]
            if len(set(values)) != self.points:
                raise ValueError("Delivery delay range has fewer distinct months than requested points")
        return values


@dataclass(frozen=True)
class FailureSearch:
    x: SearchAxis = SearchAxis("fuel_shock_pct", 0, 60)
    y: SearchAxis = SearchAxis("demand_shock_pct", -20, 0)
    criterion: str = "cumulative_fcf"
    threshold_musd: float = 0
    statistic: str = "p50"

    def validate(self, base: MonthlyScenario) -> tuple[list, list]:
        base.validate()
        x, y = self.x.values(), self.y.values()
        if self.x.driver == self.y.driver:
            raise ValueError("Choose two different search drivers")
        if self.criterion not in {"cumulative_fcf", "minimum_cash"} or self.statistic not in {"p10", "p50", "p90"}:
            raise ValueError("Unsupported failure criterion or statistic")
        if isinstance(self.threshold_musd, bool) or not isinstance(self.threshold_musd, (int, float)) or not math.isfinite(self.threshold_musd) or not -100000 <= self.threshold_musd <= 100000:
            raise ValueError("Threshold must be finite and between -100000 and 100000 USD millions")
        if base.paths > 10000 or base.paths * base.horizon_months * len(x) * len(y) > 6000000:
            raise ValueError("Search supports at most 10000 paths and 6 million path-month-grid combinations; reduce grid or horizon")
        for a in x:
            for b in y:
                replace(base, **{self.x.driver: a, self.y.driver: b}).validate()
        return x, y


def failure_regions(cells: list[dict], x_values: list, y_values: list, mode: str) -> list[dict]:
    """Exact row runs of observed failing points. No interpolation or monotonicity."""
    lookup = {(c["x_index"], c["y_index"]): c["policies"][mode]["fails"] for c in cells}
    regions = []
    for j, y in enumerate(y_values):
        start = None
        for i in range(len(x_values) + 1):
            failed = i < len(x_values) and lookup[i, j]
            if failed and start is None:
                start = i
            if not failed and start is not None:
                regions.append({"y": y, "x_values": x_values[start:i], "tested_points": i - start})
                start = None
    return regions


def search_failures(base: MonthlyScenario, search: FailureSearch, snapshot: dict | None = None, fuel: list[dict] | None = None) -> dict:
    started = perf_counter()
    xs, ys = search.validate(base)
    snapshot = load_snapshot() if snapshot is None else snapshot
    b = baseline(snapshot, base.information_cutoff, base.cash_plan, base.purchase_plan)
    fuel = load_fuel() if fuel is None else fuel
    fuel = [r for r in fuel if r["date"] <= base.information_cutoff]
    if len(fuel) < 30:
        raise ValueError("At least 30 fuel observations at or before the cutoff are required")
    spot = fuel[-1]["usd_per_gallon"]
    # Draw once for the whole grid: differences between tested points should
    # reflect their drivers, rather than a new random world at every point.
    worlds = _world(base, spot, fuel_volatility(fuel) * math.sqrt(21))
    months = month_sequence(base.information_cutoff, base.horizon_months)
    policies = ("frozen", "assumed_constrained")
    initial_fleet = capacity_paths(b, base, b["fleet_plan"], months, shock_weights(base))
    controls = {}
    for mode in policies:
        control = _rollout(b, base, *worlds, spot, False, mode, initial_fleet)
        controls[mode] = control["free_cash_flow_musd"].sum(axis=1)
    cells = []
    q = {"p10": .1, "p50": .5, "p90": .9}[search.statistic]
    for j, y in enumerate(ys):
        for i, x in enumerate(xs):
            s = replace(base, **{search.x.driver: x, search.y.driver: y})
            fleet = capacity_paths(b, s, b["fleet_plan"], months, shock_weights(s))
            outcomes = {}
            for mode in policies:
                result = _rollout(b, s, *worlds, spot, True, mode, fleet)
                total_fcf = result["free_cash_flow_musd"].sum(axis=1)
                # The opening balance is part of a cash-floor condition. Quantile
                # of path minima differs from minimum of monthly quantiles.
                minimum_cash = np.minimum(b["cash_musd"], result["cash_musd"].min(axis=1))
                criterion_values = total_fcf if search.criterion == "cumulative_fcf" else minimum_cash
                selected = float(np.quantile(criterion_values, q))
                median_cash = np.median(result["cash_musd"], axis=0)
                lowest = int(median_cash.argmin())
                outcomes[mode] = {"cumulative_fcf_musd": summary(total_fcf), "minimum_cash_musd": summary(minimum_cash),
                    "ending_cash_musd": summary(result["cash_musd"][:, -1]),
                    "cumulative_fcf_impact_musd": summary(total_fcf - controls[mode]),
                    "criterion_value_musd": selected, "threshold_margin_musd": selected - search.threshold_musd,
                    "fails": selected < search.threshold_musd,
                    "conditional_breach_path_fraction": float((criterion_values < search.threshold_musd).mean()),
                    "lowest_median_cash_month": "opening" if b["cash_musd"] <= median_cash[lowest] else months[lowest],
                    "maximum_schedule_shortfall_asm_million": float(result["unserved_schedule_asm_million"].max())}
            cells.append({"x_index": i, "y_index": j, "x": x, "y": y, "overrides": {search.x.driver: x, search.y.driver: y}, "policies": outcomes})
    regions = {mode: failure_regions(cells, xs, ys, mode) for mode in policies}
    manifest = {"version": VERSION, "model_version": MODEL_VERSION, "base_scenario": asdict(base), "search": asdict(search),
        "evidence_snapshot_id": digest(snapshot), "fuel_snapshot_id": digest(fuel), "fuel_last_observation": fuel[-1]["date"],
        "numpy_version": np.__version__, "python_version": platform.python_version(),
        "code_sha256": hashlib.sha256(b"".join(Path(__file__).with_name(n).read_bytes() for n in ("failure_search.py", "monthly.py", "fleet.py", "evidence.py", "data.py", "cash_bridge.py"))).hexdigest()}
    manifest["run_id"] = digest(manifest)
    output = {"months": months, "x_values": xs, "y_values": ys, "cells": cells, "failure_regions": regions,
        "counts": {mode: {"failed_tested_points": sum(c["policies"][mode]["fails"] for c in cells), "tested_points": len(cells)} for mode in policies},
        "policy_disagreement_points": sum(c["policies"][policies[0]]["fails"] != c["policies"][policies[1]]["fails"] for c in cells),
        "status": "Conditional sensitivity on a finite tested grid; counts and range summaries are not event probabilities or continuous boundaries",
        "range_status": "Each region lists only tested values at one y value; behavior between points and beyond the chosen ranges is untested",
        "criterion_status": "Strictly below the threshold, using the chosen quantile of cumulative path FCF or path minimum cash including opening cash",
        "path_fraction_status": "Within-cell fractions describe assumed stochastic paths, not calibrated event probabilities; grid points have no probability weights",
        "policy_status": "Frozen and assumed constrained pricing only; fitted policy, rivals and detailed cash timing remain pending",
        "exploratory": base.horizon_months > 12}
    output["result_digest"] = digest(output)
    return {**output, "manifest": manifest, "evidence": evidence_manifest(snapshot, base.information_cutoff),
        "replay_inputs": {"evidence_snapshot": snapshot, "fuel_observations": fuel},
        "runtime_seconds": round(perf_counter() - started, 3)}
