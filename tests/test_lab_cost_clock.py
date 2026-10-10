"""Cost clock tests. Simulations are seeded; the build tests read only the committed inputs; no network."""
import copy
from datetime import date, timedelta
import hashlib
import json

import numpy as np
import pytest

from app.lab import cost_clock as cc
from app.lab import market_data
from scripts.lab import build_cost_clock

STAMP = "2026-10-10T00:00:00Z"
TIERS = ("Profit sharing is the sum of three tiers: about 3.6% of adjusted pre-tax earnings up to and above a 6.9% "
         "margin, about 6.0% up to $2.5 billion and about 12.0% above it, and about 3.3% of earnings above the prior year's.")


# ------------------------------------------------------------------ simulators

def simulate_spot(seed, first=date(2012, 6, 1), last=date(2026, 7, 31), vol=0.02):
    """Weekday prints of a slowly mean-reverting log price around $2.20."""
    rng = np.random.default_rng(seed)
    days = [d for d in (first + timedelta(days=i) for i in range((last - first).days + 1)) if d.weekday() < 5]
    level, x = np.empty(len(days)), 0.0
    for i in range(len(days)):
        x = 0.995 * x + rng.normal(0, vol)
        level[i] = x
    return days, 2.2 * np.exp(level)


def simulate_rows(dates, values, basis, weights, noise, seed, spec="lagged_window", first="2013Q1", last="2023Q4"):
    """Quarterly reports whose price is basis + weights . the specification's window means + noise."""
    rng = np.random.default_rng(seed)
    quarters = [cc.quarter_label(i) for i in range(cc.quarter_index(first), cc.quarter_index(last) + 1)]
    x = cc.design(spec, quarters, cc._ordinals(dates), values)
    price = basis + x @ np.asarray(weights, dtype=float) + rng.normal(0, noise, len(quarters))
    return [{"quarter": q, "period_end": cc.quarter_bounds(q)[1], "filed_on": cc.quarter_bounds(q)[1] + timedelta(days=20),
             "price": float(p)} for q, p in zip(quarters, price)]


@pytest.fixture(scope="module")
def world():
    dates, values = simulate_spot(11)
    rows = simulate_rows(dates, values, 0.25, (0.4, 0.6), 0.01, 12)
    return dates, values, cc._ordinals(dates), rows


@pytest.fixture(scope="module")
def payload():
    return cc.build(generated_at=STAMP)


# ------------------------------------------------------------------ the regression

def test_recovers_known_basis_slope_and_lag(world):
    dates, values, ords, rows = world
    model = cc.fit_cost_model(rows, ords, values, "lagged_window")
    summary = model.summary()
    assert model.n == 44 and model.quarters[0] == "2013Q1"
    assert abs(model.basis - 0.25) < 0.03
    assert abs(model.slope - 1.0) < 0.015
    assert abs(float(model.lag_days()) - 18.0) < 3.0          # 30 days x 0.6 / (0.4 + 0.6)
    assert summary["ci"][0] < 1.0 < summary["ci"][1] and summary["ci"][1] - summary["ci"][0] < 0.05
    assert summary["lag_days_ci"][0] < 18.0 < summary["lag_days_ci"][1]
    assert abs(model.sigma - 0.01) < 0.004


def test_same_quarter_specification_recovers_a_plain_relationship():
    dates, values = simulate_spot(21)
    rows = simulate_rows(dates, values, 0.31, (0.95,), 0.02, 22, spec="same_quarter")
    model = cc.fit_cost_model(rows, cc._ordinals(dates), values, "same_quarter")
    assert abs(model.basis - 0.31) < 0.05 and abs(model.slope - 0.95) < 0.02 and float(model.lag_days()) == 0.0


