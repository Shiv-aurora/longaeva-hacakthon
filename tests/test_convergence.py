from copy import deepcopy
from dataclasses import replace
import json
import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.convergence import (ConvergenceSpec, METRICS, POLICIES, Z95,
    level_diagnostics, path_outcomes, replica_seeds, study_convergence, summarize_replica, wilson_interval)
from app.data import fuel_volatility, load_fuel
from app.evidence import load_snapshot
from app.fleet import capacity_paths
from app.model_archive import capture_source, deterministic, digest, replay_saved, run_preserved
from app.monthly import MonthlyScenario, _world, baseline, month_sequence, shock_weights, simulate_monthly
from app.server import app


def spec(**kw):
    return ConvergenceSpec(**{"path_counts": (20, 40, 80), "replicates": 8, **kw})


def paths(s):
    b = baseline(load_snapshot(), s.information_cutoff, s.cash_plan, s.purchase_plan)
    fuel = [r for r in load_fuel() if r["date"] <= s.information_cutoff]
    fleet = capacity_paths(b, s, b["fleet_plan"], month_sequence(s.information_cutoff, s.horizon_months), shock_weights(s))
    return path_outcomes(s, b, fuel[-1]["usd_per_gallon"], fuel_volatility(fuel) * math.sqrt(21), fleet)


@pytest.mark.parametrize("mode", POLICIES)
def test_path_diagnostics_match_production_with_financing_retention_and_partial_horizons(mode):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    s = MonthlyScenario(paths=40, horizon_months=7, response_mode=mode, delivery_aircraft_count=30,
        delivery_group="narrowbody", delivery_delay_months=9,
        purchase_plan=json.loads((root / "data/purchase_plan_example.json").read_text()),
        retention_plan=json.loads((root / "data/retention_plan_example.json").read_text()),
        cash_plan={"basis": "test financing and working capital", "events": [
            {"month": "2026-08", "kind": "revolver_draw", "amount_musd": 800, "scope": "intervention"},
            {"month": "2026-09", "kind": "advance_sales_change", "amount_musd": -100, "scope": "intervention"}]})
    diagnostic = summarize_replica(paths(s), s.cash_floor_musd, s.seed, s.paths)
    production = simulate_monthly(s)
    assert list(diagnostic["policies"][mode]) == ["1", "3", "6", "7"]
    for metric in ("cumulative_fcf_musd", "cumulative_fcf_impact_musd", "ending_cash_musd",
                   "cumulative_operating_income_impact_musd", "response_contribution_to_fcf_musd"):
        assert {q: round(x, 4) for q, x in diagnostic["policies"][mode]["7"][metric].items()} == production["summary"][metric]
    assert diagnostic["policies"][mode]["7"]["cash_floor_breach"]["fraction"] == production["summary"]["cash_floor_breach_path_fraction"]
    for h in (1, 3, 6, 7):
        assert {q: round(x, 4) for q, x in diagnostic["policies"][mode][str(h)]["cumulative_fcf_impact_musd"].items()} == production["cumulative_fcf_impact_musd"][h - 1]


def test_replica_streams_are_distinct_reproducible_and_nested():
    seeds = replica_seeds(2026, 24)
    assert seeds[0] == 2026 and len(set(seeds)) == 24
    assert seeds == replica_seeds(2026, 24)
    assert seeds[1:] != replica_seeds(2027, 24)[1:]
    s = MonthlyScenario(paths=20, horizon_months=6)
    for seed in seeds[:3]:
        small = _world(replace(s, seed=seed), 4., .2)
        big = _world(replace(s, seed=seed, paths=80), 4., .2)
        for a, b in zip(small, big):
            np.testing.assert_array_equal(a, b[:20])
        a, b = paths(replace(s, seed=seed)), paths(replace(s, seed=seed, paths=80))
        for mode in POLICIES:
            for h in a[mode]:
                for metric in METRICS:
                    np.testing.assert_array_equal(a[mode][h][metric], b[mode][h][metric][:20])


