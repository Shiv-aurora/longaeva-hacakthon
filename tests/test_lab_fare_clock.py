"""Fare clock tests. Everything here is simulated with a seed; nothing reads the network."""
import copy
from datetime import date, timedelta
import json
import sys

import numpy as np
import pytest

from app.lab import fare_clock as fc
from app.lab import stats
from scripts.lab import build_fare_clock

TRUE_CURVE = np.array([0.10, 0.40, 0.60, 0.70, 0.75])   # share recovered 0..4 quarters after a shock
TRUE_LAGS = np.diff(np.concatenate([[0.0], TRUE_CURVE]))
STAMP = "2026-10-09T00:00:00Z"


# ------------------------------------------------------------------ simulators

def simulate_table(seed, n_quarters, first_year=2013, noise=0.05, capacity_effect=-0.03, curve=TRUE_CURVE, skip=0):
    """A table shaped like load_ual_quarterly's, in which unit revenue responds to fuel cost per ASM by `curve`."""
    rng = np.random.default_rng(seed)
    quarters = first_year * 4 + skip + np.arange(n_quarters)
    season = np.array([-0.4, 0.3, 0.5, -0.2])[quarters % 4]
    asm = 60000.0 * (1 + 0.08 * season) * np.exp(np.cumsum(rng.normal(0.008, 0.02, n_quarters)))
    price = np.empty(n_quarters)
    price[0] = 2.5
    for t in range(1, n_quarters):
        price[t] = max(0.6, 2.5 + 0.9 * (price[t - 1] - 2.5) + rng.normal(0, 0.3))
    gallons = asm / 62.0
    fuel_cpasm = 100.0 * price * gallons / asm
    lags = np.diff(np.concatenate([[0.0], curve]))
    response = np.array([sum(lags[j] * fuel_cpasm[max(t - j, 0)] for j in range(len(lags))) for t in range(n_quarters)])
    trasm = 12.0 + season + response + capacity_effect * 100.0 * np.log(asm / 60000.0) + rng.normal(0, noise, n_quarters)
    return {"q": quarters, "year": quarters // 4, "absent_quarters": [], "asm_m": asm,
            "total_revenue_musd": trasm * asm / 100.0, "passenger_revenue_musd": 0.9 * trasm * asm / 100.0,
            "trasm_cents": trasm, "prasm_cents": 0.9 * trasm, "fuel_gallons_m": gallons,
            "fuel_price_usd_gal": price, "fuel_expense_musd": price * gallons}


def write_ual_csv(path, table):
    lines = ["period_end,filed_on,accession," + ",".join(fc.UAL_COLUMNS) + ",source_note"]
    for i, q in enumerate(table["q"]):
        cells = [f"{table[c][i]:.6f}" for c in fc.UAL_COLUMNS]
        lines.append(",".join([fc.quarter_end(int(q)), "", ""] + cells + ["simulated"]))
    path.write_text("\n".join(lines) + "\n")
    return path


def write_delta_csv(path, table):
    """Delta's layout: GAAP columns inflated by refinery sales, adjusted columns after source_note; as in the
    real file the first quarter prints adjusted fuel but no adjusted revenue."""
    adjusted = list(fc.DAL_ADJUSTED.values())
    lines = ["period_end," + ",".join(fc.UAL_COLUMNS) + ",source_note," + ",".join(adjusted)]
    for i, q in enumerate(table["q"]):
        gaap = [f"{table[c][i] * (1.3 if c in fc.DAL_ADJUSTED else 1.0):.6f}" for c in fc.UAL_COLUMNS]
        clean = ["" if i == 0 and "fuel" not in c else f"{table[c][i]:.6f}" for c in fc.DAL_ADJUSTED]
        lines.append(",".join([fc.quarter_end(int(q))] + gaap + ["simulated"] + clean))
    path.write_text("\n".join(lines) + "\n")
    return path


def write_demand_json(path, seed):
    rng = np.random.default_rng(seed)
    months = [f"{y}-{m:02d}" for y in range(2019, 2027) for m in range(1, 13)][:93]     # 2019-01 .. 2026-09
    rows = [{"month": month, "yoy": None if month < "2020" else round(float(rng.normal(0.03, 0.03)), 6),
             "complete": True} for month in months]
    rows.append({"month": "2026-10", "yoy": 0.5, "complete": False})
    path.write_text(json.dumps({"monthly_control": rows}))
    return path


def write_delta_check(path):
    path.write_text(json.dumps({"guidance_vs_actual_sep_quarter": {
        "fuel_price_usd_gal": {"assumed_all_in": 3.0, "actual_adj": 3.5, "actual_adj_minus_assumed": 0.5,
                               "evidence_ids": ["fuel"]},
        "unit_revenue": {"adj_trasm_yoy_stated_pct_sep_quarter": 9, "evidence_ids": ["unit"]},
        "eps": {"guided_low": 2.0, "guided_high": 2.5, "actual_adj": 1.5, "position": "below_range",
                "evidence_ids": ["eps"]}}}))
    return path


def write_market_csvs(tmp_path, seed, lag_coefs, up_scale=1.0, noise=0.0005, jump=1.6):
    """Monthly fuel (as identical business-day prints) and CPI in which real fares follow fuel by `lag_coefs`.

    Fuel increases pass through `up_scale` times as strongly as decreases. Fuel
    jumps by `jump` on the first business day of March 2026.
    """
    rng = np.random.default_rng(seed)
    months = [(y, m) for y in range(2010, 2027) for m in range(1, 13)][:200]   # 2010-01 .. 2026-08
    dlog_fuel = rng.normal(0, 0.08, len(months))
    dlog_fuel[months.index((2026, 3))] = np.log(jump)
    felt = np.where(dlog_fuel > 0, up_scale * dlog_fuel, dlog_fuel)
    dlog_real = np.array([sum(b * felt[t - j] for j, b in enumerate(lag_coefs) if t - j >= 0)
                          for t in range(len(months))]) + rng.normal(0, noise, len(months))
    fuel = 2.0 * np.exp(np.cumsum(dlog_fuel))
    all_items = 220.0 * np.exp(0.002 * np.arange(len(months)))
    fare = 250.0 * np.exp(np.cumsum(dlog_real)) * all_items / 220.0
    fuel_lines, cpi_lines = ["date,usd_per_gallon"], ["date,series_id,value"]
    for (y, m), f, a, p in zip(months, fuel, all_items, fare):
        day = date(y, m, 1)
        while day.month == m:
            if day.weekday() < 5:
                fuel_lines.append(f"{day.isoformat()},{f:.6f}")
            day += timedelta(days=1)
        stamp = date(y, m, 1).isoformat()
        cpi_lines += [f"{stamp},CUSR0000SA0,{a:.6f}", f"{stamp},CUSR0000SETG01,{p:.6f}",
                      f"{stamp},CUUR0000SETG01,{p:.6f}"]
    (tmp_path / "fuel.csv").write_text("\n".join(fuel_lines) + "\n")
    (tmp_path / "cpi.csv").write_text("\n".join(cpi_lines) + "\n")
    return tmp_path / "cpi.csv", tmp_path / "fuel.csv"


