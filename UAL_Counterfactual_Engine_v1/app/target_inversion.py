"""Compatible operating assumptions and optional, explicitly assumed FCFF valuation."""
from dataclasses import asdict, dataclass, replace
import hashlib
import math
from pathlib import Path
import platform
from time import perf_counter

import numpy as np

from .data import fuel_volatility, load_fuel
from .evidence import digest, evidence_manifest, load_snapshot
from .failure_search import FailureSearch, SearchAxis
from .fleet import capacity_paths
from .monthly import MonthlyScenario, VERSION as MODEL_VERSION, _rollout, _world, baseline, month_sequence, shock_weights, summary

VERSION = "operating-target-inversion-0.1.0"
VALUATION_REFERENCE = "https://pages.stern.nyu.edu/~adamodar/pdfiles/eqnotes/fcff.pdf"


def finite_number(value, label: str, low: float, high: float):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label} must be finite and between {low} and {high}")


@dataclass(frozen=True)
class OperatingTarget:
    metric: str = "cumulative_fcf"
    value: float = 1000
    relation: str = "at_least"
    tolerance: float = 0
    statistic: str = "p50"
    minimum_cash_floor_musd: float | None = None

    def validate(self):
        if self.metric not in {"cumulative_fcf", "cumulative_revenue", "operating_margin_pct"}:
            raise ValueError("Target must be cumulative FCF, cumulative revenue or operating margin percent")
        if self.relation not in {"at_least", "at_most", "within_band"} or self.statistic not in {"p10", "p50", "p90"}:
            raise ValueError("Unsupported target relation or statistic")
        bounds = (-1000, 100) if self.metric == "operating_margin_pct" else (0, 1000000) if self.metric == "cumulative_revenue" else (-1000000, 1000000)
        finite_number(self.value, "Target value", *bounds)
        finite_number(self.tolerance, "Target tolerance", 0, 1000000)
        if self.minimum_cash_floor_musd is not None:
            finite_number(self.minimum_cash_floor_musd, "Minimum cash floor", 0, 100000)

    def matches(self, values):
        if self.relation == "at_least":
            return values >= self.value
        if self.relation == "at_most":
            return values <= self.value
        return np.abs(values - self.value) <= self.tolerance


@dataclass(frozen=True)
class ValuationScenario:
    annual_discount_rate: float
    terminal_growth_rate: float
    terminal_annual_fcff_musd: float
    annual_non_cash_charges_musd: float
    annual_working_capital_investment_musd: float
    cash_operating_tax_rate: float
    enterprise_value_target_musd: float
    enterprise_value_tolerance_musd: float

    def validate(self):
        finite_number(self.annual_discount_rate, "Annual discount rate", .001, 1)
        finite_number(self.terminal_growth_rate, "Terminal growth rate", -.1, .1)
        if self.terminal_growth_rate >= self.annual_discount_rate:
            raise ValueError("Terminal growth must be below the annual discount rate")
        for name in ("terminal_annual_fcff_musd", "annual_non_cash_charges_musd", "enterprise_value_target_musd", "enterprise_value_tolerance_musd"):
            finite_number(getattr(self, name), name, 0, 1000000)
        finite_number(self.annual_working_capital_investment_musd, "Annual working-capital investment", -100000, 100000)
        finite_number(self.cash_operating_tax_rate, "Cash operating tax rate", 0, .5)


def value_operating_assets(result: dict, v: ValuationScenario) -> dict:
    v.validate()
    ebit = result["operating_income_musd"]
    # Explicit conservative assumption: no immediate cash tax credit on losses,
    # no NOL carry-forward or month-to-month netting is fitted here.
    cash_taxes = np.maximum(ebit, 0) * v.cash_operating_tax_rate
    fcff = ebit - cash_taxes + v.annual_non_cash_charges_musd / 12 - result["cash_capex_musd"] - v.annual_working_capital_investment_musd / 12
    years = np.arange(1, ebit.shape[1] + 1) / 12
    discounts = (1 + v.annual_discount_rate) ** years
    explicit_pv = (fcff / discounts).sum(axis=1)
    # This terminal assumption values operating assets. A separate net-debt and
    # non-operating-asset bridge would be needed before interpreting equity value.
    terminal_value = v.terminal_annual_fcff_musd / (v.annual_discount_rate - v.terminal_growth_rate)
    terminal_pv = terminal_value / discounts[-1]
    return {"enterprise_value_musd": explicit_pv + terminal_pv, "explicit_fcff_pv_musd": explicit_pv,
            "terminal_value_musd": terminal_value, "terminal_pv_musd": terminal_pv, "monthly_fcff_musd": fcff,
            "monthly_cash_operating_taxes_musd": cash_taxes}


