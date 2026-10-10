from copy import deepcopy
import csv
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import platform

import numpy as np
import pytest

from app.evidence import digest, load_snapshot
from app.lab import response as R
from app.lab import scenario as S
from app.monthly import (RECOVERY_MODE, MonthlyScenario, _rollout, _world, baseline, simulate_monthly,
                         simulate_recovery)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "lab_scenario"
EXAMPLE = FIXTURES / "example_response.json"
DEFAULT_REQUEST = {"fuel_shock_pct": 25, "fuel_shock_months": 6, "capacity_change_pct": 0, "demand_shock_pct": 0,
                   "delivery_delay": {"aircraft": 0, "months": 0}, "horizon_months": 12, "seed": 1}
QUARTERLY_COLUMNS = ("period_end,filed_on,accession,asm_m,rpm_m,total_revenue_musd,passenger_revenue_musd,trasm_cents,prasm_cents,"
                     "casm_cents,casm_ex_cents,fuel_gallons_m,fuel_price_usd_gal,fuel_expense_musd,pretax_income_musd,"
                     "adj_pretax_income_musd,net_income_musd,gaap_eps_diluted,adj_eps_diluted,diluted_shares_m,profit_sharing_musd,source_note").split(",")


def synthetic_measured() -> R.Response:
    """Evenly spaced placeholder curve with a common +/-0.2 shift. Not an estimate of anything."""
    shift = np.random.default_rng(0).uniform(-.2, .2, size=(500, 1))
    return R.from_draws(np.array([.2, .4, .6, .8, 1.0]) + shift, name="measured",
                        source={"kind": "synthetic", "detail": "evenly spaced placeholder curve, common shift uniform in +/-0.2"},
                        note="Synthetic placeholder for tests and front-end work.")


def example_response() -> dict:
    """Regenerate tests/fixtures/lab_scenario/example_response.json:
    .venv/bin/python -c "import json, tests.test_lab_scenario as t; t.EXAMPLE.write_text(json.dumps(t.example_response(), indent=1) + '\\n')"
    """
    S.clear_cache()
    R.CAPACITY_RESPONSE = FIXTURES / "absent_capacity_response.json"
    return S.run_scenario(DEFAULT_REQUEST, {"measured": synthetic_measured()})


def capacity_file(path, curve=(0, -.3, -.6, -.8, -.8), spread=.2) -> None:
    """A synthetic capacity response: the given curve with a common shift, as the builder would write it."""
    draws = np.array(curve) + np.random.default_rng(3).uniform(-spread, spread, size=(200, 1)) * (np.array(curve) != 0)
    rows = [{"quarters_after": q, **{f"p{k}": float(np.quantile(draws[:, q], k / 100)) for k in (10, 50, 90)}} for q in range(5)]
    path.write_text(json.dumps({"as_of": "2026-09-30", "curve": rows, "curve_draws": draws.tolist()}))


@pytest.fixture(autouse=True)
def pinned_inputs(tmp_path, monkeypatch):
    """Other builders write the measured curves, clocks and quarterly table; tests must not depend on their state."""
    table = tmp_path / "ual_quarterly.csv"
    with table.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=QUARTERLY_COLUMNS, restval="")
        writer.writeheader()
        for period_end, filed_on, pretax, net, shares in (("2026-03-31", "2026-04-21", 900, 700, 390),
                                                          ("2026-06-30", "2026-07-15", 1000, 800, 400),
                                                          ("2026-09-30", "2026-10-20", 2000, 1000, 100)):  # filed after the cutoff
            writer.writerow({"period_end": period_end, "filed_on": filed_on, "accession": "synthetic-test-row",
                             "pretax_income_musd": pretax, "net_income_musd": net, "diluted_shares_m": shares})
    monkeypatch.setattr(S, "QUARTERLY_CSV", table)
    monkeypatch.setattr(R, "FARE_CLOCK", tmp_path / "fare_clock.json")
    monkeypatch.setattr(R, "CAPACITY_RESPONSE", tmp_path / "capacity_response.json")
    monkeypatch.setattr(S, "COST_CLOCK", tmp_path / "cost_clock.json")
    monkeypatch.setattr(S, "DEMAND_SIGNAL", tmp_path / "demand_signal.json")
    S.clear_cache()
    yield
    S.clear_cache()


def small(**kw):
    return {**DEFAULT_REQUEST, "paths": 400, **kw}


def stable(result):
    return {k: v for k, v in result.items() if k not in ("elapsed_ms", "cached")}


# ------------------------------------------------------------------ legacy engine

def legacy_view(result):
    clean = dict(result)
    clean["summary"] = {k: v for k, v in result["summary"].items() if k != "runtime_seconds"}
    clean["manifest"] = {k: v for k, v in result["manifest"].items() if k not in ("code_sha256", "run_id")}
    return clean


def rollout_hash(rollout):
    return hashlib.sha256(b"".join(np.ascontiguousarray(rollout[name]).tobytes() for name in sorted(rollout))).hexdigest()


def close(actual, expected):
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(close(actual[k], expected[k]) for k in expected)
    if isinstance(expected, float):
        return math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-3)
    return actual == expected


def test_existing_modes_and_defaults_are_bit_identical_to_before_the_recovery_mode():
    saved = json.loads((FIXTURES / "legacy_engine.json").read_text())
    same_machine = saved["environment"] == {"python": platform.python_version(), "numpy": np.__version__,
                                            "system": platform.system(), "machine": platform.machine()}
    inputs = saved["inputs"]
    assert set(saved["cases"]) == {"default", "non_default", "plans", "cash"} and saved["cases"]["default"] == {}
    for name, kwargs in saved["cases"].items():
        result = simulate_monthly(MonthlyScenario(**kwargs), snapshot=deepcopy(inputs["snapshot"]), fuel=deepcopy(inputs["fuel"]))
        expected = saved["expected"]["simulate_monthly"][name]
        assert close(legacy_view(result)["summary"], expected["summary"]), name
        assert close(result["intervention"]["quarterly"][0]["operating_income_musd"], expected["first_quarter_operating_income_musd"]), name
        assert close(result["frozen_cumulative_fcf_impact_musd"][-1], expected["frozen_cumulative_fcf_impact_last_musd"]), name
        # Exact equality of every deterministic field needs the capturing interpreter,
        # numpy build and CPU; elsewhere the rounded numbers above are compared.
        if same_machine:
            assert digest(legacy_view(result)) == expected["digest"], name
    s = MonthlyScenario(**saved["rollout_case"])
    b = baseline(inputs["snapshot"], s.information_cutoff)
    fuel, demand = _world(s, saved["rollout_world"]["spot"], saved["rollout_world"]["sigma"])
    if same_machine:
        hashes = {"world": hashlib.sha256(fuel.tobytes() + demand.tobytes()).hexdigest()}
        for mode in ("frozen", "assumed_constrained"):
            for on in (False, True):
                hashes[f"{mode}:{'intervention' if on else 'control'}"] = rollout_hash(
                    _rollout(b, s, fuel, demand, saved["rollout_world"]["spot"], on, mode))
        assert hashes == saved["expected"]["rollout_sha256"]