def write_literature(path):
    def entry(entry_id, numbers, verified=True):
        return {"id": entry_id, "authors": "Simulated", "year": 2026, "url": f"https://example.invalid/{entry_id}",
                "kind": "filing", "quality": "primary", "verified": verified,
                "numbers": [{"label": label, "value": value, "unit": "percent", "quote": f"quote for {label}"}
                            for label, value in numbers]}
    path.write_text(json.dumps({"generated_at": STAMP, "entries": [
        entry("ual-2026q1-investor-update", [("Assumed recovery, 2026Q2, low", 40), ("Assumed recovery, 2026Q2, high", 50),
                                             ("Assumed recovery, 2026Q3, low", 70), ("Assumed recovery, 2026Q3, high", 80),
                                             ("Assumed recovery, 2026Q4, low", 85), ("Assumed recovery, 2026Q4, high", 100)]),
        entry("ual-2026q2-investor-update", [("Expected recovery, 2026Q3, low", 80), ("Expected recovery, 2026Q3, high", 90),
                                             ("Expected recovery by 2026Q4", 100)]),
        entry("ual-2026q2-earnings-release", [("Share of the second-quarter fuel expense increase recovered (stated as "
                                               "'approximately half')", 50), ("Expected recovery, 2026Q3, low", 80)]),
        entry("ubs-2026-03-ual-pass-through-45", [("UBS assumed fuel pass-through rate for United, fiscal 2026", 45)]),
        entry("chuang-2020-eb", [("Same-quarter fare change per one-dollar positive fuel cost shock", 1.6)], verified=False),
    ]}))
    return path


@pytest.fixture(scope="module")
def inputs(tmp_path_factory):
    folder = tmp_path_factory.mktemp("fare_clock")
    cpi, fuel = write_market_csvs(folder, 11, [0.02] * 6)
    ual = write_ual_csv(folder / "ual.csv", simulate_table(5, 54))
    return {"cpi_path": cpi, "fuel_path": fuel, "ual_path": ual, "literature_path": write_literature(folder / "lit.json"),
            "dal_path": write_delta_csv(folder / "dal.csv", simulate_table(15, 36, first_year=2017, skip=3)),
            "dal_check_path": write_delta_check(folder / "dal_check.json"),
            "demand_path": write_demand_json(folder / "demand.json", 16)}


@pytest.fixture(scope="module")
def payload(inputs):
    return fc.build(seed=7, n_draws=500, generated_at=STAMP, **inputs)


# ------------------------------------------------------------------ the regression

def test_cents_per_asm_regression_recovers_a_known_curve():
    table = simulate_table(1, 600, first_year=1800)
    fit = fc.recovery_fit(fc.united_panel(table), years=(1800, 1950))
    cum = fit.cumulative()
    assert fit.fit.n == 600 - 8 and len(cum["cumulative"]) == 5
    assert np.allclose(cum["cumulative"], TRUE_CURVE, atol=0.03)
    assert np.all(cum["lo"] <= TRUE_CURVE) and np.all(TRUE_CURVE <= cum["hi"])
    assert fit.fit.beta[6] == pytest.approx(-0.03, abs=0.01)          # capacity growth, cents per point
    assert np.allclose(fc.padded_curve(fit)["cumulative"], cum["cumulative"])
    # Reported fuel expense and price times gallons are the same thing here, so the curve is too.
    other = fc.recovery_fit(fc.united_panel(table, fuel="fuel_expense"), years=(1800, 1950))
    assert np.allclose(other.cumulative()["cumulative"], cum["cumulative"])


def test_shorter_and_seasonally_differenced_variants_recover_the_curve():
    table = simulate_table(2, 800, first_year=1800)
    panel = fc.united_panel(table)
    differenced = fc.recovery_fit(panel, years=(1800, 2000), transform="qoq_sd")
    assert np.allclose(differenced.cumulative()["cumulative"], TRUE_CURVE, atol=0.05)
    short = fc.padded_curve(fc.recovery_fit(panel, years=(1800, 2000), max_lag=2))
    assert len(short["cumulative"]) == 5 and short["cumulative"][2] == short["cumulative"][3] == short["cumulative"][4]
    with pytest.raises(ValueError):
        fc.recovery_fit(panel, years=(1800, 2000), transform="levels")


def test_lags_use_no_future_data():
    table = simulate_table(3, 80)
    panel = fc.united_panel(table)
    cutoff = 50
    last_year = int(panel["year"][cutoff])
    fit = fc.recovery_fit(panel, years=(2013, last_year))
    # Row t's lag j is the fuel change at t-j, nothing later.
    for t in fit.rows[:5]:
        assert np.array_equal(fit.design[t, :5], [panel["d_fuel"][t - j] for j in range(5)])
    late = panel["year"] > last_year
    changed = copy.deepcopy(table)
    for column in ("fuel_price_usd_gal", "trasm_cents", "total_revenue_musd", "asm_m", "fuel_gallons_m"):
        changed[column][late] *= 3.0
    panel2 = fc.united_panel(changed)
    refit = fc.recovery_fit(panel2, years=(2013, last_year))
    assert np.array_equal(refit.rows, fit.rows) and np.array_equal(refit.fit.beta, fit.fit.beta)
    assert not np.allclose(panel2["d_fuel"][late], panel["d_fuel"][late])
    early = ~late
    term, term2 = fc.fuel_term(panel["d_fuel"], TRUE_CURVE), fc.fuel_term(panel2["d_fuel"], TRUE_CURVE)
    assert np.array_equal(term[early], term2[early], equal_nan=True) and np.isnan(term[:8]).all()
    before = fc.rule_predictions(panel, TRUE_CURVE, fit.rows)
    after = fc.rule_predictions(panel2, TRUE_CURVE, fit.rows)
    for horizon in ("h1", "h4"):
        assert np.array_equal(before[horizon][early], after[horizon][early], equal_nan=True)
    # The term is the convolution of past changes with the curve's increments.
    t = 30
    assert term[t] == pytest.approx(sum(TRUE_LAGS[j] * panel["d_fuel"][t - j] for j in range(5)))


