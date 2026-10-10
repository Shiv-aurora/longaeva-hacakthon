import csv
from copy import deepcopy
import io
import json
from pathlib import Path
from zipfile import ZipFile

from fastapi.testclient import TestClient
import pytest

from app.form41_history import (TABLE_CODES, normalize_export, preserve_export,
    build_form41_history, form41_history_status)
from app.source_store import byte_hash, preserve_source

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_rows(schedule):
    with (FIXTURES / f"form41_{schedule.lower()}.csv").open(newline="") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def exported(schedule, rows=None):
    fields, original = fixture_rows(schedule)
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=fields)
    writer.writeheader()
    writer.writerows(original if rows is None else rows)
    blob = io.BytesIO()
    with ZipFile(blob, "w") as z:
        z.writestr(f"T_F41SCHEDULE_{schedule}.csv", text.getvalue())
    content = blob.getvalue()
    return content, {"schedule": schedule, "sha256": byte_hash(content),
        "source_url": f"https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ={TABLE_CODES[schedule]}&QO_fu146_anzr=Nv4+Pn44vr4+Sv0n0pvny",
        "filters": {"year": "All Years", "period": "All", "fields": "All"}}


def preserved(tmp_path):
    raw = tmp_path / "raw"
    sources = []
    for schedule in TABLE_CODES:
        content, source = exported(schedule)
        meta = preserve_source(content, source["source_url"], "application/zip", raw)
        definition = preserve_source(f"Fixture definition {schedule}".encode(), source["source_url"], "text/plain", raw)
        for metadata in (meta, definition):
            path = raw / metadata["sha256"] / "source.json"
            saved = json.loads(path.read_text())
            saved["retrieved_at_utc"] = "2026-10-09T16:00:00+00:00"
            path.write_text(json.dumps(saved))
        sources.append({**source, "definition_sha256": definition["sha256"]})
    return raw, sources


def test_actual_source_rows_preserve_units_signs_nulls_and_totals():
    p12 = normalize_export(*exported("P12"))["rows"]
    latin = next(r for r in p12 if r["unique_carrier"] == "UA" and r["period"] == "2026-Q2" and r["reporting_region"] == "L")
    assert latin["values"]["OP_REVENUES"] == pytest.approx(1364.22222)
    assert latin["values"]["OP_PROFIT_LOSS"] == pytest.approx(-27.1222)
    assert latin["values"]["INTEREST_LONG_DEBT"] == pytest.approx(-38.17207)
    assert latin["values"]["TOTAL_PROPERTY"] is None  # Not inferred from detailed subaccounts.
    p52 = normalize_export(*exported("P52"))["rows"]
    aircraft = next(r for r in p52 if r["aircraft_type"] == "623" and r["reporting_region"] == "A")
    assert aircraft["values"]["FUEL_FLY_OPS"] == pytest.approx(.04518)
    assert aircraft["values"]["AIR_FUELS_ISSUED"] == pytest.approx(.01324)
    assert aircraft["values"]["TOTAL_AIR_HOURS"] == 20
    assert aircraft["values"]["AIR_DAYS_ASSIGN"] == 0
    assert aircraft["values"]["OIL_FLY_OPS"] is None
    assert any(r["aircraft_type"] == "999" and r["aircraft_config"] == "9" for r in p52)
    fuel = normalize_export(*exported("P12A"))["rows"]
    june = next(r for r in fuel if r["unique_carrier"] == "UA" and r["period"] == "2026-06")
    assert june["values"]["TOTAL_GALLONS"] == pytest.approx(376.132265)
    assert june["values"]["TOTAL_COST"] == pytest.approx(1338.279238)
    assert june["values"]["SALA_GALLONS"] == 0
    assert not june["quality_flags"]
    assert june["partition"] == "locked_test_2024_plus"
    assert {r["partition"] for r in fuel} == {"fit_2013_2019", "selection_2020_2023", "locked_test_2024_plus"}


def test_missing_unique_identity_remains_unassigned():
    rows = normalize_export(*exported("P12A"))["rows"]
    unassigned = next(r for r in rows if r["carrier"] == "0JQ")
    assert unassigned["unique_carrier"] is None and unassigned["airline_id"] is None
    assert unassigned["values"]["TOTAL_GALLONS"] == .003
    assert unassigned["quality_flags"] == ["missing_unique_carrier_identity"]


def test_grain_retains_regions_aircraft_and_legacy_carriers_without_double_count():
    _, rows = fixture_rows("P12")
    # Add separate system/international observations, with independent entities.
    base = next(r for r in rows if r["YEAR"] == "2026" and r["REGION"] == "D")
    extra = [{**base, "REGION": region, "UNIQUE_CARRIER_ENTITY": f"SYSTEM_{region}"} for region in ("S", "I")]
    normalized = normalize_export(*exported("P12", rows + extra))["rows"]
    assert len(normalized) == len(rows) + 2
    assert {r["reporting_region"] for r in normalized} >= {"D", "A", "P", "L", "S", "I"}
    assert {r["unique_carrier"] for r in normalized} >= {"UA", "AA", "DL", "US"}
    with pytest.raises(ValueError, match="Duplicate"):
        normalize_export(*exported("P12", rows + [base]))


