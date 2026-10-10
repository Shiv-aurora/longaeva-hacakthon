from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

import duckdb
import pytest

from app.evidence import digest
from app.operating_history import normalize_archive, wac_lookup, WAC_URL
from app.operating_vintages import (build_vintages, reconstruct, origin_history,
    normalize_source, code_bundle, verify_vintages)
from app.source_store import preserve_source

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLES = json.loads((FIXTURES / "bts_segment_subset.json").read_text())[:2]
WAC = (FIXTURES / "bts_wac_lookup.csv").read_bytes()


def setup_sources(raw):
    sources, normalized = [], []
    for example in EXAMPLES:
        data = BytesIO()
        with ZipFile(data, "w") as archive:
            archive.writestr(example["member"], "\n".join(r["text"] for r in example["rows"]))
        source = preserve_source(data.getvalue(), example["source_url"], "application/zip", raw)
        sources.append(source)
        value = normalize_archive(data.getvalue(), source, wac_lookup(WAC))
        value["rows"] = [r for r in value["rows"] if r["carrier"] in ("UA", "DL", "AA")]
        normalized.append(value)
    wac = preserve_source(WAC, WAC_URL, "text/csv", raw)
    return sources, normalized, wac


def revision(sources, normalized, empty=False):
    source = {**sources[0], "sha256": "a" * 64,
        "source_url": sources[0]["source_url"].replace("REL02.21MAR2014", "REL03.10FEB2020")}
    value = deepcopy(normalized[0]); value["source_sha256"] = source["sha256"]
    for r in value["rows"]:
        r["source_sha256"] = source["sha256"]; r["published_on"] = "2020-02-10"
        r["rpm"] = r["rpm"] // 2
    if empty: value["rows"] = []
    return source, value


def spec(sources, wac):
    return {"sources": deepcopy(sources), "wac_source": wac, "catalogs": [], "acquisition_cutoff": "2026-10-09",
        "origin_first": "2020-01", "origin_last": "2023-12", "target_first": "2013-01", "target_last": "2023-12",
        "carriers": ["UA", "DL", "AA"], "boundary": "Declared sparse test certificate records"}


def test_later_revision_does_not_erase_earlier_released_quantity(tmp_path):
    sources, normal, _ = setup_sources(tmp_path)
    later, changed = revision(sources, normal)
    a = reconstruct(normal + [changed], sources + [later], ["2020-01-31", "2020-02-29"], "2026-10-09")
    early = next(r for r in origin_history(a, "2020-01-31") if (r["carrier"], r["market"]) == ("UA", "domestic"))
    late = next(r for r in origin_history(a, "2020-02-29") if (r["carrier"], r["market"]) == ("UA", "domestic"))
    assert early["rpm_million"] == 5762 / 1e6
    assert late["rpm_million"] == 2881 / 1e6
    assert early["source_sha256s"] == [sources[0]["sha256"]]
    assert late["source_sha256s"] == [later["sha256"]]
    assert late in a["evaluation_targets"]["observations"]


def test_withdrawn_contribution_does_not_fall_back_to_old_release(tmp_path):
    sources, normal, _ = setup_sources(tmp_path)
    later, empty = revision(sources, normal, empty=True)
    a = reconstruct(normal + [empty], sources + [later], ["2020-01-31", "2020-02-29"], "2026-10-09")
    assert any(r["market"] == "domestic" for r in origin_history(a, "2020-01-31"))
    assert not any(r["market"] == "domestic" for r in origin_history(a, "2020-02-29"))
    assert any(r["market"] == "latin_america" for r in origin_history(a, "2020-02-29"))
    assert all(r["published_on"] == "2020-02-10" for r in origin_history(a, "2020-02-29"))


def test_noncontributing_scope_must_be_released(tmp_path):
    sources, normal, _ = setup_sources(tmp_path)
    delayed = deepcopy(sources[1]); delayed["source_url"] = delayed["source_url"].replace("04JUN2014", "10FEB2020")
    normal[1] = deepcopy(normal[1])
    for r in normal[1]["rows"]: r["published_on"] = "2020-02-10"
    a = reconstruct(normal, [sources[0], delayed], ["2020-01-31", "2020-02-29"], "2026-10-09")
    assert not origin_history(a, "2020-01-31")
    assert origin_history(a, "2020-02-29")
    assert all(r["published_on"] == "2020-02-10" for r in origin_history(a, "2020-02-29"))


def test_conflicting_same_release_fails(tmp_path):
    sources, normal, _ = setup_sources(tmp_path)
    collision = {**sources[0], "sha256": "a" * 64}
    with pytest.raises(ValueError, match="Conflicting"):
        reconstruct(normal, sources + [collision], ["2020-01-31"], "2026-10-09")


