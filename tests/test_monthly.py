from copy import deepcopy
from dataclasses import replace
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.evidence import EvidenceRecord, available_records, build_warehouse, digest, load_snapshot, query_as_of
from app.monthly import MonthlyScenario, _rollout, _world, baseline, month_sequence, shock_weights, simulate_monthly
from app.server import app


def settings(**kw):
    return MonthlyScenario(**{"paths": 40, **kw})


def test_ledger_respects_publication_and_review_status():
    snapshot = load_snapshot()
    # Release and filing have different actual publication dates.
    early = available_records(snapshot, "2026-07-15")
    assert early and all(r["source_id"] != "ual_q2_2026_10q" for r in early)
    assert all(r["published_on"] <= "2026-07-15" for r in early)
    assert any(r["evidence_type"] == "guidance" for r in available_records(snapshot, "2026-07-16"))
    changed = deepcopy(snapshot)
    changed["records"][0]["review_status"] = "pending"
    assert changed["records"][0] not in available_records(changed, "2026-10-09")
    with pytest.raises(ValueError, match="No verified"):
        baseline(snapshot, "2026-07-15")


def test_evidence_validates_units_dates_and_provenance():
    record = EvidenceRecord(**load_snapshot()["records"][0])
    record.validate()
    for patch in ({"units": "made_up"}, {"value": float("nan")}, {"source_passage": ""}, {"effective_end": "2025-01-01"}):
        with pytest.raises(ValueError):
            replace(record, **patch).validate()


def test_parquet_roundtrip_and_immutable_snapshot(tmp_path):
    snapshot = load_snapshot()
    built = build_warehouse(snapshot, tmp_path)
    target = tmp_path / built["snapshot_id"]
    assert query_as_of(target / "evidence.parquet", "2026-07-15") == sorted(available_records(snapshot, "2026-07-15"), key=lambda r: r["record_id"])
    original_manifest = (target / "manifest.json").read_bytes()
    assert build_warehouse(snapshot, tmp_path)["snapshot_id"] == built["snapshot_id"]
    assert (target / "manifest.json").read_bytes() == original_manifest
    (target / "evidence.json").write_text("[]")
    with pytest.raises(ValueError, match="modified"):
        build_warehouse(snapshot, tmp_path)


def test_reported_market_and_cash_flow_anchors_reconcile():
    b = baseline(load_snapshot(), "2026-07-16")
    assert sum(m["asm_million"] for m in b["markets"]) * 3 == pytest.approx(87279)
    assert sum(m["rpm_million"] for m in b["markets"]) * 3 == pytest.approx(72765)
    assert sum(m["passenger_revenue_musd"] for m in b["markets"]) * 3 == pytest.approx(16100)
    assert b["operating_revenue_musd"] * 3 == pytest.approx(17672)
    assert b["operating_expense_musd"] * 3 == pytest.approx(16576)
    assert b["operating_income_musd"] * 3 == pytest.approx(1096)
    assert b["operating_cash_flow_musd"] * 3 == pytest.approx(6409 - 4799)
    assert b["cash_capex_musd"] * 3 == pytest.approx(3015 - 1672)
    assert b["cash_flow_derivation"]["operating_cash_flow"]["method"] == "H1 minus Q1"
    assert b["cash_musd"] == 10166
    assert b["short_term_investments_musd"] == 6471


def test_q2_cash_derivation_rejects_missing_or_mismatched_prior_periods():
    snap = load_snapshot()
    q1 = next(r for r in snap["records"] if r["variable"] == "operating_cash_flow_q1")
    q1["review_status"] = "pending"
    with pytest.raises(ValueError, match="No verified"):
        baseline(snap, "2026-07-16")
    q1["review_status"] = "verified"
    q1["effective_end"] = "2025-03-31"
    with pytest.raises(ValueError, match="matching H1 and Q1"):
        baseline(snap, "2026-07-16")


def test_zero_intervention_matches_paired_control_even_with_world_noise():
    result = simulate_monthly(settings(fuel_shock_pct=0))
    assert all(x == {"p10": 0, "p50": 0, "p90": 0} for x in result["cumulative_fcf_impact_musd"])
    assert result["intervention"] == result["control"]


def test_raw_accounting_and_operating_feasibility():
    s = settings(fuel_shock_pct=80, capacity_shock_pct=-70, demand_shock_pct=100)
    b = baseline(load_snapshot(), s.information_cutoff)
    fuel, demand = _world(s, 4, .2)
    r = _rollout(b, s, fuel, demand, 4, True, s.response_mode)
    np.testing.assert_allclose(r["revenue_musd"] - r["fuel_expense_musd"] - r["nonfuel_expense_musd"], r["operating_income_musd"])
    np.testing.assert_allclose(r["operating_cash_flow_musd"] - r["cash_capex_musd"], r["free_cash_flow_musd"])
    np.testing.assert_allclose(b["cash_musd"] + r["free_cash_flow_musd"].cumsum(axis=1), r["cash_musd"])
    assert (r["rpm_million"] <= r["asm_million"]).all()
    assert (r["market_load_factor"] >= 0).all() and (r["market_load_factor"] <= 1).all()
    assert (r["market_yield_change_pct"][:, :s.response_lag_months] == 0).all()
    assert np.abs(np.diff(r["market_yield_change_pct"], axis=1)).max() <= s.max_yield_step_pct + 1e-10


