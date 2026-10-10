from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.evidence import load_snapshot
from app.fleet import GROUPS, capacity_paths, fleet_plan, market_split, trajectory
from app.monthly import MonthlyScenario, _rollout, _world, baseline, month_sequence, shock_weights, simulate_monthly
from app.server import app


def scenario(**kw):
    return MonthlyScenario(**{"paths": 20, "fuel_shock_pct": 0, "stochastic": False, **kw})


def test_issuer_inventory_endpoints_reconcile_without_using_purchase_deliveries():
    p = fleet_plan(load_snapshot(), "2026-07-16")
    assert p["opening_by_group"] == {"narrowbody": 885, "widebody": 237, "regional": 430}
    s = scenario()
    track = trajectory(p, month_sequence(s.information_cutoff, 12), s, shock_weights(s), False)
    sept, dec = track["monthly"][1], track["monthly"][4]
    assert sept["closing_inventory"] == {"narrowbody": 902, "widebody": 243, "regional": 434}
    assert dec["closing_inventory"] == {"narrowbody": 924, "widebody": 249, "regional": 442}
    assert sept["entries"] == {"narrowbody": 27, "widebody": 6, "regional": 4}
    assert sept["exits"]["narrowbody"] == 10
    for r in track["monthly"]:
        for g in GROUPS:
            assert r["closing_inventory"][g] == r["opening_inventory"][g] + r["entries"][g] - r["exits"][g]
    # No unsourced fleet growth in 2027.
    assert track["monthly"][-1]["closing_inventory"] == dec["closing_inventory"]


def test_fleet_evidence_cutoff_rejected_records_and_fractional_inventory():
    snap = load_snapshot()
    with pytest.raises(ValueError, match="No common"):
        fleet_plan(snap, "2026-07-14")
    before = fleet_plan(snap, "2026-07-16")
    changed = deepcopy(snap)
    r = next(r for r in changed["records"] if r["entity"] == "fleet.B787" and r["effective_end"] == "2026-09-30")
    r["value"] = 500
    r["published_on"] = "2027-01-01"
    assert next(e for e in fleet_plan(changed, "2026-07-16")["events"] if e["family"] == "B787")["aircraft"] == 12
    r["published_on"] = "2026-07-15"
    r["value"] = 94.5
    with pytest.raises(ValueError, match="integer"):
        fleet_plan(changed, "2026-07-16")
    r["review_status"] = "pending"
    assert all(e["record_id"] != r["record_id"] for e in fleet_plan(changed, "2026-07-16")["events"])
    assert before["effective_on"] == "2026-06-30"


def test_delays_conserve_aircraft_and_remain_in_queue_beyond_horizon():
    s = scenario(horizon_months=6, delivery_delay_months=12, delivery_group="widebody")
    p = fleet_plan(load_snapshot(), s.information_cutoff)
    t = trajectory(p, month_sequence(s.information_cutoff, 6), s, shock_weights(s), True)
    assert t["monthly"][-1]["closing_inventory"]["widebody"] == 237
    remaining = [e for e in t["remaining_queue"] if e["group"] == "widebody"]
    assert [(e["month"], e["aircraft"]) for e in remaining] == [("2027-09", 6), ("2027-12", 6)]
    assert t["monthly"][1]["entries"]["narrowbody"] == 27
    long_s = replace(s, horizon_months=24)
    long = trajectory(p, month_sequence(s.information_cutoff, 24), long_s, shock_weights(long_s), True)
    assert long["monthly"][-1]["closing_inventory"] == {"narrowbody": 924, "widebody": 249, "regional": 442}
    # A later information cutoff cannot delay previously scheduled entries.
    late = replace(s, information_cutoff="2026-10-09")
    late_t = trajectory(p, month_sequence(late.information_cutoff, 6), late, shock_weights(late), True)
    assert late_t["monthly"][0]["opening_inventory"]["widebody"] == 243
    # Onset/duration select originally due events, not every future delivery.
    short = replace(s, onset_month=2, duration_months=1)
    short_t = trajectory(p, month_sequence(short.information_cutoff, 6), short, shock_weights(short), True)
    assert short_t["monthly"][-1]["closing_inventory"]["widebody"] == 243


def test_outages_restore_capacity_without_destroying_inventory():
    s = scenario(horizon_months=12, aircraft_unavailable_pct=40, availability_group="widebody", duration_months=1, recovery_half_life_months=.1)
    p = fleet_plan(load_snapshot(), s.information_cutoff)
    t = trajectory(p, month_sequence(s.information_cutoff, 12), s, shock_weights(s), True)
    assert t["monthly"][0]["unavailable"] == {"narrowbody": 0, "widebody": 95, "regional": 0}
    assert t["monthly"][0]["available"]["widebody"] == 142
    assert t["monthly"][1]["unavailable"]["widebody"] == 0
    assert t["monthly"][-1]["closing_inventory"]["widebody"] == 249


