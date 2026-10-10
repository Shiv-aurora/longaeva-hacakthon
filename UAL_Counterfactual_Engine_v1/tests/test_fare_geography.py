import hashlib
from io import BytesIO
import json
from zipfile import ZipFile

import duckdb
import pytest

from app.evidence import digest
from app.fare_geography import endpoint_fields, scan_archive


def ticket(quarter="20131", wac="41", origin="JFK", origin_wac="22", destination="ORD", city="30977"):
    # Monetary and passenger fields intentionally invalid: geography must not read them.
    return "|".join(["not-a-fare", "OO", quarter, "1", "not-passengers", origin, "*", "31703", "0", origin_wac,
        "--", "D", "DL", "A", "U", "0", destination, "Y", city, "0", wac]) + "\n"


def test_original_geography_reads_endpoints_without_fare_or_carrier_selection():
    assert endpoint_fields(ticket(), "2013-Q1") == [(0, "JFK", 31703, 22), (1, "ORD", 30977, 41)]
    assert endpoint_fields(ticket().rstrip("\n") + "|\n", "2013-Q1") == endpoint_fields(ticket(), "2013-Q1")
    assert endpoint_fields(ticket(wac="826", destination="GUM", city="31865"), "2013-Q1")[-1] == (1, "GUM", 31865, 826)


def test_geography_rejects_locked_quarter_layout_and_invalid_endpoint_before_selection():
    with pytest.raises(ValueError, match="locked"):
        endpoint_fields(ticket(quarter="20241"), "2024-Q1")
    with pytest.raises(ValueError, match="quarter"):
        endpoint_fields(ticket(), "2013-Q2")
    with pytest.raises(ValueError, match="layout"):
        endpoint_fields(ticket().replace("|0|ORD|", "|0|extra|ORD|"), "2013-Q1")
    for invalid in (ticket(wac="0"), ticket(city="NaN"), ticket(destination="TOOLONG")):
        with pytest.raises(ValueError):
            endpoint_fields(invalid, "2013-Q1")


def test_full_endpoint_occurrences_keep_conflicts_without_majority_vote(tmp_path):
    first = ticket(); second = ticket(wac="42")
    content = first + "\n" + first + second
    path = tmp_path / "source.zip"
    with ZipFile(path, "w") as archive:
        archive.writestr("records.asc", content)
    crosswalk = {str(code): {"description": str(code), "market": "domestic"} for code in (22, 41, 42)}
    source = {"quarter": "2013-Q1", "sha256": "a" * 64, "published_on": "2017-10-03"}
    part = scan_archive(path, source, crosswalk)
    assert part["records"] == 3 and part["endpoint_occurrences"] == 6
    origin, a, b = part["rows"]
    assert origin["airport"] == "JFK" and origin["origin_occurrences"] == 3
    assert a["endpoint_occurrences"] == 2 and b["endpoint_occurrences"] == 1
    assert a["observed_wacs"] == b["observed_wacs"] == [41, 42]
    assert a["assignment_status"] == b["assignment_status"] == "conflicting_wac"
    assert a["market"] is b["market"] is None
    assert a["source_line_witnesses"] == [{"line": 1, "endpoint_position": 1}, {"line": 3, "endpoint_position": 1}]
    assert a["contributor_digest"] == hashlib.sha256(b"1:1:" + first.encode() + b"3:1:" + first.encode()).hexdigest()


def test_unknown_wac_is_visible_and_pacific_territory_not_called_foreign(tmp_path):
    path = tmp_path / "source.zip"
    with ZipFile(path, "w") as archive:
        archive.writestr("records.asc", ticket(wac="826", destination="GUM", city="31865"))
    source = {"quarter": "2013-Q1", "sha256": "a" * 64, "published_on": "2017-10-03"}
    part = scan_archive(path, source, {"826": {"description": "Guam", "market": "pacific"}})
    by_airport = {r["airport"]: r for r in part["rows"]}
    assert by_airport["GUM"]["market"] == "pacific" and by_airport["GUM"]["wac_description"] == "Guam"
    assert by_airport["JFK"]["assignment_status"] == "unmapped_wac" and by_airport["JFK"]["market"] is None


