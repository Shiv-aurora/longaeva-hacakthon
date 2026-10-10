from copy import deepcopy
import json
import math
from pathlib import Path

import numpy as np
import pytest

from app.evidence import digest
from app.monthly_traffic import (SERIES, FEATURES, observations, history, month_end, periods, shift,
    kalman_step, process_covariance, regress, filter_state, fit_state, prediction, ar_predict,
    select, build_traffic, traffic_status)


def spec():
    return json.loads((Path(__file__).parents[1] / "data/monthly_traffic_spec.json").read_text())


def source_rows():
    result = []
    for i, (c, m) in enumerate(SERIES):
        for n, p in enumerate(periods("2013-01", "2023-12")):
            rpm = math.exp(6 + i / 12 + .003 * n + .08 * math.sin(2 * math.pi * int(p[5:]) / 12))
            # Preserve delayed annual source releases, including initial June 2020.
            published = f"{int(p[:4]) + 1}-06-02"
            result.append({"carrier": c, "market": m, "period": p, "published_on": published,
                "rpm": rpm * 1e6, "asm": rpm * 1.3e6, "quality_flags": [], "both_archive_scopes_available": True,
                "source_sha256s": ["a" * 64], "carrier_entities": [f"{i:05d}"]})
    return result


@pytest.fixture(scope="module")
def study():
    rows = observations(source_rows())["observations"]
    return rows, select(rows, spec())


def test_months_and_completed_release_gate():
    assert shift("2019-12", 1) == "2020-01"
    assert shift("2020-01", -12) == "2019-01"
    assert month_end("2020-02") == "2020-02-29"
    row = source_rows()[0]
    row["published_on"] = "2013-01-02"
    with pytest.raises(ValueError, match="precedes"):
        observations([row])


def test_locked_target_and_unreleased_quantity_never_accessed():
    assert observations([{"period": "2024-01", "carrier": "UA"}])["observations"] == []
    assert history([{"period": "2023-01", "published_on": "2024-06-02"}], "2023-12-31") == {}
    assert history([{"period": "2024-01"}], "2023-12-31") == {}


@pytest.mark.parametrize("patch", [{"rpm": -1}, {"asm": 1}, {"rpm": True}, {"rpm": float("nan")}])
def test_impossible_or_nonfinite_traffic_is_rejected(patch):
    row = {**source_rows()[0], **patch}
    with pytest.raises(ValueError, match="identity"):
        observations([row])


def test_zero_unknown_and_duplicate_are_distinct():
    row = source_rows()[0]
    assert observations([{**row, "rpm": 0}])["excluded_rows"][0]["reason"].startswith("nonpositive")
    assert not observations([{**row, "both_archive_scopes_available": False}])["observations"]
    with pytest.raises(ValueError, match="Duplicate"):
        observations([row, row])


def test_noncontributing_scope_must_be_released_before_complete_market_is_known():
    row = {**source_rows()[0], "published_on": "2014-03-21"}
    releases = [{"scope": scope, "period": "2013-01", "published_on": published, "source_sha256": "a" * 64}
        for scope, published in (("DD", "2014-03-21"), ("FD", "2014-06-04"))]
    prepared = observations([row], releases)["observations"]
    assert prepared[0]["contributor_published_on"] == "2014-03-21"
    assert prepared[0]["published_on"] == "2014-06-04"
    assert history(prepared, "2014-04-30") == {}
    assert len(history(prepared, "2014-06-30")) == 1
    assert not observations([row], releases[:1])["observations"]


def test_scalar_independent_kalman_update_and_missing_step():
    mean = np.zeros(12); covariance = np.eye(12) * .4
    q = process_covariance(.2, 0)
    actual, post, ll = kalman_step(mean, covariance, q, {0: 2.}, .3)
    # Independent scalar oracle: prior=.6, S=.9, K=2/3.
    assert actual[0] == pytest.approx(4 / 3)
    assert post[0, 0] == pytest.approx(.2)
    assert np.allclose(post.diagonal()[1:], .6)
    assert ll == pytest.approx(-.5 * (math.log(2 * math.pi * .9) + 4 / .9))
    missing, missing_cov, missing_ll = kalman_step(actual, post, q, {}, .3)
    assert np.array_equal(missing, actual)
    assert np.allclose(missing_cov, post + q)
    assert missing_ll == 0


