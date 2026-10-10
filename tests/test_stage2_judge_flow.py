from pathlib import Path
from fastapi.testclient import TestClient
from app.server import app

ROOT = Path(__file__).resolve().parents[1]
client = TestClient(app)

def test_the_landing_page_is_the_lab_dashboard():
    landing = client.get("/")
    assert landing.status_code == 200
    assert landing.content == client.get("/lab").content
    assert "lab.js" in landing.text

def test_judge_preset_runs_the_actual_model_and_exposes_scope():
    r = client.post("/api/monthly/simulate", json={
        "information_cutoff": "2026-07-16",
        "fuel_shock_pct": 20,
        "demand_shock_pct": 0,
        "capacity_shock_pct": 0,
        "geography": "all",
        "horizon_months": 12,
        "onset_month": 1,
        "duration_months": 9,
        "delivery_delay_months": 9,
        "delivery_aircraft_count": 30,
        "delivery_group": "all",
        "response_lag_months": 2,
        "response_mode": "assumed_constrained",
        "paths": 20
    })
    assert r.status_code == 200, r.text[:1000]
    data = r.json()
    assert data["scenario"]["delivery_aircraft_count"] == 30
    assert data["scenario"]["response_lag_months"] == 2
    assert len(data["months"]) == 12
    assert data["manifest"]["run_id"]
    for key in [
        "cumulative_operating_income_impact_musd",
        "cumulative_fcf_impact_musd",
        "response_contribution_to_fcf_musd"
    ]:
        dist = data["summary"][key]
        assert all(q in dist for q in ("p10", "p50", "p90"))
        assert dist["p10"] <= dist["p50"] <= dist["p90"]
    assert data["assumptions"], "The example must disclose modeling assumptions"