def test_full_quarter_snapshot_native_content_rebuild_corruption_and_atomic_promotion(tmp_path, monkeypatch):
    import app.fare_geography as module
    from app.source_store import preserve_source, read_source
    raw = tmp_path / "data/raw"; raw.mkdir(parents=True)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "read_source", lambda sha: read_source(sha, destination=raw))
    definition = preserve_source(b"%PDF fixture", "https://example.org/definition", "application/pdf", raw)
    lookup = preserve_source(b'Code,Description\n22,New York\n41,Illinois\n', "https://example.org/wac", "text/csv", raw)
    sources = []
    for year in range(2013, 2024):
        for quarter in (1, 2, 3, 4):
            buffer = BytesIO()
            with ZipFile(buffer, "w") as archive:
                archive.writestr("records.asc", ticket(quarter=f"{year}{quarter}"))
            url = f"https://example.org/{year}-{quarter}"
            saved = preserve_source(buffer.getvalue(), url, "application/zip", raw)
            sources.append({"quarter": f"{year}-Q{quarter}", "sha256": saved["sha256"], "url": url,
                "bytes": saved["bytes"], "published_on": f"{year+1}-03-01"})
    specification = {"sources": sources, "definition_source_sha256": definition["sha256"],
        "wac_source_sha256": lookup["sha256"], "wac_source_url": "https://example.org/wac",
        "crosswalk": {"22": {"description": "New York", "market": "domestic"}, "41": {"description": "Illinois", "market": "domestic"}}}
    destination = tmp_path / "data/fare_geography"
    first = module.build_geography(sources, specification, destination)
    assert first == module.build_geography(sources, specification, destination)
    assert first["quarters"] == 44 and first["rows"] == 88 and first["endpoint_occurrences"] == 88
    folder = destination / first["snapshot_id"]
    manifest = json.loads((folder / "manifest.json").read_text())
    assert digest({k: v for k, v in manifest.items() if k != "snapshot_id"}) == first["snapshot_id"]
    con = duckdb.connect()
    values = con.execute("SELECT count(*), sum(endpoint_occurrences), sum(origin_occurrences) FROM read_parquet(?)", [str(folder / "endpoints.parquet")]).fetchone(); con.close()
    assert values == (88, 88, 44)
    import scripts.audit_fare_geography as oracle
    monkeypatch.setattr(oracle, "ROOT", tmp_path)
    monkeypatch.setattr(oracle, "read_source", lambda sha: read_source(sha, destination=raw))
    audit = oracle.audit_snapshot(folder / "manifest.json")
    assert audit["native_parquet_rows"] == 88 and len(audit["quarter_reports"]) == 44
    assert sum(r["raw_ticket_records"] for r in audit["quarter_reports"]) == 44
    part_file = destination / "parts" / manifest["parts"][0]["part_id"] / "part.json"
    original = part_file.read_text(); part = json.loads(original); part["rows"][0]["endpoint_occurrences"] += 1
    part_file.write_text(json.dumps(part))
    with pytest.raises(ValueError, match="Cached geography"):
        module.build_geography(sources, specification, destination)
    # Even rehashing a corrupted output cannot satisfy the raw-record oracle.
    metadata_path = part_file.parent / "manifest.json"
    original_metadata = metadata_path.read_text()
    metadata = json.loads(original_metadata); metadata["part_digest"] = digest(part)
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="Original geography quantities"):
        oracle.audit_part(destination / "designs" / first["design_id"] / "design.json", "2013-Q1")
    metadata_path.write_text(original_metadata)
    part_file.write_text(original)
    # Failed last-quarter input cannot replace an accepted complete snapshot.
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("records.asc", ticket(quarter="20241"))
    saved = preserve_source(buffer.getvalue(), sources[-1]["url"], "application/zip", raw)
    altered = [*sources[:-1], {**sources[-1], "sha256": saved["sha256"], "bytes": saved["bytes"]}]
    with pytest.raises(ValueError, match="locked"):
        module.build_geography(altered, {**specification, "sources": altered}, destination)
    assert json.loads((destination / "latest.json").read_text())["snapshot_id"] == first["snapshot_id"]


def test_missing_quarters_and_changed_wac_definition_are_rejected(tmp_path, monkeypatch):
    import app.fare_geography as module
    with pytest.raises(ValueError, match="44"):
        module.build_geography([], {}, tmp_path)
    sources = [{"quarter": f"{year}-Q{quarter}"} for year in range(2013, 2024) for quarter in (1, 2, 3, 4)]
    monkeypatch.setattr(module, "read_source", lambda sha: (b"%PDF fixture", {}) if sha == "definition" else (b"Code,Description\n22,New York\n", {"source_url": "https://example.org/wac"}))
    specification = {"sources": sources, "definition_source_sha256": "definition", "wac_source_sha256": "wac",
        "wac_source_url": "https://example.org/wac", "crosswalk": {"22": {"description": "Wrong", "market": "domestic"}}}
    with pytest.raises(ValueError, match="definitions"):
        module.build_geography(sources, specification, tmp_path)
