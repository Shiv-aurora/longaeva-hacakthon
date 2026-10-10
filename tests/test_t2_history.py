import copy
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from app.evidence import digest
from app.source_store import RAW, read_source, preserve_source
from app.t2_history import METRICS, normalize_export, capacity_panel, build_t2_history, preserve_export

ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ=FIH&QO_fu146_anzr=Nv4%20Pn44vr4%20f7zzn4B"


def fixture_bytes():
    return (ROOT / "tests/fixtures/t2_rows.csv").read_bytes()


def packed(content):
    stream = io.BytesIO()
    with ZipFile(stream, "w") as z: z.writestr("T_SCHEDULE_T2.csv", content)
    return stream.getvalue()


def normalized(content=None):
    data = packed(content or fixture_bytes())
    import hashlib
    return normalize_export(data, {"source_url": URL, "sha256": hashlib.sha256(data).hexdigest()})


def classification():
    return json.loads((ROOT / "data/t2_aircraft_classification.json").read_text())


def real_z():
    return copy.deepcopy(next(r for r in normalized()["rows"] if r["unique_carrier"] == "UA" and r["period"] == "2019-Q4" and r["service_class"] == "Z"))


def grouped(rows):
    return capacity_panel(rows, classification(), [])["group_quarters"]


def test_actual_export_preserves_units_missing_exposure_and_unresolved_identity():
    rows = normalized()["rows"]
    r = real_z()
    assert r["values"]["AVL_SEAT_MILES_320"] == 6687551862
    assert r["values"]["HOURS_AIRBORNE_650"] == 103541
    assert r["values"]["AIR_DAYS_EQUIP_810"] == 10591
    assert r["values"]["AIRCRAFT_FUELS_921"] == 82258094
    assert r["values"]["NON_REV_ACRFT_HRS_AIRBORNE_620"] is None
    unknown = [r for r in rows if r["unique_carrier"] is None]
    assert len(unknown) == 2
    assert all("unresolved_reporting_service_identity_keep_source_row" in r["quality_flags"] for r in unknown)
    assert next(r for r in rows if r["aircraft_type"] == "822")["values"]["AIR_DAYS_EQUIP_810"] is None
    assert any(r["values"]["HOURS_AIRBORNE_650"] is not None and r["values"]["HOURS_AIRBORNE_650"] < 0 for r in rows)


def test_Z_selected_once_without_double_counting_service_or_system_totals():
    r = real_z()
    f = copy.deepcopy(r); f["service_class"] = "F"
    k = copy.deepcopy(r); k["service_class"] = "K"
    system = copy.deepcopy(r); system["reporting_region"] = "S"
    rows = grouped([r, f, k, system])
    assert len(rows) == 1 and rows[0]["source_rows"] == 1
    assert rows[0]["all_service_totals"]["AVL_SEAT_MILES_320"] == 6687551862
    assert rows[0]["targets"]["capacity_asm_per_assigned_day"] == pytest.approx(6687551862/10591)
    assert rows[0]["capacity_decomposition_difference"] == pytest.approx(0, abs=1e-9)
    assert rows[0]["calendar_days"] == 92
    assert not rows[0]["cross_schedule_model_transfer_eligible"]


def test_ratios_use_sums_and_target_specific_exposure_gates():
    a, b = real_z(), real_z()
    b["reporting_entity"] = "OTHER"; b["source_line"] += 1
    for r, asm, hours, days in ((a, 1000, 10, 0), (b, 10000, 100, 100)):
        r["values"]["AVL_SEAT_MILES_320"] = asm
        r["values"]["REV_PAX_MILES_140"] = asm/2
        r["values"]["REV_ACRFT_HRS_AIRBORNE_610"] = hours
        r["values"]["HOURS_AIRBORNE_650"] = hours
        r["values"]["AIR_DAYS_EQUIP_810"] = days
    out = grouped([a,b])[0]
    assert out["targets"]["capacity_asm_per_assigned_day"] == 110
    assert out["targets"]["reported_hours_per_assigned_day"] == 1.1
    a["values"]["AIR_DAYS_EQUIP_810"] = None
    out = grouped([a,b])[0]
    assert out["targets"]["capacity_asm_per_assigned_day"] is None
    assert out["targets"]["asm_per_revenue_hour"] == 100
    assert "AIR_DAYS_EQUIP_810:missing_quantity" in out["target_exclusions"]["capacity_asm_per_assigned_day"]
    a["values"]["AIR_DAYS_EQUIP_810"] = -1
    out = grouped([a,b])[0]
    assert out["all_service_totals"]["AIR_DAYS_EQUIP_810"] is None