@dataclass(frozen=True)
class TargetInversion:
    x: SearchAxis = SearchAxis("fuel_shock_pct", 0, 40)
    y: SearchAxis = SearchAxis("demand_shock_pct", -10, 10)
    target: OperatingTarget = OperatingTarget()
    valuation: ValuationScenario | None = None

    def validate(self, base: MonthlyScenario):
        self.target.validate()
        if self.valuation is not None:
            self.valuation.validate()
        return FailureSearch(x=self.x, y=self.y).validate(base)


def invert_target(base: MonthlyScenario, inversion: TargetInversion, snapshot: dict | None = None, fuel: list[dict] | None = None) -> dict:
    started = perf_counter()
    xs, ys = inversion.validate(base)
    snapshot = load_snapshot() if snapshot is None else snapshot
    b = baseline(snapshot, base.information_cutoff, base.cash_plan, base.purchase_plan)
    fuel = load_fuel() if fuel is None else fuel
    fuel = [f for f in fuel if f["date"] <= base.information_cutoff]
    if len(fuel) < 30:
        raise ValueError("At least 30 fuel observations at or before the cutoff are required")
    spot = fuel[-1]["usd_per_gallon"]
    worlds = _world(base, spot, fuel_volatility(fuel) * math.sqrt(21))
    months = month_sequence(base.information_cutoff, base.horizon_months)
    target, v = inversion.target, inversion.valuation
    q = {"p10": .1, "p50": .5, "p90": .9}[target.statistic]
    modes = ("frozen", "assumed_constrained")
    cells = []
    for j, y in enumerate(ys):
        for i, x in enumerate(xs):
            s = replace(base, **{inversion.x.driver: x, inversion.y.driver: y})
            fleet = capacity_paths(b, s, b["fleet_plan"], months, shock_weights(s))
            policies = {}
            for mode in modes:
                r = _rollout(b, s, *worlds, spot, True, mode, fleet)
                revenue, oi = r["revenue_musd"].sum(axis=1), r["operating_income_musd"].sum(axis=1)
                fcf = r["free_cash_flow_musd"].sum(axis=1)
                margin = np.divide(oi * 100, revenue, out=np.zeros_like(oi), where=revenue > 0)
                cash_min = np.minimum(b["cash_musd"], r["cash_musd"].min(axis=1))
                metric = {"cumulative_fcf": fcf, "cumulative_revenue": revenue, "operating_margin_pct": margin}[target.metric]
                selected = float(np.quantile(metric, q))
                operating_matches = bool(target.matches(selected))
                mask = target.matches(metric)
                cash_statistic_matches = True
                if target.minimum_cash_floor_musd is not None:
                    cash_statistic_matches = bool(np.quantile(cash_min, q) >= target.minimum_cash_floor_musd)
                    mask &= cash_min >= target.minimum_cash_floor_musd
                valuation = value_operating_assets(r, v) if v else None
                valuation_matches = True
                if v:
                    ev = valuation["enterprise_value_musd"]
                    valuation_matches = abs(float(np.quantile(ev, q)) - v.enterprise_value_target_musd) <= v.enterprise_value_tolerance_musd
                    mask &= np.abs(ev - v.enterprise_value_target_musd) <= v.enterprise_value_tolerance_musd
                indexes = np.flatnonzero(mask)
                witnesses = []
                for k in indexes[:5]:
                    k = int(k)
                    witness = {"path_index": k, "target_value": float(metric[k]), "cumulative_revenue_musd": float(revenue[k]),
                        "operating_margin_pct": float(margin[k]), "cumulative_fcf_musd": float(fcf[k]), "minimum_cash_musd": float(cash_min[k]),
                        "monthly": {key: r[key][k].tolist() for key in ("revenue_musd", "operating_income_musd", "free_cash_flow_musd", "cash_musd", "asm_million", "rpm_million")}}
                    if v:
                        witness["enterprise_value_musd"] = float(valuation["enterprise_value_musd"][k])
                        witness["monthly"]["fcff_musd"] = valuation["monthly_fcff_musd"][k].tolist()
                    witnesses.append(witness)
                policies[mode] = {"target_distribution": summary(metric), "selected_target_value": selected,
                    "target_gap": selected - target.value, "operating_statistic_matches": operating_matches,
                    "cash_statistic_matches": cash_statistic_matches, "valuation_statistic_matches": bool(valuation_matches),
                    "compatible": operating_matches and cash_statistic_matches and bool(valuation_matches) and bool(len(indexes)),
                    "compatible_path_count": len(indexes), "conditional_compatible_path_fraction": len(indexes) / base.paths,
                    "witness_paths": witnesses, "cumulative_revenue_musd": summary(revenue), "operating_margin_pct": summary(margin),
                    "cumulative_fcf_musd": summary(fcf), "minimum_cash_musd": summary(cash_min), "ending_cash_musd": summary(r["cash_musd"][:, -1]),
                    "cash_floor_breach_path_fraction": float((cash_min < base.cash_floor_musd).mean()),
                    "maximum_schedule_shortfall_asm_million": float(r["unserved_schedule_asm_million"].max())}
                if v:
                    policies[mode]["valuation"] = {"enterprise_value_musd": summary(valuation["enterprise_value_musd"]),
                        "explicit_fcff_pv_musd": summary(valuation["explicit_fcff_pv_musd"]),
                        "terminal_value_musd": valuation["terminal_value_musd"], "terminal_pv_musd": valuation["terminal_pv_musd"]}
            cells.append({"x_index": i, "y_index": j, "x": x, "y": y, "overrides": {inversion.x.driver: x, inversion.y.driver: y}, "policies": policies})
    counts = {mode: {"operating_target_points": sum(c["policies"][mode]["operating_statistic_matches"] for c in cells),
        "compatible_points": sum(c["policies"][mode]["compatible"] for c in cells), "tested_points": len(cells)} for mode in modes}
    manifest = {"version": VERSION, "model_version": MODEL_VERSION, "base_scenario": asdict(base), "inversion": asdict(inversion),
        "evidence_snapshot_id": digest(snapshot), "fuel_snapshot_id": digest(fuel), "fuel_last_observation": fuel[-1]["date"],
        "python_version": platform.python_version(), "numpy_version": np.__version__,
        "code_sha256": hashlib.sha256(b"".join(Path(__file__).with_name(n).read_bytes() for n in ("target_inversion.py", "failure_search.py", "monthly.py", "fleet.py", "evidence.py", "data.py", "cash_bridge.py"))).hexdigest()}
    manifest["run_id"] = digest(manifest)
    output = {"months": months, "x_values": xs, "y_values": ys, "cells": cells, "counts": counts,
        "compatible_sets": {mode: [{"x": c["x"], "y": c["y"], "overrides": c["overrides"]} for c in cells if c["policies"][mode]["compatible"]] for mode in modes},
        "status": "Compatible tested assumptions under this model; not a unique inverse solution, market forecast or probability over grid points",
        "target_status": "Targets apply to absolute horizon totals; operating margin is path total operating income divided by path total revenue, in percent",
        "compatibility_status": "Selected path quantiles must meet operating, optional cash and valuation constraints; at least one actual path must jointly meet them. Witnesses are first matching path indexes, not representative or most likely paths.",
        "reference_status": "User-defined operating target and optional enterprise-value target. Neither is imported guidance, analyst consensus or an observed market price.",
        "path_status": "Conditional path fractions are uncalibrated; no probability weighting or inference outside the tested ranges",
        "policy_status": "Frozen and assumed constrained pricing; fitted policies, rivals, full cash timing and parameter uncertainty remain pending",
        "valuation_status": ("Assumed FCFF = operating income minus cash tax on positive monthly operating income + annual noncash charges/12 - modeled cash capex - annual working-capital investment/12. No immediate loss tax credit or tax carry-forward. End-of-month discounting at an effective annual cost of capital. User-specified first full terminal-year FCFF/(discount rate-growth), discounted from the horizon end. Stable growth starts immediately after this horizon; terminal cash flow is independent of simulated final-month cash flow. Operating-asset value excludes excess cash, debt, leases, other claims and shares; no equity price is inferred." if v else "Valuation not requested"),
        "valuation_reference": VALUATION_REFERENCE if v else None, "exploratory": base.horizon_months > 12}
    output["result_digest"] = digest(output)
    return {**output, "manifest": manifest, "evidence": evidence_manifest(snapshot, base.information_cutoff),
        "replay_inputs": {"evidence_snapshot": snapshot, "fuel_observations": fuel}, "runtime_seconds": round(perf_counter() - started, 3)}