def test_pooling_updates_missing_peer_in_same_market_only():
    mean, covariance, _ = kalman_step(np.zeros(12), np.eye(12), process_covariance(.1, .9), {0: 1.}, .01)
    assert mean[4] > 0 and mean[8] > 0  # UA/DL/AA domestic indices.
    assert mean[1] == 0
    assert np.allclose(covariance, covariance.T)
    assert np.linalg.eigvalsh(covariance).min() > 0


def test_synthetic_process_and_measurement_variance_recovery():
    rng = np.random.default_rng(104)
    q = process_covariance(.01, .5)
    latent = rng.normal(size=12)
    known = {}
    for p in periods("2013-01", "2023-12"):
        latent += rng.multivariate_normal(np.zeros(12), q)
        measured = 6 + latent + rng.normal(0, math.sqrt(.001), size=12)
        for i, (c, m) in enumerate(SERIES):
            known[c, m, p] = {"rpm_million": math.exp(measured[i])}
    seasonal = {"coefficients": [6.] * 12 + [0.] * (len(FEATURES) - 12)}
    fitted = fit_state(known, seasonal, .5, spec(), "2023-12")
    assert fitted["q"] == .01 and fitted["r"] == .001
    assert fitted["innovation_loglikelihood"] == max(x["loglikelihood"] for x in fitted["grid_likelihoods"])


def test_predictive_variance_includes_unobserved_release_gap():
    rows = observations(source_rows())["observations"]
    known = history(rows, "2020-06-30")
    seasonal = regress(known, 1)
    a, p, _ = filter_state(known, seasonal, .001, .001, .5, "2019-12")
    b, future, _ = filter_state(known, seasonal, .001, .001, .5, "2020-06")
    assert np.array_equal(a, b)
    assert np.allclose(future, p + 6 * process_covariance(.001, .5))
    state = {"mean": b.tolist(), "covariance": future.tolist(), "q": .001, "r": .001}
    d = prediction(state, seasonal, "UA", "domestic", "2020-07", 1)
    assert 0 < d["p10"] < d["p50"] < d["p90"]
    assert d["log_variance"] == pytest.approx(future[0, 0] + .002)


def test_regularized_forecast_is_recursive_and_future_isolated():
    known = history(observations(source_rows())["observations"], "2020-06-30")
    fitted = regress(known, 10, True)
    first = ar_predict(fitted, known, "UA", "domestic", "2021-06")
    # Caller uses release-aware history; post-origin targets never enter it.
    later = {"period": "2021-06", "published_on": "2022-06-02", "carrier": "UA", "market": "domestic", "rpm_million": float("nan")}
    assert history([*known.values(), later], "2020-06-30") == known
    assert ar_predict(fitted, known, "UA", "domestic", "2021-06") == first
    assert all(k[2] <= "2019-12" for k in fitted["training_keys"])


def test_real_time_boundary_expanding_fits_and_common_scores(study):
    rows, result = study
    assert result["first_initial_release"] == "2020-06-02"
    assert result["fits"][0]["origin"] == "2020-06"
    assert len({s["prediction_count"] for s in result["scorecard"]}) == 1
    assert all(len(s["series_horizon_scores"]) == 48 for s in result["scorecard"])
    assert result["fits"][-1]["seasonal"]["target_count"] > result["fits"][0]["seasonal"]["target_count"]
    released = {(r["carrier"], r["market"], r["period"]): r for r in rows}
    for fit in result["fits"]:
        assert fit["max_known_publication"] <= fit["cutoff"]
        assert all(released[tuple(k)]["published_on"] <= fit["cutoff"] for k in fit["known_keys"])
    assert all(f["target"] <= "2023-12" and f["persistence_published_on"] <= f["cutoff"] for f in result["forecasts"])


def test_future_targets_cannot_change_earlier_fit_or_forecast(study):
    rows, baseline = study
    altered = deepcopy(rows)
    for r in altered:
        if r["period"] >= "2022-01":
            r["rpm_million"] *= 10
    changed = select(altered, spec())
    original = next(f for f in baseline["fits"] if f["origin"] == "2021-12")
    assert next(f for f in changed["fits"] if f["origin"] == "2021-12") == original
    old = [f["predictions"] for f in baseline["forecasts"] if f["origin"] == "2021-12"]
    new = [f["predictions"] for f in changed["forecasts"] if f["origin"] == "2021-12"]
    assert old == new


