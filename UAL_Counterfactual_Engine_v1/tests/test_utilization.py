from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest

from app.utilization import make_panel, fit, forecast, select, shift, SERIES, _history
from app.utilization import build_utilization, utilization_status
from app.evidence import digest


def row(period="2013-Q1", region="D", aircraft="612", hours=100., days=10., config="1"):
    return {"schedule": "P52", "period": period, "unique_carrier": "UA", "airline_id": 19977,
        "reporting_entity": "domestic" if region == "D" else "other", "reporting_region": region,
        "aircraft_type": aircraft, "aircraft_config": config, "aircraft_group": "6",
        "values": {"TOTAL_AIR_HOURS": hours, "AIR_DAYS_ASSIGN": days}, "quality_flags": [], "source_line": 2}


def classification():
    return {"rows": [{"aircraft_type": "612", "fleet_group": "narrowbody"}, {"aircraft_type": "626", "fleet_group": "widebody"}]}


def synthetic():
    observations = []
    for i, (carrier, group) in enumerate(SERIES):
        state = {}
        for t in range(44):
            period = shift("2013-Q1", t)
            if t < 4:
                value = 8. + i + (t == 2)
            else:
                value = .65 * state[shift(period, -1)] + .15 * state[shift(period, -4)] + 1.2 + i * .2 + (int(period[-1]) == 3) * .3
            if "2020-Q2" <= period <= "2021-Q1":
                value *= .5
            state[period] = value
            observations.append({"unique_carrier": carrier, "fleet_group": group, "period": period, "utilization_hpd": value})
    return observations


def specification():
    return json.loads((Path(__file__).parents[1] / "data/utilization_spec.json").read_text())


def test_same_schedule_ratio_aggregates_regions_and_excludes_equipment_totals():
    rows = [row(hours=100, days=10), row(region="A", hours=50, days=10),
        row(region="S", hours=9000, days=1), row(aircraft="999", config="9", hours=9999, days=1)]
    result = make_panel(rows, classification())
    observed = result["observations"][0]
    assert observed["hours"] == 150 and observed["aircraft_days"] == 20
    assert observed["utilization_hpd"] == 7.5
    assert len(observed["source_rows"]) == 2
    assert observed["rounding_sensitivity_hpd"] == pytest.approx([140/30, 160/10])
    assert not result["excluded_group_quarters"]


def test_missing_or_negative_quantities_withhold_entire_group_quarter():
    for hours, days, flag in ((None, 10, "missing_reported_hours_or_days"), (100, None, "missing_reported_hours_or_days"),
                             (-1, 10, "invalid_reported_hours_or_days"), (250, 1, "group_airborne_hours_exceed_24_per_day"),
                             (10, 0, "nonpositive_group_aircraft_days")):
        result = make_panel([row(hours=hours, days=days)], classification())
        assert not result["observations"]
        assert flag in result["excluded_group_quarters"][0]["quality_flags"]


def test_unmapped_type_and_certificate_conflict_cannot_silently_shrink_fleet():
    rows = [row(), row(aircraft="9999")]
    result = make_panel(rows, classification())
    assert not result["observations"] and result["unmapped_certificate_quarters"]
    rows = [row(), {**row(region="A"), "airline_id": 1234}]
    assert "ambiguous_certificate_identity" in make_panel(rows, classification())["excluded_group_quarters"][0]["quality_flags"]


def test_locked_observations_are_ignored_before_reading_target_fields():
    rows = [row(), {"period": "2024-Q1", "unique_carrier": "UA"}]
    assert len(make_panel(rows, classification())["observations"]) == 1
    with pytest.raises(ValueError, match="locked"):
        make_panel(rows, classification(), last="2024-Q1")
    with pytest.raises(ValueError, match="Duplicate"):
        make_panel([row(), row()], classification())


def test_known_dynamic_system_recovers_predictions_on_training_period():
    training = [r for r in synthetic() if r["period"] <= "2019-Q4"]
    model = fit(training, 1e-8)
    assert model["training_residual_rmse_hpd"] < 1e-6
    assert all(n == 24 for n in model["training_series_counts"].values())
    prediction = forecast(model, _history(training), "UA", "narrowbody", "2019-Q3", 1)[0]
    expected = next(r["utilization_hpd"] for r in training if r["unique_carrier"] == "UA" and r["fleet_group"] == "narrowbody" and r["period"] == "2019-Q4")
    assert prediction == pytest.approx(expected, abs=1e-5)


def test_recursive_forecast_never_uses_intermediate_future_observations():
    observations = synthetic(); model = fit([r for r in observations if r["period"] <= "2019-Q4"], 1)
    history = _history(observations)
    original = forecast(model, history, "UA", "narrowbody", "2019-Q4", 4)
    poisoned = {k: 24. if k[2] > "2019-Q4" else v for k, v in history.items()}
    assert forecast(model, poisoned, "UA", "narrowbody", "2019-Q4", 4) == original
    del poisoned[("UA", "narrowbody", "2019-Q1")]
    assert forecast(model, poisoned, "UA", "narrowbody", "2019-Q4", 4) is None