def test_physical_identity_unknown_type_and_incomplete_quarter_gates():
    r = real_z(); r["values"]["AIR_DAYS_EQUIP_810"] = 1
    assert grouped([r])[0]["targets"]["reported_hours_per_assigned_day"] is None
    r = real_z(); r["months_reported"] = 2
    assert all(v is None for v in grouped([r])[0]["targets"].values())
    r = real_z(); unknown = copy.deepcopy(r); unknown["aircraft_type"] = "9999"
    assert all(v is None for v in grouped([r, unknown])[0]["targets"].values())
    r = real_z(); r["values"]["REV_PAX_MILES_140"] = r["values"]["AVL_SEAT_MILES_320"] + 1
    assert all(v is None for v in grouped([r])[0]["targets"].values())
    with pytest.raises(ValueError, match="Duplicate selected"): grouped([real_z(),real_z()])


def test_P52_comparison_reports_rounding_discrepancies_and_missing_values():
    r = real_z()
    p = {**r, "schedule": "P52", "values": {"TOTAL_AIR_HOURS": 103540,
        "AIR_DAYS_ASSIGN": 10590, "AIR_FUELS_ISSUED": 82.258090}}
    panel = capacity_panel([r], classification(), [p])
    compare = panel["P52_reconciliation"][0]["comparisons"]
    assert all(v["status"] == "within_P52_rounding_resolution" for v in compare.values())
    assert compare["AIRCRAFT_FUELS_921"]["difference"] == pytest.approx(4)
    p["values"]["AIR_DAYS_ASSIGN"] = 10000
    p["values"]["TOTAL_AIR_HOURS"] = None
    compare = capacity_panel([r], classification(), [p])["P52_reconciliation"][0]["comparisons"]
    assert compare["AIR_DAYS_EQUIP_810"]["status"] == "beyond_P52_rounding_resolution"
    assert compare["HOURS_AIRBORNE_650"]["status"] == "missing_quantity"


def test_reverse_P52_exposure_does_not_disappear_or_become_a_zero_T2_fleet():
    r=real_z()
    p={**r,"schedule":"P52","reporting_entity":"OTHER","source_line":12345,
        "values":{"TOTAL_AIR_HOURS":0,"AIR_DAYS_ASSIGN":20,"AIR_FUELS_ISSUED":0}}
    panel=capacity_panel([r],classification(),[p])
    assert panel["P52_without_T2_counts"]=={"positive_reported_exposure_or_activity":1}
    out=panel["group_quarters"][0]
    assert out["P52_rows_without_T2"]["source_lines"]==[12345]
    assert not out["P52_exposure_reconciled"]
    assert out["targets"]["capacity_asm_per_assigned_day"] is not None # Covered T2 rows only.
    p["values"]["AIR_DAYS_ASSIGN"]=None
    assert capacity_panel([r],classification(),[p])["P52_without_T2_counts"]=={"unknown_or_invalid_aircraft_quantities":1}


def test_strict_schema_duplicate_known_grain_and_nonfinite_values():
    rows = list(csv.DictReader(io.StringIO(fixture_bytes().decode())))
    known = next(r for r in rows if r["UNIQUE_CARRIER"] == "UA" and r["SERVICE_CLASS"] == "Z")
    def encode(items):
        s=io.StringIO(); w=csv.DictWriter(s,fieldnames=list(known));w.writeheader();w.writerows(items);return s.getvalue().encode()
    with pytest.raises(ValueError, match="Duplicate"): normalized(encode([known,known]))
    bad = {**known, "AIR_DAYS_EQUIP_810": "NaN"}
    with pytest.raises(ValueError, match="Nonfinite"): normalized(encode([bad]))
    with pytest.raises(ValueError, match="schema"): normalized(b"YEAR,QUARTER\n2019,4\n")