def test_equivalent_same_release_archives_preserve_alias_hashes(tmp_path):
    sources, normal, _ = setup_sources(tmp_path)
    alias = {**sources[0], "sha256":"a" * 64}
    duplicate = deepcopy(normal[0]); duplicate["source_sha256"] = alias["sha256"]
    for row in duplicate["rows"]: row["source_sha256"] = alias["sha256"]
    a = reconstruct(normal + [duplicate], sources + [alias], ["2020-01-31"], "2026-10-09")
    release = next(r for r in a["origins"][0]["selected_releases"] if (r["scope"],r["period"]) == ("DD","2013-10"))
    assert release["equivalent_source_sha256s"] == sorted([sources[0]["sha256"],alias["sha256"]])
    assert release["source_sha256"] == min(sources[0]["sha256"],alias["sha256"])
    duplicate["rows"][0]["departures_performed"] += 1
    with pytest.raises(ValueError,match="Conflicting same-release"):
        reconstruct(normal + [duplicate], sources + [alias], ["2020-01-31"], "2026-10-09")


def test_independent_raw_revision_audit_and_tamper(tmp_path):
    from scripts.audit_traffic_vintages import raw_sums, audit
    sources, normal, wac = setup_sources(tmp_path)
    a = reconstruct(normal,sources,["2020-01-31","2020-02-29"],"2026-10-09")
    totals,count = raw_sums(sources,tmp_path)
    result = audit(a,spec(sources,wac),totals)
    assert count == 6 and result["checks_passed"] > 75
    identity = a["origins"][0]["observation_ids"][0]
    changed = a["observation_versions"].pop(identity)
    changed["rpm_million"] += .001
    revised = digest(changed); a["observation_versions"][revised] = changed
    for origin in a["origins"]:
        origin["observation_ids"] = [revised if k==identity else k for k in origin["observation_ids"]]
    with pytest.raises(ValueError,match="original RPM sum"):
        audit(a,spec(sources,wac),totals)


@pytest.mark.parametrize("issues", [[], ["2020-01-31", "2020-01-31"], ["2024-01-31"], ["2019-12-31"]])
def test_invalid_or_locked_issue_design_fails(tmp_path, issues):
    sources, normal, _ = setup_sources(tmp_path)
    with pytest.raises(ValueError): reconstruct(normal, sources, issues, "2026-10-09")


def test_history_lookup_rejects_missing_issue_and_future_poison(tmp_path):
    sources, normal, _ = setup_sources(tmp_path)
    a = reconstruct(normal, sources, ["2020-01-31"], "2026-10-09")
    with pytest.raises(ValueError, match="absent"): origin_history(a, "2020-02-29")
    key = a["origins"][0]["observation_ids"][0]
    a["observation_versions"][key]["published_on"] = "2020-02-01"
    with pytest.raises(ValueError, match="future"): origin_history(a, "2020-01-31")


def test_normalization_cache_verifies_original_source_and_retains_declared_carriers(tmp_path):
    sources, _, wac = setup_sources(tmp_path / "raw")
    task = (sources[0], wac["sha256"], str(tmp_path / "raw"), str(tmp_path / "cache"), code_bundle())
    result = normalize_source(task)
    assert {r["carrier"] for r in result["rows"]} == {"UA", "DL", "AA"}
    assert result == normalize_source(task)
    path = next((tmp_path / "cache").glob("*.json"))
    modified = json.loads(path.read_text()); modified["normalized"]["rows"][0]["rpm"] += 1
    path.write_text(json.dumps(modified))
    with pytest.raises(ValueError, match="cache differs"): normalize_source(task)
    path.unlink()
    original = tmp_path / "raw" / sources[0]["sha256"] / "source.bin"
    original.write_bytes(b"changed original bytes")
    with pytest.raises(ValueError, match="checksum"): normalize_source(task)


def test_immutable_build_exact_rebuild_and_parquet_tamper(tmp_path):
    sources, _, wac = setup_sources(tmp_path / "raw")
    design = spec(sources, wac)
    first = build_vintages(design, tmp_path / "vintages", tmp_path / "raw")
    second = build_vintages(design, tmp_path / "vintages", tmp_path / "raw")
    assert first == second and first["origins"] == 48
    artifact = verify_vintages(Path(first["path"]), tmp_path / "vintages", tmp_path / "raw")
    assert len(artifact["evaluation_targets"]["observations"]) == 6
    p = Path(first["path"]) / "versions.parquet"
    connection = duckdb.connect()
    connection.execute("CREATE TABLE changed AS SELECT * FROM read_parquet(?)", [str(p)])
    connection.execute("UPDATE changed SET rpm_million = rpm_million + 1")
    connection.execute(f"COPY changed TO '{p}' (FORMAT PARQUET)")
    connection.close()
    with pytest.raises(ValueError, match="Parquet differs"):
        verify_vintages(Path(first["path"]), tmp_path / "vintages", tmp_path / "raw")


def test_locked_archives_and_modified_builder_bytes_are_rejected(tmp_path):
    sources, _, wac = setup_sources(tmp_path / "raw")
    design = spec(sources, wac)
    design["sources"][0] = {**sources[0], "source_url": sources[0]["source_url"].replace("201301.201312", "202401.202412").replace("21MAR2014", "21MAR2025")}
    with pytest.raises(ValueError, match="locked archive"):
        build_vintages(design, tmp_path / "vintages", tmp_path / "raw")
    design = spec(sources, wac)
    result = build_vintages(design, tmp_path / "vintages", tmp_path / "raw")
    (Path(result["path"]) / "code/operating_vintages.py").write_text("changed")
    with pytest.raises(ValueError, match="builder checksum"):
        verify_vintages(Path(result["path"]), tmp_path / "vintages", tmp_path / "raw")