def test_fitted_rule_reproduces_the_regression_and_rules_share_the_scoring():
    panel = fc.united_panel(simulate_table(4, 120))
    fit = fc.recovery_fit(panel, years=(2013, 2032))
    fitted = fc.rule_predictions(panel, fit.cumulative()["cumulative"], fit.rows)
    assert np.allclose(fitted["h4"][fit.rows], fit.predict(fit.rows))
    assert fitted["intercept"] == pytest.approx(fit.fit.beta[0]) and fitted["capacity_coef"] == pytest.approx(fit.fit.beta[6])
    none = fc.rule_predictions(panel, [0.0] * 5, fit.rows, baseline="none")
    assert np.allclose(none["h4"][fit.rows], 0.0) and np.allclose(none["h1"][fit.rows[1:]], panel["d_rev"][fit.rows[1:] - 1])
    rules = {"costs_only": [0.0] * 5, "full_immediate": [1.0] * 5, "management": [0.85, 1, 1, 1, 1],
             "fitted": list(fit.cumulative()["cumulative"])}
    scores = fc.score_rules(panel, rules, fit.rows, 3, evaluate_test=True)
    test = scores["h4"]["test"]
    assert test["n"] == 12 and test["lowest_mae_core_rule"] == "fitted"          # the truth is a delayed response
    assert test["fitted_minus_rival_abs_error"]["costs_only"]["mean_diff"] < 0
    assert test["fitted_minus_rival_abs_error"]["costs_only"]["n"] == 12
    assert scores["h1"]["select_excluding_pandemic"]["n"] == 0                   # every select quarter is touched
    withheld = fc.score_rules(panel, rules, fit.rows, 3, evaluate_test=False)
    assert "status" in withheld["h4"]["test"] and "mae_cents_per_asm" not in withheld["h4"]["test"]


def test_pandemic_variants_differ_as_described():
    panel = fc.united_panel(simulate_table(6, 54))
    fit_only = fc.recovery_fit(panel)
    excluded = fc.recovery_fit(panel, years=(2013, 2023), pandemic="exclude")
    indicators = fc.recovery_fit(panel, years=(2013, 2023), pandemic="indicators")
    labels = lambda f: [fc.quarter_label(int(q)) for q in panel["q"][f.rows]]
    assert labels(fit_only)[0] == "2015Q1" and labels(fit_only)[-1] == "2019Q4" and fit_only.fit.n == 20
    assert labels(excluded) == labels(fit_only)                   # every select quarter has a pandemic level behind it
    assert indicators.fit.n == 36 and indicators.fit.k == 7 + 12  # 2020Q1..2022Q4 each absorbed by an indicator
    assert np.allclose(indicators.fit.resid[20:32], 0.0, atol=1e-9)
    assert fc.pandemic_touched(panel["q"], 8).sum() == 16


# ------------------------------------------------------------------ the smooth two-parameter curve

def test_smooth_curve_recovers_a_known_long_run_share_and_speed():
    truth = fc.smooth_curve(0.8, 0.5)
    assert np.allclose(truth, [0.4, 0.6, 0.7, 0.75, 0.775])
    assert fc.half_life(0.5) == pytest.approx(1.0) and fc.half_life(0.0) == 0.0
    assert fc.half_life(0.25) == pytest.approx(0.5) and fc.half_life(0.5 ** 0.25) == pytest.approx(4.0)
    table = simulate_table(31, 600, first_year=1800, curve=truth)
    panel = fc.united_panel(table)
    reference = fc.recovery_fit(panel, years=(1800, 1950))
    lags = reference.design[:, :5]
    fit = fc.smooth_fit(panel, lags, reference.rows, 3, 500)
    assert fit["L"] == pytest.approx(0.8, abs=0.04) and fit["rho"] == pytest.approx(0.5, abs=0.05)
    assert np.allclose(fit["curve_estimate"], fc.smooth_curve(fit["L"], fit["rho"])) and fit["n"] == reference.fit.n
    assert fit["half_life_quarters"] == pytest.approx(1.0, abs=0.15)
    assert fit["L_range"]["p05"] <= 0.8 <= fit["L_range"]["p95"] and fit["rho_range"]["p05"] <= 0.5 <= fit["rho_range"]["p95"]
    assert fit["capacity_coef_cents_per_point"] == pytest.approx(-0.03, abs=0.01)
    draws = fit["draws"]
    assert draws.shape == (500, 5) and np.all(np.diff(draws, axis=1) >= 0)          # every draw rises steadily
    assert [row["p50"] for row in fit["curve"]] == pytest.approx(np.median(draws, axis=0))
    assert np.array_equal(fc.smooth_fit(panel, lags, reference.rows, 3, 500)["draws"], draws)
    assert not np.array_equal(fc.smooth_fit(panel, lags, reference.rows, 4, 500)["draws"], draws)
    assert not fit["ridge"]["poorly_separated"]
    # An immediate response is rho = 0, and the profile agrees with ordinary least squares at that rho.
    flat = fc.united_panel(simulate_table(32, 600, first_year=1800, curve=np.full(5, 0.6)))
    now = fc.smooth_profile(flat["d_rev"][reference.rows], fc.recovery_fit(flat, years=(1800, 1950)).design[reference.rows, :5],
                            flat["cap"][reference.rows])
    assert now[0] == pytest.approx(0.6, abs=0.03) and now[1] <= 0.05


def test_expanding_fits_never_see_their_own_future():
    table = simulate_table(33, 54, curve=fc.smooth_curve(0.8, 0.5))
    panel = fc.united_panel(table)
    clean = fc.recovery_fit(panel, years=(2013, 2026), pandemic="exclude")
    lags = clean.design[:, :5]
    entries = fc.expanding_fits(panel, lags, clean.rows, int(panel["q"][-1]) + 1)
    assert [e["as_of_period"] for e in entries][:2] == ["2019Q1", "2019Q2"] and entries[-1]["as_of_period"] == "2026Q3"
    assert entries[0]["n"] == 16 and entries[0]["fitted_through"] == "2018Q4" and entries[-1]["n"] == 30
    assert all(a["n"] <= b["n"] for a, b in zip(entries, entries[1:]))
    by_period = {e["as_of_period"]: e for e in entries}
    assert by_period["2023Q4"]["n"] == 20 and by_period["2024Q1"]["n"] == 20 and by_period["2024Q2"]["n"] == 21
    assert all(np.allclose(e["curve_p50"], fc.smooth_curve(e["L"], e["rho"])) for e in entries)
    # Changing everything from 2025Q1 on leaves every entry dated up to 2025Q1 exactly as it was.
    changed = copy.deepcopy(table)
    late = changed["q"] >= 2025 * 4
    for column in ("fuel_price_usd_gal", "trasm_cents", "total_revenue_musd"):
        changed[column][late] *= 2.0
    panel2 = fc.united_panel(changed)
    clean2 = fc.recovery_fit(panel2, years=(2013, 2026), pandemic="exclude")
    again = {e["as_of_period"]: e for e in fc.expanding_fits(panel2, clean2.design[:, :5], clean2.rows, int(panel2["q"][-1]) + 1)}
    for period, entry in by_period.items():
        same = entry["L"] == again[period]["L"] and entry["rho"] == again[period]["rho"]
        assert same if period <= "2025Q1" else True
    assert by_period["2026Q3"]["L"] != again["2026Q3"]["L"]


