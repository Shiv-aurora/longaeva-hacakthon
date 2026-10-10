"""Track record tests. Everything reads the committed inputs or small made-up figures; no network."""
import csv
from datetime import date, timedelta
import json

import numpy as np
import pytest

from app.lab import cost_clock as cc
from app.lab import stats
from app.lab import track_record as tr
from scripts.lab import build_track_record

STAMP = "2026-10-10T00:00:00Z"


@pytest.fixture(scope="module")
def world():
    return tr.World()


@pytest.fixture(scope="module")
def payload():
    return tr.build(generated_at=STAMP)


@pytest.fixture(scope="module")
def withheld():
    return tr.build(evaluate_test=False, generated_at=STAMP)


def made_up(**changes):
    """Chain inputs with round numbers: one dollar a gallon is 1.8 dollars a share after tax and profit sharing."""
    base = {"mid": 3.0, "assumed": {"usd_gal": 3.0, "source": "stated"}, "gallons_m": 1000.0,
            "per_share": {"tax_rate": 0.2, "diluted_shares_m": 400.0}, "profit_sharing_rate": 0.1,
            "curve": {"share": 0.25}, "management": {"share": 0.8},
            "misses": [{"miss": m} for m in (0.1, 0.3, -0.4)], "clock_errors": [{"error": e} for e in (0.02, 0.04, 0.10)]}
    return {**base, **changes}


def truncated(tmp_path, cutoff: date) -> tr.Paths:
    """Copies of the dated inputs with everything after `cutoff` removed."""
    full = tr.Paths()
    for name, column in (("quarterly", "filed_on"), ("fuel", "date")):
        with open(getattr(full, name), newline="") as stream:
            reader = csv.DictReader(stream)
            rows = [row for row in reader if row[column] <= cutoff.isoformat()]
            with open(tmp_path / f"{name}.csv", "w", newline="") as out:
                writer = csv.DictWriter(out, fieldnames=reader.fieldnames)
                writer.writeheader()
                writer.writerows(rows)
    records = [r for r in json.loads(full.guidance.read_text()) if r["issued_on"] <= cutoff.isoformat()]
    (tmp_path / "guidance.json").write_text(json.dumps(records))
    return tr.Paths(guidance=tmp_path / "guidance.json", quarterly=tmp_path / "quarterly.csv", fuel=tmp_path / "fuel.csv")


# ------------------------------------------------------------------ the chain's arithmetic

def test_calls_on_made_up_figures():
    steps = tr.point_calls(made_up(), realised=3.5, late=0.2)
    calls = steps["calls"]
    assert steps["usd_per_share_per_usd_gal"] == pytest.approx(1.8)
    assert steps["surprise_musd"] == pytest.approx(500.0) and steps["surprise_per_share"] == pytest.approx(0.9)
    assert calls["guidance_mid"] == 3.0
    assert calls["costs_only"] == pytest.approx(3.0 - 0.9)
    assert calls["management"] == pytest.approx(3.0 - 0.2 * 0.9)
    assert calls["fitted"] == pytest.approx(3.0 - 0.75 * 0.9)
    assert calls["habit"] == pytest.approx(calls["fitted"] + 0.1)                      # the middle earlier miss
    assert calls["late_moves"] == pytest.approx(3.0 - (0.75 * 0.3 + 0.2) * 1.8)        # the late 20 cents get nothing back
    assert calls["clock_bias"] == pytest.approx(3.0 - 0.75 * (0.5 + 0.04) * 1.8)       # the clock's middle miss is added
    plain = tr.point_calls(made_up(), 3.5, 0.2, with_profit_sharing=False)
    assert plain["surprise_per_share"] == pytest.approx(1.0) and plain["calls"]["costs_only"] == pytest.approx(2.0)


def test_cheaper_fuel_raises_every_call_and_no_surprise_leaves_the_midpoint():
    up = tr.point_calls(made_up(), realised=2.5, late=0.0)["calls"]
    assert up["costs_only"] > up["fitted"] > up["management"] > up["guidance_mid"]
    flat = tr.point_calls(made_up(management=None), realised=3.0, late=0.0)["calls"]
    assert flat["costs_only"] == flat["fitted"] == flat["late_moves"] == 3.0 and flat["management"] is None


