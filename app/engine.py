"""Auditable V1 fuel-to-fare counterfactual. Scenario policies are assumptions, NOT fitted UAL behavior."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import date, timedelta
import math
import random
from statistics import mean
from typing import Sequence
from .data import fuel_volatility


@dataclass(frozen=True)
class Scenario:
    fuel_shock_pct: float = 20.0
    fuel_shock_half_life_days: int = 35
    response_lag_days: int = 10
    response_half_life_days: int = 18
    pass_through_fraction: float = 0.55
    demand_elasticity: float = 0.55
    horizon_days: int = 60
    paths: int = 300
    seed: int = 2026

    def validate(self) -> None:
        bounds = {
            "fuel_shock_pct": (-60, 100), "fuel_shock_half_life_days": (1, 180),
            "response_lag_days": (0, 90), "response_half_life_days": (1, 120),
            "pass_through_fraction": (0, 1.25), "demand_elasticity": (0, 1.5),
            "horizon_days": (14, 180), "paths": (20, 1200), "seed": (0, 1_000_000_000)
        }
        for k, (lo, hi) in bounds.items():
            v = getattr(self, k)
            if not isinstance(v, (float, int)) or not math.isfinite(v) or v < lo or v > hi:
                raise ValueError(f"{k} must be between {lo} and {hi}")


def _pct(values: Sequence[float], proportion: float) -> float:
    if not values: raise ValueError("Cannot summarize empty values")
    seq = sorted(values)
    index = (len(seq) - 1) * proportion
    low = math.floor(index)
    high = math.ceil(index)
    return seq[low] * (1 - (index - low)) + seq[high] * (index - low)


def _summary(values: Sequence[float]) -> dict:
    return {"p10": round(_pct(values, .1), 3), "p50": round(_pct(values, .5), 3), "p90": round(_pct(values, .9), 3)}


def _distribution(rows: Sequence[Sequence[float]]) -> list[dict]:
    # Each column is a marginal time-point summary. Connecting its percentiles
    # does not produce a single simulated path or a joint coverage guarantee.
    return [
        {"p10": round(_pct(column, .1), 3), "p50": round(_pct(column, .5), 3), "p90": round(_pct(column, .9), 3)}
        for column in zip(*rows)
    ]


def simulate(observations: list[dict], ual: dict, scenario: Scenario) -> dict:
    """Paired shock/control paths with common RNG noise and operating-response lag.

    Assumptions are *conditional stress modeling*, not a fitted investment forecast.
    Baseline P&L unit economics equal UAL Q2 2026 run rate, held fixed outside fuel and fares.
    EIA spot used as cost-change proxy, not a claim about paid jet fuel.
    """
    scenario.validate()
    if len(observations) < 30:
        raise ValueError("At least 30 actual fuel observations needed")
    gallons_m_per_day = ual["fuel_gallons_million"] / ual["period_days"]
    asm_m_per_day = ual["asm_million"] / ual["period_days"]
    pax_rev_m_per_day = asm_m_per_day * ual["prasm_cents_per_asm"] / 100.0
    fuel_per_asm = ual["fuel_gallons_million"] / ual["asm_million"]
    latest_fuel = observations[-1]["usd_per_gallon"]
    mean20 = mean(x["usd_per_gallon"] for x in observations[-20:])
    sigma = fuel_volatility(observations, window=65)
    starting = date.fromisoformat(observations[-1]["date"])

    shock_factor = scenario.fuel_shock_pct / 100.0
    shock_halflife = scenario.fuel_shock_half_life_days
    response_alpha = 1 - 2 ** (-1.0 / scenario.response_half_life_days)
    scenario_dates = [(starting + timedelta(days=t)).isoformat() for t in range(1, scenario.horizon_days + 1)]
    rng = random.Random(scenario.seed)
    paths_fuel, paths_profit, paths_cumulative, paths_response, paths_unrecovered = [], [], [], [], []
    final_fuel_cost, final_recovered_rev = [], []

    for _ in range(scenario.paths):
        base_spot = latest_fuel
        current_response = 0.0
        cumulative_profit = 0.0
        series_fuel, series_profit, series_cumulative, series_response, series_unrecovered = [], [], [], [], []
        total_fuel_pressure, total_revenue_recovery = 0.0, 0.0
        for day_idx, date_str in enumerate(scenario_dates):
            day = day_idx + 1
            d = date.fromisoformat(date_str)
            # No new spot print on weekends. Calendar-day operating costs still accrue.
            if d.weekday() < 5:
                r = math.log(base_spot) * .978 + math.log(mean20) * .022 + rng.gauss(0.0, sigma)
                base_spot = max(.2, min(15, math.exp(r)))
            impulse = shock_factor * (2 ** (-day_idx / shock_halflife))
            shocked_fuel = max(.05, base_spot * (1 + impulse))
            delta_fuel_usd = shocked_fuel - base_spot
            fuel_pnl_m = gallons_m_per_day * delta_fuel_usd
            # Required gross PRASM change to pay for incremental fuel, before fare-demand effects.
            target_fare_change = scenario.pass_through_fraction * fuel_pnl_m / pax_rev_m_per_day
            target_fare_change = max(-0.50, min(0.50, target_fare_change))
            if day > scenario.response_lag_days:
                desired_step = (target_fare_change - current_response) * response_alpha
                # Operational limit on average daily yield repricing; this is not an observed policy.
                current_response += max(-0.006, min(0.006, desired_step))
            # Fare/demand accounting: positive fare premium reduces demand with fixed ASM.
            demand_factor = (1 + current_response) ** (-scenario.demand_elasticity)
            max_load_factor_ratio = 1.0 / ual["passenger_load_factor"]
            demand_factor = max(.3, min(max_load_factor_ratio, demand_factor))
            realized_revenue_delta_m = pax_rev_m_per_day * ((1 + current_response) * demand_factor - 1.0)
            profit_delta_m = realized_revenue_delta_m - fuel_pnl_m
            cumulative_profit += profit_delta_m
            total_fuel_pressure += fuel_pnl_m
            total_revenue_recovery += realized_revenue_delta_m
            series_fuel.append(shocked_fuel)
            series_profit.append(profit_delta_m)
            series_cumulative.append(cumulative_profit)
            series_response.append(current_response * 100)
            series_unrecovered.append(fuel_pnl_m - realized_revenue_delta_m)
        paths_fuel.append(series_fuel)
        paths_profit.append(series_profit)
        paths_cumulative.append(series_cumulative)
        paths_response.append(series_response)
        paths_unrecovered.append(series_unrecovered)
        final_fuel_cost.append(total_fuel_pressure)
        final_recovered_rev.append(total_revenue_recovery)

    total_fuel = _summary(final_fuel_cost)
    revenue_recovered = _summary(final_recovered_rev)
    return {
        "scenario": asdict(scenario),
        "as_of_observation": observations[-1]["date"],
        "dates": scenario_dates,
        "history": observations[-125:],
        "series": {
            "fuel_scenario_usd_gal": _distribution(paths_fuel),
            "daily_operating_income_delta_musd": _distribution(paths_profit),
            "cumulative_operating_income_delta_musd": _distribution(paths_cumulative),
            "modeled_fare_change_pct": _distribution(paths_response),
            "unrecovered_cost_per_day_musd": _distribution(paths_unrecovered)
        },
        "summary": {
            "cumulative_operating_income_impact_musd": _distribution(paths_cumulative)[-1],
            "fuel_cost_increment_musd": total_fuel,
            "modeled_revenue_recovery_musd": revenue_recovered,
            "ending_modeled_fare_change_pct": _distribution(paths_response)[-1],
            "fuel_per_asm_gallons": round(fuel_per_asm, 6),
            "incremental_fuel_cost_per_asm_cents_initial": round(latest_fuel * shock_factor * fuel_per_asm * 100, 3),
            "gross_prasm_increase_required_pct_initial": round(100 * latest_fuel * shock_factor * fuel_per_asm / (ual["prasm_cents_per_asm"] / 100), 3),
            "estimated_spot_log_return_sd": round(sigma, 5),
            "model_status": "conditional scenario, not calibrated UAL response or trading alpha",
        },
        "assumptions": {
            "fuel": "Gulf Coast EIA spot used as proxy for incremental UAL paid fuel price; mean-reverting stochastic baseline fitted only to spot changes; shock decays exogenously.",
            "business": "Q2 2026 UAL units held constant. No capacity, fleet, competitor, liquidity, hedging, nonfuel expense or seasonality dynamics.",
            "pricing": "Response lag, adjustment speed, pass-through fraction, and demand elasticity are explicit user-set assumptions; no learned management function.",
            "inference": "P10/P90 describe simulated conditional fuel/process noise, not full predictive intervals or calibrated forecast coverage.",
            "financial": "Operating-income delta is an indicative scenario sensitivity, not a cash-flow, EPS, stock-price, or investment forecast."
        }
    }
