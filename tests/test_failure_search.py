from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.evidence import digest
from app.failure_search import FailureSearch, SearchAxis, failure_regions, search_failures
from app.monthly import MonthlyScenario, _rollout, _world, baseline, simulate_monthly
from app.data import load_fuel, fuel_volatility
from app.evidence import load_snapshot
from app.server import app
from scripts.run_failure_search import replay


def small(**kwargs):
    return MonthlyScenario(**{"paths": 40, "horizon_months": 3, **kwargs})


def grid(**kwargs):
    return FailureSearch(**{"x": SearchAxis("fuel_shock_pct", 0, 30, 3), "y": SearchAxis("demand_shock_pct", -10, 0, 3), **kwargs})


def test_search_witness_agrees_with_full_paired_simulator():
    base = small()
    result = search_failures(base, grid())
    assert len(result["cells"]) == 9
    for cell in (result["cells"][0], result["cells"][-1]):
        for mode in ("frozen", "assumed_constrained"):
            full = simulate_monthly(replace(base, **cell["overrides"], response_mode=mode))
            outcomes = cell["policies"][mode]
            assert outcomes["cumulative_fcf_impact_musd"] == full["summary"]["cumulative_fcf_impact_musd"]
            assert outcomes["ending_cash_musd"] == full["summary"]["ending_cash_musd"]
    assert result["manifest"]["base_scenario"]["paths"] == 40


def test_path_minimum_includes_opening_cash_and_strict_threshold():
    base = small(stochastic=False)
    search = grid(criterion="minimum_cash", threshold_musd=10166)
    result = search_failures(base, search)
    zero = next(c for c in result["cells"] if c["x"] == 0 and c["y"] == 0)
    for outcome in zero["policies"].values():
        assert outcome["minimum_cash_musd"]["p50"] == 10166
        assert outcome["lowest_median_cash_month"] == "opening"
        assert not outcome["fails"]  # Equality is not a breach.
        assert outcome["conditional_breach_path_fraction"] == 0
    above = search_failures(base, replace(search, threshold_musd=10167))
    assert all(c["policies"]["frozen"]["fails"] for c in above["cells"])


def test_cash_quantile_is_of_path_minima_not_minimum_of_monthly_quantiles():
    base = small(horizon_months=6, demand_monthly_log_sd=.2)
    search = grid(criterion="minimum_cash", statistic="p10", threshold_musd=9000)
    result = search_failures(base, search)
    cell = result["cells"][0]
    s = replace(base, **cell["overrides"])
    fuel = [f for f in load_fuel() if f["date"] <= s.information_cutoff]
    spot = fuel[-1]["usd_per_gallon"]
    b = baseline(load_snapshot(), s.information_cutoff)
    worlds = _world(s, spot, fuel_volatility(fuel) * np.sqrt(21))
    raw = _rollout(b, s, *worlds, spot, True, "frozen")
    expected = np.quantile(np.minimum(b["cash_musd"], raw["cash_musd"].min(axis=1)), .1)
    assert cell["policies"]["frozen"]["criterion_value_musd"] == expected


def test_failure_discovery_keeps_holes_and_never_interpolates():
    cells = [{"x_index": i, "y_index": 0, "policies": {"frozen": {"fails": failed}}} for i, failed in enumerate((True, False, True, True))]
    assert failure_regions(cells, [0, 10, 20, 30], [-5], "frozen") == [
        {"y": -5, "x_values": [0], "tested_points": 1},
        {"y": -5, "x_values": [20, 30], "tested_points": 2}]


def test_bounds_unsupported_driver_and_compute_budget_fail_before_simulation():
    cases = [grid(x=SearchAxis("rival_capacity_pct", 0, 10)), grid(y=SearchAxis("fuel_shock_pct", 0, 20)),
        grid(x=SearchAxis("fuel_shock_pct", 0, 101)), grid(x=SearchAxis("fuel_shock_pct", float("nan"), 20)),
        grid(statistic="mean"), grid(criterion="bankruptcy"), grid(threshold_musd=float("nan")),
        grid(x=SearchAxis("delivery_delay_months", 0, 1, 3))]
    for search in cases:
        with pytest.raises(ValueError):
            search_failures(small(), search)
    with pytest.raises(ValueError, match="6 million"):
        search_failures(MonthlyScenario(paths=10000, horizon_months=24), grid(x=SearchAxis("fuel_shock_pct", 0, 60, 7), y=SearchAxis("demand_shock_pct", -20, 0, 7)))
    assert SearchAxis("delivery_delay_months", 0, 6, 4).values() == [0, 2, 4, 6]


def test_preserved_search_replay_and_result_input_tamper(tmp_path):
    saved = search_failures(small(), grid())
    path = tmp_path / 'search.json'
    path.write_text(json.dumps(saved))
    assert replay(path)["manifest"] == saved["manifest"]
    saved["cells"][0]["policies"]["frozen"]["fails"] = not saved["cells"][0]["policies"]["frozen"]["fails"]
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="checksum"):
        replay(path)
    saved = search_failures(small(), grid())
    saved['replay_inputs']['fuel_observations'][-1]['usd_per_gallon'] += .1
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="differ"):
        replay(path)


def test_common_world_seed_and_future_source_exclusion():
    base = small()
    a = search_failures(base, grid())
    b = search_failures(base, grid(), fuel=load_fuel() + [{"date": "2099-01-01", "usd_per_gallon": 99}])
    assert a["manifest"] == b["manifest"] and a["result_digest"] == b["result_digest"]
    assert a["result_digest"] != search_failures(replace(base, seed=2027), grid())["result_digest"]
    assert not a["exploratory"]


def test_search_api_validates_nested_contract_and_returns_provenance():
    client = TestClient(app)
    payload = {"base_scenario": {"paths": 20, "horizon_months": 3}, "x": {"driver": "fuel_shock_pct", "low": 0, "high": 20, "points": 2}, "y": {"driver": "demand_shock_pct", "low": -10, "high": 0, "points": 2}}
    response = client.post('/api/monthly/failure-search', json=payload)
    assert response.status_code == 200
    assert len(response.json()["cells"]) == 4
    for patch in ({"criterion": "bankruptcy"}, {"bogus": True}, {"base_scenario": {"competitor_capacity_pct": 10}}, {"x": {"driver": "fuel_shock_pct", "low": 0, "high": 1000}}):
        assert client.post('/api/monthly/failure-search', json={**payload, **patch}).status_code == 422


@pytest.mark.parametrize(('x', 'y'), [
    (SearchAxis('delivery_delay_months', 0, 6, 2), SearchAxis('aircraft_unavailable_pct', 0, 40, 2)),
    (SearchAxis('capacity_shock_pct', -50, 0, 2), SearchAxis('regional_contract_shock_pct', 0, 50, 2)),
])
def test_fleet_and_contract_search_witnesses_preserve_paired_control(x, y):
    base = small(horizon_months=12, availability_group='widebody', nonfuel_fixed_fraction=.95)
    result = search_failures(base, grid(x=x, y=y))
    for cell in result['cells']:
        for mode in ('frozen', 'assumed_constrained'):
            full = simulate_monthly(replace(base, **cell['overrides'], response_mode=mode))
            assert cell['policies'][mode]['cumulative_fcf_impact_musd'] == full['summary']['cumulative_fcf_impact_musd']
            assert cell['policies'][mode]['ending_cash_musd'] == full['summary']['ending_cash_musd']