def test_choice_between_specifications_uses_fit_and_select_only(world):
    dates, values, ords, rows = world
    later = simulate_rows(dates, values, 5.0, (0.0, 0.0), 0.5, 13, first="2024Q1", last="2025Q4")   # nonsense test years
    picked, with_later = cc.choose(rows, ords, values), cc.choose(rows + later, ords, values)
    assert picked["spec"] == "lagged_window"
    assert picked["by_spec"]["lagged_window"]["select_mae"] < picked["by_spec"]["same_quarter"]["select_mae"]
    assert picked["by_spec"] == with_later["by_spec"] and picked["walk_forward"] == with_later["walk_forward"]
    assert picked["by_spec"]["same_quarter"]["select_n"] == 8          # 2020Q1-2021Q4 left out of the 16
    assert all(not cc.in_pandemic(row["quarter"]) for row in cc.final_rows(rows, 2013))


def test_window_means_match_the_shared_loader():
    dates, values = market_data.load_fuel_daily()
    shared = {row["period"]: row for row in market_data.quarterly_mean_fuel("2019-01-01", "2019-12-31")}
    for quarter, row in shared.items():
        mean, count = cc.window_mean(cc._ordinals(dates), values, *cc.quarter_bounds(quarter))
        assert count == row["n_prints"] and mean == pytest.approx(row["usd_per_gallon"])
    start, end = cc.windows("lagged_window", "2019Q2")[1]
    assert (start, end) == (date(2019, 3, 2), date(2019, 5, 31))
    assert cc.windows("previous_quarter", "2019Q1")[1] == (date(2018, 10, 1), date(2018, 12, 31))


# ------------------------------------------------------------------ the predictive range

def test_prediction_uses_only_prints_up_to_its_as_of_date(world):
    dates, values, ords, rows = world
    model = cc.fit_cost_model(rows, ords, values, "lagged_window")
    as_of = date(2024, 8, 9)
    full = cc.predicted_price_paid("2024Q3", dates, values, as_of=as_of, model=model, n_draws=800)
    cut = [(d, v) for d, v in zip(dates, values) if d <= as_of]
    truncated = cc.predicted_price_paid("2024Q3", [d for d, _ in cut], [v for _, v in cut], model=model, n_draws=800)
    tampered = np.where(np.array([d > as_of for d in dates]), 99.0, values)
    assert full == truncated == cc.predicted_price_paid("2024Q3", dates, tampered, as_of=as_of, model=model, n_draws=800)
    assert full["last_print"] == "2024-08-09" and not full["complete"] and full["weekdays_remaining"] == 36
    assert full["p10"] < full["p50"] < full["p90"]


def test_prediction_refuses_a_model_estimated_on_later_reports(world):
    dates, values, ords, rows = world
    model = cc.fit_cost_model(rows, ords, values, "lagged_window")
    assert model.last_filed_on == date(2024, 1, 20)
    with pytest.raises(ValueError, match="not before the as-of date"):
        cc.predicted_price_paid("2023Q4", dates, values, as_of=date(2024, 1, 20), model=model)
    cc.predicted_price_paid("2023Q4", dates, values, as_of=date(2024, 1, 21), model=model, n_draws=100)


def test_range_narrows_as_the_quarter_fills_and_ends_on_the_model(world):
    dates, values, ords, rows = world
    model = cc.fit_cost_model(rows, ords, values, "lagged_window")
    runs = [cc.predicted_price_paid("2024Q3", dates, values, as_of=day, model=model, n_draws=2000)
            for day in (date(2024, 7, 5), date(2024, 8, 15), date(2024, 10, 4))]
    widths = [run["p90"] - run["p10"] for run in runs]
    assert widths[0] > widths[1] > widths[2] > 0
    done = runs[-1]
    assert done["complete"] and done["weekdays_remaining"] == 0 and done["daily_volatility"] is None
    exact = float(model.predict(cc.design("lagged_window", ["2024Q3"], ords, values)[0]))
    assert done["point"] == pytest.approx(exact) and abs(done["p50"] - exact) < 0.002
    assert widths[2] < 6 * model.sigma                      # only coefficient and residual uncertainty is left
    assert runs[0] == cc.predicted_price_paid("2024Q3", dates, values, as_of=date(2024, 7, 5), model=model, n_draws=2000)
    assert runs[0]["p50"] != cc.predicted_price_paid("2024Q3", dates, values, as_of=date(2024, 7, 5), model=model,
                                                     n_draws=2000, seed=5)["p50"]