def test_smooth_curve_is_published_for_the_simulator_and_scored_as_post_hoc(payload):
    smooth = payload["smooth"]
    assert smooth["published_as_curve"] == "through_latest" and payload["curve"] == smooth["through_latest"]["curve"]
    assert [set(row) >= {"quarters_after", "p10", "p50", "p90"} for row in payload["curve"]] == [True] * 5
    draws = np.array(payload["curve_draws"])
    assert draws.shape == (500, 5) and np.all((np.diff(draws, axis=1) >= 0).all(axis=1) | (np.diff(draws, axis=1) <= 0).all(axis=1))
    assert smooth["fit_only"]["n"] == 20 and smooth["through_latest"]["n"] == 30 and "draws" not in smooth["fit_only"]
    assert set(smooth["half_life_quarters"]) == {"fit_only", "through_latest"} and "grid" in smooth["method"]
    assert [e["as_of_period"] for e in smooth["expanding"]][0] == "2019Q1" and len(smooth["expanding"]) == 31
    assert {"as_of_period", "L", "rho", "curve_p50", "n"} <= set(smooth["expanding"][0])
    assert payload["primary_spec"]["chosen_after_test_split_seen"] is True
    assert payload["primary_spec"]["prespecified"]["id"] == fc.UNITED_SPECS[0]["id"]
    scored = payload["oos_test"]["smooth_fit_only"]
    assert scored["descriptive_not_a_clean_test"] is True and "AFTER" in scored["note"]
    test = scored["scores"]["h4"]["test"]
    main = payload["oos_test"]["scores"]["h4"]["test"]
    assert test["mae_cents_per_asm"]["smooth_fit_only"] == main["mae_cents_per_asm"]["smooth_fit_only"]
    assert test["mae_cents_per_asm"]["prespecified_five_lag"] == main["mae_cents_per_asm"]["fitted"]
    assert set(test["smooth_minus_rival_abs_error"]) == {"costs_only", "full_immediate", "management", "prespecified_five_lag"}
    assert "smooth_fit_only" not in fc.CORE_RULES and main["lowest_mae_core_rule"] in fc.CORE_RULES
    assert payload["by_split"]["test"]["average_miss_smooth_curve_chosen_after_test"] == test["mae_cents_per_asm"]["smooth_fit_only"]
    assert any("chosen AFTER" in limit for limit in payload["limits"])
    assert any("chosen AFTER" in sentence for sentence in payload["plain_summary"])
    assert any("pre-specified" in sentence and "Locked test" in sentence for sentence in payload["plain_summary"])


def test_smooth_headline_is_short_and_goes_neutral_when_the_range_reaches_zero(payload):
    base = {"one_year_share": {"estimate": 0.8, "p05": 0.5, "p50": 0.65, "p95": 0.9}}
    clear = fc.smooth_headline(base)
    assert "about 65%" in clear and "50% to 90%" in clear and "best single fit 80%" in clear and "cannot" not in clear
    wide = fc.smooth_headline({"one_year_share": {"estimate": 0.8, "p05": -0.2, "p50": 0.65, "p95": 1.9}})
    assert "cannot tell a revenue response to fuel cost from none" in wide and "-20% to 190%" in wide
    for text in (clear, wide, payload["headline"]):
        assert text.count(".") - text.count("0.") - text.count("1.") - text.count("2.") <= 1 or text.endswith(".")
        assert len(text.split()) <= 32
    assert "management" not in payload["headline"].lower()
    assert fc._sentences(["One thing. Another thing: 0.5 of it. Third."]) == ["One thing.", "Another thing: 0.5 of it.", "Third."]


# ------------------------------------------------------------------ uncertainty

def test_curve_draws_are_cumulative_sums_of_the_coefficient_draws():
    panel = fc.united_panel(simulate_table(8, 54))
    fit = fc.recovery_fit(panel)
    block = fit.blocks[0]
    beta, cov = fit.fit.beta[block], fit.cov[np.ix_(block, block)]
    coefficients, cumulative = fc.curve_draws(beta, cov, 4000, 9, df=fit.fit.df_resid)
    assert coefficients.shape == cumulative.shape == (4000, 5)
    assert np.allclose(np.cumsum(coefficients, axis=1), cumulative)
    assert np.allclose(np.diff(cumulative, axis=1, prepend=0.0), coefficients)
    assert np.allclose(np.median(cumulative, axis=0), np.cumsum(beta), atol=0.05)
    again = fc.curve_draws(beta, cov, 4000, 9, df=fit.fit.df_resid)
    assert np.array_equal(again[0], coefficients) and np.array_equal(again[1], cumulative)
    assert not np.array_equal(fc.curve_draws(beta, cov, 4000, 10, df=fit.fit.df_resid)[0], coefficients)
    normal, _ = fc.curve_draws(beta, cov, 200000, 9)
    assert np.allclose(np.cov(normal.T), cov, atol=0.05 * np.abs(cov).max())
    student, _ = fc.curve_draws(beta, cov, 200000, 9, df=5)
    assert np.all(student.std(axis=0) > normal.std(axis=0))        # Student t is wider than normal


def test_cautious_covariance_is_never_below_either_input():
    rng = np.random.default_rng(12)
    for _ in range(20):
        a, b = rng.normal(size=(6, 6)), rng.normal(size=(6, 6))
        classical, hac = a @ a.T, b @ b.T
        cautious = fc.cautious_cov(classical, hac)
        for w in rng.normal(size=(50, 6)):
            assert w @ cautious @ w >= max(w @ classical @ w, w @ hac @ w) - 1e-9
    assert np.allclose(fc.cautious_cov(classical, classical), classical)
    fit = fc.recovery_fit(fc.united_panel(simulate_table(8, 54)))
    cum = fit.cumulative()
    assert np.all(cum["se"] >= np.maximum(cum["se_newey_west"], cum["se_classical"]) - 1e-12)


# ------------------------------------------------------------------ industry, monthly