def test_no_noise_reference_reproduces_reported_operating_run_rate():
    s = settings(fuel_shock_pct=0, stochastic=False)
    result = simulate_monthly(s)
    assert result["intervention"]["monthly"]["revenue_musd"][0]["p50"] == pytest.approx(17672 / 3, abs=1e-4)
    assert result["intervention"]["monthly"]["operating_income_musd"][0]["p50"] == pytest.approx(1096 / 3, abs=1e-4)
    assert result["intervention"]["monthly"]["free_cash_flow_musd"][0]["p50"] == pytest.approx(((6409 - 4799) - (3015 - 1672)) / 3, abs=1e-4)


def test_policy_comparison_and_market_specific_intervention():
    assumed = simulate_monthly(settings(stochastic=False, geography="pacific"))
    frozen = simulate_monthly(settings(stochastic=False, geography="pacific", response_mode="frozen"))
    assert assumed["summary"]["cumulative_fcf_impact_musd"]["p50"] > frozen["summary"]["cumulative_fcf_impact_musd"]["p50"]
    for market in ("domestic", "atlantic", "latin_america"):
        assert assumed["intervention"]["markets"][market] == assumed["control"]["markets"][market]


def test_common_paths_are_reproducible_and_future_fuel_is_excluded():
    from app.data import load_fuel
    s = settings()
    a = simulate_monthly(s)
    fuel = load_fuel()
    b = simulate_monthly(s, fuel=fuel + [{"date": "2099-01-01", "usd_per_gallon": 15}])
    assert a["manifest"] == b["manifest"]
    assert a["intervention"] == b["intervention"]
    assert a["manifest"]["fuel_last_observation"] <= s.information_cutoff
    assert a["cumulative_fcf_impact_musd"][-1] == a["summary"]["cumulative_fcf_impact_musd"]


def test_months_quarters_and_onset_duration_recovery():
    assert month_sequence("2026-12-09", 3) == ["2027-01", "2027-02", "2027-03"]
    s = settings(onset_month=3, duration_months=2, recovery_half_life_months=1)
    assert shock_weights(s)[:6] == pytest.approx([0, 0, 1, 1, .5, .25])
    r = simulate_monthly(s)
    assert r["months"][0] == "2026-08"
    quarters = r["intervention"]["quarterly"]
    assert quarters[0]["complete_quarter"] is False
    assert quarters[1]["complete_quarter"] is True
    assert sum(len(q["months_included"]) for q in quarters) == s.horizon_months


def test_monthly_api_rejects_unimplemented_modes_and_invalid_cutoffs():
    client = TestClient(app)
    assert client.get("/monthly").status_code == 200
    assert client.get("/api/monthly/bootstrap").status_code == 200
    assert client.post("/api/monthly/simulate", json={"paths": 20, "horizon_months": 3}).status_code == 200
    for body in ({"response_mode": "fitted"}, {"information_cutoff": "2026-07-14"}, {"information_cutoff": "bad"}, {"competitor_capacity_pct": 9}):
        assert client.post("/api/monthly/simulate", json=body).status_code == 422


def test_parameter_validation_rejects_nan_and_fractional_path_counts():
    for kw in ({"paths": 20.5}, {"demand_shock_pct": float("nan")}, {"fuel_demand_correlation": 1}, {"geography": "moon"}):
        with pytest.raises(ValueError):
            settings(**kw).validate()


def test_preserved_input_replay_and_tamper_detection(tmp_path):
    from scripts.run_monthly import replay
    saved = simulate_monthly(settings(horizon_months=3))
    target = tmp_path / "run.json"
    target.write_text(json.dumps(saved))
    assert replay(target)["manifest"] == saved["manifest"]
    saved["replay_inputs"]["fuel_observations"][-1]["usd_per_gallon"] += .1
    target.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="differ"):
        replay(target)


def test_browser_number_roundtrip_preserves_manifest_and_replay(tmp_path):
    from scripts.run_monthly import replay
    saved = TestClient(app).post("/api/monthly/simulate", json={"paths": 20, "horizon_months": 3, "fuel_shock_pct": 0, "aircraft_unavailable_pct": 40, "nonfuel_fixed_fraction": .95}).json()
    # JSON.stringify parses all numeric values as JavaScript Numbers and emits
    # integral values without a decimal, even when the server sent 0.0/1.0.
    def browser_numbers(item):
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, dict):
            return {k: browser_numbers(v) for k, v in item.items()}
        if isinstance(item, list):
            return [browser_numbers(v) for v in item]
        return item
    target = tmp_path / "browser.json"
    target.write_text(json.dumps(browser_numbers(saved)))
    assert replay(target)["manifest"] == saved["manifest"]
    assert digest({"a": [0., 1., 4.125]}) == digest({"a": [0, 1, 4.125]})
    assert digest({"a": 4.125}) != digest({"a": 4.126})