def test_capacity_commitments_ceiling_and_spare_tradeoff():
    s = scenario(delivery_delay_months=6, aircraft_unavailable_pct=50, availability_group="narrowbody", fleet_spare_fraction=0)
    b = baseline(load_snapshot(), s.information_cutoff)
    f = capacity_paths(b, s, b["fleet_plan"], month_sequence(s.information_cutoff, 12), shock_weights(s))
    split = np.array(f["reference_split_asm_million"])
    initial = np.array([885, 237, 430])
    available = np.array([[r["available"][g] for g in GROUPS] for r in f["intervention"]["monthly"]])
    assert (f["intervention_components"] <= split[None, :, :] * (available / initial)[:, None, :] + 1e-10).all()
    assert (f["intervention_components"] <= f["committed_components"] + 1e-10).all()
    assert f["intervention_components"][0].sum() < f["control_components"][0].sum()
    reserve = capacity_paths(b, replace(s, fleet_spare_fraction=.2), b["fleet_plan"], month_sequence(s.information_cutoff, 12), shock_weights(s))
    assert reserve["intervention_components"][0].sum() > f["intervention_components"][0].sum()


def test_regional_partition_reconciles_and_contract_shock_has_expected_cost():
    s = scenario(regional_contract_shock_pct=10, duration_months=12, response_mode="frozen")
    b = baseline(load_snapshot(), s.information_cutoff)
    split = market_split(b, b["regional_asm_share"])
    np.testing.assert_allclose(split.sum(axis=1), [m["asm_million"] for m in b["markets"]])
    assert split[:, 2].sum() == pytest.approx(87279 / 3 * .061)
    assert b["regional_capacity_purchase_musd"] == pytest.approx(743 / 3)
    fuel, demand = _world(s, 4, .2)
    r = _rollout(b, s, fuel, demand, 4, True, "frozen")
    c = _rollout(b, s, fuel, demand, 4, False, "frozen")
    np.testing.assert_allclose(r["mainline_fuel_expense_musd"] + r["regional_fuel_expense_musd"], r["fuel_expense_musd"])
    np.testing.assert_allclose(r["regional_contract_expense_musd"] + r["other_nonfuel_expense_musd"], r["nonfuel_expense_musd"])
    assert r["fuel_expense_musd"][0, 0] == pytest.approx(5110 / 3)
    assert r["operating_income_musd"][0, 0] - c["operating_income_musd"][0, 0] == pytest.approx(-743 / 3 * .1)
    assert r["free_cash_flow_musd"][0, 0] - c["free_cash_flow_musd"][0, 0] == pytest.approx(-743 / 3 * .1 * .79)
    np.testing.assert_allclose(r["mainline_asm_million"] + r["regional_asm_million"], r["asm_million"])


def test_api_fleet_extremes_binding_constraints_and_zero_placebo():
    client = TestClient(app)
    response = client.post("/api/monthly/simulate", json={"paths": 20, "fuel_shock_pct": 0, "delivery_delay_months": 9, "aircraft_unavailable_pct": 90, "availability_group": "widebody"})
    assert response.status_code == 200
    run = response.json()
    assert run["intervention"]["monthly"]["unserved_schedule_asm_million"][0]["p50"] > 0
    assert run["fleet"]["intervention"]["remaining_queue"]
    for patch in ({"delivery_delay_months": 1.5}, {"availability_group": "hub"}, {"delivery_group": "regional"}, {"aircraft_unavailable_pct": 101}, {"fleet_spare_fraction": 1}):
        assert client.post("/api/monthly/simulate", json=patch).status_code == 422
    zero = simulate_monthly(scenario(stochastic=True))
    assert zero["intervention"] == zero["control"]
    assert zero["fleet"]["intervention"] == zero["fleet"]["control"]


def test_capacity_loss_profit_sign_depends_on_unfitted_cost_flexibility():
    # A disclosed structural sensitivity, not a validated economic benefit.
    s = scenario(aircraft_unavailable_pct=40, availability_group="widebody", response_mode="frozen")
    flexible = simulate_monthly(s)
    sticky = simulate_monthly(replace(s, nonfuel_fixed_fraction=.95))
    assert flexible["summary"]["cumulative_fcf_impact_musd"]["p50"] > 0
    assert sticky["summary"]["cumulative_fcf_impact_musd"]["p50"] < 0
    assert flexible["fleet"] == sticky["fleet"]