def test_monthly_fuel_averages_the_prints_and_counts_them():
    days = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 2, 1), date(2024, 2, 2), date(2024, 2, 5)]
    monthly = fc.monthly_fuel(days, [2.0, 3.0, 1.0, 2.0, 6.0])
    assert monthly == {fc.month_index(date(2024, 1, 1)): (2.5, 2), fc.month_index(date(2024, 2, 1)): (3.0, 3)}


def test_industry_regression_recovers_a_known_elasticity_and_asymmetry(tmp_path):
    cpi, fuel = write_market_csvs(tmp_path, 21, [0.02] * 6, up_scale=2.0)
    panel = fc.industry_panel(cpi, fuel)
    assert fc.month_label(int(panel["months"][0])) == "2010-01" and panel["last_cpi_month"] == "2026-08"
    assert panel["prints"].min() >= 20 and not panel["short_fuel_months"] and not panel["missing_cpi_months"]
    asymmetric = fc.industry_fit(panel, next(s for s in fc.INDUSTRY_SPECS if s["id"] == "industry_asymmetric_lags0_12_fit"))
    result = fc.asymmetry_test(asymmetric, 12)
    assert result["up"] == pytest.approx(0.24, abs=0.01) and result["down"] == pytest.approx(0.12, abs=0.01)
    assert result["p_value"] < 0.001 and result["difference"] == pytest.approx(0.12, abs=0.02)
    cpi, fuel = write_market_csvs(tmp_path, 22, [0.02] * 6)
    panel = fc.industry_panel(cpi, fuel)
    fit = fc.industry_fit(panel, fc.INDUSTRY_SPECS[0])
    rows = fc._lag_rows(fit)
    assert fit.fit.n == 84 and len(rows) == 13
    assert [rows[k]["coef"] for k in (0, 5, 12)] == pytest.approx([0.02, 0.12, 0.12], abs=0.01)
    assert all(r["lo"] <= r["coef"] <= r["hi"] for r in rows)
    assert fc.asymmetry_test(fc.industry_fit(panel, fc.INDUSTRY_SPECS[7]), 12)["p_value"] > 0.01


def test_missing_cpi_month_drops_the_two_changes_that_touch_it(tmp_path):
    cpi, fuel = write_market_csvs(tmp_path, 23, [0.02] * 6)
    kept = [line for line in cpi.read_text().splitlines() if not line.startswith("2025-10-01")]
    cpi.write_text("\n".join(kept) + "\n")
    panel = fc.industry_panel(cpi, fuel)
    at = lambda label: int(np.flatnonzero(panel["months"] == fc.month_index(date.fromisoformat(label)))[0])
    assert panel["missing_cpi_months"] == ["2025-10"]
    assert np.isnan(panel["dlog_real_sa"][at("2025-10-01")]) and np.isnan(panel["dlog_real_sa"][at("2025-11-01")])
    assert np.isfinite(panel["dlog_real_sa"][at("2025-12-01")]) and np.isfinite(panel["dlog_fuel"][at("2025-10-01")])


def test_short_fuel_months_have_no_average(tmp_path):
    cpi, fuel = write_market_csvs(tmp_path, 24, [0.02] * 6)
    lines = fuel.read_text().splitlines()
    kept = [line for line in lines if not line.startswith("2012-05") or line < "2012-05-08"]
    fuel.write_text("\n".join(kept) + "\n")
    panel = fc.industry_panel(cpi, fuel)
    at = int(np.flatnonzero(panel["months"] == fc.month_index(date(2012, 5, 1)))[0])
    assert panel["prints"][at] < fc.MIN_FUEL_PRINTS and np.isnan(panel["fuel"][at])
    assert panel["short_fuel_months"] == [{"month": "2012-05", "n_prints": int(panel["prints"][at])}]


# ------------------------------------------------------------------ management's statements

def test_management_statements_paths_and_mapping(inputs):
    literature = fc.load_literature(inputs["literature_path"])
    assert "chuang-2020-eb" not in literature                      # unverified entries are never used
    statements = fc.management_statements(literature)
    table = {(s["path"], s["period"], s["kind"]): (s["low"], s["high"], s["source_id"]) for s in statements}
    assert table[("april_2026", "2026Q2", "guidance assumption")] == (0.4, 0.5, "ual-2026q1-investor-update")
    assert table[("april_2026", "2026Q4", "guidance assumption")] == (0.85, 1.0, "ual-2026q1-investor-update")
    assert table[("july_2026", "2026Q3", "expectation")] == (0.8, 0.9, "ual-2026q2-investor-update")
    assert table[("july_2026", "2026Q4", "expectation")] == (1.0, 1.0, "ual-2026q2-investor-update")
    assert table[("july_2026", "2026Q2", "reported outcome")] == (0.5, 0.5, "ual-2026q2-earnings-release")
    assert fc.promise_rule(statements) == [0.85, 1.0, 1.0, 1.0, 1.0]
    from app.lab import market_data
    onset = fc.shock_onset(*market_data.load_fuel_daily(inputs["fuel_path"]))
    assert onset["date"] == "2026-03-02" and onset["onset_quarter"] == "2026Q1" and onset["first_full_quarter"] == "2026Q2"
    paths = fc.management_paths(statements, onset)
    assert paths["july_2026"]["mapped_midpoints"] == [0.5, 0.85, 1.0, 1.0, 1.0]
    assert paths["april_2026"]["mapped_midpoints"] == pytest.approx([0.45, 0.75, 0.925, 0.925, 0.925])
    assert [m["basis"] for m in paths["july_2026"]["mapped"]][:2] == ["reported outcome", "expectation"]
    assert paths["july_2026"]["mapped"][4]["basis"].startswith("not stated")
    assert fc.management_paths(statements, None)["july_2026"]["mapped"] is None
    # Without the literature file the July promise comes from the contract, marked as such.
    fallback = fc.management_statements({})
    assert [(s["period"], s["low"], s["high"], s["source_id"]) for s in fallback] == [("2026Q3", 0.8, 0.9, None),
                                                                                     ("2026Q4", 1.0, 1.0, None)]
    published, missing = fc.published_estimates(literature)
    assert [p["entry_id"] for p in published] == ["ubs-2026-03-ual-pass-through-45"] and published[0]["value"] == 0.45
    assert published[0]["value_as_published"] == 45 and published[0]["stated_as_share_of_cost"]
    assert len(missing) == len(fc.PUBLISHED) - 1
    literature["chuang-2020-eb"] = {"authors": "A", "year": 2020, "numbers": [
        {"label": "Same-quarter fare change per one-dollar positive fuel cost shock", "value": 1.6,
         "unit": "US cents of fare per passenger per 1 USD of flight fuel cost", "quote": "q"}]}
    other = next(p for p in fc.published_estimates(literature)[0] if p["entry_id"] == "chuang-2020-eb")
    assert other["value"] is None and other["value_as_published"] == 1.6          # never converted to a share