def test_recovery_mode_is_not_a_scenario_setting_and_needs_a_curve_per_path():
    with pytest.raises(ValueError, match="Fitted and feasible-policy modes"):
        MonthlyScenario(response_mode=RECOVERY_MODE).validate()
    s = MonthlyScenario(paths=30)
    b = baseline(load_snapshot(), s.information_cutoff)
    worlds = _world(s, 3.5, .2)
    for bad in (None, np.zeros((30, 5)), np.zeros((29, 12))):
        with pytest.raises(ValueError, match="one monthly recovery curve per path"):
            _rollout(b, s, *worlds, 3.5, True, RECOVERY_MODE, None, bad)
    with pytest.raises(ValueError, match="only in shock sizes"):
        simulate_recovery(s, {"none": R.costs_only()}, cases={"other": replace(s, seed=3)})
    with pytest.raises(ValueError, match="reserved"):
        simulate_recovery(s, {"none": R.costs_only()}, cases={"control": s})


# ------------------------------------------------------------------ recovery mode

def test_zero_curve_equals_the_frozen_rollout_exactly():
    # No tolerance: with a zero curve the implied fare change is exactly zero, so demand is
    # multiplied by (1 + 0) ** -elasticity == 1 and revenue by exactly 1, as in frozen mode.
    for kw in ({}, {"geography": "pacific", "demand_shock_pct": -5, "capacity_shock_pct": -5, "delivery_delay_months": 3}):
        s = MonthlyScenario(paths=300, seed=4, fuel_shock_pct=35, **kw)
        b = baseline(load_snapshot(), s.information_cutoff)
        worlds = _world(s, 3.5, .2)
        for on in (False, True):
            frozen = _rollout(b, s, *worlds, 3.5, on, "frozen")
            zero = _rollout(b, s, *worlds, 3.5, on, RECOVERY_MODE, None, np.zeros((s.paths, s.horizon_months)))
            assert set(frozen) == set(zero)
            assert all(np.array_equal(frozen[name], zero[name]) for name in frozen)
    out = simulate_recovery(MonthlyScenario(paths=200), {"costs_only": R.costs_only()})["responses"]["costs_only"]
    legacy = simulate_monthly(MonthlyScenario(paths=200, response_mode="frozen"))
    impact = (out["intervention"]["operating_income_musd"] - out["control"]["operating_income_musd"]).sum(axis=1)
    assert legacy["summary"]["cumulative_operating_income_impact_musd"]["p50"] == round(float(np.median(impact)), 4)


def test_unit_curve_offsets_the_fuel_shock_and_shares_follow_the_curve():
    # Tolerance 1e-6 USD million (one dollar) per path and month: recovered dollars go through
    # revenue = served * yield * (1 + recovered / (served * yield)), a division and a product.
    s = MonthlyScenario(paths=500, seed=2, fuel_shock_pct=40, geography="atlantic")
    out = simulate_recovery(s, {"ones": R.from_draws(np.ones((1, 5))), "none": R.costs_only()})["responses"]
    effect = out["ones"]["intervention"]["operating_income_musd"] - out["ones"]["control"]["operating_income_musd"]
    unrecovered = out["none"]["intervention"]["operating_income_musd"] - out["none"]["control"]["operating_income_musd"]
    assert np.abs(effect).max() < 1e-6 and unrecovered.max() < -1
    assert np.array_equal(out["ones"]["intervention"]["fuel_expense_musd"], out["none"]["intervention"]["fuel_expense_musd"])
    assert np.array_equal(out["ones"]["intervention"]["rpm_million"], out["none"]["intervention"]["rpm_million"])
    # A lasting shock from the first month of a quarter recovers the stated share in each quarter.
    curve = [.2, .4, .6, .8, 1.1]
    lasting = MonthlyScenario(paths=20, horizon_months=18, fuel_shock_pct=30, duration_months=24, stochastic=False)
    r = simulate_recovery(lasting, {"curve": R.from_draws([curve])})["responses"]["curve"]
    revenue = r["intervention"]["revenue_musd"] - r["control"]["revenue_musd"]
    cost = r["intervention"]["fuel_expense_musd"] - r["control"]["fuel_expense_musd"]
    shares = [revenue[0, 3 * q:3 * q + 3].sum() / cost[0, 3 * q:3 * q + 3].sum() for q in range(6)]
    assert shares == pytest.approx(curve + [1.1], abs=1e-9)


def test_slower_curve_is_worse_early_on_the_same_worlds():
    s = MonthlyScenario(paths=1000, seed=6, fuel_shock_pct=25, duration_months=24)
    out = simulate_recovery(s, {"fast": R.from_draws([[1, 1, 1, 1, 1]]), "slow": R.from_draws([[0, .5, 1, 1, 1]])})
    def effect(name):
        r = out["responses"][name]
        return r["intervention"]["operating_income_musd"] - r["control"]["operating_income_musd"]
    fast, slow = effect("fast"), effect("slow")
    assert (slow[:, 0] < fast[:, 0] - 1).all()  # every world, first month
    assert np.median(slow[:, :3].sum(axis=1)) < np.median(fast[:, :3].sum(axis=1)) - 100
    assert np.median(slow[:, 3:6].sum(axis=1)) < np.median(fast[:, 3:6].sum(axis=1))
    control = {name: out["responses"][name]["control"] for name in ("fast", "slow")}
    assert np.array_equal(control["fast"]["fuel_expense_musd"], control["slow"]["fuel_expense_musd"])
    # Without noise the only price change is the shock in month 1: worse for six months, then alike.
    out = simulate_recovery(replace(s, paths=20, stochastic=False),
                            {"fast": R.from_draws([[1, 1, 1, 1, 1]]), "slow": R.from_draws([[0, .5, 1, 1, 1]])})
    fast, slow = effect("fast"), effect("slow")
    assert (slow[:, :6] < fast[:, :6] - 100).all() and np.abs(slow[:, 6:] - fast[:, 6:]).max() < 1e-6


def test_per_path_draws_change_the_spread_but_not_the_worlds():
    s = MonthlyScenario(paths=2000, seed=8, fuel_shock_pct=25, duration_months=24)
    spread = np.linspace(.2, 1.0, 9)[:, None] * np.ones((1, 5))
    out = simulate_recovery(s, {"point": R.from_draws([[.6] * 5]), "spread": R.from_draws(spread)})
    from app.data import fuel_volatility, load_fuel
    known = [r for r in load_fuel() if r["date"] <= s.information_cutoff]
    fuel, demand = _world(s, known[-1]["usd_per_gallon"], fuel_volatility(known) * math.sqrt(21))
    assert np.array_equal(out["fuel_world"], fuel) and np.array_equal(out["demand_world"], demand)
    point, wide = out["responses"]["point"], out["responses"]["spread"]
    assert np.array_equal(point["control"]["fuel_expense_musd"], wide["control"]["fuel_expense_musd"])
    assert np.array_equal(point["intervention"]["fuel_expense_musd"], wide["intervention"]["fuel_expense_musd"])
    assert len(np.unique(wide["recovery"][:, 0])) == 9 and len(np.unique(point["recovery"][:, 0])) == 1
    def band(r):
        effect = (r["intervention"]["operating_income_musd"] - r["control"]["operating_income_musd"])[:, :3].sum(axis=1)
        low, high = np.quantile(effect, [.1, .9])
        return high - low
    assert band(wide) > 1.5 * band(point)


# ------------------------------------------------------------------ response module

