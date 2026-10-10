import json
import shutil

from fastapi.testclient import TestClient
import pytest

from app import model_archive as archive
from app.failure_search import FailureSearch, SearchAxis, search_failures
from app.monthly import MonthlyScenario, simulate_monthly
from app.server import app
from app.target_inversion import TargetInversion, invert_target


def small_run(kind):
    base = MonthlyScenario(paths=20, horizon_months=3)
    if kind == "convergence":
        from app.convergence import ConvergenceSpec, study_convergence
        return study_convergence(base, ConvergenceSpec(path_counts=(20, 40, 80), replicates=8))
    if kind == "monthly":
        return simulate_monthly(base)
    axes = {"x": SearchAxis("fuel_shock_pct", 0, 20, 2),
            "y": SearchAxis("demand_shock_pct", -5, 5, 2)}
    if kind == "failure_search":
        return search_failures(base, FailureSearch(**axes))
    return invert_target(base, TargetInversion(**axes))


def write(path, data):
    path.write_text(json.dumps(data, allow_nan=False))
    return path


def frozen_run(tmp_path, kind="monthly"):
    root = tmp_path / "archive"
    receipt = archive.archive_source(archive.capture_source(kind), root)
    saved = archive.attach_archive(small_run(kind), receipt)
    return root, saved, write(tmp_path / "run.json", saved)


@pytest.mark.parametrize("kind", ["monthly", "failure_search", "target_inversion", "convergence"])
def test_replay_uses_frozen_source_after_checkout_and_loaded_models_change(tmp_path, monkeypatch, kind):
    checkout = tmp_path / "checkout"
    for name in archive._names(kind):
        src = "app/replay_worker.py" if name == "replay_worker.py" else name
        path = checkout / src
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive.ROOT / src, path)
    captured = archive.capture_source(kind, checkout)
    root = tmp_path / "archive"
    receipt = archive.archive_source(captured, root)
    descriptor_bytes = (root / receipt["bundle_id"] / "descriptor.json").read_bytes()
    assert archive.archive_source(captured, root) == receipt
    assert (root / receipt["bundle_id"] / "descriptor.json").read_bytes() == descriptor_bytes
    saved = archive.attach_archive(small_run(kind), receipt)
    path = write(tmp_path / "run.json", saved)
    (checkout / "app" / "monthly.py").write_text("raise RuntimeError('new implementation')\n")
    def changed(*args, **kwargs):
        raise AssertionError("Replay must not execute or capture the new checkout")
    monkeypatch.setattr(archive, "capture_source", changed)
    import app.monthly, app.failure_search, app.target_inversion, app.convergence
    monkeypatch.setattr(app.monthly, "simulate_monthly", changed)
    monkeypatch.setattr(app.failure_search, "search_failures", changed)
    monkeypatch.setattr(app.target_inversion, "invert_target", changed)
    monkeypatch.setattr(app.convergence, "study_convergence", changed)
    result = archive.replay_saved(path, kind, root)
    assert archive.deterministic(result) == archive.deterministic(saved)
    assert result["replay_code"] == saved["replay_code"]
    assert not (root / receipt["bundle_id"] / "data").exists()


@pytest.mark.parametrize("field", ["summary", "frozen_cumulative", "assumptions", "evidence"])
def test_all_deterministic_results_are_recomputed_even_with_rehashed_receipt(tmp_path, field):
    root, saved, path = frozen_run(tmp_path)
    if field == "summary":
        saved["summary"]["cash_floor_breach_path_fraction"] += .1
    elif field == "frozen_cumulative":
        saved["frozen_cumulative_fcf_impact_musd"][0]["p50"] += 1
    elif field == "assumptions":
        saved["assumptions"].append("unverified new claim")
    else:
        saved["evidence"]["information_cutoff"] = "2099-01-01"
    # A checksum alone is not evidence of correct simulation results.
    saved["replay_code"]["result_sha256"] = archive.digest(archive.deterministic(saved))
    with pytest.raises(ValueError, match="checksum differs"):
        archive.replay_saved(write(path, saved), "monthly", root)