def test_implied_baseline_makes_the_stated_share_true(payload):
    q2 = next(q for q in payload["management_comparison"]["quarters"] if q["period"] == "2026Q2")
    raw, implied = q2["raw"], q2["implied_by_statement"]
    assert implied["stated_share"] == 0.5 and implied["label"].startswith("IMPLIED")
    recovered = raw["revenue_change_musd"] - implied["implied_baseline_revenue_change_musd"]
    assert recovered / raw["fuel_expense_change_musd"] == pytest.approx(0.5, abs=1e-4)
    revenue = q2["inputs"]["total_revenue_musd"]
    assert raw["revenue_change_over_fuel_expense_change"] == pytest.approx(
        (revenue[1] - revenue[0]) / raw["fuel_expense_change_musd"], abs=1e-5)
    model = q2["fitted_model"]
    assert model["fuel_attributed_revenue_musd"] == pytest.approx(
        model["fuel_attributed_unit_revenue_cents"] * q2["inputs"]["asm_m"][1] / 100.0, rel=1e-4)
    spread = model["share_of_fuel_cost_per_asm_change_range"]
    assert spread["p10"] <= spread["p50"] <= spread["p90"]
    q1 = next(q for q in payload["management_comparison"]["quarters"] if q["period"] == "2026Q1")
    assert q1["implied_by_statement"] is None and "no recovered share" in q1["implied_by_statement_note"]


# ------------------------------------------------------------------ the builder and its output

def test_builder_is_deterministic_for_a_fixed_seed(inputs, payload):
    again = fc.build(seed=7, n_draws=500, generated_at=STAMP, **inputs)
    assert json.dumps(again, sort_keys=True) == json.dumps(payload, sort_keys=True)
    other = fc.build(seed=8, n_draws=500, generated_at=STAMP, **inputs)
    assert other["curve_draws"] != payload["curve_draws"]
    assert [r["estimate"] for r in other["curve"]] == [r["estimate"] for r in payload["curve"]]
    assert other["industry_monthly"] == payload["industry_monthly"]
    with pytest.raises(ValueError):
        fc.build(seed=7, n_draws=100, **inputs)


def test_output_validates_and_keeps_the_contract_shape(inputs, payload):
    fc.validate(payload)
    assert "fixture" not in payload and payload["generated_at"] == STAMP
    assert payload["n"] == 30 and payload["n_unrestricted"] == 20          # smooth through latest; pre-specified fit split
    assert payload["headline_prespecified"].startswith(payload["headline_history_only"])
    assert "locked test quarters" in payload["headline_prespecified"]
    assert "locked test" not in payload["headline_history_only"]
    assert {i["path"] for i in payload["inputs"]} == {str(p.resolve()) for p in inputs.values()}
    for curve_key, draws_key in (("curve", "curve_draws"), ("curve_unrestricted", "curve_unrestricted_draws")):
        draws = np.array(payload[draws_key])
        assert draws.shape == (500, 5)
        for k, row in enumerate(payload[curve_key]):
            assert row["quarters_after"] == k and {"p10", "p50", "p90"} <= set(row)
            assert [row["p10"], row["p50"], row["p90"]] == pytest.approx(np.quantile(draws[:, k], [0.1, 0.5, 0.9]), abs=1e-3)
    old = payload["curve_unrestricted"]
    assert all(row["lo"] <= row["estimate"] <= row["hi"] for row in old)
    # The pre-specified draws are running sums whose first differences centre on the fitted lag coefficients.
    assert np.allclose(np.median(np.diff(np.array(payload["curve_unrestricted_draws"]), axis=1, prepend=0.0), axis=0),
                       [r["lag_coef"] for r in old], atol=0.05)
    assert [r["estimate"] for r in old] == pytest.approx(TRUE_CURVE, abs=0.35)
    rivals = payload["rivals"]
    assert rivals["costs_only"] == [0] * 5 and rivals["management_rule_for_scoring"]["curve"] == [0.85, 1, 1, 1, 1]
    assert [(m["period"], m["low"], m["high"], m["source_id"]) for m in rivals["management"]] == [
        ("2026Q3", 0.8, 0.9, "ual-2026q2-investor-update"), ("2026Q4", 1.0, 1.0, "ual-2026q2-investor-update")]
    assert set(rivals["management_paths"]["paths"]) == {"april_2026", "july_2026"}
    assert payload["oos_test"]["rules"]["management"] == [0.85, 1, 1, 1, 1]
    assert set(fc.CORE_RULES) <= set(payload["oos_test"]["scores"]["h4"]["test"]["mae_cents_per_asm"])
    assert payload["oos_test"]["summary"]["test_h4"]["n"] == 10 and payload["oos_test"]["summary"]["test_h1"]["n"] == 9
    assert payload["oos_test"]["scores"]["h1"]["test"]["left_out_pandemic_touched"] == ["2024Q1"]
    assert payload["oos_test"]["scores"]["h4"]["test"]["left_out_pandemic_touched"] == []
    assert {p["split"] for p in payload["united_points"]} == {"fit", "select", "test"}
    assert len(payload["industry_monthly"]) == 13 and set(payload["by_split"]) == {"fit", "select", "test"}
    assert len(payload["variants_tried"]) == len(fc.INDUSTRY_SPECS) + 1 + len(fc.UNITED_SPECS) + 2 + 1 + 3
    flat = payload["by_split"]["test"]
    assert flat["n_quarters"] == 10 and flat["n_quarters_one_quarter_ahead"] == 9
    assert flat["average_miss_fitted_curve"] == payload["oos_test"]["scores"]["h4"]["test"]["mae_cents_per_asm"]["fitted"]
    assert {"united", "industry"} <= set(flat) and "average_miss_management_path" in payload["by_split"]["fit"]
    share = payload["industry"]["recovery_share_conversion"]["fuel_share"]["mean_of_quarterly_ratios"]
    assert payload["asymmetry"]["up"] == pytest.approx(payload["asymmetry"]["up_elasticity"] / share, abs=1e-5)
    assert payload["asymmetry"]["unit"].startswith("share of a fuel cost change")
    assert payload["promise_comparison"]["quarters_after"] == 1 and payload["promise_comparison"]["period"] == "2026Q3"
    assert payload["industry"]["recovery_share_conversion"]["recovery_share"][12]["share"] == pytest.approx(
        payload["industry_monthly"][12]["coef"]
        / payload["industry"]["recovery_share_conversion"]["fuel_share"]["mean_of_quarterly_ratios"], abs=1e-4)
    for text in [payload["headline"], payload["recovery_definition"]] + payload["plain_summary"]:
        assert isinstance(text, str) and len(text) > 20
    assert payload["plain_summary"][0] == payload["headline"] and len(payload["plain_summary"]) > 10
    assert all(not sentence.endswith(" ") and sentence[-1] in ".:" for sentence in payload["plain_summary"])
    assert any("co-movement" in limit.lower() for limit in payload["limits"])
    assert any("hedg" in limit.lower() for limit in payload["limits"])