def test_replica_error_is_single_run_sd_and_zero_breach_bound_is_nonzero():
    outcomes = {mode: {"1": {metric: np.arange(20, dtype=float) for metric in METRICS}} for mode in POLICIES}
    replicas = [summarize_replica(outcomes, -100, i, 20) for i in range(8)]
    for i, row in enumerate(replicas):
        row["policies"]["frozen"]["1"]["cumulative_fcf_musd"]["p50"] = i * 10.
    checks = level_diagnostics(replicas, replicas, spec(), True)
    value = next(r for r in checks if r["policy"] == "frozen" and r["metric"] == "cumulative_fcf_musd" and r["statistic"] == "p50")
    # Eight equally spaced values 0,10,...,70 have sample variance 600.
    assert value["mc_standard_error_single_run"] == pytest.approx(math.sqrt(600))
    assert value["approximate_mc_half_width95"] == pytest.approx(Z95 * math.sqrt(600))
    assert value["paired_rms_change"] == 0 and not value["passes"]
    breach = next(r for r in checks if r["metric"] == "cash_floor_breach")
    assert breach["replicate_mean"] == 0 and breach["mc_standard_error_single_run"] == 0
    assert breach["approximate_mc_half_width95"] == pytest.approx(Z95 ** 2 / (20 + Z95 ** 2))
    assert not breach["passes"]


def test_wilson_bounds_match_published_example_and_endpoints():
    # NIST's two-sided score method, independently tabulated 95% 4/20 interval.
    assert wilson_interval(4, 20) == pytest.approx((.08065766, .41601743), abs=1e-8)
    a = wilson_interval(0, 20)
    b = wilson_interval(20, 20)
    assert a[0] == 0 and a[1] > 0
    assert b[1] == 1 and b[0] < 1
    assert a == pytest.approx((1 - b[1], 1 - b[0]))
    assert wilson_interval(0, 10000)[1] < a[1]
    for success, n in ((-1, 2), (3, 2), (0, 0), (True, 2)):
        with pytest.raises(ValueError):
            wilson_interval(success, n)


def test_flow_quantiles_and_paired_impact_do_not_use_marginal_arithmetic():
    s = MonthlyScenario(paths=80, horizon_months=12, cash_floor_musd=10000)
    raw = paths(s)
    production = simulate_monthly(s)
    final = summarize_replica(raw, s.cash_floor_musd, s.seed, s.paths)["policies"][s.response_mode]["12"]
    median_month_sum = sum(row["p50"] for row in production["intervention"]["monthly"]["free_cash_flow_musd"])
    assert abs(final["cumulative_fcf_musd"]["p50"] - median_month_sum) > 1
    assert production["summary"]["cumulative_fcf_impact_musd"]["p10"] != pytest.approx(
        production["summary"]["cumulative_fcf_musd"]["p10"] - sum(row["p10"] for row in production["control"]["monthly"]["free_cash_flow_musd"]))
    assert (raw["frozen"]["12"]["response_contribution_to_fcf_musd"] == 0).all()


def test_adaptive_stop_requires_consecutive_levels_and_displayed_paths():
    s = MonthlyScenario(paths=80, horizon_months=3, stochastic=False)
    r = study_convergence(s, spec())
    assert [x["paths"] for x in r["levels"]] == [20, 40, 80]
    assert r["levels"][0]["passes"] is False
    assert [x["consecutive_passing_levels"] for x in r["levels"]] == [0, 1, 2]
    assert r["summary"]["status"] == "tolerance_met"
    assert all(r["summary"]["reference_checks"].values())
    assert r["summary"]["evaluated_path_draws"] == 8 * 80
    assert r["summary"]["prefix_paths_scored"] == 8 * 140
    assert all(row["approximate_mc_half_width95"] == pytest.approx(0, abs=1e-9) for row in r["levels"][-1]["diagnostics"])
    r = study_convergence(replace(s, paths=40), spec(path_counts=(20, 40, 80, 160)))
    assert r["summary"]["stopping_paths"] == 80


def test_tight_tolerance_reaches_limit_without_claiming_stability():
    r = study_convergence(MonthlyScenario(paths=40, horizon_months=3), spec(
        absolute_tolerance_musd=.0001, relative_tolerance=0, margin_tolerance_pp=.0001, breach_tolerance_fraction=.000001))
    assert r["summary"]["status"] == "path_limit_reached"
    assert r["summary"]["displayed_level_passes"] is False
    assert r["summary"]["stopping_paths"] == 80
    assert r["levels"][-1]["failed_checks"] > 0