def test_code_corruption_and_extra_import_file_fail_before_execution(tmp_path):
    root, saved, path = frozen_run(tmp_path)
    bundle = root / saved["replay_code"]["bundle_id"]
    source = bundle / "app" / "monthly.py"
    original = source.read_bytes()
    source.write_bytes(original + b"\n# changed\n")
    with pytest.raises(ValueError, match="source checksum"):
        archive.replay_saved(path, "monthly", root)
    source.write_bytes(original)
    (bundle / "app" / "platform.py").write_text("raise RuntimeError('must never import')")
    with pytest.raises(ValueError, match="Unexpected"):
        archive.replay_saved(path, "monthly", root)


@pytest.mark.parametrize("bundle_id", ["../app", "a" * 64])
def test_run_cannot_supply_an_execution_path_or_unregistered_code(tmp_path, bundle_id):
    root, saved, path = frozen_run(tmp_path)
    saved["replay_code"]["bundle_id"] = bundle_id
    with pytest.raises(ValueError, match="identifier|not locally registered"):
        archive.replay_saved(write(path, saved), "monthly", root)


def test_copied_bundle_without_registration_and_receipt_kind_are_rejected(tmp_path):
    root, saved, path = frozen_run(tmp_path)
    unregistered = tmp_path / "unregistered"
    shutil.copytree(root / saved["replay_code"]["bundle_id"], unregistered / saved["replay_code"]["bundle_id"])
    with pytest.raises(ValueError, match="not locally registered"):
        archive.replay_saved(path, "monthly", unregistered)
    saved["replay_code"]["kind"] = "target_inversion"
    with pytest.raises(ValueError, match="contract"):
        archive.replay_saved(write(path, saved), "monthly", root)


def test_environment_mismatch_and_disk_edits_fail_closed(tmp_path, monkeypatch):
    root, saved, path = frozen_run(tmp_path)
    actual = archive.environment()
    monkeypatch.setattr(archive, "environment", lambda: {**actual, "numpy": "different"})
    with pytest.raises(ValueError, match="environment differs"):
        archive.replay_saved(path, "monthly", root)
    monkeypatch.undo()
    captured = archive.capture_source("monthly")
    monkeypatch.setattr(archive, "capture_source", lambda *args: archive.SourceCapture("monthly", captured.checkout,
        tuple((name, content + b"\n") for name, content in captured.sources)))
    with pytest.raises(ValueError, match="restart"):
        archive.run_preserved(captured, lambda: pytest.fail("must reject before running"), root)


def test_legacy_runs_use_registered_source_and_missing_versions_are_not_fabricated(tmp_path):
    root, saved, path = frozen_run(tmp_path)
    saved.pop("replay_code")
    assert archive.replay_saved(write(path, saved), "monthly", root)["manifest"] == saved["manifest"]
    saved["manifest"]["code_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="original executable source is unavailable"):
        archive.replay_saved(write(path, saved), "monthly", root)


def test_only_documented_runtime_fields_may_differ(tmp_path):
    root, saved, path = frozen_run(tmp_path)
    saved["summary"]["runtime_seconds"] = 999
    archive.replay_saved(write(path, saved), "monthly", root)
    saved["baseline_inputs"]["runtime_seconds"] = 999
    saved["replay_code"]["result_sha256"] = archive.digest(archive.deterministic(saved))
    with pytest.raises(ValueError, match="checksum differs"):
        archive.replay_saved(write(path, saved), "monthly", root)


@pytest.mark.parametrize("route,kind", [("simulate", "monthly"), ("failure-search", "failure_search"), ("target-inversion", "target_inversion")])
def test_api_exports_reference_verified_source_and_replay_all_browser_numbers(tmp_path, route, kind):
    payload = {"paths": 20, "horizon_months": 3} if kind == "monthly" else {
        "base_scenario": {"paths": 20, "horizon_months": 3},
        "x": {"driver": "fuel_shock_pct", "low": 0, "high": 20, "points": 2},
        "y": {"driver": "demand_shock_pct", "low": -5, "high": 5, "points": 2}}
    response = TestClient(app).post("/api/monthly/" + route, json=payload)
    assert response.status_code == 200
    result = response.json()
    archive.verify_bundle(result["replay_code"]["bundle_id"], kind)
    def browser(value):
        if isinstance(value, float) and value.is_integer(): return int(value)
        if isinstance(value, dict): return {k: browser(v) for k, v in value.items()}
        if isinstance(value, list): return [browser(v) for v in value]
        return value
    replayed = archive.replay_saved(write(tmp_path / "export.json", browser(result)), kind)
    assert archive.deterministic(replayed) == archive.deterministic(result)