def test_position_against_a_range():
    assert [cc.position(v, 3.0, 4.0) for v in (2.9, 3.0, 3.5, 4.0, 4.1)] == [
        "below_range", "inside_range", "inside_range", "inside_range", "above_range"]


# ------------------------------------------------------------------ wording

def test_headline_is_neutral_when_the_slope_interval_spans_zero():
    fit = {"airline": "UAL", "ci": [-0.2, 0.3], "slope": 0.05, "n": 16, "sample": "2018Q1 to 2023Q4", "lag_days": 0.0,
           "basis_usd_gal": 2.0, "resid_sd_usd_gal": 0.3}
    text = cc.headline(fit, None)
    assert "No measurable link" in text and "includes zero" in text and "follows" not in text
    clear = cc.headline({**fit, "ci": [0.9, 1.1], "slope": 1.0, "lag_days": 18.0, "in_sample_mae": 0.03}, {"mae": 0.07, "n": 10})
    assert "follows" in clear and "18 days" in clear and "by 3 cents" in clear and "by 7 cents" in clear
    assert "No measurable" not in clear and "it never saw" not in cc.headline({**fit, "ci": [0.9, 1.1]}, None)


def test_unrelated_prices_give_a_neutral_headline():
    dates, values = simulate_spot(31)
    rows = simulate_rows(dates, values, 2.5, (0.0, 0.0), 0.3, 33)
    summary = cc.fit_cost_model(rows, cc._ordinals(dates), values, "same_quarter").summary()
    assert summary["ci"][0] < 0.0 < summary["ci"][1]
    assert "No measurable link" in cc.headline(summary, None)


# ------------------------------------------------------------------ management's arithmetic

def test_management_arithmetic_on_made_up_figures(world):
    dates, values, ords, rows = world
    model = cc.fit_cost_model(rows, ords, values, "lagged_window")
    literature = {"entries": [{"id": "ual-2026q2-investor-update", "url": "https://example.test/doc", "notes": TIERS, "numbers": [
        {"label": "Increase in third-quarter fuel cost since the beginning of July", "value": 500, "quote": "increased $500 million"},
        {"label": "The same increase expressed per share", "value": 1.0, "quote": "or $1.00"},
        {"label": "Assumed all-in average fuel price, 2026Q3", "value": 3.5, "quote": "approximately $3.50 as of July 14, 2026"}]}]}
    quarters = [{"quarter": q, "fuel_gallons_m": g, "diluted_shares_m": 400.0, "pretax_income_musd": 1000.0, "net_income_musd": 750.0}
                for q, g in (("2025Q3", 1250.0), ("2025Q4", 1100.0), ("2026Q1", 1000.0), ("2026Q2", 1200.0))]
    check = cc.management_check(literature, quarters, dates, values, model)
    assert check["computed"] and check["claimed_usd_m"] == 500 and check["claimed_eps"] == 1.0
    assert check["implied_price_change_usd_gal"] == {"prior_year_third_quarter": 0.4, "latest_quarter": pytest.approx(500 / 1200)}
    share = check["per_share"]
    assert share["tax"]["rate"] == pytest.approx(0.25) and share["profit_sharing"]["rate"] == pytest.approx(0.189)
    assert share["eps_with_tax_only"] == pytest.approx(500 * 0.75 / 400)
    assert share["eps_with_tax_and_profit_sharing"] == pytest.approx(500 * 0.75 * 0.811 / 400)
    assert share["after_tax_share_implied_by_claim"] == pytest.approx(0.8) and not share["consistent"]
    assert share["profit_sharing_rate_needed_at_this_tax_rate"] == pytest.approx(1 - 0.8 / 0.75)
    july = [(d, float(v)) for d, v in zip(dates, values) if date(2026, 7, 1) <= d <= date(2026, 7, 14)]
    move = check["price_move"]
    assert move["start"]["date"] == "2026-07-01" and move["end"]["date"] == "2026-07-14"
    assert move["spot_change_usd_gal"] == pytest.approx(july[-1][1] - july[0][1])
    estimate = move["cost_clock_quarter_estimate"]
    assert check["inputs"] == {"gallons_m": 1250.0, "price_change_usd_gal": estimate["change_usd_gal"], "tax_rate": pytest.approx(0.25),
                               "diluted_shares_m": 400.0, "profit_sharing_rate": pytest.approx(0.189)}
    assert check["reproduced_usd_m"] == pytest.approx(1250.0 * estimate["change_usd_gal"])
    assert check["reproduced_eps"] == pytest.approx(check["reproduced_usd_m"] * 0.75 * 0.811 / 400)
    assert len(check["cases"]) == 4
    assert cc.management_check({"entries": []}, quarters, dates, values, model)["computed"] is False