@pytest.mark.parametrize("patch", [{"path_counts": (20, 20, 80)}, {"path_counts": (20, 40)},
    {"path_counts": (20, 40, True)}, {"path_counts": (20, 40, 1000001)}, {"replicates": 7},
    {"replicates": True}, {"consecutive_levels": 1}, {"relative_tolerance": float("nan")},
    {"absolute_tolerance_musd": 0}, {"margin_tolerance_pp": True}, {"breach_tolerance_fraction": -.1}])
def test_invalid_study_controls_rejected(patch):
    with pytest.raises(ValueError):
        spec(**patch).validate(MonthlyScenario(paths=40))


def test_missing_displayed_count_and_work_budget_are_explicit_boundaries():
    with pytest.raises(ValueError, match="displayed"):
        spec().validate(MonthlyScenario(paths=100))
    with pytest.raises(ValueError, match="work limit"):
        ConvergenceSpec(path_counts=(20, 10000, 1000000), replicates=24).validate(MonthlyScenario(paths=10000, horizon_months=24))


def test_study_replays_all_diagnostics_and_rejects_rehashed_tamper(tmp_path):
    saved = run_preserved(capture_source("convergence"), lambda: study_convergence(MonthlyScenario(paths=40, horizon_months=3), spec()))
    path = tmp_path / "study.json"
    path.write_text(json.dumps(saved))
    assert deterministic(replay_saved(path, "convergence")) == deterministic(saved)
    saved["levels"][-1]["diagnostics"][0]["mc_standard_error_single_run"] += 1
    saved["replay_code"]["result_sha256"] = digest(deterministic(saved))
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="checksum differs"):
        replay_saved(path, "convergence")


def test_api_checks_reference_and_returns_replayable_study(tmp_path):
    client = TestClient(app)
    source = client.post("/api/monthly/simulate", json={"paths": 40, "horizon_months": 3}).json()
    payload = {"run_record": source, "convergence": {"path_counts": [20, 40, 80], "replicates": 8}}
    response = client.post("/api/monthly/convergence", json=payload)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["manifest"]["reference_run_id"] == source["manifest"]["run_id"]
    path = tmp_path / "browser-study.json"
    path.write_text(json.dumps(saved))
    assert deterministic(replay_saved(path, "convergence")) == deterministic(saved)
    altered = deepcopy(payload)
    altered["run_record"]["summary"]["ending_cash_musd"]["p50"] += 1
    assert client.post("/api/monthly/convergence", json=altered).status_code == 422
    for patch in ({"replicates": 3}, {"path_counts": [20, 40, 80.5]}, {"path_counts": [20, 40, 80.]}, {"absolute_tolerance_musd": True}, {"made_up": 2}):
        assert client.post("/api/monthly/convergence", json={**payload, "convergence": patch}).status_code == 422


def test_future_fuel_does_not_enter_sampling_study():
    s = MonthlyScenario(paths=40, horizon_months=1)
    a = study_convergence(s, spec())
    b = study_convergence(s, spec(), fuel=load_fuel() + [{"date": "2099-01-01", "usd_per_gallon": 20}])
    assert deterministic(a) == deterministic(b)


def test_independent_statistics_audit_detects_false_error_and_false_stopping():
    from scripts.audit_convergence import audit
    saved = study_convergence(MonthlyScenario(paths=40, horizon_months=3), spec())
    assert audit(saved)["checks_passed"] > 2000
    altered = deepcopy(saved)
    altered["levels"][-1]["diagnostics"][0]["mc_standard_error_single_run"] /= math.sqrt(8)
    with pytest.raises(ValueError, match="single-run sample SD"):
        audit(altered, production_witnesses=False)
    altered = deepcopy(saved)
    altered["summary"]["status"] = "tolerance_met"
    with pytest.raises(ValueError, match="stopping status"):
        audit(altered, production_witnesses=False)


@pytest.mark.parametrize("horizon", [1, 12, 24])
def test_world_stream_chunks_match_monolithic_draw_and_generator_state(horizon):
    s = MonthlyScenario(paths=91, horizon_months=horizon)
    rng = np.random.default_rng(s.seed)
    pieces = [_world(replace(s, paths=n), 4., .2, rng=rng) for n in (20, 33, 1, 37)]
    whole_rng = np.random.default_rng(s.seed)
    whole = _world(s, 4., .2, rng=whole_rng)
    for i in range(2):
        np.testing.assert_array_equal(np.concatenate([p[i] for p in pieces]), whole[i])
    assert rng.bit_generator.state == whole_rng.bit_generator.state