def test_corruption_schema_and_periods_fail_before_publication():
    for schedule in TABLE_CODES:
        _, rows = fixture_rows(schedule)
        bad = deepcopy(rows)
        bad[0]["YEAR"] = "2026.5"
        with pytest.raises(ValueError, match="year"):
            normalize_export(*exported(schedule, bad))
        content, metadata = exported(schedule)
        with pytest.raises(ValueError, match="source hash"):
            normalize_export(content + b"tamper", metadata)
    _, rows = fixture_rows("P12A")
    bad = deepcopy(rows); bad[0]["QUARTER"] = "4"; bad[0]["MONTH"] = "1"
    with pytest.raises(ValueError, match="month/quarter"):
        normalize_export(*exported("P12A", bad))
    bad = deepcopy(rows); bad[0]["TOTAL_COST"] = "NaN"
    with pytest.raises(ValueError, match="Nonfinite"):
        normalize_export(*exported("P12A", bad))
    with ZipFile(io.BytesIO(exported("P12")[0])) as z:
        content = z.read(z.namelist()[0]).replace(b"NET_INCOME", b"UNKNOWN_COLUMN", 1)
    blob = io.BytesIO()
    with ZipFile(blob, "w") as z: z.writestr("wrong.csv", content)
    _, meta = exported("P12"); meta["sha256"] = byte_hash(blob.getvalue())
    with pytest.raises(ValueError, match="schema"):
        normalize_export(blob.getvalue(), meta)


def test_fuel_identity_difference_is_flagged_and_not_repaired():
    _, rows = fixture_rows("P12A")
    row = next(r for r in rows if r["UNIQUE_CARRIER"] == "UA" and r["YEAR"] == "2026")
    row = {**row, "TOTAL_GALLONS": "376132266.00"}
    result = normalize_export(*exported("P12A", [row]))["rows"][0]
    assert result["values"]["TOTAL_GALLONS"] == pytest.approx(376.132266)
    assert result["quality_flags"] == ["fuel_identity_difference_TOTAL_GALLONS"]


def test_negative_aircraft_quantities_survive_with_explicit_flags():
    _, rows = fixture_rows("P52")
    row = {**rows[0], "AIR_FUELS_ISSUED": "-1.50", "TOTAL_AIR_HOURS": "-0.01", "AIR_DAYS_ASSIGN": "-0.02"}
    result = normalize_export(*exported("P52", [row]))["rows"][0]
    assert result["values"]["AIR_FUELS_ISSUED"] == -.0015
    assert result["values"]["TOTAL_AIR_HOURS"] == -10
    assert result["values"]["AIR_DAYS_ASSIGN"] == -20
    assert set(result["quality_flags"]) == {f"negative_reported_aircraft_quantity_{k}" for k in ("AIR_FUELS_ISSUED", "TOTAL_AIR_HOURS", "AIR_DAYS_ASSIGN")}


def test_offline_build_parquet_rebuild_and_tamper_guards(tmp_path):
    raw, sources = preserved(tmp_path)
    destination = tmp_path / "history"
    first = build_form41_history(sources, "2026-10-09", destination, raw)
    assert form41_history_status(destination)["snapshot_id"] == first["snapshot_id"]
    assert build_form41_history(first["sources"], "2026-10-09", destination, raw)["snapshot_id"] == first["snapshot_id"]
    assert len(first["coverage"]) == 12
    path = Path(first["path"]) / "P12A.json"
    panel = json.loads(path.read_text()); panel["rows"][0]["values"]["TOTAL_COST"] = 123
    path.write_text(json.dumps(panel))
    with pytest.raises(ValueError, match="checksum"):
        form41_history_status(destination)
    with pytest.raises(ValueError, match="modified"):
        build_form41_history(sources, "2026-10-09", destination, raw)


def test_acquisition_cutoff_and_incomplete_export_are_rejected(tmp_path):
    raw, sources = preserved(tmp_path)
    with pytest.raises(ValueError, match="not acquired"):
        build_form41_history(sources, "2026-07-16", tmp_path / "history", raw)
    assert not (tmp_path / "history" / "latest.json").exists()
    bad = deepcopy(sources); bad[0]["filters"]["year"] = "2026"
    with pytest.raises(ValueError, match="complete"):
        build_form41_history(bad, "2026-10-09", tmp_path / "history", raw)
    with pytest.raises(ValueError, match="one complete"):
        build_form41_history(sources[:2], "2026-10-09", tmp_path / "history", raw)
    with pytest.raises(ValueError, match="official"):
        preserve_export({"schedule": "P12", "source_url": "https://example.com/export"}, raw)


def test_cached_api_reports_failure_without_fabricated_coverage(monkeypatch):
    from app import server
    client = TestClient(server.app)
    monkeypatch.setattr(server, "form41_history_status", lambda: {"status": "not_built", "message": "not built"})
    assert client.get("/api/form41-history/status").json()["status"] == "not_built"
    def corrupt(): raise ValueError("checksum")
    monkeypatch.setattr(server, "form41_history_status", corrupt)
    assert client.get("/api/form41-history/status").status_code == 503
