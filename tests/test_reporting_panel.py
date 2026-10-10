from copy import deepcopy
import csv
import io
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from app.evidence import digest
from app.form41_history import TABLE_CODES, normalize_export
from app.operating_history import METRICS
from app.reporting_panel import reporting_panel, build_reporting_panel, latest_snapshot
from app.source_store import byte_hash

FIXTURES = Path(__file__).parent / "fixtures"


def financial(schedule="P12", carrier="UA", entity="0A875", region="D", airline=19977):
    return {"schedule": schedule, "unique_carrier": carrier, "reporting_entity": entity,
        "reporting_region": region, "airline_id": airline, "period": "2013-Q1",
        "quality_flags": [], "values": {"OP_REVENUES": 30.0}, "source_line": 2}


def inputs():
    rows = []
    for month, asm, rpm, market in (("2013-01", 100, 80, "domestic"), ("2013-02", 200, 100, "latin_america"), ("2013-03", 300, 240, "domestic")):
        rows.append({"period": month, "carrier": "UA", "carrier_entity": "0A875", "carrier_wac": 1,
            "market": market, "scope": "DD", "source_sha256": "new", "published_on": "2014-03-21", **{metric: 0 for metric in METRICS},
            "asm": asm, "rpm": rpm})
    releases = [{"scope": scope, "period": month, "source_sha256": "new" if scope == "DD" else "fd"}
                for month in ("2013-01", "2013-02", "2013-03") for scope in ("DD", "FD")]
    return [{"source_sha256": "new", "rows": rows}], releases, [financial()], [financial("P52")], []


def test_exact_entity_join_preserves_geography_and_weighted_load_factor():
    args = inputs()
    # An older archive must not add duplicate capacity to a revised release.
    old = deepcopy(args[0][0]); old["source_sha256"] = "old"
    for row in old["rows"]:
        row["source_sha256"] = "old"; row["asm"] = 9999
    args[0].insert(0, old)
    result = reporting_panel(*args)
    row = result["entity_quarters"][0]
    assert row["traffic"]["asm"] == 600 and row["traffic"]["rpm"] == 420
    assert row["load_factor"] == .7  # Ratio of totals, not average (.8 + .5 + .8)/3.
    assert row["matched_reporting_identity"]
    assert row["analytical_market_crosswalk"][1]["asm"] == 200
    assert row["reporting_regions"] == ["D"]
    assert row["quality_flags"] == ["financial_region_spans_other_analytical_markets"]
    assert not row["cross_schedule_fit_eligible"]
    assert {r["normalized_archive_index"] for r in row["traffic_source_legs"]} == {1}


def test_same_market_does_not_join_wrong_entity_or_other_carrier():
    args = list(inputs())
    args[2] = [financial(entity="DIFFERENT"), financial(carrier="US", entity="0A875", airline=20355)]
    result = reporting_panel(*args)
    assert len(result["entity_quarters"]) == 3
    assert not any(r["matched_reporting_identity"] for r in result["entity_quarters"])
    traffic = next(r for r in result["entity_quarters"] if r["traffic_source_legs"])
    assert not traffic["P12_rows"]


def test_incomplete_months_remain_unknown_even_with_complete_scope_releases():
    args = inputs()
    args[0][0]["rows"].pop(1)
    row = reporting_panel(*args)["entity_quarters"][0]
    assert row["traffic"]["asm"] == 400
    assert row["observed_traffic_months"] == ["2013-01", "2013-03"]
    assert "traffic_entity_months_incomplete_no_zero_imputation" in row["quality_flags"]
    assert row["missing_scope_month_releases"] == []
    args[1].pop()
    row = reporting_panel(*args)["entity_quarters"][0]
    assert row["missing_scope_month_releases"] == ["FD:2013-03"]


def test_conflicting_certificate_ids_or_regions_withhold_matched_status():
    args = inputs()
    args[3][0]["airline_id"] = 999
    row = reporting_panel(*args)["entity_quarters"][0]
    assert not row["matched_reporting_identity"]
    assert "ambiguous_financial_reporting_boundary" in row["quality_flags"]
    args[3][0]["airline_id"] = 19977
    args[3][0]["reporting_region"] = "S"
    row = reporting_panel(*args)["entity_quarters"][0]
    assert not row["matched_reporting_identity"]


def fuel_rows():
    return [{"schedule": "P12A", "unique_carrier": "UA", "airline_id": 19977,
        "period": f"2013-{m:02d}", "values": {"TOTAL_GALLONS": g, "TOTAL_COST": c},
        "quality_flags": [], "source_line": m + 1}
        for m, g, c in ((1, 1., 1.), (2, 2., 4.), (3, 3., 7.))]