def test_response_constructors_sampling_and_monthly_rule():
    assert np.array_equal(R.monthly_curve([0, 1, 2, 3, 4], 18), [0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4, 4, 4, 4])
    assert R.monthly_curve(np.arange(10.).reshape(2, 5), 4).tolist() == [[0, 0, 0, 1], [5, 5, 5, 6]]
    assert R.costs_only().draws.tolist() == [[0] * 5] and R.costs_only().curve() == [0] * 5
    promise = R.management([(.8, .9), 1.0])
    assert promise.draws.shape == (R.MANAGEMENT_DRAWS, 5)
    first = promise.draws[:, 0]
    assert .8 <= first.min() < .805 and .895 < first.max() <= .9 and first.mean() == pytest.approx(.85, abs=.005)
    assert np.quantile(first, [.25, .5, .75]) == pytest.approx([.825, .85, .875], abs=.006)  # uniform within the range
    assert (promise.draws[:, 1:] == 1).all()  # later quarters hold the last stated value
    assert R.flat(.3).draws.tolist() == [[.3] * 5] and R.flat(.3).source["kind"] == "flat override"
    keyed = R.management([{"quarters_after": 1, "recovery_low": .9, "recovery_high": 1.0}, {"quarters_after": 0, "low": .5, "high": .7}])
    position = (keyed.draws[:, 0] - .5) / .2
    assert keyed.draws[:, 1] == pytest.approx(.9 + .1 * position) and np.array_equal(keyed.draws[:, 1], keyed.draws[:, 4])
    assert np.array_equal(R.management([(.8, .9), 1.0]).draws, promise.draws)
    for bad in ([], [(.9, .8)], [{"quarters_after": 1, "low": .5, "high": .6}], [(0, 1)] * 6, [(.1, float("nan"))]):
        with pytest.raises(ValueError):
            R.management(bad)
    draws = np.arange(35.).reshape(7, 5)
    response = R.from_draws(draws)
    a, b = response.sample(1000, 5), response.sample(1000, 5)
    assert np.array_equal(a, b) and not np.array_equal(a, response.sample(1000, 6))
    assert {tuple(row) for row in a} == {tuple(row) for row in draws}  # whole joint rows, never mixed quarters
    assert response.monthly_shares(1000, 14, 5).shape == (1000, 14)
    assert np.array_equal(response.monthly_shares(1000, 14, 5)[:, [0, 3, 13]], a[:, [0, 1, 4]])
    assert response.describe()["curve_p50"] == [15, 16, 17, 18, 19] and response.fingerprint != R.from_draws(draws + 1).fingerprint
    with pytest.raises(ValueError):
        response.draws[0, 0] = 9
    for bad in (np.zeros((3, 4)), np.zeros(5), np.zeros((0, 5)), [[0, 0, 0, 0, float("inf")]]):
        with pytest.raises(ValueError):
            R.from_draws(bad)