def test_profit_sharing_tiers_and_dates_are_read_from_text():
    entry = {"entries": [{"id": "ual-2026q2-investor-update", "notes": TIERS}]}
    assert cc.profit_sharing_rate(entry)["tiers"] == pytest.approx({"base": 0.036, "top_tier": 0.12, "above_prior_year": 0.033})
    assert cc.profit_sharing_rate({"entries": [{"id": "ual-2026q2-investor-update", "notes": "nothing"}]}) is None
    assert cc.quoted_date("the Gulf Coast jet forward curve as of July 14, 2026") == date(2026, 7, 14)
    assert cc.quoted_date("no date here") is None


# ------------------------------------------------------------------ the output file

def test_build_is_deterministic_and_valid(payload):
    assert cc.build(generated_at=STAMP) == payload
    cc.validate(payload)
    json.dumps(payload, allow_nan=False)
    assert "fixture" not in payload and payload["generated_at"] == STAMP
    for item in payload["inputs"]:
        assert hashlib.sha256((cc.ROOT / item["path"]).read_bytes()).hexdigest() == item["sha256"]
    assert payload["headline"] == cc.headline(payload["fit"], payload["by_split"]["test"])
    assert payload["plain_summary"].startswith(payload["headline"])


def test_output_keeps_the_contract_keys(payload):
    assert {"headline", "daily_fuel", "reported", "fit", "by_split", "management_check", "variants_tried",
            "predicted_2026q3", "delta_check", "limits", "plain_summary", "generated_at", "as_of", "inputs"} <= set(payload)
    assert {"spec", "lag_days", "basis_usd_gal", "slope", "ci", "r2", "n"} <= set(payload["fit"])
    assert set(payload["by_split"]) == {"fit", "select", "test"}
    assert all({"mae", "n"} <= set(part) for part in payload["by_split"].values())
    check = payload["management_check"]
    assert check["claimed_usd_m"] == 575 and check["claimed_eps"] == 1.12
    assert set(check["inputs"]) >= {"gallons_m", "price_change_usd_gal", "tax_rate", "diluted_shares_m"}
    assert payload["reported"][0].keys() >= {"period_end", "usd_gal"} and len(payload["daily_fuel"][0]) == 2
    assert payload["daily_fuel"][-1][0] == payload["as_of_detail"]["fuel_daily_last_print"]


def test_frozen_model_and_selection_never_use_test_year_reports(payload):
    assert payload["fit"]["estimated_on_reports_published_through"] < "2024-02-01"
    assert all(row["split"] != "test" and not row["pandemic"] for row in payload["reported"] if row["in_estimation"])
    assert sum(row["in_estimation"] for row in payload["reported"]) == payload["fit"]["n"]
    quiet = cc.build(generated_at=STAMP, evaluate_test=False)
    assert quiet["fit"] == payload["fit"] and quiet["selection"] == payload["selection"]
    assert quiet["by_split"]["test"] is None and quiet["delta_check"] is None and quiet["predicted_2026q3"]["sensitivity"] is None
    assert all(row["fitted_usd_gal"] is None for row in quiet["reported"] if row["split"] == "test")
    for key in ("p10", "p50", "p90"):
        assert quiet["predicted_2026q3"][key] == payload["predicted_2026q3"][key]


