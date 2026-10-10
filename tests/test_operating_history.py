from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

import duckdb
import pytest

from app.operating_history import (archive_metadata, build_operating_history, monthly_panel,
    normalize_archive, operating_history_status, parse_record, segment_market, wac_lookup, WAC_URL)
from app.source_store import preserve_source

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLES = json.loads((FIXTURES / "bts_segment_subset.json").read_text())
LOOKUP_BYTES = (FIXTURES / "bts_wac_lookup.csv").read_bytes()
LOOKUP = wac_lookup(LOOKUP_BYTES)


def archive_bytes(example):
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(example["member"], "\n".join(r["text"] for r in example["rows"]) + "\n")
    return output.getvalue()


def sources_for_test(raw):
    sources = [preserve_source(archive_bytes(e), e["source_url"], "application/zip", raw) for e in EXAMPLES]
    wac = preserve_source(LOOKUP_BYTES, WAC_URL, "text/csv", raw)
    return sources, wac


def record_for(example, carrier):
    return next(r["text"] for r in EXAMPLES[example]["rows"] if r["text"].split("|")[10] == carrier)


def test_actual_legacy_cabin_layout_and_observed_metrics():
    record = parse_record(record_for(1, "UA"), 1)
    assert record["layout"] == "legacy_32"
    assert (record["seats"], record["passengers"], record["asm"], record["rpm"]) == (138, 18, 131376, 17136)
    assert (record["ramp_minutes"], record["airborne_minutes"]) == (230, 123)


def test_actual_28_field_layout_in_old_and_new_domestic_archives():
    old = parse_record(record_for(0, "UA"), 1)
    new = parse_record(record_for(2, "UA"), 1)
    assert (old["asm"], old["rpm"]) == (8040, 5762)
    assert (new["asm"], new["rpm"]) == (11792, 10653)
    assert (new["ramp_minutes"], new["airborne_minutes"]) == (173, 23)


def test_decimal_freight_is_valid_without_rounding_passenger_counts():
    line = EXAMPLES[0]["rows"][-1]["text"]
    record = parse_record(line, 1)
    assert record["asm"] == 2240 and record["rpm"] == 680
    fields = line.split("|")
    fields[22] = "17.5"
    with pytest.raises(ValueError, match="numeric"):
        parse_record("|".join(fields), 1)


def test_real_aircraft_groups_are_distinct_and_load_factor_is_weighted():
    result = normalize_archive(archive_bytes(EXAMPLES[0]), {"source_url": EXAMPLES[0]["source_url"], "sha256": "a" * 64}, LOOKUP)
    row = next(r for r in result["rows"] if r["carrier"] == "5V")
    assert row["segment_records"] == 2
    assert (row["asm"], row["rpm"], row["departures_performed"]) == (2844, 1185, 2)
    panel = monthly_panel([result], [{"source_url": EXAMPLES[0]["source_url"], "sha256": "a" * 64}], "2014-04-01")
    row = next(r for r in panel["rows"] if r["carrier"] == "5V")
    assert row["load_factor"] == pytest.approx(5 / 12)
    assert not row["both_archive_scopes_available"]


def test_duplicate_segments_cannot_double_capacity():
    example = deepcopy(EXAMPLES[0])
    example["rows"].append(example["rows"][0])
    with pytest.raises(ValueError, match="Duplicate"):
        normalize_archive(archive_bytes(example), {"source_url": example["source_url"], "sha256": "a" * 64}, LOOKUP)


def test_geographic_boundaries_and_ambiguous_pairs():
    assert segment_market(91, 936, LOOKUP) == "domestic"  # Canada
    assert segment_market(91, 5, LOOKUP) == "pacific"  # Guam's historical WAC
    assert segment_market(74, 3, LOOKUP) == "latin_america"  # Puerto Rico
    assert segment_market(13, 733, LOOKUP) == "atlantic"  # India
    assert segment_market(91, 736, LOOKUP) == "pacific"
    assert segment_market(13, 975, LOOKUP) == "atlantic"  # Greenland is not Canada
    assert segment_market(148, 493, LOOKUP) == "cross_region"
    assert segment_market(91, 999, LOOKUP) == "unmapped"


def test_real_null_city_identifier_retains_valid_operating_metrics():
    record = parse_record(EXAMPLES[4]["rows"][0]["text"], 1)
    assert record["missing_city_market_metadata"]
    assert record["carrier"] == "QR"
    assert (record["seats"], record["passengers"], record["departures_performed"]) == (10974, 10514, 30)
    result = normalize_archive(archive_bytes(EXAMPLES[4]), {"source_url": EXAMPLES[4]["source_url"], "sha256": "a" * 64}, LOOKUP)
    assert result["issues"]["missing_city_market_metadata_records"] == 1
    assert result["rows"][0]["rpm"] == 78413412


def test_filename_release_dates_and_source_validation():
    meta = archive_metadata(EXAMPLES[0]["source_url"])
    assert (meta["scope"], meta["published_on"], meta["release_number"]) == ("DD", "2014-03-21", 2)
    assert archive_metadata(EXAMPLES[3]["source_url"])["published_on"] == "2026-10-02"
    with pytest.raises(ValueError, match="official"):
        archive_metadata(EXAMPLES[0]["source_url"].replace("www.bts.gov", "example.com"))
    with pytest.raises(ValueError, match="Unsupported"):
        archive_metadata(EXAMPLES[0]["source_url"].replace("DD.DB28DS", "FF.DB28IS"))


