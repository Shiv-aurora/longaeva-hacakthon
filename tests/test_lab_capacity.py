"""Capacity response tests. Simulations are seeded; the build tests read only the committed inputs; no network."""
import copy
import hashlib
import json

import numpy as np
import pytest

from app.lab import capacity_response as cr
from app.lab import stats
from scripts.lab import build_capacity_response

STAMP = "2026-10-09T00:00:00Z"
TRUE_LAGS = np.array([0.0, -0.15, -0.25, -0.10, 0.0])     # nothing in the quarter of the rise, then a cut
TRUE_CURVE = np.cumsum(TRUE_LAGS)


# ------------------------------------------------------------------ simulator

def simulate(seed, n=168, first_year=1960, lags=TRUE_LAGS, noise=0.5, level=3.0, trend=0.0, fuel_seed=None):
    """A panel in which capacity growth responds to fuel changes by `lags` (points per 10% step)."""
    rng = np.random.default_rng(seed)
    fuel_rng = np.random.default_rng(seed if fuel_seed is None else fuel_seed)
    q = first_year * 4 + np.arange(n)
    log_fuel = np.cumsum(fuel_rng.normal(0, 0.12, n))
    steps = np.full(n, np.nan)
    steps[4:] = (log_fuel[4:] - log_fuel[:-4]) / cr.STEP
    asm = np.full(n, 60000.0)
    for t in range(8, n):
        growth = level + trend * (t - n / 2) + sum(lags[j] * steps[t - j] for j in range(len(lags))) + rng.normal(0, noise)
        asm[t] = asm[t - 4] * (1 + growth / 100.0)
    panel = cr.build_panel({int(i): float(a) for i, a in zip(q[4:], asm[4:])},
                           {int(i): float(2.0 * np.exp(v)) for i, v in zip(q, log_fuel)})
    return panel, int(q[8]), int(q[-1])


@pytest.fixture(scope="module")
def payload():
    return cr.build(generated_at=STAMP)


# ------------------------------------------------------------------ the regression

def test_recovers_a_known_delayed_response():
    panel, first, last = simulate(7)
    fit = cr.lag_fit(panel, first, last)
    assert fit["n"] == 160 and fit["k"] == 6 and fit["df"] == 154
    assert np.all(np.abs(fit["cumulative"] - TRUE_CURVE) < 0.08)
    assert np.all((fit["lo"] < TRUE_CURVE) & (TRUE_CURVE < fit["hi"]))
    assert abs(fit["cumulative"][0]) < 2 * fit["se"][0]                 # no response in the quarter of the rise
    assert fit["hi"][-1] < 0 and cr.verdict(cr.curve_rows(fit)) == "lower"
    assert np.all(fit["se"] >= fit["se_classical"] - 1e-12) and np.all(fit["se"] >= fit["se_newey_west"] - 1e-12)
    assert abs(fit["intercept"] - 3.0) < 0.2 and abs(fit["resid_sd"] - 0.5) < 0.08


def test_trend_specification_separates_a_trend_from_the_response():
    panel, first, last = simulate(8, trend=0.05)
    plain, trend = cr.lag_fit(panel, first, last), cr.lag_fit(panel, first, last, trend=True)
    assert abs(trend["controls"]["trend_per_quarter"] - 0.05) < 0.005
    assert np.all(np.abs(trend["cumulative"] - TRUE_CURVE) < 0.08)
    assert trend["bic"] < plain["bic"] and trend["resid_sd"] < plain["resid_sd"]


def test_an_indicator_removes_a_quarter_exactly():
    panel, first, last = simulate(9, n=80)
    drop = np.zeros(len(panel["q"]), dtype=bool)
    drop[[20, 21, 22, 40, 63]] = True
    fit = cr.lag_fit(panel, first, last, drop=drop)
    lags, kept = stats.lag_matrix(panel["fuel_steps"], cr.MAX_LAG)
    rows = [i for i, t in enumerate(kept) if first <= panel["q"][t] <= last and not drop[t]]
    direct = stats.ols(lags[rows], panel["growth"][kept[rows]])
    assert fit["n"] == len(rows) == 67 and fit["k"] == 6 and fit["df"] == direct.df_resid
    assert np.allclose(fit["beta"], direct.beta[1:]) and np.isclose(fit["intercept"], direct.beta[0])
    assert np.isclose(fit["resid_sd"], np.sqrt(direct.sigma2)) and np.isclose(fit["r2"], direct.r2)
    assert not set(panel["q"][drop].tolist()) & set(fit["quarters"].tolist())