def test_selection_scores_common_targets_and_training_coefficients_stay_frozen():
    observations = synthetic(); spec = specification(); result = select(observations, spec)
    selected = result["selected_model"]
    training = [r for r in observations if r["period"] <= "2019-Q4"]
    assert selected == fit(training, selected["ridge_penalty"])
    assert {r["prediction_count"] for r in result["scorecard"]} == {264}
    assert all(len(r["series_horizon_scores"]) == 18 for r in result["scorecard"])
    scores = next(r for r in result["scorecard"] if r["model"] == "seasonal_persistence")
    cell = next(r for r in scores["series_horizon_scores"] if r["carrier"] == "UA" and r["group"] == "narrowbody" and r["horizon_quarters"] == 1)
    # Independent metric calculation from the observed target/reference values.
    h = _history(observations)
    error = [h[("UA", "narrowbody", shift("2020-Q1", i-4))] - h[("UA", "narrowbody", shift("2020-Q1", i))] for i in range(16)]
    assert cell["MAE_hpd"] == pytest.approx(sum(abs(e) for e in error)/16)
    assert all(r["target"] <= "2023-Q4" and r["origin"] >= "2019-Q4" for r in result["selection_predictions"])


def test_training_and_selection_reject_future_targets_and_small_samples():
    observations = synthetic()
    with pytest.raises(ValueError, match="cannot enter"):
        fit(observations, 1)
    with pytest.raises(ValueError, match="locked"):
        select(observations + [{**observations[-1], "period": "2024-Q1"}], specification())
    with pytest.raises(ValueError, match="eight"):
        fit(observations[:3], 1)
    bad = deepcopy(specification()); bad["selection_last"] = "2024-Q1"
    with pytest.raises(ValueError, match="frozen"):
        select(observations, bad)


def test_missing_lag_excludes_same_target_from_all_candidates_and_baselines():
    observations = [r for r in synthetic() if not (r["unique_carrier"] == "UA" and r["fleet_group"] == "narrowbody" and r["period"] == "2020-Q3")]
    result = select(observations, specification())
    assert result["excluded_forecasts"]
    assert len({r["prediction_count"] for r in result["scorecard"]}) == 1
    for r in result["excluded_forecasts"]:
        assert not any(p["carrier"] == r["carrier"] and p["group"] == r["group"] and p["origin"] == r["origin"] and p["horizon_quarters"] == r["horizon_quarters"] for p in result["selection_predictions"])


def test_estimate_rebuild_status_and_tamper_checks(tmp_path, monkeypatch):
    import app.utilization as module
    rows = []
    for observation in synthetic():
        source = row(period=observation["period"], aircraft="612" if observation["fleet_group"] == "narrowbody" else "626", hours=observation["utilization_hpd"]*1000, days=1000)
        source["unique_carrier"] = observation["unique_carrier"]
        rows.append(source)
    panels = {"P12": {"rows": []}, "P52": {"rows": rows}, "P12A": {"rows": []}}
    manifest = {"panel_digests": {s: digest(p) for s, p in panels.items()}}
    manifest["snapshot_id"] = digest(manifest)
    upstream = tmp_path / "source"; upstream.mkdir()
    (upstream / "manifest.json").write_text(json.dumps(manifest))
    for schedule, panel in panels.items():
        (upstream / f"{schedule}.json").write_text(json.dumps(panel))
    classes = classification()
    for item, description in zip(classes["rows"], ["Boeing 737-700/700LR/Max 7", "Boeing 767-300/300ER"]):
        item["description"] = description
    classes.update({"type_lookup_sha256": "types", "configuration_lookup_sha256": "config", "group_lookup_sha256": "groups"})
    lookup = b'Code,Description\n612,Boeing 737-700/700LR/Max 7\n626,Boeing 767-300/300ER\n'
    monkeypatch.setattr(module, "read_source", lambda sha: (lookup, {"retrieved_at_utc": "2026-10-09T20:00:00+00:00"}))
    classpath = tmp_path / "class.json"; classpath.write_text(json.dumps(classes))
    specpath = tmp_path / "spec.json"; specpath.write_text(json.dumps(specification()))
    destination = tmp_path / "estimated"
    first = build_utilization(upstream, classpath, specpath, destination)
    assert build_utilization(upstream, classpath, specpath, destination)["snapshot_id"] == first["snapshot_id"]
    status = utilization_status(destination)
    assert status["snapshot_id"] == first["snapshot_id"]
    assert status["training_series_counts"]["UA:widebody"] == 24
    assert status["selection_predictions_per_model"] == 264
    assert status["minimum_selection_cell_targets"] == 13
    assert status["excluded_disruption_group_quarters"] == 0
    frozen = destination / "designs" / first["design_id"] / "design.json"
    declaration = json.loads(frozen.read_text())
    assert declaration["design"]["specification"] == specification()
    assert declaration["design"]["code_sha256"] == first["code_sha256"]
    artifact = Path(first["path"]) / "artifact.json"
    original = artifact.read_text(); artifact.write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        utilization_status(destination)
    with pytest.raises(ValueError, match="Immutable"):
        build_utilization(upstream, classpath, specpath, destination)
    artifact.write_text(original)
    declaration["design"]["numpy_version"] = "tampered"; frozen.write_text(json.dumps(declaration))
    with pytest.raises(ValueError, match="design checksum"):
        utilization_status(destination)


def test_cached_api_returns_unbuilt_or_verification_failure(monkeypatch):
    from fastapi.testclient import TestClient
    import app.server as server
    client = TestClient(server.app)
    monkeypatch.setattr(server, "utilization_status", lambda: {"status": "not_built", "message": "Not estimated"})
    assert client.get("/api/utilization/status").json()["status"] == "not_built"
    def corrupt():
        raise ValueError("Checksum failure")
    monkeypatch.setattr(server, "utilization_status", corrupt)
    response = client.get("/api/utilization/status")
    assert response.status_code == 503 and "failed verification" in response.json()["detail"]