def test_habit_and_clock_bias_wait_for_three_earlier_quarters_and_bias_needs_a_stated_price():
    young = made_up(misses=[{"miss": 0.5}, {"miss": 0.7}], clock_errors=[{"error": 0.1}])
    calls = tr.point_calls(young, 3.5, 0.0)["calls"]
    assert calls["habit"] == calls["fitted"] == calls["clock_bias"]
    assert tr.spread(young["misses"]) is None
    from_clock = made_up(assumed={"usd_gal": 3.0, "source": "cost_clock"})
    assert tr.point_calls(from_clock, 3.5, 0.0)["calls"]["clock_bias"] == tr.point_calls(from_clock, 3.5, 0.0)["calls"]["fitted"]


def test_range_is_the_earlier_misses_centred_on_the_call():
    residuals = tr.spread([{"miss": m} for m in (0.1, 0.3, -0.4, 1.0)])
    assert np.median(residuals) == pytest.approx(0.0)
    call = tr.with_range(2.0, residuals)
    assert call["p50"] == 2.0 and call["p10"] < 2.0 < call["p90"]
    assert sorted(call["members"]) == pytest.approx(sorted(2.0 + residuals))
    assert tr.crps(call, 2.2) == pytest.approx(stats.crps_ensemble(call["members"], 2.2))
    point = tr.with_range(2.0, None)
    assert point["p10"] is None and tr.crps(point, 2.2) == pytest.approx(0.2)


# ------------------------------------------------------------------ no look-ahead

def test_known_refuses_anything_dated_after_its_cutoff(world):
    known = tr.Known(world, date(2024, 3, 31))
    assert known.report("2023Q4", "check")["quarter"] == "2023Q4"                      # published 2024-01-22
    with pytest.raises(tr.LookAhead):
        known.report("2024Q1", "check")                                                # published 2024-04-16
    later = next(r for r in world.guidance if r["issued_on"] > "2024-03-31")
    with pytest.raises(tr.LookAhead):
        known.record(later, "check")
    with pytest.raises(tr.LookAhead):
        known.clock("2024Q1", date(2024, 4, 1), "check")
    with pytest.raises(tr.LookAhead):
        known.clock("2024Q1", date(2024, 3, 31), "check", model_as_of=date(2024, 5, 1))
    assert all(m["filed_on"] <= known.cutoff for m in known.misses("2025Q4"))
    assert [m["quarter"] for m in known.misses("2025Q4")] == ["2022Q4", "2023Q1", "2023Q2", "2023Q3"]
    assert all(e["filed_on"] < known.cutoff for e in known.clock_errors())


def test_no_replay_row_reads_a_value_filed_after_its_cutoff(world, payload):
    filed = {row["quarter"]: row["filed_on"].isoformat() for row in world.reports}
    issued = {r["id"]: r["issued_on"] for r in world.guidance}
    assert len(payload["quarters"]) == 14
    for row in payload["quarters"]:
        cutoff = row["forecast_as_of"]
        assert cutoff == cc.quarter_bounds(row["period"])[1].isoformat() < row["results_on"] == filed[row["period"]]
        assert row["uses"] and all(use["dated"] <= cutoff for use in row["uses"])
        # The dates are checked against the files themselves, not only against what the row logged.
        for use in row["uses"]:
            if "results for " in use["what"]:
                assert filed[use["what"].split("results for ")[1]] == use["dated"] <= cutoff
            if "guidance record " in use["what"]:
                assert issued[use["what"].split("guidance record ")[1]] == use["dated"] <= cutoff
        for key in ("gallons_quarter", "tax_and_shares_quarter", "recovery_curve_fitted_through"):
            assert filed[row[key]] <= cutoff
        assert cc.quarter_index(row["recovery_curve_fitted_through"]) < cc.quarter_index(row["period"])
        assert issued[row["guidance_evidence_id"]] == row["guided_on"] <= cutoff
        assert row["earlier_misses_n"] == sum(1 for other in payload["quarters"] if filed[other["period"]] <= cutoff
                                               and other["period"] < row["period"])