def test_measured_response_loads_curve_draws_or_says_not_computed(tmp_path):
    with pytest.raises(R.NotComputed, match="has not been built"):
        R.measured()
    path = tmp_path / "fare_clock.json"
    curve = [{"quarters_after": q, "p10": .1 * q, "p50": .2 * q, "p90": .3 * q} for q in range(5)]
    draws = [[.1, .2, .3, .4, .5], [.3, .4, .5, .6, .7], [.2, .3, .4, .5, .6]]
    path.write_text(json.dumps({"fixture": True, "curve": curve, "curve_draws": draws}))
    with pytest.raises(R.NotComputed, match="placeholder"):
        R.measured()
    path.write_text(json.dumps({"curve": curve}))
    with pytest.raises(R.NotComputed, match="curve_draws"):
        R.measured()
    path.write_text(json.dumps({"curve": curve[:4], "curve_draws": draws}))
    with pytest.raises(ValueError, match="quarters_after 0..4"):
        R.measured()
    path.write_text(json.dumps({"as_of": "2026-10-08", "curve": curve, "curve_draws": draws}))
    fitted = R.measured()
    assert fitted.name == "measured" and fitted.draws.tolist() == draws and fitted.curve() == [.2, .3, .4, .5, .6]
    assert fitted.source["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest() and fitted.source["published_curve"] == curve
    result = S.run_scenario(small())
    assert result["modes"]["measured"]["status"] == "ok" and result["response_used"]["measured"]["source"]["kind"] == "measured"
    assert {"path": fitted.source["path"], "sha256": fitted.source["sha256"]} in result["inputs"]


# ------------------------------------------------------------------ scenario runner

def test_measured_mode_reports_not_computed_while_the_other_modes_run():
    result = S.run_scenario(small())
    assert result["status"] == "ok" and S.validate_response(result) == []
    assert result["modes"]["measured"]["status"] == "not_computed" and "has not been built" in result["modes"]["measured"]["reason"]
    assert result["bridge_per_share"] is None and "measured" not in result["response_used"]
    assert result["modes"]["costs_only"]["status"] == result["modes"]["management"]["status"] == "ok"
    assert "synthetic_response" not in result
    costs, stated = (result["modes"][m]["quarters"] for m in ("costs_only", "management"))
    first = [q[0]["operating_income_change_musd"]["p50"] for q in (costs, stated)]
    assert first[0] < first[1] < 0 and first[1] == pytest.approx(.55 * first[0], rel=.1)  # 40-50% recovered in quarter 0
    second = [q[1]["operating_income_change_musd"]["p50"] for q in (costs, stated)]
    assert second[1] == pytest.approx(.15 * second[0], rel=.15)  # 80-90% in quarter 1
    source = result["response_used"]["management"]["source"]
    assert source["literature_ids"] == ["ual-2026q1-investor-update", "ual-2026q2-investor-update", "ual-2026q2-earnings-release"]
    assert source["path_name"] == "july_2026" and source["mapping"] == R.MANAGEMENT_MAPPING
    assert any(R.MANAGEMENT_MAPPING in line for line in result["assumptions"])
    assert any("symmetric: a falling fuel price is given back at the same speed" in line for line in result["assumptions"])
    assert S.run_scenario(small(), {"measured": None})["modes"]["measured"] == {"status": "not_computed", "reason": "no response was supplied"}


def shape(value):
    if isinstance(value, dict):
        return {k: shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return "number" if isinstance(value, (int, float)) and not isinstance(value, bool) else type(value).__name__


def test_response_validates_against_the_documented_shape_and_matches_the_saved_example():
    result = S.run_scenario(DEFAULT_REQUEST, {"measured": synthetic_measured()})
    assert S.validate_response(result) == []
    assert result["synthetic_response"] is True and result["paths"] == S.DEFAULT_PATHS
    assert [q["period"] for q in result["modes"]["measured"]["quarters"]] == ["2026Q4", "2027Q1", "2027Q2", "2027Q3"]
    assert all(q["complete"] for q in result["modes"]["measured"]["quarters"])
    assert result["bridge_per_share"] == result["modes"]["measured"]["bridge"]
    assert result["as_of"]["shock_starts"] == "2026-10" and result["as_of"]["unreported_lead_months"] == ["2026-08", "2026-09"]
    json.dumps(result, allow_nan=False)
    saved = json.loads(EXAMPLE.read_text())
    assert saved["synthetic_response"] is True and saved["response_used"]["measured"]["source"]["kind"] == "synthetic"
    assert S.validate_response(saved) == []
    assert shape(saved) == shape(result)
    for broken in ({**result, "extra": 1}, {**result, "modes": {**result["modes"], "measured": {"status": "ok"}}},
                   {**result, "bridge_per_share": None}, {"status": "invalid", "errors": []}, {"status": "weird"}, None):
        assert S.validate_response(broken)
    worse = deepcopy(result)
    worse["modes"]["costs_only"]["quarters"][0]["fcf_musd"]["p10"] = 1e9
    assert any("fcf_musd" in problem for problem in S.validate_response(worse))


def test_same_request_is_deterministic_cached_and_has_a_stable_run_id(monkeypatch):
    first = S.run_scenario(small())
    again = S.run_scenario(small())
    assert first["cached"] is False and again["cached"] is True and stable(first) == stable(again)
    reordered = S.run_scenario(dict(reversed(list(small(fuel_shock_pct=25.0, seed=1.0).items()))))
    assert reordered["cached"] is True and reordered["run_id"] == first["run_id"]
    again["modes"]["costs_only"]["prob"]["fcf_negative_12m"] = 9  # callers cannot corrupt the cache
    assert S.run_scenario(small())["modes"]["costs_only"]["prob"] == first["modes"]["costs_only"]["prob"]
    S.clear_cache()
    fresh = S.run_scenario(small())
    assert fresh["cached"] is False and stable(fresh) == stable(first)
    other = S.run_scenario(small(seed=2))
    assert other["run_id"] != first["run_id"] and other["modes"]["costs_only"] != first["modes"]["costs_only"]
    assert S.run_scenario(small(), {"measured": synthetic_measured()})["run_id"] != first["run_id"]
    monkeypatch.setattr(S, "CACHE_SIZE", 3)
    for i in range(4):
        S.run_scenario(small(paths=200, seed=100 + i))
    assert len(S._cache) == 3 and first["run_id"] not in S._cache


def test_fuel_level_and_percentage_are_two_statements_of_one_shock():
    reference = S.baseline_fuel_usd_gal()
    assert reference["usd_gal"] == round(5110 / 1219, 4) and reference["source"]["values"] == {"fuel_expense": 5110, "fuel_gallons": 1219}
    assert reference["source"]["record_ids"] == ["UAL.fuel_expense.2026-04-01.ual_q2_2026_release", "UAL.fuel_gallons.2026-04-01.ual_q2_2026_release"]
    assert reference["market"]["spot_at_cutoff"]["date"] <= reference["information_cutoff"] == S.INFORMATION_CUTOFF
    by_pct = S.run_scenario(small(fuel_shock_pct=25))
    level = 5110 / 1219 * 1.25
    by_level = S.run_scenario({k: v for k, v in small(fuel_usd_gal=level).items() if k != "fuel_shock_pct"})
    assert by_pct["fuel"]["given_as"] == "fuel_shock_pct" and by_level["fuel"]["given_as"] == "fuel_usd_gal"
    for run in (by_pct, by_level):
        assert run["fuel"]["shock_pct"] == pytest.approx(25, abs=1e-6) and run["fuel"]["level_usd_gal"] == pytest.approx(level, abs=1e-4)
        assert run["fuel"]["shock_usd_gal"] == pytest.approx(level - 5110 / 1219, abs=1e-4)
    for mode in ("costs_only", "management"):
        a, b = by_pct["modes"][mode], by_level["modes"][mode]
        assert a["prob"] == b["prob"]
        assert a["next_12m"]["operating_income_change_musd"]["p50"] == pytest.approx(b["next_12m"]["operating_income_change_musd"]["p50"], abs=.11)
    none = S.run_scenario({"paths": 400})
    assert none["fuel"]["shock_pct"] == 0 and none["fuel"]["level_usd_gal"] == none["fuel"]["reference_usd_gal"]
    for mode in ("costs_only", "management"):
        assert all(q["operating_income_change_musd"] == {"p10": 0, "p50": 0, "p90": 0} for q in none["modes"][mode]["quarters"])
        assert none["modes"][mode]["prob"]["fcf_negative_12m"] == none["modes"][mode]["prob"]["fcf_negative_12m_no_shock"]


def test_nonsense_requests_return_invalid_with_explicit_bounds_and_never_raise():
    cases = {
        "not an object": ("please run it", "JSON object"),
        "both fuel forms": ({"fuel_shock_pct": 10, "fuel_usd_gal": 5}, "not both"),
        "pct too high": ({"fuel_shock_pct": 400}, "fuel_shock_pct must be between -50 and 80"),
        "level too high": ({"fuel_usd_gal": 40}, "fuel_usd_gal must be between 2.10 and 7.54"),
        "level negative": ({"fuel_usd_gal": -1}, "fuel_usd_gal must be between"),
        "text number": ({"fuel_shock_pct": "25%"}, "fuel_shock_pct must be a number"),
        "boolean": ({"demand_shock_pct": True}, "demand_shock_pct must be a number"),
        "nan": ({"fuel_usd_gal": float("nan")}, "fuel_usd_gal must be a number"),
        "capacity growth": ({"capacity_change_pct": 5}, "capacity_change_pct must be between -90 and 0"),
        "fractional months": ({"fuel_shock_months": 2.5}, "fuel_shock_months must be a whole number between 1 and 24"),
        "short horizon": ({"horizon_months": 3}, "horizon_months must be between 12 and 22"),
        "paths": ({"paths": 10 ** 7}, "paths must be between 200 and 20000"),
        "seed": ({"seed": -1}, "seed must be between 0 and 1000000000"),
        "unknown": ({"fuel_shock": 25, "competitor_capacity_pct": 9}, "unknown field(s)"),
        "delay shape": ({"delivery_delay": 6}, "delivery_delay must be an object"),
        "delay half given": ({"delivery_delay": {"aircraft": 5, "months": 0}}, "both aircraft and months above zero"),
        "delay too many": ({"delivery_delay": {"aircraft": 500, "months": 6}}, "delivery_delay.aircraft must be at most"),
        "delay outside window": ({"fuel_shock_months": 1, "delivery_delay": {"aircraft": 1, "months": 6}}, "at most 0"),
        "capacity option": ({"capacity_response": "fitted"}, 'capacity_response must be "none" or "measured"'),
    }
    for name, (request, message) in cases.items():
        result = S.run_scenario(request)
        assert result["status"] == "invalid" and any(message in e for e in result["errors"]), (name, result)
        assert S.validate_response(result) == []
    many = S.run_scenario({"fuel_shock_pct": 400, "seed": "x", "horizon_months": 99})
    assert len(many["errors"]) == 3
    assert S.run_scenario({"fuel_shock_pct": None, "delivery_delay": None, "paths": 200})["status"] == "ok"


def test_compare_subtracts_p50s_and_probabilities_per_mode_and_quarter():
    mild, harsh = S.run_scenario(small(fuel_shock_pct=10)), S.run_scenario(small(fuel_shock_pct=40))
    gap = S.compare(mild, harsh)
    assert gap["status"] == "ok" and gap["definition"] == "run_b minus run_a" and gap["same_worlds"] is True
    assert (gap["run_a"], gap["run_b"]) == (mild["run_id"], harsh["run_id"])
    assert gap["modes"]["measured"] == {"status": "not_computed"}
    for mode in ("costs_only", "management"):
        rows = gap["modes"][mode]["quarters"]
        assert [r["period"] for r in rows] == [q["period"] for q in mild["modes"][mode]["quarters"]]
        for row, a, b in zip(rows, mild["modes"][mode]["quarters"], harsh["modes"][mode]["quarters"]):
            assert row["operating_income_musd_p50"] == pytest.approx(b["operating_income_musd"]["p50"] - a["operating_income_musd"]["p50"], abs=.051)
            assert row["operating_margin_p50"] == pytest.approx(b["operating_margin"]["p50"] - a["operating_margin"]["p50"], abs=1e-4)
            assert row["fcf_musd_p50"] == pytest.approx(b["fcf_musd"]["p50"] - a["fcf_musd"]["p50"], abs=.051)
            assert row["eps_change_p50"] == pytest.approx(b["eps_change"]["p50"] - a["eps_change"]["p50"], abs=1e-3)
        assert gap["modes"][mode]["prob"]["fcf_negative_12m"] == pytest.approx(
            harsh["modes"][mode]["prob"]["fcf_negative_12m"] - mild["modes"][mode]["prob"]["fcf_negative_12m"], abs=1e-4)
    assert gap["modes"]["costs_only"]["quarters"][0]["operating_income_musd_p50"] < 0
    assert gap["modes"]["costs_only"]["prob"]["fcf_negative_12m"] > 0
    assert S.compare(mild["run_id"], harsh["run_id"]) == gap
    assert S.compare(mild, S.run_scenario(small(fuel_shock_pct=40, seed=5)))["same_worlds"] is False
    for bad in (("0" * 64, harsh), (mild, {"status": "invalid", "errors": ["x"]}), (None, 3)):
        assert S.compare(*bad)["status"] == "invalid"


def test_per_share_figures_use_only_sourced_inputs_or_are_null_with_a_reason(tmp_path, monkeypatch):
    inputs = S.per_share_inputs()
    assert inputs["available"] and inputs["tax_rate"] == pytest.approx(.2) and inputs["diluted_shares_m"] == 400
    assert inputs["source"]["period_end"] == "2026-06-30"  # latest quarter filed by the cutoff, not the later one
    result = S.run_scenario(small(), {"measured": synthetic_measured()})
    for mode in S.MODES:
        year = result["modes"][mode]["next_12m"]
        assert year["eps_change"]["p50"] == pytest.approx(year["operating_income_change_musd"]["p50"] * .8 / 400, abs=2e-3)
        for step in result["modes"][mode]["bridge"]:
            if step["step"] != "profit sharing":
                assert step["value"] == pytest.approx(step["operating_income_musd"] * .8 / 400, abs=2e-3)
    monkeypatch.setattr(S, "QUARTERLY_CSV", tmp_path / "absent.csv")
    S.clear_cache()
    bare = S.run_scenario(small(), {"measured": synthetic_measured()})
    assert bare["run_id"] != result["run_id"] and S.validate_response(bare) == []
    assert bare["per_share"]["available"] is False and "has not been built" in bare["per_share"]["reason"]
    assert bare["per_share"]["tax_rate"] is None and bare["per_share"]["diluted_shares_m"] is None
    assert all(q["eps_change"] is None for q in bare["modes"]["measured"]["quarters"])
    assert all(step["value"] is None for step in bare["bridge_per_share"])
    assert bare["modes"]["measured"]["quarters"] == [{**q, "eps_change": None} for q in result["modes"]["measured"]["quarters"]]
    # The two older anchor files still carry neither input; wire them in if this ever fails.
    q2 = json.loads((ROOT / "data" / "ual_q2_2026.json").read_text())
    anchors = {r["variable"] for r in load_snapshot()["records"]}
    assert not any("tax" in key or "dilut" in key or "share" in key for key in q2)
    assert not any("tax" in v or "dilut" in v or v.endswith("shares") for v in anchors)


def test_bridge_adds_up_and_separates_fuel_recovery_capacity_and_demand():
    request = small(paths=600, capacity_change_pct=-4, demand_shock_pct=-3, delivery_delay={"aircraft": 10, "months": 6})
    result = S.run_scenario(request, {"measured": synthetic_measured()})
    assert S.validate_response(result) == []
    for mode in S.MODES:
        steps = {row["step"]: row for row in result["modes"][mode]["bridge"]}
        parts = sum(steps[name]["operating_income_musd"] for name in ("fuel cost", "fare recovery", "capacity", "demand"))
        assert parts == pytest.approx(steps["net"]["operating_income_musd"], abs=.21)  # five values rounded to 0.1
        assert steps["profit sharing"] == {"step": "profit sharing", "value": None, "operating_income_musd": None}
        assert steps["fuel cost"]["operating_income_musd"] < 0 and steps["demand"]["operating_income_musd"] < 0
    costs, measured = (({row["step"]: row for row in result["modes"][m]["bridge"]}) for m in ("costs_only", "measured"))
    assert costs["fare recovery"]["operating_income_musd"] == 0 and measured["fare recovery"]["operating_income_musd"] > 0
    assert costs["fuel cost"] == measured["fuel cost"]  # same worlds, same fuel bill
    fuel_only = S.run_scenario(small(paths=600), {"measured": synthetic_measured()})
    alone = {row["step"]: row["operating_income_musd"] for row in fuel_only["modes"]["measured"]["bridge"]}
    assert alone["capacity"] == alone["demand"] == 0 and alone["fuel cost"] == costs["fuel cost"]["operating_income_musd"]
    assert alone["fare recovery"] == measured["fare recovery"]["operating_income_musd"]


def test_monte_carlo_error_is_reported_and_shrinks_with_paths():
    few, many = S.run_scenario(small(paths=400)), S.run_scenario(small(paths=6400))
    for run in (few, many):
        assert run["monte_carlo"]["paths"] == run["paths"] and "batch means" in run["monte_carlo"]["method"]
        assert set(run["monte_carlo"]["standard_error"]) == {"costs_only", "management"}
        assert all(v > 0 for v in run["monte_carlo"]["standard_error"]["costs_only"].values())
    a, b = (run["monte_carlo"]["standard_error"]["costs_only"]["operating_income_change_12m_musd_p50"] for run in (few, many))
    assert b < a
    # The reported error is honest: the two runs differ by no more than a few of them.
    gap = abs(few["modes"]["costs_only"]["next_12m"]["operating_income_change_musd"]["p50"]
              - many["modes"]["costs_only"]["next_12m"]["operating_income_change_musd"]["p50"])
    assert gap < 5 * math.hypot(a, b)


def test_management_paths_are_united_statements_on_quarters_after_the_shock():
    expected = {"july_2026": [(.40, .50), (.80, .90), (1, 1), (1, 1), (1, 1)],
                "april_2026": [(.40, .50), (.70, .80), (.85, 1), (1, 1), (1, 1)]}
    assert R.management().source["path_name"] == "july_2026" and set(R.MANAGEMENT_PATHS) == set(expected)
    for name, ranges in expected.items():
        path = R.management(name)
        assert path.draws.shape == (R.MANAGEMENT_DRAWS, 5) and path.name == "management"
        for quarter, (low, high) in enumerate(ranges):
            column = path.draws[:, quarter]
            assert low <= column.min() and column.max() <= high
            assert column.mean() == pytest.approx((low + high) / 2, abs=.005)  # uniform within the stated range
            if high > low:
                assert np.quantile(column, [.25, .75]) == pytest.approx([low + .25 * (high - low), low + .75 * (high - low)], abs=.008)
        assert path.source["mapping"] == R.MANAGEMENT_MAPPING and path.source["path"] == "data/lab/literature.json"
        assert [r["low"] for r in path.source["ranges"]] == [low for low, _ in ranges]
    assert R.management("april_2026").source["literature_ids"] == ["ual-2026q1-investor-update"]
    assert R.management().fingerprint != R.management("april_2026").fingerprint
    with pytest.raises(ValueError, match="Unknown management path"):
        R.management("october_2026")
    # The ranges are the numbers quoted in the literature file, entry by entry.
    entries = {e["id"]: e for e in json.loads(R.LITERATURE.read_text())["entries"]}
    def stated(entry_id, word):
        return {n["value"] for n in entries[entry_id]["numbers"] if word in n["label"].lower() and "percent" in n["unit"]}
    assert {40, 50, 70, 80, 85, 100} <= stated("ual-2026q1-investor-update", "recovery")
    assert {80, 90, 100} <= stated("ual-2026q2-investor-update", "recovery")
    assert 50 in stated("ual-2026q2-earnings-release", "recovered")
    april = S.run_scenario(small(), {"management": R.management("april_2026")})
    assert april["response_used"]["management"]["source"]["path_name"] == "april_2026"
    july = S.run_scenario(small())
    second = [run["modes"]["management"]["quarters"][1]["operating_income_change_musd"]["p50"] for run in (april, july)]
    assert second[0] < second[1] < 0  # 70-80% recovered is worse than 80-90%


# ------------------------------------------------------------------ grid

GRID = {"x": {"driver": "fuel_shock_pct", "values": [40, 0, 20, -20]}, "y": {"driver": "demand_shock_pct", "values": [-6, 0]},
        "metric": "fcf_12m_musd", "threshold": 0, "direction": "below", "mode": "costs_only", "base": {"horizon_months": 12, "paths": 600}}


def rows(grid, key):
    width = len(grid["x"]["values"])
    return [[cell[key] for cell in grid["cells"][i:i + width]] for i in range(0, len(grid["cells"]), width)]


def test_grid_is_monotonic_in_fuel_and_matches_the_scenario_runner_on_the_same_worlds():
    grid = S.run_grid(GRID)
    assert set(grid) == {"status", "run_id", "elapsed_ms", "cached", "x", "y", "metric", "period", "threshold", "direction", "mode",
                         "base", "cells", "boundary", "boundary_method", "prob_definition", "paths", "monte_carlo",
                         "response_used", "capacity_response", "notes"}
    assert grid["status"] == "ok" and grid["period"] == "12m" and grid["paths"] == 600 and grid["mode"] == "costs_only"
    assert grid["x"]["values"] == [-20, 0, 20, 40] and grid["y"]["values"] == [-6, 0]  # sorted
    assert [(c["x"], c["y"]) for c in grid["cells"]] == [(x, y) for y in (-6, 0) for x in (-20, 0, 20, 40)]
    assert all(set(c) == {"x", "y", "prob", "p50"} for c in grid["cells"])
    for prob, p50 in zip(rows(grid, "prob"), rows(grid, "p50")):
        assert prob == sorted(prob) and prob[0] < prob[-1]  # dearer fuel, more runs with negative cash flow
        assert p50 == sorted(p50, reverse=True)
    assert all(low >= high for low, high in zip(*rows(grid, "prob")))  # weaker demand is never safer
    run = S.run_scenario(small(paths=600, fuel_shock_pct=20, demand_shock_pct=-6))["modes"]["costs_only"]
    cell = next(c for c in grid["cells"] if (c["x"], c["y"]) == (20, -6))
    assert cell["prob"] == run["prob"]["fcf_negative_12m"] and cell["p50"] == run["next_12m"]["fcf_musd"]["p50"]
    assert grid["monte_carlo"]["prob_standard_error_at_half"] == pytest.approx(.5 / math.sqrt(600), abs=1e-4)
    assert 0 < grid["monte_carlo"]["prob_standard_error_max"] <= grid["monte_carlo"]["prob_standard_error_at_half"]
    assert "not the probability of the event in the world" in grid["prob_definition"] and "linear interpolation" in grid["boundary_method"]
    again = S.run_grid({**GRID, "x": {"driver": "fuel_shock_pct", "values": [-20.0, 0, 20, 40]}})
    assert again["cached"] is True and stable(again) == stable(grid)
    S.clear_cache()
    assert stable(S.run_grid(GRID)) == stable(grid)
    flipped = S.run_grid({**GRID, "direction": "at_or_above"})
    assert rows(flipped, "prob") == [[pytest.approx(1 - p, abs=1e-4) for p in row] for row in rows(grid, "prob")]
    json.dumps(grid, allow_nan=False)


def test_boundary_is_linear_interpolation_between_tested_points():
    found = S.boundary([0, 10, 20], [0, 1], [[.2, .4, .8], [.5, .9, 1.0]])
    assert found == [{"x": 0, "y": 1}, {"x": 10, "y": .2}, {"x": 12.5, "y": 0}]
    assert S.boundary([0, 1], [0, 1], [[.1, .2], [.3, .4]]) == [] and S.boundary([5], [7], [[.5]]) == [{"x": 5, "y": 7}]
    # A plane prob = 0.05 x + 0.07 y is linear, so every interpolated point lies exactly where it crosses one half.
    xs, ys = [0, 4, 8, 12], [0, 1, 2, 3]
    plane = S.boundary(xs, ys, [[.05 * x + .07 * y for x in xs] for y in ys])
    assert len(plane) == 5 and all(.05 * point["x"] + .07 * point["y"] == pytest.approx(.5, abs=1e-4) for point in plane)
    grid = S.run_grid(GRID)
    prob = {(c["x"], c["y"]): c["prob"] for c in grid["cells"]}
    xs = grid["x"]["values"]
    for point in grid["boundary"]:
        if point["y"] in grid["y"]["values"] and point["x"] not in xs:
            left = max(x for x in xs if x < point["x"])
            right = min(x for x in xs if x > point["x"])
            low, high = prob[left, point["y"]], prob[right, point["y"]]
            assert low < .5 < high
            assert point["x"] == pytest.approx(left + (.5 - low) / (high - low) * (right - left), abs=.2)  # cells are rounded to 4 places
    assert any(point["x"] not in xs for point in grid["boundary"])


def test_recovery_share_override_spans_costs_only_to_a_full_offset():
    request = {"x": {"driver": "fuel_shock_pct", "values": [0, 30]}, "y": {"driver": "recovery_share", "values": [0, .5, 1]},
               "metric": "operating_income_12m_musd", "threshold": 3000, "base": {"paths": 500}}
    grid = S.run_grid(request)  # no mode's curve is needed, so the missing measured response does not matter
    assert grid["status"] == "ok" and grid["response_used"]["name"] == "recovery_share"
    costs_only = S.run_grid({**request, "y": {"driver": "demand_shock_pct", "values": [0]}, "mode": "costs_only"})
    assert [(c["prob"], c["p50"]) for c in grid["cells"][:2]] == [(c["prob"], c["p50"]) for c in costs_only["cells"]]
    none, half, full = rows(grid, "p50")
    assert none[1] < half[1] < full[1] and none[1] < none[0] - 1000
    change = S.run_grid({**request, "metric": "eps_change", "threshold": -.001})
    none, half, full = rows(change, "p50")
    assert full == [0, 0] and rows(change, "prob")[2] == [0, 0]  # share 1: the shock is offset on every path
    assert none[1] < half[1] < 0 and half[1] == pytest.approx(.5 * none[1], rel=.02) and rows(change, "prob")[0] == [0, 1]
    assert change["boundary"] == [{"x": 15, "y": 0}, {"x": 15, "y": .5}, {"x": 30, "y": .75}]
    quarter = S.run_grid({**request, "metric": "eps_change", "period": "2027Q1", "threshold": -1})
    assert quarter["period"] == "2027Q1" and rows(quarter, "p50")[0][1] > rows(change, "p50")[0][1]


def test_grid_refuses_nonsense_caps_cells_and_lowers_paths_by_a_fixed_rule(monkeypatch, tmp_path):
    cases = {
        "not an object": ([1, 2], "JSON object"),
        "missing axis": ({"x": GRID["x"]}, "y must be an object"),
        "unknown driver": ({**GRID, "y": {"driver": "oil", "values": [1]}}, "y.driver must be one of"),
        "same driver": ({**GRID, "y": GRID["x"]}, "x and y must be different drivers"),
        "two fuel drivers": ({**GRID, "y": {"driver": "fuel_usd_gal", "values": [4, 5]}}, "only one of them may be a fuel driver"),
        "too many cells": ({**GRID, "x": {"driver": "fuel_shock_pct", "values": list(range(41))}, "y": {"driver": "demand_shock_pct", "values": [0, 1, 2]}}, "the limit is 81"),
        "repeated value": ({**GRID, "x": {"driver": "fuel_shock_pct", "values": [10, 10]}}, "x.values must not repeat"),
        "text value": ({**GRID, "x": {"driver": "fuel_shock_pct", "values": ["high"]}}, "x.values must be a non-empty list of numbers"),
        "value out of range": ({**GRID, "x": {"driver": "fuel_shock_pct", "values": [0, 300]}}, "fuel_shock_pct must be between -50 and 80"),
        "metric": ({**GRID, "metric": "share_price"}, "metric must be one of"),
        "direction": ({**GRID, "direction": "under"}, 'direction must be "below" or "at_or_above"'),
        "threshold": ({**GRID, "threshold": "zero"}, "threshold must be a number"),
        "mode": ({**GRID, "mode": "hopeful"}, "mode must be one of"),
        "period": ({**GRID, "metric": "operating_margin", "period": "2031Q1"}, "period for operating_margin must be one of"),
        "period for a 12-month metric": ({**GRID, "period": "2026Q4"}, "period for fcf_12m_musd must be one of ['12m']"),
        "driver also in base": ({**GRID, "base": {"fuel_usd_gal": 5}}, "remove fuel_usd_gal from base"),
        "delay without aircraft": ({**GRID, "y": {"driver": "delivery_delay_months", "values": [0, 6]}}, "set base.delivery_delay.aircraft"),
        "share out of range": ({**GRID, "y": {"driver": "recovery_share", "values": [0, 3]}}, "recovery_share values must be between -0.5 and 1.5"),
        "unknown field": ({**GRID, "colour": "red"}, "unknown field(s)"),
        "bad base": ({**GRID, "base": {"seed": -4}}, "seed must be between 0 and 1000000000"),
    }
    for name, (request, message) in cases.items():
        result = S.run_grid(request)
        assert result["status"] == "invalid" and any(message in e for e in result["errors"]), (name, result)
    assert S.run_grid({**GRID, "mode": "measured"}) == {"status": "not_computed", "reason": S.run_scenario(small())["modes"]["measured"]["reason"]}
    delays = S.run_grid({**GRID, "y": {"driver": "delivery_delay_months", "values": [0, 12]}, "base": {"paths": 300, "delivery_delay": {"aircraft": 36}}})
    assert delays["status"] == "ok" and delays["base"]["delivery_delay"] == {"aircraft": 36} and rows(delays, "p50")[0] != rows(delays, "p50")[1]
    assert rows(delays, "p50")[0] == rows(S.run_grid({**GRID, "y": {"driver": "demand_shock_pct", "values": [0]}, "base": {"paths": 300}}), "p50")[0]
    level = S.run_grid({**GRID, "x": {"driver": "fuel_usd_gal", "values": [4, 5, 6]}, "metric": "operating_margin", "threshold": .02})
    assert level["period"] == "2026Q4" and "fuel_shock_pct" not in level["base"] and level["base"]["fuel_shock_months"] == 6
    assert rows(level, "p50")[1] == sorted(rows(level, "p50")[1], reverse=True)
    monkeypatch.setattr(S, "GRID_PATH_MONTHS", 9 * 14 * 700)
    lowered = S.run_grid({**GRID, "x": {"driver": "fuel_shock_pct", "values": [0, 20, 40]}, "y": {"driver": "demand_shock_pct", "values": [-4, 0, 4]}, "base": {}})
    assert lowered["paths"] == 700 and lowered["monte_carlo"]["paths_requested"] == S.DEFAULT_PATHS
    assert any("lowered from 10000 to 700" in note for note in lowered["notes"])
    assert lowered["monte_carlo"]["prob_standard_error_at_half"] == pytest.approx(.5 / math.sqrt(700), abs=1e-4)
    monkeypatch.setattr(S, "QUARTERLY_CSV", tmp_path / "absent.csv")
    assert S.run_grid({**GRID, "metric": "eps_change"})["status"] == "not_computed"
    assert S.run_grid(GRID)["status"] == "ok"  # other metrics do not need per-share inputs


# ------------------------------------------------------------------ capacity response

def test_capacity_response_is_optional_and_runs_without_the_file():
    plain = S.run_scenario(small(fuel_shock_pct=40))
    assert plain["capacity_response"] == {"status": "not_requested"} and plain["request"]["capacity_response"] == "none"
    asked = S.run_scenario(small(fuel_shock_pct=40, capacity_response="measured"))
    assert S.validate_response(asked) == [] and asked["run_id"] != plain["run_id"]
    assert asked["capacity_response"]["status"] == "not_computed" and "has not been built" in asked["capacity_response"]["reason"]
    assert asked["modes"] == plain["modes"]  # ran without it
    assert any("capacity follows the plan" in line for line in asked["assumptions"])


def test_measured_capacity_response_cuts_capacity_by_group_on_the_same_worlds():
    capacity_file(R.CAPACITY_RESPONSE)
    request = small(paths=800, fuel_shock_pct=40, fuel_shock_months=12, capacity_response="measured")
    plain = S.run_scenario({**request, "capacity_response": "none"}, {"measured": synthetic_measured()})
    cut = S.run_scenario(request, {"measured": synthetic_measured()})
    block = cut["capacity_response"]
    assert S.validate_response(cut) == [] and block["status"] == "ok" and block["groups"] == S.CAPACITY_GROUPS
    assert block["source"]["kind"] == "measured" and {"path": block["source"]["path"], "sha256": block["source"]["sha256"]} in cut["inputs"]
    assert "groups of paths" in block["approximation"] and block["approximation"] in cut["assumptions"]
    changes = block["capacity_change_pct"]
    assert [q["period"] for q in changes] == ["2026Q4", "2027Q1", "2027Q2", "2027Q3"]
    assert changes[0] == {"period": "2026Q4", "p10": 0, "p50": 0, "p90": 0}  # the curve starts at zero in quarter 0
    assert changes[1]["p50"] == pytest.approx(-.3 * 4, abs=.15) and changes[3]["p50"] == pytest.approx(-.8 * 4, abs=.15)  # per 10% of a 40% shock
    assert all(q["p10"] <= q["p50"] <= q["p90"] <= 0 for q in changes) and changes[3]["p10"] < changes[3]["p90"]
    for mode in S.MODES:
        steps, before = ({row["step"]: row["operating_income_musd"] for row in run["modes"][mode]["bridge"]} for run in (cut, plain))
        assert steps["fuel cost"] == before["fuel cost"] and steps["fare recovery"] == before["fare recovery"]  # same worlds
        assert before["capacity"] == 0 and steps["capacity"] != 0
        assert sum(steps[name] for name in ("fuel cost", "fare recovery", "capacity", "demand")) == pytest.approx(steps["net"], abs=.21)
        assert cut["modes"][mode]["quarters"][0] == plain["modes"][mode]["quarters"][0]  # no cut yet in quarter 0
        assert cut["modes"][mode]["quarters"][3] != plain["modes"][mode]["quarters"][3]
    # Stitching groups back together loses nothing: a zero cut reproduces the plain rollout exactly.
    c = S._context()
    req, _, engine_pct, _ = S._checked(request, c)
    s = S._engine_scenario(req, c, engine_pct)
    raw = S._worlds(s, c)
    recovery = synthetic_measured().monthly_shares(s.paths, s.horizon_months, s.seed)
    whole = S._roll(raw, s, recovery)
    order = np.random.default_rng(5).permutation(s.paths)
    parts = S._roll(raw, s, recovery, groups=[(index, np.zeros(s.horizon_months)) for index in np.array_split(order, 8)])
    assert set(whole) == set(parts) and all(np.array_equal(whole[name], parts[name]) for name in whole)
    groups, change = S._capacity_groups(R.capacity(), s, 40.0, c["lead"])
    assert len(groups) == 8 and sorted(np.concatenate([index for index, _ in groups]).tolist()) == list(range(s.paths))
    means = [cuts[c["lead"]:c["lead"] + 12].mean() for _, cuts in groups]
    assert means == sorted(means) and change.shape == (s.paths, s.horizon_months) and (change <= 0).all()
    assert np.mean([cuts for _, cuts in groups], axis=0) == pytest.approx(change.mean(axis=0), abs=1e-9)  # groups are equal-sized
    # The simulator models cuts only: cheaper fuel, or a curve that adds capacity, changes nothing.
    cheaper = S.run_scenario({**request, "fuel_shock_pct": -30}, {"measured": synthetic_measured()})
    assert all(q["p10"] == q["p90"] == 0 for q in cheaper["capacity_response"]["capacity_change_pct"])
    assert cheaper["modes"] == S.run_scenario({**request, "fuel_shock_pct": -30, "capacity_response": "none"}, {"measured": synthetic_measured()})["modes"]
    capacity_file(R.CAPACITY_RESPONSE, curve=(0, .3, .6, .8, .8))
    adding = S.run_scenario(request, {"measured": synthetic_measured()})
    assert adding["modes"] == plain["modes"] and adding["run_id"] != cut["run_id"]
    grid = S.run_grid({**GRID, "mode": "measured", "base": {"paths": 400, "capacity_response": "measured"}}, {"measured": synthetic_measured()})
    assert grid["status"] == "ok" and grid["capacity_response"]["status"] == "ok" and grid["synthetic_response"] is True


# ------------------------------------------------------------------ today

def test_today_request_states_where_each_number_came_from(tmp_path, monkeypatch):
    fuel = tmp_path / "fuel_daily.csv"
    fuel.write_text("date,usd_per_gallon\n2026-10-01,4.000\n2026-10-02,4.500\n")
    monkeypatch.setattr(S, "FUEL_DAILY", fuel)
    bare = S.today_request()
    assert bare["status"] == "ok" and bare["request"] == {"fuel_usd_gal": 4.5, "fuel_shock_months": 12, "horizon_months": 12}
    assert bare["information_cutoff"] == S.INFORMATION_CUTOFF  # the cutoff does not move
    assert [(b["field"], b["value"], b["date"]) for b in bare["basis"]] == [("fuel_usd_gal", 4.5, "2026-10-02"), ("fuel_shock_months", 12, None)]
    assert bare["basis"][0]["source"] == [{"path": str(fuel), "sha256": hashlib.sha256(fuel.read_bytes()).hexdigest()}]
    assert bare["context"]["fuel"]["converted_with_cost_clock"] is False and bare["context"]["fuel"]["spot_usd_gal"] == 4.5
    assert bare["context"]["fuel"]["shock_pct"] == pytest.approx((4.5 / (5110 / 1219) - 1) * 100, abs=1e-3)
    assert any("raw spot price" in line for line in bare["assumptions"])
    assert bare["context"]["demand"]["status"] == "not_computed"
    assert any("no demand reading is built" in line for line in S.today_request(apply_demand=True)["assumptions"])
    run = S.run_scenario({**bare["request"], "paths": 300})
    assert run["status"] == "ok" and run["fuel"]["level_usd_gal"] == 4.5 and run["fuel"]["held_months"] == 12
    # With the cost clock the spot print is converted to the price United pays.
    S.COST_CLOCK.write_text(json.dumps({"fixture": True, "fit": {"basis_usd_gal": 9, "slope": 9}}))
    assert S.today_request()["request"] == bare["request"]  # a placeholder is not used
    S.COST_CLOCK.write_text(json.dumps({"fit": {"spec": "test", "lag_days": 20, "basis_usd_gal": .25, "slope": 1.1}}))
    clocked = S.today_request()
    assert clocked["request"]["fuel_usd_gal"] == pytest.approx(.25 + 1.1 * 4.5) and clocked["context"]["fuel"]["converted_with_cost_clock"] is True
    assert [s["path"] for s in clocked["basis"][0]["source"]] == [str(fuel), str(S.COST_CLOCK)] and "0.25 + 1.1 x spot" in clocked["basis"][0]["how"]
    assert not any("raw spot price" in line for line in clocked["assumptions"])
    # The demand reading is context unless the caller asks for it to be applied.
    S.DEMAND_SIGNAL.write_text(json.dumps({"as_of": "2026-10-08", "source": {"series": "test travellers"}, "quarterly": [
        {"period": "2026Q3", "yoy": -.03, "days_used": 92, "days_in_period": 92, "complete": True},
        {"period": "2026Q4", "yoy": .0125, "days_used": 8, "days_in_period": 92, "complete": False},
        {"period": "2027Q1", "yoy": None}]}))
    context = S.today_request()
    assert "demand_shock_pct" not in context["request"] and len(context["basis"]) == 2
    assert {k: context["context"]["demand"][k] for k in ("status", "yoy_pct", "period", "as_of", "days_used", "complete", "applied")} == {
        "status": "ok", "yoy_pct": 1.25, "period": "2026Q4", "as_of": "2026-10-08", "days_used": 8, "complete": False, "applied": False}
    assert any("Demand is left at plan" in line and "+1.25%" in line for line in context["assumptions"])
    applied = S.today_request(apply_demand=True)
    assert applied["request"]["demand_shock_pct"] == 1.25 and applied["context"]["demand"]["applied"] is True
    assert (applied["basis"][2]["field"], applied["basis"][2]["date"]) == ("demand_shock_pct", "2026-10-08") and "Approximation" in applied["basis"][2]["how"]
    assert S.run_scenario({**applied["request"], "paths": 300})["request"]["demand_shock_pct"] == 1.25
    # A level the simulator cannot take is refused, and a missing fuel file is not invented.
    fuel.write_text("date,usd_per_gallon\n2026-10-02,19.0\n")
    assert S.today_request()["status"] == "invalid" and "fuel_usd_gal must be between" in S.today_request()["errors"][0]
    monkeypatch.setattr(S, "FUEL_DAILY", tmp_path / "absent.csv")
    assert S.today_request()["status"] == "not_computed"