def test_certificate_fuel_stays_separate_and_price_weights_gallons():
    args = list(inputs()); args[4] = fuel_rows()
    result = reporting_panel(*args)
    fuel = result["certificate_fuel_quarters"][0]
    assert fuel["values"] == {"TOTAL_GALLONS": 6., "TOTAL_COST": 12.}
    assert fuel["reported_total_fuel_cost_per_gallon"] == 2.
    assert "P12A_rows" not in result["entity_quarters"][0]
    assert len(fuel["monthly_source_rows"]) == 3


def test_fuel_null_incomplete_and_flagged_quarters_withhold_ratio():
    args = list(inputs()); args[4] = fuel_rows()
    args[4][0]["values"]["TOTAL_COST"] = None
    fuel = reporting_panel(*args)["certificate_fuel_quarters"][0]
    assert fuel["values"]["TOTAL_COST"] is None
    assert fuel["nonnull_month_counts"]["TOTAL_COST"] == 2
    assert fuel["reported_total_fuel_cost_per_gallon"] is None
    args[4] = fuel_rows()[:2]
    fuel = reporting_panel(*args)["certificate_fuel_quarters"][0]
    assert fuel["values"]["TOTAL_GALLONS"] is None
    assert not fuel["complete_quarter"]
    args[4] = fuel_rows(); args[4][0]["quality_flags"] = ["negative_reported_fuel_quantity"]
    assert reporting_panel(*args)["certificate_fuel_quarters"][0]["reported_total_fuel_cost_per_gallon"] is None


def test_duplicate_selected_release_grain_and_fuel_month_fail():
    args = inputs(); args[1].append(args[1][0])
    with pytest.raises(ValueError, match="selected traffic release"):
        reporting_panel(*args)
    args = inputs(); args[0][0]["rows"].append(args[0][0]["rows"][0])
    with pytest.raises(ValueError, match="traffic grain"):
        reporting_panel(*args)
    args = list(inputs()); args[4] = fuel_rows() + [fuel_rows()[0]]
    with pytest.raises(ValueError, match="fuel month"):
        reporting_panel(*args)


def normalized_fixture(schedule):
    content = io.BytesIO()
    with ZipFile(content, "w") as z:
        z.writestr(f"{schedule}.csv", (FIXTURES / f"form41_{schedule.lower()}.csv").read_bytes())
    blob = content.getvalue()
    return normalize_export(blob, {"schedule": schedule, "sha256": byte_hash(blob),
        "source_url": f"https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ={TABLE_CODES[schedule]}"})


def save_upstream(path, manifest, files):
    manifest["snapshot_id"] = digest(manifest)
    path.mkdir()
    (path / "manifest.json").write_text(json.dumps(manifest))
    for name, content in files.items():
        (path / name).write_text(json.dumps(content))


def upstream(tmp_path):
    traffic, releases, *_ = inputs()
    operating = tmp_path / "traffic"; form41 = tmp_path / "financial"
    save_upstream(operating, {"normalized_digest": digest(traffic), "panel_digest": digest({"selected_releases": releases}),
        "cutoff": "2026-10-09", "sources": [{"retrieved_at_utc": "2026-10-09T01:00:00+00:00"}],
        "wac_source": {"retrieved_at_utc": "2026-10-09T01:00:00+00:00"}},
        {"normalized.json": traffic, "panel.json": {"selected_releases": releases}})
    panels = {s: normalized_fixture(s) for s in TABLE_CODES}
    save_upstream(form41, {"panel_digests": {s: digest(p) for s, p in panels.items()}, "cutoff": "2026-10-09",
        "sources": [{"available_on": "2026-10-09"}]}, {f"{s}.json": p for s, p in panels.items()})
    return operating, form41


def test_preserved_actual_financial_fixtures_roundtrip_and_rebuild(tmp_path):
    operating, form41 = upstream(tmp_path)
    target = tmp_path / "joined"
    first = build_reporting_panel(operating, form41, target)
    assert first["available_on"] == "2026-10-09"
    assert build_reporting_panel(operating, form41, target)["snapshot_id"] == first["snapshot_id"]
    path = latest_snapshot(target)
    panel = json.loads((path / "panel.json").read_text())
    latin = next(r for r in panel["entity_quarters"] if r["unique_carrier"] == "UA" and r["period"] == "2026-Q2" and r["reporting_regions"] == ["L"])
    assert latin["P12_rows"][0]["values"]["OP_REVENUES"] == pytest.approx(1364.22222)
    assert not latin["traffic_source_legs"] and not latin["matched_reporting_identity"]
    assert (path / "builder.py").read_bytes() == (Path(__file__).parents[1] / "app/reporting_panel.py").read_bytes()
    (path / "panel.json").write_text("{}")
    with pytest.raises(ValueError, match="Immutable"):
        build_reporting_panel(operating, form41, target)


def test_upstream_corruption_fails_without_publishing_pointer(tmp_path):
    operating, form41 = upstream(tmp_path)
    (operating / "normalized.json").write_text("[]")
    target = tmp_path / "joined"
    with pytest.raises(ValueError, match="panel checksum"):
        build_reporting_panel(operating, form41, target)
    assert not (target / "latest.json").exists()