def test_a_report_dated_after_the_cutoff_stops_the_forecast(tmp_path):
    """The guard is live: move one report it needs past the forecast date and the row fails."""
    full = tr.Paths()
    with open(full.quarterly, newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
    for row in rows:
        if row["period_end"] == "2025-06-30":      # the gallons 2026Q2 reads; really published 2025-07-16
            row["filed_on"] = "2026-07-01"
    path = tmp_path / "quarterly.csv"
    with open(path, "w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=reader.fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    world = tr.World(tr.Paths(quarterly=path))
    with pytest.raises(tr.LookAhead, match="2025Q2"):
        tr.forecast(world, "2026Q2", date(2026, 6, 30), date(2026, 7, 15))


@pytest.mark.parametrize("quarter", ["2024Q3", "2026Q2"])
def test_a_replay_row_is_unchanged_when_everything_after_its_cutoff_is_deleted(world, tmp_path, quarter):
    cutoff, results_on = cc.quarter_bounds(quarter)[1], world.by_quarter[quarter]["filed_on"]
    full = tr.forecast(world, quarter, cutoff, results_on)
    cut = tr.forecast(tr.World(truncated(tmp_path, cutoff)), quarter, cutoff, results_on)
    assert cut["computed"] and cut["uses"] == full["uses"]
    assert cut["fuel"]["usd_gal"] == pytest.approx(full["fuel"]["usd_gal"], abs=1e-12)
    for treatment in ("primary", "tax_only"):
        for name in tr.VARIANTS:
            ours, theirs = cut[treatment]["ranges"][name], full[treatment]["ranges"][name]
            assert [ours[k] for k in ("p10", "p50", "p90")] == pytest.approx([theirs[k] for k in ("p10", "p50", "p90")], abs=1e-12)
        assert cut[treatment]["calls"] == pytest.approx(full[treatment]["calls"], abs=1e-12)


def test_validate_rejects_a_row_that_reads_past_its_forecast_date(payload):
    broken = json.loads(json.dumps(payload))
    broken["quarters"][5]["uses"][0]["dated"] = broken["quarters"][5]["results_on"]
    with pytest.raises(ValueError, match="after its forecast date"):
        tr.validate(broken)
    with pytest.raises(ValueError, match="lacks"):
        tr.validate({k: v for k, v in payload.items() if k != "variants_tried"})
    with pytest.raises(ValueError, match="fixture"):
        tr.validate({**payload, "fixture": True})


# ------------------------------------------------------------------ selection and scores

def test_the_method_is_chosen_without_the_test_years(payload, withheld):
    assert withheld["test_split"] == "withheld" and withheld["scores"] == [] and "test" not in withheld["scores_by_split"]
    assert all(row["split"] != "test" for row in withheld["quarters"]) and withheld["delta_check"] is None
    assert withheld["selection"] == payload["selection"]
    assert withheld["scores_by_split"]["select"] == payload["scores_by_split"]["select"]
    early = [row for row in payload["quarters"] if row["split"] != "test"]
    assert early == withheld["quarters"]
    assert payload["selection"]["chosen"] in tr.VARIANTS and payload["selection"]["n_by_split"] == {"fit": 0, "select": 4}
    table = payload["selection"]["table"]
    assert table[payload["selection"]["chosen"]]["crps"] == min(v["crps"] for v in table.values())
    assert len(payload["variants_tried"]) == len(tr.VARIANTS) + 1
    assert sum("Chosen" in line for line in payload["variants_tried"]) == 1
    # Unchosen variants are shown for the quarters they were chosen on and never for the test quarters.
    assert all(("variants" in row) == (row["split"] != "test") for row in payload["quarters"])


def test_scores_are_the_rows_scored(payload):
    assert payload["scores"] == payload["scores_by_split"]["test"]
    assert [s["model"] for s in payload["scores"]] == list(tr.MODELS)
    for split, table in payload["scores_by_split"].items():
        rows = [row for row in payload["quarters"] if row["split"] == split]
        for score in table:
            errors = [row["error"][score["model"]] for row in rows if row["error"][score["model"]] is not None]
            assert score["n"] == len(errors)
            if not errors:
                assert score["mae"] is None and score["crps"] is None
                continue
            assert score["mae"] == pytest.approx(np.mean(np.abs(errors)), abs=1e-5)
            if score["model"] != "fitted":
                assert score["crps"] == score["mae"] and score["inside_p10_p90"] is None
            else:
                assert score["crps"] == pytest.approx(np.mean([row["crps_fitted"] for row in rows]), abs=1e-5)
                assert score["crps"] <= score["mae"] + 1e-9
                inside = [row["inside_p10_p90"] for row in rows if row["inside_p10_p90"] is not None]
                assert score["n_with_range"] == len(inside)
                assert score["inside_p10_p90"] == (pytest.approx(np.mean(inside)) if inside else None)
    counts = {split: table[0]["n"] for split, table in payload["scores_by_split"].items()}
    assert counts == {"fit": 0, "select": 4, "test": 10}
    assert next(s for s in payload["scores"] if s["model"] == "management")["n"] == 1


def test_rows_follow_the_chain(payload):
    for row in payload["quarters"]:
        calls, mid = row["calls"], (row["guidance_low"] + row["guidance_high"]) / 2
        assert calls["guidance_mid"] == pytest.approx(mid) == pytest.approx(row["guidance_mid"])
        assert row["fuel_surprise_usd_gal"] == pytest.approx(row["fuel_realized"] - row["fuel_assumed"], abs=2e-6)
        per_share = row["fuel_surprise_usd_gal"] * row["gallons_m"] * (1 - row["tax_rate"]) / row["diluted_shares_m"]
        assert row["fuel_surprise_per_share_without_profit_sharing"] == pytest.approx(per_share, abs=1e-5)
        assert row["fuel_surprise_per_share"] == pytest.approx(per_share * (1 - row["profit_sharing_rate"]), abs=1e-5)
        assert calls["costs_only"] == pytest.approx(mid - row["fuel_surprise_per_share"], abs=1e-5)
        assert row["calls_without_profit_sharing"]["costs_only"] == pytest.approx(mid - per_share, abs=1e-5)
        assert (calls["management"] is None) == (row["management_share"] is None)
        if calls["management"] is not None:
            assert calls["management"] == pytest.approx(mid - (1 - row["management_share"]) * row["fuel_surprise_per_share"], abs=1e-5)
        fitted = calls["fitted"]
        assert (fitted["p10"] is None) == (row["earlier_misses_n"] < tr.MIN_HISTORY)
        if fitted["p10"] is not None:
            assert fitted["p10"] <= fitted["p50"] <= fitted["p90"]
        assert row["fuel_assumed_source"] in ("stated", "cost_clock")
        assert (row["fuel_assumed_evidence_id"] is None) == (row["fuel_assumed_source"] == "cost_clock")
        assert 0 < row["recovery_share"] < 1 and row["actual_eps"] - mid == pytest.approx(row["miss_against_midpoint"])
    assert [r["period"] for r in payload["quarters"] if r["fuel_assumed_source"] == "stated"] == [
        "2022Q4", "2023Q1", "2023Q2", "2023Q3", "2026Q2"]
    q = {r["period"]: r for r in payload["quarters"]}
    assert q["2023Q1"]["guided_on"] == "2023-03-13" and q["2023Q1"]["guidance_low"] == -1.0      # the revised range
    assert q["2026Q2"]["management_share"] == pytest.approx(0.45) and "2023Q4" not in q


# ------------------------------------------------------------------ side checks

def test_fuel_price_test_adds_up(payload):
    check = payload["fuel_price_test"]
    clean = [row for row in check["quarters"] if not row["pandemic"]]
    assert check["n"] == len(clean) == sum(part["n"] for part in check["by_split"].values())
    assert check["mae_guided"] == pytest.approx(np.mean([abs(r["error_guided"]) for r in clean]), abs=1e-5)
    assert check["mae_marked"] == pytest.approx(np.mean([abs(r["error_marked"]) for r in clean]), abs=1e-5)
    assert check["with_pandemic_quarters"]["n"] == len(check["quarters"]) > check["n"]
    for row in check["quarters"]:
        assert row["error_guided"] == pytest.approx(row["fuel_guided"] - row["fuel_reported"], abs=2e-6)
        assert row["stated_on"] <= cc.quarter_bounds(row["period"])[1].isoformat()


def test_delta_check_is_arithmetic_on_the_delta_file(payload):
    data = json.loads(tr.Paths().dal_check.read_text())
    guide, actual = data["guidance_sep_quarter"], data["actuals_sep_quarter"]
    check = payload["delta_check"]
    tax = data["derived"]["actuals_jun_quarter"]["adj_effective_tax_rate_pct"] / 100
    per_share = ((actual["fuel_price_adj_usd_gal"] - guide["fuel_price_assumed_usd_gal"]) * actual["prior_year"]["fuel_gallons_m"]
                 * (1 - tax) / data["actuals_jun_quarter"]["diluted_shares_m"])
    assert check["fuel_surprise_per_share"] == pytest.approx(per_share, abs=1e-5)
    assert check["calls"]["guidance_mid"] == 2.25 and check["calls"]["management"] is None
    assert check["calls"]["costs_only"] == pytest.approx(2.25 - per_share, abs=1e-5)
    fitted = check["calls"]["fitted"]
    assert fitted["p10"] <= fitted["p50"] <= fitted["p90"] and check["calls"]["costs_only"] < fitted["p50"] < 2.25
    assert check["actual_eps"] == actual["adj_eps_diluted"] and check["reported_on"] == actual["reported_on"]
    assert check["implied_recovery_share"] == pytest.approx(1 - (2.25 - actual["adj_eps_diluted"]) / per_share, abs=1e-5)
    assert check["evidence_ids"] and all(i.startswith("dal-") for i in check["evidence_ids"] + check["other_record_ids"])
    records = {record["id"] for record in data["evidence"]} if isinstance(data["evidence"], list) else set(data["evidence"])
    assert set(check["evidence_ids"] + check["other_record_ids"]) <= records


def test_results_day_test_counts_and_dates(payload):
    day = payload["results_day_test"]
    by = day["by_split"]
    assert by["fit"]["n"] == 0 and by["select"]["n"] == 4 and by["test"]["n"] == 10 and by["all"]["n"] == len(day["points"]) == 14
    assert by["select"]["lo"] is None and by["test"]["lo"] <= by["test"]["slope"] <= by["test"]["hi"]
    q = {row["period"]: row for row in payload["quarters"]}
    for point in day["points"]:
        row = q[point["period"]]
        assert point["results_on"] == row["results_on"] < point["next_session"]
        assert point["gap_usd_per_share"] == pytest.approx(row["calls"]["fitted"]["p50"] - row["guidance_mid"], abs=2e-6)
        assert abs(point["hedged_return"]) < 0.5
    test = [p for p in day["points"] if p["split"] == "test"]
    hits = sum(np.sign(p["gap_usd_per_share"]) == np.sign(p["hedged_return"]) for p in test)
    assert by["test"]["n_hits"] == hits and by["test"]["hit_rate"] == pytest.approx(hits / len(test))
    slope = np.polyfit([p["gap_usd_per_share"] for p in test], [p["hedged_return"] for p in test], 1)[0]
    assert by["test"]["slope"] == pytest.approx(slope, abs=1e-5)


# ------------------------------------------------------------------ the output file

def test_build_is_deterministic_valid_and_plain(payload):
    assert tr.build(generated_at=STAMP) == payload
    tr.validate(payload)
    for key in ("generated_at", "as_of", "inputs", "headline", "plain_summary", "limits", "quarters", "scores", "fuel_price_test",
                "delta_check", "results_day_test", "variants_tried", "method"):
        assert payload[key] not in (None, "", [])
    assert isinstance(payload["plain_summary"], list) and payload["plain_summary"][0] == payload["headline"]
    assert "cannot be re-run" in payload["method"]["what_is_replayed"]
    assert all(not item["path"].startswith("/") and len(item["sha256"]) == 64 for item in payload["inputs"])
    assert payload["profit_sharing"]["verified_quote"] is False and payload["scores_without_profit_sharing"]
    text = json.dumps(payload)
    assert "/Users/" not in text and '"fixture"' not in text
    assert payload["as_of"] == max(payload["as_of"], *(row["results_on"] for row in payload["quarters"]))


def test_contract_keys_survive(payload):
    row = payload["quarters"][-1]
    assert {"period", "guided_on", "guidance_low", "guidance_high", "fuel_assumed", "fuel_realized", "actual_eps", "split", "calls"} <= set(row)
    assert set(row["calls"]) == {"guidance_mid", "costs_only", "management", "fitted"} and set(row["calls"]["fitted"]) == {"p10", "p50", "p90"}
    assert {"model", "mae", "crps", "inside_p10_p90", "n"} <= set(payload["scores"][0])
    assert {"n", "mae_guided", "mae_marked"} <= set(payload["fuel_price_test"])
    assert {"period", "guidance_low", "guidance_high", "actual_eps", "calls", "evidence_ids"} <= set(payload["delta_check"])


def test_script_writes_what_it_validated_and_will_not_publish_a_withheld_build(tmp_path, payload, monkeypatch, capsys):
    out = tmp_path / "track_record.json"
    build_track_record.write(payload, out)
    assert json.loads(out.read_text()) == payload
    monkeypatch.setattr("sys.argv", ["build_track_record.py", "--no-test"])
    with pytest.raises(SystemExit):
        build_track_record.main()
    capsys.readouterr()


def test_published_file_is_the_rebuild_of_its_inputs(payload):
    if not tr.OUTPUT.is_file():
        pytest.skip("app/static/data/track_record.json has not been built")
    published = json.loads(tr.OUTPUT.read_text())
    tr.validate(published)
    assert published["test_split"].startswith("computed once")
    if published["inputs"] != payload["inputs"]:
        pytest.skip("an input changed since app/static/data/track_record.json was built; rebuild it")
    assert {**published, "generated_at": STAMP} == payload