def test_published_prediction_is_what_the_function_returns(payload):
    dates, values = market_data.load_fuel_daily()
    block = payload["predicted_2026q3"]
    direct = cc.predicted_price_paid("2026Q3", dates, values)
    assert [round(direct[k], 6) for k in ("p10", "p50", "p90")] == [block[k] for k in ("p10", "p50", "p90")]
    assert block["complete"] and block["management"]["value"] == 3.69
    assert block["management"]["position"] == cc.position(3.69, block["p10"], block["p90"])
    assert block["path"][-1]["p50"] == block["p50"] and block["path"][0]["weekdays_remaining"] > 60
    early = cc.predicted_price_paid("2026Q3", dates, values, as_of="2026-07-14")
    assert round(early["p50"], 6) == block["at_guidance_date"]["p50"] and early["last_print"] == "2026-07-14"


def test_refit_model_learns_only_from_reports_published_before_its_date(payload):
    frozen = cc.default_model()
    same = cc.model_as_of("2024-01-23")
    assert np.allclose(same.beta, frozen.beta) and same.quarters == frozen.quarters
    later = cc.model_as_of("2025-07-01")
    assert later.last_filed_on < date(2025, 7, 1) and later.quarters[-1] == "2025Q1" and later.n == frozen.n + 5
    refit = payload["predicted_2026q3"]["sensitivity"][0]
    assert refit["id"] == "refit_through_latest" and refit["n"] == cc.model_as_of(payload["as_of_detail"]["fuel_daily_last_print"]).n
    assert all(item["management_position"] == cc.position(3.69, item["p10"], item["p90"])
               for item in payload["predicted_2026q3"]["sensitivity"])
    test = payload["by_split"]["test"]
    assert payload["predicted_2026q3"]["calibration"]["note"] == cc.calibration_sentence(test)
    assert ("too narrow" in cc.calibration_sentence(test)) == (test["inside_p10_p90"] < 0.6)


def test_delta_check_sets_the_method_beside_delta_report(payload):
    block = payload["delta_check"]
    assert block["quarter"] == "2026Q3" and block["actual"]["value"] == 3.61 and block["assumed"]["value"] == 3.15
    assert block["actual"]["position"] == cc.position(3.61, block["p10"], block["p90"])
    assert block["method_error"] == pytest.approx(block["p50"] - 3.61, abs=1e-6)
    assert block["assumption_error"] == pytest.approx(3.15 - 3.61)
    assert payload["delta_fit"]["estimated_on_reports_published_through"] < "2024-02-01"
    assert block["as_of"] < block["actual"]["reported_on"]


def test_builder_writes_what_it_validated(tmp_path, payload):
    out = tmp_path / "cost_clock.json"
    build_cost_clock.write(payload, out)
    assert json.loads(out.read_text()) == payload
    broken = copy.deepcopy(payload)
    broken["fit"]["ci"] = [2.0, 3.0]
    with pytest.raises(ValueError, match="bracket"):
        build_cost_clock.write(broken, tmp_path / "bad.json")
    for change in ({"fixture": True}, {"headline": ""}):
        with pytest.raises(ValueError):
            cc.validate({**payload, **change})
    with pytest.raises(ValueError, match="lacks"):
        cc.validate({k: v for k, v in payload.items() if k != "delta_check"})


def test_published_file_is_the_rebuild_of_its_inputs(payload):
    if not cc.OUTPUT.is_file():
        pytest.skip("app/static/data/cost_clock.json has not been built")
    published = json.loads(cc.OUTPUT.read_text())
    cc.validate(published)
    if published["inputs"] != payload["inputs"]:
        pytest.skip("an input changed since app/static/data/cost_clock.json was built; rebuild it")
    assert {**published, "generated_at": STAMP} == payload