LOOKUPS = ("type_lookup_sha256","configuration_lookup_sha256","group_lookup_sha256")


@pytest.fixture
def preserved_lookups(require_local_cache):
    # build_fixture copies the three official BTS aircraft lookups named by the tracked
    # classification out of the local immutable raw store. Those bytes are not tracked in
    # Git, so a fresh clone skips the offline-build tests instead of failing on a missing file.
    classes=classification()
    for k in LOOKUPS:
        require_local_cache("data/raw/"+classes[k], "docs/utilization.md (preserved lookup SHA-256 values) and docs/t2_history.md",
            "preserved BTS aircraft lookup source ("+k+" in data/t2_aircraft_classification.json)")


def build_fixture(tmp_path, monkeypatch):
    # Synthetic acquisition must stay aligned with the declared fixture cutoff,
    # independently of the wall clock. Production metadata is never changed.
    class FixtureAcquisitionClock(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = cls(2026, 10, 9, 12, tzinfo=timezone.utc)
            return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)
    monkeypatch.setattr("app.source_store.datetime", FixtureAcquisitionClock)
    raw = tmp_path / "raw"
    export = tmp_path / "export.zip"; export.write_bytes(packed(fixture_bytes()))
    definitions=tmp_path / "definitions.txt"; definitions.write_text("Inspected T2 definitions fixture")
    entry={"path":str(export),"definition_path":str(definitions),"source_url":URL,"filters":{"year":"All Years","period":"All","fields":"All"}}
    source=preserve_export(entry,raw)
    classes=classification()
    for k in LOOKUPS:
        content,meta=read_source(classes[k]); preserve_source(content,meta["source_url"],meta["media_type"],raw)
    class_path=tmp_path / "classes.json"; class_path.write_text(json.dumps(classes))
    p52={"rows":[]}
    manifest={"panel_digests":{"P52":digest(p52)},"sources":[{"schedule":"P52","available_on":"2026-10-09"}]}
    manifest["snapshot_id"]=digest(manifest)
    form41=tmp_path / "financial";form41.mkdir();(form41/"manifest.json").write_text(json.dumps(manifest));(form41/"P52.json").write_text(json.dumps(p52))
    return source,form41,class_path,tmp_path/"output",raw


def test_immutable_offline_build_and_four_parquet_roundtrips(preserved_lookups, tmp_path, monkeypatch):
    source,form41,classes,dest,raw=build_fixture(tmp_path, monkeypatch)
    out=build_t2_history(source,form41,classes,"2026-10-09",dest,raw)
    folder=Path(out["path"])
    assert all((folder / n).exists() for n in ("rows.parquet","group_quarters.parquet","P52_reconciliation.parquet","P52_without_T2_Z.parquet"))
    assert build_t2_history(source,form41,classes,"2026-10-09",dest,raw)["snapshot_id"] == out["snapshot_id"]
    pointer=(dest/"latest.json").read_bytes()
    (folder/"normalized.json").write_text("{}")
    with pytest.raises(ValueError,match="Immutable"): build_t2_history(source,form41,classes,"2026-10-09",dest,raw)
    assert (dest/"latest.json").read_bytes() == pointer


def test_acquisition_and_upstream_checksum_gates(preserved_lookups, tmp_path, monkeypatch):
    source,form41,classes,dest,raw=build_fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError,match="not acquired"): build_t2_history(source,form41,classes,"2026-07-16",dest,raw)
    (form41/"P52.json").write_text('{"rows":[{}]}')
    with pytest.raises(ValueError,match="panel checksum"): build_t2_history(source,form41,classes,"2026-10-09",dest,raw)