def test_pandemic_quarters_and_what_they_touch_are_marked():
    q = np.arange(2010 * 4, 2026 * 4 + 3)
    panel = cr.build_panel({int(i): 60000.0 + i for i in q}, {int(i): 2.0 + 0.001 * (i % 7) for i in q})
    touched = [cr.quarter_label(int(t)) for t, flag in zip(panel["q"], panel["touched"]) if flag]
    level = [cr.quarter_label(int(t)) for t, flag in zip(panel["q"], panel["level_touched"]) if flag]
    assert (touched[0], touched[-1], len(touched)) == ("2020Q1", "2023Q4", 16)
    assert (level[0], level[-1], len(level)) == ("2020Q1", "2022Q4", 12)
    assert np.isnan(panel["growth"][:4]).all() and np.isfinite(panel["growth"][4:]).all()
    assert cr.split_of(2019 * 4 + 3) == "fit" and cr.split_of(2024 * 4) == "test" and cr.quarter_end(2026 * 4 + 1) == "2026-06-30"


def test_units_are_points_of_growth_per_ten_percent_of_fuel():
    q = np.arange(1990 * 4, 1994 * 4)
    fuel = {int(i): 2.0 * (1.1 if i >= q[8] else 1.0) for i in q}
    panel = cr.build_panel({int(i): 100.0 * (1.02 if i >= q[8] else 1.0) for i in q}, fuel)
    assert panel["fuel_steps"][8] == pytest.approx(1.0) and panel["fuel_pct"][8] == pytest.approx(10.0)
    assert panel["growth"][8] == pytest.approx(2.0) and "10%" in cr.UNIT


def test_joint_draws_follow_the_estimate_and_its_covariance():
    panel, first, last = simulate(7)
    fit = cr.lag_fit(panel, first, last)
    draws = cr.curve_draws(fit["beta"], fit["cov"], fit["df"], 4000, 3)
    assert draws.shape == (4000, 5)
    assert np.allclose(draws.mean(axis=0), fit["cumulative"], atol=0.01)
    assert np.allclose(draws.std(axis=0), fit["se"], rtol=0.1)
    assert np.array_equal(draws, cr.curve_draws(fit["beta"], fit["cov"], fit["df"], 4000, 3))
    assert not np.array_equal(draws, cr.curve_draws(fit["beta"], fit["cov"], fit["df"], 4000, 4))
    rows = cr.curve_rows(fit, draws)
    assert [row["quarters_after"] for row in rows] == [0, 1, 2, 3, 4]
    assert all(row["p10"] < row["p50"] < row["p90"] and row["lo"] < row["estimate"] < row["hi"] for row in rows)
    assert abs(np.corrcoef(draws[:, 3], draws[:, 4])[0, 1]) > 0.3       # joint, not independent by quarter


def test_pooled_estimate_recovers_a_common_response():
    one, _, _ = simulate(41, fuel_seed=40, level=2.0)
    two, _, _ = simulate(42, fuel_seed=40, level=5.0)
    pooled = cr.pooled_fit({"A": one, "B": two}, seed=1, n_boot=200)
    estimate = np.array([row["estimate"] for row in pooled["curve"]])
    assert pooled["rows_by_airline"] == {"A": 160, "B": 160} and pooled["n_quarters"] == 160
    assert np.all(np.abs(estimate - TRUE_CURVE) < 0.08)
    assert all(row["lo"] < row["estimate"] < row["hi"] for row in pooled["curve"])
    assert pooled == cr.pooled_fit({"A": one, "B": two}, seed=1, n_boot=200)
    trended, _, _ = simulate(43, fuel_seed=40, level=2.0, trend=0.05)
    with_trend = cr.pooled_fit({"A": trended, "B": two}, seed=1, n_boot=50, trend_for=("A",))
    assert np.all(np.abs(np.array([row["estimate"] for row in with_trend["curve"]]) - TRUE_CURVE) < 0.08)


# ------------------------------------------------------------------ wording

def rows_at(estimate, half):
    return [{"quarters_after": k, "estimate": estimate, "lo": estimate - half, "hi": estimate + half} for k in range(5)]


def test_headline_is_neutral_when_the_interval_spans_zero():
    text = cr.headline(rows_at(-0.1, 0.4), rows_at(0.1, 0.4), 24, "2014Q1 to 2019Q4")
    assert "no measurable response" in text and "cannot tell a response from none" in text and "24 quarters" in text
    assert "flown less" not in text and "did not cut" not in text


def test_headline_is_neutral_when_the_two_specifications_disagree():
    for primary, other in ((rows_at(0.1, 0.3), rows_at(0.5, 0.2)), (rows_at(-0.5, 0.2), rows_at(-0.1, 0.3))):
        text = cr.headline(primary, other, 24, "2014Q1 to 2019Q4")
        assert "cannot be told apart from zero" in text and "no response is established" in text


def test_headline_states_a_sign_only_when_both_specifications_show_it():
    assert "flown less" in cr.headline(rows_at(-0.5, 0.2), rows_at(-0.4, 0.2), 24, "x")
    up = cr.headline(rows_at(0.5, 0.2), rows_at(0.4, 0.2), 24, "x")
    assert "did not cut" in up and "not fuel causing more flying" in up
    assert [cr.verdict(rows_at(e, 0.2)) for e in (-0.5, 0.0, 0.5)] == ["lower", "none", "higher"]


