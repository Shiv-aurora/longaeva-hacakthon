from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.financial_history import build_history, financial_panel, history_status, normalize_facts, select_fact
from app.source_store import byte_hash, preserve_source, read_source

FIXTURE = Path(__file__).parent / "fixtures" / "sec_ual_2025_2026.json"


def input_facts():
    content = FIXTURE.read_bytes()
    return normalize_facts(json.loads(content), "UAL", byte_hash(content))


def quarter(panel, name):
    return next(r for r in panel["rows"] if r["quarter"] == name)


def test_actual_sec_q2_operating_and_differenced_cash_values():
    p = financial_panel(input_facts(), "UAL", "2026-07-16")
    q = quarter(p, "2026-Q2")["values"]
    assert q["operating_revenue"]["value_musd"] == 17672
    assert q["operating_income"]["value_musd"] == 1096
    assert q["fuel_expense"]["value_musd"] == 5110
    assert q["operating_expense"]["value_musd"] == 16576
    assert q["cash"]["value_musd"] == 10166
    # Independent reported H1/Q1 totals: never divide six-month flows into Q2.
    assert q["operating_cash_flow"]["value_musd"] == 6409 - 4799 == 1610
    assert q["cash_asset_purchases"]["value_musd"] == 3015 - 1672 == 1343
    assert q["free_cash_flow"]["value_musd"] == 267
    assert q["operating_cash_flow"]["method"] == "ytd_difference"
    assert q["operating_cash_flow"]["cross_filing"]
    assert len(q["operating_cash_flow"]["source_fact_ids"]) == 2


def test_actual_filing_boundary_and_no_future_comparative_leakage():
    facts = input_facts()
    assert not any(r["quarter"] == "2026-Q2" for r in financial_panel(facts, "UAL", "2026-07-15")["rows"])
    original = quarter(financial_panel(facts, "UAL", "2026-07-16"), "2026-Q2")["values"]["cash"]
    changed = deepcopy(facts)
    raw = next(r for r in changed if r["variable"] == "cash" and r["end"] == "2026-06-30")
    future = {**raw, "value_musd": 1, "published_on": "2027-01-01", "fact_id": raw["fact_id"] + ":future"}
    changed.append(future)
    assert quarter(financial_panel(changed, "UAL", "2026-07-16"), "2026-Q2")["values"]["cash"] == original
    assert quarter(financial_panel(changed, "UAL", "2027-01-01"), "2026-Q2")["values"]["cash"]["value_musd"] == 1


def test_ytd_requires_both_legs_and_does_not_impute_missing_prior_quarter():
    facts = [r for r in input_facts() if not (r["variable"] == "operating_cash_flow" and r["end"] == "2026-03-31")]
    q = quarter(financial_panel(facts, "UAL", "2026-07-16"), "2026-Q2")
    assert "operating_cash_flow" in q["missing_variables"]
    assert "free_cash_flow" not in q["values"]


def test_fiscal_frames_do_not_relabel_comparative_period_dates():
    blob = json.loads(FIXTURE.read_text())
    for concept in blob["facts"]["us-gaap"].values():
        for row in concept["units"]["USD"]:
            row["fy"] = 2099
            row["frame"] = "CY2099Q1"
    facts = normalize_facts(blob, "UAL", byte_hash(FIXTURE.read_bytes()))
    p = financial_panel(facts, "UAL", "2026-07-16")
    assert quarter(p, "2026-Q2")["values"]["operating_revenue"]["value_musd"] == 17672


def test_same_release_conflicting_contexts_are_excluded_and_reported():
    facts = input_facts()
    r = next(r for r in facts if r["variable"] == "cash" and r["end"] == "2026-06-30")
    facts.append({**r, "value_musd": 5, "fact_id": r["fact_id"] + ":conflict"})
    with pytest.raises(ValueError, match="Conflicting"):
        select_fact(facts, "cash", None, "2026-06-30", "2026-07-16")
    p = financial_panel(facts, "UAL", "2026-07-16")
    assert "cash" in quarter(p, "2026-Q2")["missing_variables"]
    assert any(issue["variable"] == "cash" for issue in p["issues"])


def test_source_bytes_and_metadata_are_verified_and_never_overwritten(tmp_path):
    content = FIXTURE.read_bytes()
    metadata = preserve_source(content, "https://data.sec.gov/example", "application/json", tmp_path)
    original = (tmp_path / metadata["sha256"] / "source.json").read_bytes()
    preserve_source(content, "https://data.sec.gov/example", "application/json", tmp_path)
    assert (tmp_path / metadata["sha256"] / "source.json").read_bytes() == original
    assert read_source(metadata["sha256"], tmp_path)[0] == content
    (tmp_path / metadata["sha256"] / "source.bin").write_bytes(b"modified")
    with pytest.raises(ValueError, match="checksum"):
        read_source(metadata["sha256"], tmp_path)


def test_offline_history_build_parquet_and_tamper_detection(tmp_path):
    raw = tmp_path / "raw"
    content = FIXTURE.read_bytes()
    meta = preserve_source(content, "https://data.sec.gov/example", "application/json", raw)
    sources = [{**meta, "entity": "UAL", "cik": 100517, "entity_name": "United Airlines Holdings, Inc."}]
    first = build_history(sources, "2026-07-16", tmp_path / "history", raw)
    second = build_history(sources, "2026-07-16", tmp_path / "history", raw)
    assert first == second
    assert history_status(tmp_path / "history")["snapshot_id"] == first["snapshot_id"]
    assert (Path(first["path"]) / "facts.parquet").is_file()
    panels = Path(first["path"]) / "panels.json"
    panels.write_text("[]")
    with pytest.raises(ValueError, match="checksum"):
        history_status(tmp_path / "history")
    with pytest.raises(ValueError, match="modified"):
        build_history(sources, "2026-07-16", tmp_path / "history", raw)


def test_absent_history_is_explicit_and_never_downloads(tmp_path):
    assert history_status(tmp_path)["status"] == "not_built"


def test_normalizer_rejects_wrong_entity_and_nonfinite_selected_facts():
    blob = json.loads(FIXTURE.read_text())
    with pytest.raises(ValueError, match="CIK"):
        normalize_facts(blob, "DAL", "a" * 64)
    blob["facts"]["us-gaap"]["OperatingIncomeLoss"]["units"]["USD"][0]["val"] = float("nan")
    with pytest.raises(ValueError, match="nonfinite"):
        normalize_facts(blob, "UAL", "a" * 64)