def test_release_filter_excludes_future_archive_without_network():
    sources = [{"sha256": str(i) * 64, "source_url": e["source_url"]} for i, e in enumerate(EXAMPLES)]
    normalized = [normalize_archive(archive_bytes(e), s, LOOKUP) for e, s in zip(EXAMPLES, sources)]
    assert monthly_panel(normalized, sources, "2014-03-20")["rows"] == []
    panel = monthly_panel(normalized, sources, "2014-04-01")
    assert {r["market"] for r in panel["rows"]} == {"domestic"}
    assert all(r["published_on"] <= "2014-04-01" for r in panel["rows"])
    assert all(r["period"].startswith("2013") for r in panel["rows"])


def test_revision_replaces_whole_scope_month_including_removed_records():
    old = {"source_url": EXAMPLES[2]["source_url"].replace("02OCT2026", "01OCT2026"), "sha256": "a" * 64}
    new = {"source_url": EXAMPLES[2]["source_url"], "sha256": "b" * 64}
    first = normalize_archive(archive_bytes(EXAMPLES[2]), old, LOOKUP)
    second = deepcopy(first)
    second["source_sha256"] = new["sha256"]
    second["rows"] = [r for r in second["rows"] if r["carrier"] != "UA"]
    for row in second["rows"]:
        row["source_sha256"] = new["sha256"]
        row["published_on"] = "2026-10-02"
    assert any(r["carrier"] == "UA" for r in monthly_panel([first, second], [old, new], "2026-10-01")["rows"])
    assert not any(r["carrier"] == "UA" for r in monthly_panel([first, second], [old, new], "2026-10-02")["rows"])
    conflicting = {**new, "sha256": "c" * 64}
    with pytest.raises(ValueError, match="Conflicting"):
        monthly_panel([first, second], [new, conflicting], "2026-10-09")


def test_build_preserves_parquet_and_detects_tampering(tmp_path):
    raw, history = tmp_path / "raw", tmp_path / "history"
    sources, wac = sources_for_test(raw)
    result = build_operating_history(sources, wac, "2026-10-09", history, raw)
    target = Path(result["path"])
    assert (target / "monthly.parquet").exists()
    assert operating_history_status(history)["status"] == "available"
    assert build_operating_history(sources, wac, "2026-10-09", history, raw)["snapshot_id"] == result["snapshot_id"]
    connection = duckdb.connect()
    rows = connection.execute("SELECT row FROM read_parquet(?)", [str(target / "monthly.parquet")]).fetchall()
    connection.close()
    assert any(row[0]["carrier"] == "OO" for row in rows)  # Feeder retained under its own operating code.
    assert not any(row[0]["carrier"] == "UAL" for row in rows)
    (target / "panel.json").write_text('{}')
    with pytest.raises(ValueError, match="checksum"):
        operating_history_status(history)


def test_bad_period_and_shape_fail_before_aggregation():
    line = record_for(0, "UA")
    with pytest.raises(ValueError, match="28/32"):
        parse_record(line + "|extra", 1)
    example = deepcopy(EXAMPLES[0])
    example["rows"][0]["text"] = line.replace("2013|", "2014|", 1)
    with pytest.raises(ValueError, match="outside"):
        normalize_archive(archive_bytes(example), {"source_url": example["source_url"], "sha256": "a" * 64}, LOOKUP)
    with pytest.raises(ValueError, match="numeric"):
        parse_record(line.replace("|86|", "|-86|"), 1)


def test_nonscheduled_records_with_missing_country_are_outside_passenger_scope():
    example = deepcopy(EXAMPLES[0])
    fields = record_for(0, "UA").split("|")
    fields[14], fields[27] = "L", ""
    example["rows"].append({"text": "|".join(fields)})
    result = normalize_archive(archive_bytes(example), {"source_url": example["source_url"], "sha256": "a" * 64}, LOOKUP)
    assert result["counts"]["raw_records"] == 9
    assert result["counts"]["scheduled_passenger_records"] == 8
    assert sum(r["segment_records"] for r in result["rows"]) == 8


def test_operating_status_api_returns_verification_errors(monkeypatch):
    from fastapi.testclient import TestClient
    from app import server
    client = TestClient(server.app)
    monkeypatch.setattr(server, "operating_history_status", lambda: {"status": "not_built"})
    assert client.get('/api/operating-history/status').json() == {"status": "not_built"}
    def corrupted():
        raise ValueError("changed bytes")
    monkeypatch.setattr(server, "operating_history_status", corrupted)
    response = client.get('/api/operating-history/status')
    assert response.status_code == 503
    assert "failed verification" in response.json()["detail"]


def test_status_absent_is_read_only_and_invalid_lookup_rejected(tmp_path):
    assert operating_history_status(tmp_path)["status"] == "not_built"
    with pytest.raises(ValueError, match="schema"):
        wac_lookup(b'Country,Code\nCanada,906\n')


def test_real_missing_carrier_country_is_metadata_not_zero_traffic():
    record = parse_record(EXAMPLES[5]["rows"][0]["text"], 1)
    assert record["carrier_wac"] is None and record["missing_carrier_country_metadata"]
    assert (record["seats"], record["passengers"], record["asm"], record["rpm"]) == (4563, 3732, 10011222, 8188008)
    result = normalize_archive(archive_bytes(EXAMPLES[5]), {"source_url": EXAMPLES[5]["source_url"], "sha256": "a" * 64}, LOOKUP)
    assert result["issues"]["missing_carrier_country_metadata_records"] == 1
    panel = monthly_panel([result], [{"source_url": EXAMPLES[5]["source_url"], "sha256": "a" * 64}], "2026-10-09")
    assert panel["rows"][0]["quality_flags"] == ["missing_carrier_country_metadata"]