def test_batch_sizes_have_identical_checkpoint_hashes_quantiles_and_stopping():
    s = MonthlyScenario(paths=40, horizon_months=7)
    a = study_convergence(s, spec(path_counts=(20, 40, 81), batch_paths=20))
    b = study_convergence(s, spec(path_counts=(20, 40, 81), batch_paths=31))
    c = study_convergence(s, spec(path_counts=(20, 40, 81), batch_paths=10000))
    assert a["levels"] == b["levels"] == c["levels"]
    assert a["seeds"] == b["seeds"] == c["seeds"]
    assert a["summary"]["reference_checks"] == b["summary"]["reference_checks"] == c["summary"]["reference_checks"]
    assert a["summary"]["evaluated_path_draws"] == b["summary"]["evaluated_path_draws"] == 8 * 81
    assert a["manifest"]["run_id"] != b["manifest"]["run_id"]  # recorded resource settings differ


def test_batching_funded_retention_matches_whole_path_values_for_every_seed():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    s = MonthlyScenario(paths=40, horizon_months=12, delivery_aircraft_count=30,
        delivery_group="narrowbody", delivery_delay_months=9,
        purchase_plan=json.loads((root / "data/purchase_plan_example.json").read_text()),
        retention_plan=json.loads((root / "data/retention_plan_example.json").read_text()))
    result = study_convergence(s, spec(path_counts=(20, 40, 81), batch_paths=20))
    for level in result["levels"]:
        for replica in level["replicas"]:
            scenario = replace(s, paths=level["paths"], seed=replica["seed"])
            full = summarize_replica(paths(scenario), s.cash_floor_musd, replica["seed"], level["paths"])
            assert full == replica


def test_incremental_sampling_progress_has_no_regenerated_prefixes():
    events = []
    result = study_convergence(MonthlyScenario(paths=40, horizon_months=1),
        spec(path_counts=(20, 40, 81), batch_paths=20), progress=events.append)
    samples = [event for event in events if event["phase"] == "sampling"]
    counts = np.diff([0, *[event["generated_paths"] for event in samples]])
    assert counts.min() == 1 and counts.max() <= 20
    assert counts.sum() == result["summary"]["evaluated_path_draws"] == 8 * 81
    assert [event["level_paths"] for event in events if event["phase"] == "level_complete"] == [20, 40, 81]


@pytest.mark.parametrize("batch", [True, 0, 19, 10001, 20.5])
def test_invalid_batch_sizes_fail_explicitly(batch):
    with pytest.raises(ValueError, match="batch_paths"):
        spec(batch_paths=batch).validate(MonthlyScenario(paths=40))


def test_temporary_files_removed_on_sampling_exception_and_disk_shortage(tmp_path, monkeypatch):
    import app.convergence as module
    original = module.tempfile.TemporaryDirectory
    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", lambda **kw: original(dir=tmp_path, **kw))
    def fail(event):
        raise ValueError("forced sampling interruption")
    with pytest.raises(ValueError, match="forced sampling interruption"):
        study_convergence(MonthlyScenario(paths=40, horizon_months=1), spec(batch_paths=20), progress=fail)
    assert list(tmp_path.iterdir()) == []
    from collections import namedtuple
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(module.shutil, "disk_usage", lambda path: Usage(0, 0, 0))
    with pytest.raises(ValueError, match="Insufficient temporary disk space"):
        study_convergence(MonthlyScenario(paths=40, horizon_months=1), spec())
    assert list(tmp_path.iterdir()) == []


def test_truncated_summary_is_rejected_before_quantiles(tmp_path):
    from app.convergence import append_batch, summarize_disk_replica
    s = MonthlyScenario(paths=40, horizon_months=1)
    append_batch(tmp_path, paths(s))
    path = tmp_path / "frozen-1-cumulative_fcf_musd.f64"
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ValueError, match="byte count differs"):
        summarize_disk_replica(tmp_path, s, s.seed, s.paths)