def test_validate_rejects_broken_payloads(payload):
    for change in ({"fixture": True}, {"curve_draws": payload["curve_draws"][:100]}, {"limits": []},
                   {"asymmetry": {"up": 0.1, "down": 0.1, "p_value": 1.5}},
                   {"curve": [dict(r, p10=r["p90"] + 1) for r in payload["curve"]]},
                   {"inputs": [{"path": "x", "sha256": "abc"}]}):
        with pytest.raises(ValueError):
            fc.validate({**payload, **change})
    with pytest.raises(ValueError):
        fc.validate({k: v for k, v in payload.items() if k != "variants_tried"})
    with pytest.raises(ValueError):
        fc.validate({**payload, "curve": None})                    # a missing curve must come with the other nulls


def test_no_test_build_withholds_the_locked_years(inputs, payload):
    held = fc.build(seed=7, n_draws=500, generated_at=STAMP, evaluate_test=False, **inputs)
    fc.validate(held)
    assert held["curve_unrestricted"] == payload["curve_unrestricted"]
    assert held["curve_unrestricted_draws"] == payload["curve_unrestricted_draws"]
    assert held["smooth"]["published_as_curve"] == "fit_only" and held["smooth"]["through_latest"] is None
    assert held["curve"] == held["smooth"]["fit_only"]["curve"] == payload["smooth"]["fit_only"]["curve"]
    assert held["smooth"]["expanding"][-1]["as_of_period"] == "2023Q4" and held["n"] == 20
    assert "status" in held["oos_test"]["smooth_fit_only"]["scores"]["h4"]["test"]
    assert {p["split"] for p in held["united_points"]} == {"fit", "select"}
    assert held["management_comparison"] is None and held["united"]["refit_all_clean_quarters_through_latest"] is None
    assert held["delta_look"] is None and "withheld" in held["industry"]["variants"][-1]["reason"]
    assert held["by_split"]["test"]["status"].startswith("withheld") and "Not stable" not in " ".join(held["limits"])
    for horizon in ("h1", "h4"):
        assert "status" in held["oos_test"]["scores"][horizon]["test"]
        assert "status" in held["by_split"]["test"]["united"][horizon]
    assert "status" in held["by_split"]["test"]["industry"] and held["oos_test"]["summary"]["test_h4"]["winner"] is None
    assert "withheld" in held["test_split"] and "Locked test" not in " ".join(held["plain_summary"])


def test_builder_runs_the_industry_part_alone_without_the_united_file(inputs, tmp_path):
    alone = fc.build(seed=7, n_draws=500, generated_at=STAMP, **{**inputs, "ual_path": tmp_path / "absent.csv"})
    fc.validate(alone)
    for key in ("curve", "curve_draws", "united_points", "oos_test", "management_comparison", "n"):
        assert alone[key] is None
    assert alone["united_status"]["computed"] is False and "absent" in alone["united_status"]["reason"]
    assert alone["industry"]["recovery_share_conversion"]["recovery_share"] is None
    assert "absent" in alone["industry"]["recovery_share_conversion"]["reason_null"]
    assert len(alone["industry_monthly"]) == 13 and alone["headline"].startswith("Industry fares")
    assert alone["rivals"]["management"] and alone["primary_spec"]["united"] is None
    assert "not been computed" in " ".join(alone["plain_summary"]) and isinstance(alone["plain_summary"], list)
    assert len(alone["inputs"]) == 5 and alone["delta_look"] is None and "smooth" not in alone
    assert alone["asymmetry"]["unit"].startswith("12-month elasticity")
    assert alone["asymmetry"]["up"] == alone["asymmetry"]["up_elasticity"]
    assert alone["by_split"]["fit"]["n_months"] == 84 and "average_miss_fitted_pct" in alone["by_split"]["test"]
    bare = fc.build(seed=7, n_draws=500, generated_at=STAMP, cpi_path=inputs["cpi_path"], fuel_path=inputs["fuel_path"],
                    **{k: tmp_path / "absent" for k in ("ual_path", "literature_path", "dal_path", "dal_check_path",
                                                        "demand_path")})
    fc.validate(bare)
    assert len(bare["inputs"]) == 2 and bare["rivals"]["published"] == [] and bare["delta_september_quarter"] is None
    assert [m["source_id"] for m in bare["rivals"]["management"]] == [None, None]
    assert all(v["id"] != fc.DEMAND_SPEC["id"] for v in bare["industry"]["variants"])


def test_script_writes_a_valid_file(inputs, tmp_path, monkeypatch, capsys):
    out = tmp_path / "out" / "fare_clock.json"
    monkeypatch.setattr(sys, "argv", ["build_fare_clock.py", "--seed", "7", "--draws", "500", "--out", str(out),
                                      "--cpi", str(inputs["cpi_path"]), "--fuel", str(inputs["fuel_path"]),
                                      "--ual", str(inputs["ual_path"]), "--literature", str(inputs["literature_path"]),
                                      "--generated-at", STAMP])
    build_fare_clock.main()
    written = json.loads(out.read_text())
    fc.validate(written)
    assert written["seed"] == 7 and json.loads(capsys.readouterr().out)["n"] == 30


def test_loader_rejects_a_table_that_is_not_one_row_per_quarter(tmp_path):
    table = simulate_table(5, 12)
    path = write_ual_csv(tmp_path / "ual.csv", table)
    loaded = fc.load_ual_quarterly(path)
    assert np.allclose(loaded["asm_m"], table["asm_m"], rtol=1e-6) and loaded["absent_quarters"] == []
    lines = path.read_text().splitlines()
    (tmp_path / "gap.csv").write_text("\n".join(lines[:5] + lines[6:]) + "\n")
    gap = fc.load_ual_quarterly(tmp_path / "gap.csv")
    assert gap["absent_quarters"] == ["2014Q1"] and np.isnan(gap["asm_m"][4])
    assert np.isnan(fc.united_panel(gap)["d_rev"][8])               # a change across the gap is missing, not bridged
    (tmp_path / "twice.csv").write_text("\n".join(lines + lines[-1:]) + "\n")
    (tmp_path / "bad_day.csv").write_text("\n".join(lines).replace("2013-06-30", "2013-06-29") + "\n")
    for name in ("twice.csv", "bad_day.csv"):
        with pytest.raises(ValueError):
            fc.load_ual_quarterly(tmp_path / name)