def test_no_true_response_reads_as_none():
    panel, first, last = simulate(17, n=40, lags=np.zeros(5), noise=1.2)
    fit = cr.lag_fit(panel, first, last)
    rows = cr.curve_rows(fit)
    assert cr.verdict(rows) == "none"
    assert "cannot tell a response from none" in cr.headline(rows, rows, fit["n"], fit["sample"])


# ------------------------------------------------------------------ the output file

def test_build_is_deterministic_and_valid(payload):
    assert cr.build(generated_at=STAMP) == payload
    cr.validate(payload)
    json.dumps(payload, allow_nan=False)
    assert "fixture" not in payload and payload["generated_at"] == STAMP
    for item in payload["inputs"]:
        assert hashlib.sha256((cr.ROOT / item["path"]).read_bytes()).hexdigest() == item["sha256"]
    assert cr.build(generated_at=STAMP, seed=cr.SEED + 1)["curve_draws"] != payload["curve_draws"]


def test_output_carries_the_curve_its_draws_and_the_unit(payload):
    assert {"headline", "primary_spec", "curve", "curve_draws", "united_points", "delta_curve", "by_split", "n", "limits",
            "plain_summary", "variants_tried", "generated_at", "as_of", "inputs", "unit"} <= set(payload)
    draws = np.array(payload["curve_draws"])
    assert draws.shape == (cr.N_DRAWS, 5) and cr.N_DRAWS >= 500
    for k, row in enumerate(payload["curve"]):
        assert row["quarters_after"] == k and set(row) >= {"p10", "p50", "p90"}
        assert np.allclose([row["p10"], row["p50"], row["p90"]], np.quantile(draws[:, k], [0.1, 0.5, 0.9]), atol=1e-3)
    assert "10%" in payload["unit"] and payload["primary_spec"]["units"] == payload["unit"]
    assert payload["n"] == payload["primary_spec"]["n"] == payload["by_split"]["fit"]["n"]
    assert set(payload["by_split"]) >= {"fit", "select", "test"} and payload["by_split"]["select"]["n"] == 0
    assert len(payload["variants_tried"]) >= len(payload["variants"]) >= 2


def test_headline_follows_the_published_intervals(payload):
    rows = {v["id"]: v["curve"] for v in payload["variants"]}
    primary = payload["primary_spec"]["id"]
    other = next(v["curve"] for v in payload["variants"][:2] if v["id"] != primary)
    assert payload["headline"] == cr.headline(payload["curve"], other, payload["n"], payload["primary_spec"]["sample"])
    assert payload["verdict"] == cr.verdict(rows[primary])
    if payload["verdict"] == "none" or payload["verdict"] != payload["verdict_other_specification"]:
        assert "cannot" in payload["headline"] and "flown less" not in payload["headline"]
    assert payload["plain_summary"].startswith(payload["headline"])


def test_primary_curve_uses_no_pandemic_or_test_quarter(payload):
    used = [p for p in payload["united_points"] if p["in_estimation"]]
    assert len(used) == payload["n"] and all(p["split"] == "fit" and not p["pandemic_touched"] for p in used)
    assert payload["primary_spec"]["sample"] == "2014Q1 to 2019Q4"
    quiet = cr.build(generated_at=STAMP, evaluate_test=False)
    for key in ("curve", "curve_draws", "primary_spec", "headline", "variants"):
        assert quiet[key] == payload[key]
    assert quiet["by_split"]["test"] is None and quiet["delta_curve"] is None and quiet["pooled"] is None
    assert all(p["split"] != "test" for p in quiet["united_points"])
    assert payload["delta_curve"]["quarters_before_pandemic"] + payload["delta_curve"]["quarters_after_pandemic"] == payload["delta_curve"]["n"]


def test_builder_writes_what_it_validated(tmp_path, payload):
    out = tmp_path / "capacity_response.json"
    build_capacity_response.write(payload, out)
    assert json.loads(out.read_text()) == payload
    few = copy.deepcopy(payload)
    few["curve_draws"] = few["curve_draws"][:100]
    with pytest.raises(ValueError, match="at least 500"):
        build_capacity_response.write(few, tmp_path / "bad.json")
    for change in ({"fixture": True}, {"unit": "points"}, {"headline": ""}):
        with pytest.raises(ValueError):
            cr.validate({**payload, **change})
    with pytest.raises(ValueError, match="lacks"):
        cr.validate({k: v for k, v in payload.items() if k != "curve_draws"})
    with pytest.raises(ValueError, match="500"):
        cr.build(n_draws=100)


def test_published_file_is_the_rebuild_of_its_inputs(payload):
    if not cr.OUTPUT.is_file():
        pytest.skip("app/static/data/capacity_response.json has not been built")
    published = json.loads(cr.OUTPUT.read_text())
    cr.validate(published)
    if published["inputs"] != payload["inputs"]:
        pytest.skip("an input changed since app/static/data/capacity_response.json was built; rebuild it")
    assert {**published, "generated_at": STAMP} == payload
