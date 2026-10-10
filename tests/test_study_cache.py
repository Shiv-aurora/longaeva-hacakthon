"""Caching may skip sampling only after a complete archived replay."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest
from fastapi.testclient import TestClient

from app.convergence import ConvergenceSpec, study_convergence
from app.model_archive import (capture_source, deterministic, digest, read_verified_study,
    replay_and_register_study, run_preserved)
from app.monthly import MonthlyScenario


@pytest.fixture
def saved_study(tmp_path):
    controls = ConvergenceSpec(path_counts=(20, 40, 81), replicates=8, batch_paths=31)
    saved = run_preserved(capture_source("convergence"), lambda: study_convergence(
        MonthlyScenario(paths=40, horizon_months=1), controls))
    path = tmp_path / "study.json"
    path.write_text(json.dumps(saved))
    return path, saved, tmp_path / "cache"


def lookup(saved, cache, **changes):
    m = saved["manifest"]
    return read_verified_study(changes.get("reference", m["reference_run_id"]),
        changes.get("controls", m["convergence"]), changes.get("code", m["code_sha256"]), cache_root=cache)


def test_unregistered_file_is_never_a_cache_hit(saved_study):
    path, saved, cache = saved_study
    cache.mkdir()
    (cache / (saved["manifest"]["run_id"] + ".json")).write_bytes(path.read_bytes())
    assert lookup(saved, cache) is None


def test_full_replay_admits_exact_immutable_result_without_rerunning(saved_study, monkeypatch):
    path, saved, cache = saved_study
    verified = replay_and_register_study(path, cache_root=cache)
    assert deterministic(verified) == deterministic(saved)
    replay_and_register_study(path, cache_root=cache)
    import app.model_archive as archive
    monkeypatch.setattr(archive, "replay_saved", lambda *a, **kw: pytest.fail("cache must not rerun sampling"))
    assert lookup(saved, cache) == verified


def test_rehashed_false_result_cannot_register(saved_study):
    path, saved, cache = saved_study
    saved["levels"][-1]["diagnostics"][0]["mc_standard_error_single_run"] += 1
    saved["replay_code"]["result_sha256"] = digest(deterministic(saved))
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="checksum differs"):
        replay_and_register_study(path, cache_root=cache)
    assert lookup(saved, cache) is None


@pytest.mark.parametrize("field", ["runtime", "diagnostic", "receipt", "symlink", "missing"])
def test_cached_file_changes_fail_closed(saved_study, field):
    path, saved, cache = saved_study
    replay_and_register_study(path, cache_root=cache)
    target = cache / (saved["manifest"]["run_id"] + ".json")
    changed = json.loads(target.read_text())
    if field == "runtime":
        changed["summary"]["runtime_seconds"] += 1
    elif field == "diagnostic":
        changed["levels"][0]["failed_checks"] += 1
        changed["replay_code"]["result_sha256"] = digest(deterministic(changed))
    elif field == "receipt":
        changed["replay_code"]["bundle_id"] = "0" * 64
    elif field == "symlink":
        target.unlink()
        target.symlink_to(path)
    else:
        target.unlink()
    if field in {"runtime", "diagnostic", "receipt"}:
        target.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="checksum differs|incomplete"):
        lookup(saved, cache)


def test_input_code_or_environment_changes_do_not_reuse_study(saved_study, monkeypatch):
    path, saved, cache = saved_study
    replay_and_register_study(path, cache_root=cache)
    controls = deepcopy(saved["manifest"]["convergence"])
    controls["batch_paths"] += 1
    assert lookup(saved, cache, controls=controls) is None
    controls["relative_tolerance"] += .01
    assert lookup(saved, cache, controls=controls) is None
    assert lookup(saved, cache, reference="0" * 64) is None
    assert lookup(saved, cache, code="0" * 64) is None
    import app.model_archive as archive
    recorded = archive.environment()
    monkeypatch.setattr(archive, "environment", lambda: {**recorded, "numpy": "changed"})
    assert lookup(saved, cache) is None


def test_registered_code_still_must_be_intact(saved_study, monkeypatch):
    path, saved, cache = saved_study
    replay_and_register_study(path, cache_root=cache)
    import app.model_archive as archive
    def unavailable(*a, **kw):
        raise ValueError("Model code source checksum differs")
    monkeypatch.setattr(archive, "verify_bundle", unavailable)
    with pytest.raises(ValueError, match="source checksum"):
        lookup(saved, cache)


def test_api_cache_replays_reference_and_preserves_download_record(tmp_path, monkeypatch):
    import app.server as server
    cache = tmp_path / "cache"
    monkeypatch.setattr(server, "STUDY_CACHE_ROOT", cache)
    client = TestClient(server.app)
    source = client.post("/api/monthly/simulate", json={"paths": 40, "horizon_months": 1}).json()
    controls = asdict(ConvergenceSpec(path_counts=(20, 40, 81), replicates=8, batch_paths=31))
    payload = {"run_record": source, "convergence": controls}
    initial = client.post("/api/monthly/convergence", json=payload)
    assert initial.status_code == 200, initial.text
    assert "x-ual-study-cache" not in initial.headers
    path = tmp_path / "browser.json"
    path.write_text(json.dumps(initial.json()))
    replay_and_register_study(path, cache_root=cache)
    monkeypatch.setattr(server, "study_convergence", lambda *a, **kw: pytest.fail("cached study must not recompute"))
    response = client.post("/api/monthly/convergence", json=payload)
    assert response.status_code == 200, response.text
    assert response.headers["x-ual-study-cache"] == "verified"
    assert response.json() == initial.json()
    altered = deepcopy(payload)
    altered["run_record"]["summary"]["ending_cash_musd"]["p50"] += 1
    altered["run_record"]["replay_code"]["result_sha256"] = digest(deterministic(altered["run_record"]))
    assert client.post("/api/monthly/convergence", json=altered).status_code == 422