def test_delta_look_and_demand_variant(inputs, payload):
    delta = payload["delta_look"]
    assert delta["first_quarter_with_adjusted_figures"] == "2018Q1" and delta["last_quarter"] == "2026Q3"
    assert delta["quarters"][:2] == ["2019Q4", "2024Q1"] and delta["quarters"][-1] == "2026Q3"
    assert delta["n"] == 12 and delta["k"] == 7 and delta["quarters_in_fit_split"] == ["2019Q4"]
    assert len(delta["quarters_in_test_split"]) == 11
    assert "1 of its quarters fall in the fit split (2019Q4)" in delta["split_note"] and "in-sample" in delta["split_note"]
    assert "Second look, Delta (in sample on 12 quarters, 11 of them in the test years" in " ".join(payload["plain_summary"])
    assert [r["quarters_after"] for r in delta["curve"]] == list(range(5))
    assert [r["estimate"] for r in delta["united_curve"]] == [r["estimate"] for r in payload["curve_unrestricted"]]
    scored = delta["united_rules_on_delta"]
    assert scored["h4"]["n"] == 11 and scored["h1"]["n"] == 10 and set(fc.CORE_RULES) <= set(scored["h4"]["mae_cents_per_asm"])
    # The adjusted columns are used, not the GAAP ones (which the fixture inflates by 30%).
    table = fc.load_delta_quarterly(inputs["dal_path"])
    raw = fc.load_ual_quarterly(inputs["dal_path"])
    assert np.allclose(table["trasm_cents"][1:] * 1.3, raw["trasm_cents"][1:]) and np.isnan(table["trasm_cents"][0])
    assert payload["as_of"] == "2026-09-30" and payload["as_of_detail"]["delta_last_quarter"] == "2026-09-30"
    variant = next(v for v in payload["industry"]["variants"] if v["id"] == fc.DEMAND_SPEC["id"])
    assert variant["first_month"] == "2023-02" and variant["last_month"] == "2026-08" and variant["includes_test_years"]
    assert variant["k"] == 15 and len(variant["cumulative"]) == len(variant["cumulative_without_control_same_months"]) == 4
    months = fc.industry_panel(inputs["cpi_path"], inputs["fuel_path"])["months"]
    control = fc.demand_control(inputs["demand_path"], months)
    at = lambda label: int(np.flatnonzero(months == fc.month_index(date.fromisoformat(label)))[0])
    assert np.isnan(control[at("2020-01-01")]) and np.isfinite(control[at("2020-02-01")])   # a change needs two months
    assert fc.demand_control(inputs["demand_path"].with_name("absent.json"), months) is None
    assert any("Delta's September 2026 quarter" in limit and "$3.50" in limit and "+$0.50" in limit
               for limit in payload["limits"])
    assert any("Timing inside a quarter is not measured" in limit for limit in payload["limits"])
    assert payload["delta_september_quarter"]["eps_position"] == "below_range"


# ------------------------------------------------------------------ wording from the numbers

def curve_rows(estimates, half_width):
    return [{"quarters_after": k, "estimate": e, "lo": e - half_width, "hi": e + half_width}
            for k, e in enumerate(estimates)]


def test_headline_goes_neutral_when_intervals_are_wide():
    wide = fc.united_headline(curve_rows([0.1, 0.5, 0.6, 0.6, 0.7], 1.2), 20)
    assert "too wide to tell full recovery from none" in wide and "clear" not in wide
    zero_only = fc.united_headline(curve_rows([0.0, 0.1, 0.2, 0.2, 0.2], 0.5), 20)
    assert "too wide to tell a revenue response from none" in zero_only and "clear" not in zero_only
    tight = fc.united_headline(curve_rows([0.1, 0.5, 0.6, 0.6, 0.7], 0.1), 20)
    assert "a response is clear" in tight and "short of the full cost" in tight and "too wide" not in tight
    full = fc.united_headline(curve_rows([0.1, 0.5, 0.8, 0.9, 0.95], 0.1), 20)
    assert "full recovery within a year is inside the range" in full
    assert "70%" in tight and "20 quarters" in tight and "60% to 80%" in tight
    assert fc.verdict(-0.5, 1.5, 0.8, 0.9) == "neutral"
    assert fc.verdict(0.6, 1.0, 0.8, 0.9) == "consistent_with_promise"
    assert fc.verdict(-0.2, 0.5, 0.8, 0.9) == "below_promise_zero_not_excluded"
    assert fc.verdict(0.2, 0.5, 0.8, 0.9) == "partial"
    assert fc.verdict(0.95, 1.4, 0.8, 0.9) == "above_promise"
    assert fc.verdict(-0.9, -0.1, 0.8, 0.9) == "below_zero"
    neutral = fc.promise_line(0.5, -0.5, 1.5, 0.8, 0.9, 20, 1, "2026Q3")
    assert "too wide to tell the 80% to 90% management stated for 2026Q3 from no recovery" in neutral
    assert "consistent" not in neutral and "below" not in neutral
    assert "less than the 80% to 90%" in fc.promise_line(0.35, 0.2, 0.5, 0.8, 0.9, 20, 1, "2026Q3")
    assert "consistent with the 100%" in fc.promise_line(0.9, 0.7, 1.1, 1.0, 1.0, 20, 2, "2026Q4")
    flat = fc.industry_headline({"lag_months": 12, "coef": 0.04, "lo": -0.07, "hi": 0.15}, 84)
    assert "includes zero" in flat and "+0.40%" in flat and "-0.70% to +1.50%" in flat
    assert "fares rose with fuel" in fc.industry_headline({"lag_months": 12, "coef": 0.2, "lo": 0.1, "hi": 0.3}, 84)


def test_committed_output_is_valid_and_not_a_fixture():
    if not fc.OUTPUT.is_file():
        pytest.skip("app/static/data/fare_clock.json has not been built in this checkout")
    committed = json.loads(fc.OUTPUT.read_text())
    fc.validate(committed)
    assert "fixture" not in committed and committed["interval_level"] == fc.LEVEL
    assert stats.quantiles(np.array(committed["curve_draws"]), (0.5,))[0] == pytest.approx(
        [r["p50"] for r in committed["curve"]], abs=1e-3) if committed["curve"] else True