def test_immutable_build_parquet_readback_and_corruption_gates(tmp_path):
    upstream = tmp_path / "upstream"; upstream.mkdir()
    panel = {"rows": source_rows(), "selected_releases": [{"scope": scope, "period": p,
        "published_on": f"{int(p[:4]) + 1}-06-02", "source_sha256": "a" * 64}
        for p in periods("2013-01", "2023-12") for scope in ("DD", "FD")]}
    manifest = {"version": "source-fixture", "panel_digest": digest(panel)}
    manifest["snapshot_id"] = digest(manifest)
    (upstream / "manifest.json").write_text(json.dumps(manifest))
    (upstream / "panel.json").write_text(json.dumps(panel))
    design = {**spec(), "source_snapshot_id": manifest["snapshot_id"]}
    specpath = tmp_path / "design.json"; specpath.write_text(json.dumps(design))
    root = tmp_path / "result"
    built = build_traffic(upstream, specpath, root)
    assert traffic_status(root)["status"] == "available"
    assert build_traffic(upstream, specpath, root)["snapshot_id"] == built["snapshot_id"]
    target = Path(built["path"])
    artifact = target / "artifact.json"; saved = artifact.read_bytes(); artifact.write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        traffic_status(root)
    artifact.write_bytes(saved)
    (target / "observations.json").write_text('{"rows": []}')
    with pytest.raises(ValueError, match="row JSON"):
        traffic_status(root)


def test_audit_keeps_empty_country_field_separate_from_trailing_delimiter():
    from scripts.audit_monthly_traffic import raw_fields
    fields = ["1"] * 27 + [""]
    assert raw_fields("|".join(fields)) == fields
    assert raw_fields("|".join(fields) + "|") == fields


def test_independent_audit_rejects_rehashed_regression_and_variance_tamper(study):
    from scripts.audit_monthly_traffic import audit
    rows, fitted = study
    artifact = {"specification": spec(), "panel": {"observations": rows}, "estimation": fitted}
    assert audit(artifact)["checks_passed"] > 10000
    for key in ("regression", "variance"):
        altered = deepcopy(artifact)
        if key == "regression":
            altered["estimation"]["fits"][0]["seasonal"]["coefficients"][0] += 1
        else:
            altered["estimation"]["fits"][0]["states"][0]["grid_likelihoods"][0]["loglikelihood"] += 1
        with pytest.raises(ValueError, match="Independent"):
            audit(altered)


def test_crps_diagnostic_matches_independent_cdf_integral():
    from scripts.audit_monthly_traffic import interval_diagnostics
    mu, sigma, y = .3, .6, 1.8
    nodes, weights = np.polynomial.legendre.leggauss(100)
    def integrate(a, b, upper):
        z = (nodes + 1) * (b - a) / 2 + a
        cdf = np.array([(1 + math.erf(float(v) / math.sqrt(2))) / 2 for v in z])
        integrand = (cdf - float(upper)) ** 2 * sigma * np.exp(mu + sigma * z)
        return float(np.dot(weights, integrand) * (b - a) / 2)
    pivot = (math.log(y) - mu) / sigma
    expected = integrate(-12, pivot, False) + integrate(pivot, 12, True)
    d = {"p10": math.exp(mu - 1.2815515655446004 * sigma), "p90": math.exp(mu + 1.2815515655446004 * sigma), "log_mean": mu, "log_variance": sigma ** 2}
    result = interval_diagnostics([{"carrier": "UA", "market": "domestic", "horizon_months": 1, "regime": "recovery", "actual_rpm_million": y, "distributions": {"model": d}}], "model")
    assert result["cells"][0]["CRPS_rpm_million"] == pytest.approx(expected, abs=1e-8)


def test_api_unbuilt_and_corrupt_study_gates(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.server as server
    monkeypatch.setattr(server, "traffic_status", lambda: traffic_status(tmp_path))
    client = TestClient(server.app)
    assert client.get("/api/traffic/status").json()["status"] == "not_built"
    assert client.get("/api/traffic/record").status_code == 404
    (tmp_path / "latest.json").write_text('{"snapshot_id": "bad"}')
    assert client.get("/api/traffic/status").status_code == 503
    assert client.get("/api/traffic/record").status_code == 503
