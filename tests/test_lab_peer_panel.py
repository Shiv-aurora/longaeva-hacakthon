"""Peer panel tests: the tracked panel, a seeded synthetic panel, and the published output. Nothing reads the network."""
import copy
import csv
import json
import sys

import numpy as np
import pytest

from app.lab import fare_clock as fc
from app.lab import peer_panel as pp
from scripts.lab import build_peer_panel

STAMP = "2026-10-09T00:00:00Z"
TRUE_L, TRUE_RHO = 0.8, 0.6
WINDOW = {"first": fc.PANDEMIC_Q[0], "last": fc.PANDEMIC_Q[1], "label": "2020Q1-2021Q4", "source": "test"}


# ------------------------------------------------------------------ a synthetic panel

def synthetic_rows(seed, carriers=("UAL", "DAL", "AAL"), n_quarters=54, noise=0.02, common_noise=0.0,
                   long_run=TRUE_L, rho=TRUE_RHO):
    """Rows shaped like the tracked CSV: one fuel price for all carriers, revenue responding by L (1 - rho) rho^j."""
    rng = np.random.default_rng(seed)
    quarters = 2013 * 4 + np.arange(n_quarters)
    price = np.empty(n_quarters)
    price[0] = 2.5
    for t in range(1, n_quarters):
        price[t] = max(0.6, 2.5 + 0.85 * (price[t - 1] - 2.5) + rng.normal(0, 0.35))
    shared = rng.normal(0, common_noise, n_quarters) if common_noise else np.zeros(n_quarters)
    weights = long_run * (1 - rho) * rho ** np.arange(pp.HORIZON)
    rows = []
    for c, carrier in enumerate(carriers):
        asm = (60000.0 + 5000 * c) * np.exp(np.cumsum(rng.normal(0.006, 0.02, n_quarters)))
        fuel_cpasm = 100.0 * price * (1 + 0.05 * c) / 62.0 + rng.normal(0, 0.02, n_quarters)
        response = np.array([sum(weights[j] * fuel_cpasm[max(t - j, 0)] for j in range(pp.HORIZON))
                             for t in range(n_quarters)])
        unit_rev = (12.0 + c - 0.03 * 100.0 * np.log(asm / asm[0]) + response + shared
                    + rng.normal(0, noise, n_quarters))
        for t, q in enumerate(quarters):
            rows.append({"carrier": carrier, "period_end": fc.quarter_end(int(q)),
                         "total_revenue_musd": f"{unit_rev[t] * asm[t] / 100.0:.6f}",
                         "fuel_expense_musd": f"{fuel_cpasm[t] * asm[t] / 100.0:.6f}", "asm_m": f"{asm[t]:.6f}",
                         "fuel_scope_factor": "1", "revenue_source": "synthetic", "fuel_source": "synthetic",
                         "asm_source": "synthetic"})
    return rows


def write_panel(path, rows):
    with open(path, "w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=pp.COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def synthetic_data(tmp_path, **kw):
    data = pp.stack(pp.load_panel(write_panel(tmp_path / "panel.csv", synthetic_rows(**kw))))
    return data, np.flatnonzero(data["valid"] & ~pp.touched(data["q"], WINDOW))


def demand_file(path):
    rows = [{"period": fc.quarter_label(q), "yoy": 0.02 + 0.3 * (q < 2023 * 4), "complete": True}
            for q in range(2020 * 4, 2026 * 4 + 3)]
    path.write_text(json.dumps({"quarterly": rows}))
    return path


# ------------------------------------------------------------------ the tracked panel and its loader

def test_tracked_panel_loads_on_a_regular_grid():
    panel = pp.load_panel()
    assert list(panel) == ["UAL", "DAL", "AAL"]
    for table in panel.values():
        assert np.array_equal(np.diff(table["q"]), np.ones(len(table["q"]) - 1))
        assert all(table["sources"][column] for column in pp.SOURCE_COLUMNS)
        unit_rev = 100 * table["total_revenue_musd"] / table["asm_m"]
        assert np.nanmin(unit_rev) > 5 and np.nanmax(unit_rev) < 40
    assert fc.quarter_label(int(panel["UAL"]["q"][0])) == "2013Q1" and len(panel["UAL"]["q"]) >= 54
    assert fc.quarter_label(int(panel["DAL"]["q"][0])) == "2017Q4"
    assert np.isnan(panel["DAL"]["total_revenue_musd"][0])            # adjusted revenue is not printed for 2017Q4
    assert fc.quarter_label(int(panel["AAL"]["q"][0])) == "2014Q1"    # first full quarter after the merger


def test_tracked_panel_marks_americans_mainline_only_fuel():
    american = pp.load_panel()["AAL"]
    early = american["q"] <= 2019 * 4 + 3
    assert np.all(american["fuel_scope_factor"][early] > 1.1) and np.all(american["fuel_scope_factor"][~early] == 1.0)
    assert len(set(american["fuel_scope_factor"][early])) == 1
    assert all("mainline" in s for s in american["sources"]["asm_source"])
    assert np.all(pp.load_panel()["UAL"]["fuel_scope_factor"] == 1.0)
    scaled, raw = pp.carrier_panel(american), pp.carrier_panel(american, scale_fuel=False)
    assert np.allclose(scaled["fuel_cpasm"][early], raw["fuel_cpasm"][early] * american["fuel_scope_factor"][early])
    assert np.allclose(scaled["fuel_cpasm"][~early], raw["fuel_cpasm"][~early])


def test_loader_refuses_a_bad_file(tmp_path):
    rows = synthetic_rows(1, n_quarters=12)
    with pytest.raises(ValueError, match="one row per calendar quarter"):
        pp.load_panel(write_panel(tmp_path / "twice.csv", rows + rows[:1]))
    (tmp_path / "short.csv").write_text("carrier,period_end,asm_m\nUAL,2013-03-31,1\n")
    with pytest.raises(ValueError, match="lacks the columns"):
        pp.load_panel(tmp_path / "short.csv")


def test_blank_cells_and_absent_quarters_are_missing(tmp_path):
    rows = synthetic_rows(2, carriers=("UAL",), n_quarters=12)
    rows[3]["total_revenue_musd"] = ""
    del rows[6]
    table = pp.load_panel(write_panel(tmp_path / "gaps.csv", rows))["UAL"]
    assert len(table["q"]) == 12 and np.isnan(table["total_revenue_musd"][3]) and np.isnan(table["asm_m"][6])
    data = pp.stack({"UAL": table})
    assert not data["valid"][:11].any()     # a year-over-year change and four lags need nine quarters behind a row


def test_pandemic_window_comes_from_the_fare_clock_output(tmp_path):
    path = tmp_path / "fare_clock.json"
    path.write_text(json.dumps({"primary_spec": {"sample": "2015Q1 to 2026Q2, no quarter with a 2020Q2-2021Q3 level behind it"}}))
    window = pp.pandemic_window(path)
    assert (window["first"], window["last"], window["label"]) == (2020 * 4 + 1, 2021 * 4 + 2, "2020Q2-2021Q3")
    fallback = pp.pandemic_window(tmp_path / "absent.json")
    assert (fallback["first"], fallback["last"]) == tuple(fc.PANDEMIC_Q) and "PANDEMIC_Q" in fallback["source"]
    quarters = np.arange(2013 * 4, 2027 * 4)
    assert np.array_equal(pp.touched(quarters, fallback), fc.pandemic_touched(quarters, pp.REACH))
    published = pp.pandemic_window()
    assert published["first"] <= published["last"]


# ------------------------------------------------------------------ statistics

def test_quarter_clustered_errors_are_wider_than_naive_with_a_common_shock(tmp_path):
    data, rows = synthetic_data(tmp_path, seed=11, noise=0.02, common_noise=0.25)
    fit = pp.lag_regression(data, rows)
    last = fit["curve"][-1]
    assert last["se_cluster_quarter"] > 1.2 * last["se_naive"]
    assert last["se"] >= max(last["se_naive"], last["se_cluster_quarter"], last["se_cluster_quarter_hac4"]) - 1e-12
    assert fit["design_effect_one_year_share"] > 1.4
    assert fit["n_quarters_effective_analytic"] <= fit["n_quarters"] < fit["n_rows"]
    smooth = pp.smooth_fit(data, rows, seed=5, n_draws=500)
    honest = smooth["one_year_share"]["p95"] - smooth["one_year_share"]["p05"]
    naive = smooth["one_year_share_range_if_rows_were_independent"]
    assert honest > naive["p95"] - naive["p05"]
    assert smooth["n_quarters_effective"] <= smooth["n_quarters"] < smooth["n_rows"]


def test_cluster_covariance_matches_white_with_one_row_per_cluster():
    rng = np.random.default_rng(3)
    X = np.column_stack([np.ones(40), rng.normal(size=(40, 2))])
    resid = rng.normal(size=40)
    ours = pp.cluster_cov(X, resid, np.arange(40))
    bread = np.linalg.inv(X.T @ X)
    white = bread @ (X.T * resid ** 2) @ X @ bread
    assert np.allclose(ours, 40 / 39 * 39 / 37 * white)
    with pytest.raises(ValueError):
        pp.cluster_cov(X, resid, np.zeros(40))


def test_smooth_fit_recovers_known_long_run_share_and_speed(tmp_path):
    data, rows = synthetic_data(tmp_path, seed=7, noise=0.01)
    fit = pp.smooth_fit(data, rows, seed=1, n_draws=500)
    assert abs(fit["L"] - TRUE_L) < 0.05 and abs(fit["rho"] - TRUE_RHO) < 0.05
    truth = TRUE_L * (1 - TRUE_RHO ** 5)
    assert fit["one_year_share"]["p05"] < truth < fit["one_year_share"]["p95"]
    assert abs(fit["one_year_share"]["estimate"] - truth) < 0.03
    assert fit["draws"].shape == (500, pp.HORIZON) and np.all(np.diff(fit["draws"], axis=1) * np.sign(fit["draws"][:, :1]) >= -1e-12)
    assert fit["rows_by_carrier"] == {"UAL": 30, "DAL": 30, "AAL": 30}


def test_profile_with_one_carrier_is_the_fare_clocks(tmp_path):
    data, rows = synthetic_data(tmp_path, seed=9, carriers=("UAL",), noise=0.05)
    ours = pp.smooth_profile(data["y"][rows], data["lags"][rows], pp.base_matrix(data, rows))
    theirs = fc.smooth_profile(data["y"][rows], data["lags"][rows], data["cap"][rows])
    assert ours == pytest.approx(theirs)


def test_quarter_resamples_keep_every_carrier_of_a_quarter_together():
    quarters = np.array([1, 1, 1, 2, 2, 3, 4, 4, 4, 5])
    for index in pp.quarter_resamples(quarters, 50, seed=4, block=2):
        drawn = quarters[index]
        for q in np.unique(drawn):
            assert (drawn == q).sum() % (quarters == q).sum() == 0


def test_out_of_sample_scores_each_carrier_with_n(tmp_path):
    data, rows = synthetic_data(tmp_path, seed=13, noise=0.02)
    clean = np.zeros(len(data["q"]), dtype=bool)
    clean[rows] = True
    oos = pp.out_of_sample(data, clean)
    assert oos["chosen_after_test_seen"] is True and oos["rules"]["management_path"].tolist() == [0.45, 0.85, 1.0, 1.0, 1.0]
    assert [r["carrier"] for r in oos["by_carrier"]] == ["UAL", "DAL", "AAL"] and all(r["n"] == 10 for r in oos["by_carrier"])
    assert oos["all_carriers"]["n"] == 30
    for row in oos["by_carrier"]:      # the true curve is partial adjustment, so it beats the fixed rules here
        assert row["lowest_mae"] == "pooled_smooth"


# ------------------------------------------------------------------ builder and output

def test_builder_is_deterministic_and_valid(tmp_path):
    panel = write_panel(tmp_path / "panel.csv", synthetic_rows(21, noise=0.03, common_noise=0.05))
    demand = demand_file(tmp_path / "demand.json")
    kw = dict(panel_path=panel, fare_clock_path=tmp_path / "absent.json", demand_path=demand, seed=3, n_draws=500,
              generated_at=STAMP)
    first, second = pp.build(**kw), pp.build(**kw)
    assert first == second and json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    pp.validate(first)
    assert first["inputs"][0] == {"path": str(panel.resolve()), "sha256": pp._sha(panel)}
    assert len(first["pooled"]["curve_draws"]) == 500
    assert first["pooled"]["n_rows"] == 90 and first["pooled"]["n_quarters"] == 30
    assert first["variant_with_2022"]["status"] == "computed" and first["variant_with_2022"]["rows_added"] == 24
    assert first["comparison_with_united_only"]["one_year_share_united_only"] is None
    assert first["comparison_with_united_only"]["united_only_refitted_here"]["n_rows"] == 30
    assert pp.build(**{**kw, "seed": 4})["pooled"]["curve_draws"] != first["pooled"]["curve_draws"]
    with pytest.raises(ValueError, match="at least 500"):
        pp.build(**{**kw, "n_draws": 100})


def test_variant_is_not_computed_without_the_traveller_series(tmp_path):
    panel = write_panel(tmp_path / "panel.csv", synthetic_rows(22))
    payload = pp.build(panel_path=panel, fare_clock_path=tmp_path / "absent.json", demand_path=tmp_path / "absent2.json",
                       seed=3, n_draws=500, generated_at=STAMP)
    assert payload["variant_with_2022"]["status"] == "not computed" and "VARIANT" in payload["variant_with_2022"]["label"]


def test_script_writes_the_output_from_the_tracked_style_panel(tmp_path, monkeypatch, capsys):
    panel = write_panel(tmp_path / "panel.csv", synthetic_rows(23))
    out = tmp_path / "peer_recovery.json"
    monkeypatch.setattr(sys, "argv", ["build_peer_panel.py", "--panel", str(panel), "--out", str(out), "--draws", "500",
                                      "--fare-clock", str(tmp_path / "none.json"), "--demand", str(tmp_path / "none2.json"),
                                      "--generated-at", STAMP])
    build_peer_panel.main()
    written = json.loads(out.read_text())
    pp.validate(written)
    assert written["generated_at"] == STAMP and "tracked file used as it is" in capsys.readouterr().out


def test_published_json_validates_and_matches_the_tracked_panel():
    payload = json.loads(pp.OUTPUT.read_text())
    pp.validate(payload)
    assert "fixture" not in payload
    assert payload["inputs"][0] == {"path": "data/lab/peer_quarterly.csv", "sha256": pp._sha(pp.PANEL_CSV)}
    data = pp.stack(pp.load_panel())
    window = payload["definition"]["pandemic_window"]
    clean = data["valid"] & ~pp.touched(data["q"], window)
    pooled = payload["pooled"]
    assert pooled["n_rows"] == int(clean.sum()) and pooled["n_quarters"] == len(np.unique(data["q"][clean]))
    assert sum(pooled["rows_by_carrier"].values()) == pooled["n_rows"]
    assert pooled["n_quarters_effective"] <= pooled["n_quarters"] < pooled["n_rows"]
    long_run, rho = pp.smooth_profile(data["y"][clean], data["lags"][clean], pp.base_matrix(data, np.flatnonzero(clean)))
    assert pooled["L"] == pytest.approx(long_run, abs=1e-6) and pooled["rho"] == pytest.approx(rho)
    assert len(pooled["curve_draws"]) >= 500
    assert [row["carrier"] for row in payload["by_carrier"]["table"]] == ["UAL", "DAL", "AAL"]
    assert payload["oos"]["chosen_after_test_seen"] is True
    assert payload["headline"] == payload["plain_summary"][0]
    manifest = json.loads(pp.MANIFEST_JSON.read_text())
    assert manifest["output"]["sha256"] == pp._sha(pp.PANEL_CSV)
    assert set(manifest["stores"]) == {"sec_xbrl", "bts_t100"} and all(len(s["sha256"]) == 64 for s in manifest["stores"].values())


def test_validate_refuses_fixtures_short_draws_and_unlabelled_variants(tmp_path):
    panel = write_panel(tmp_path / "panel.csv", synthetic_rows(24))
    payload = pp.build(panel_path=panel, fare_clock_path=tmp_path / "a.json", demand_path=tmp_path / "b.json",
                       seed=3, n_draws=500, generated_at=STAMP)
    for change in (lambda p: p.update(fixture=True), lambda p: p["pooled"].update(curve_draws=p["pooled"]["curve_draws"][:10]),
                   lambda p: p["variant_with_2022"].update(label="extra quarters"), lambda p: p.pop("limits"),
                   lambda p: p["oos"].update(chosen_after_test_seen=False)):
        broken = copy.deepcopy(payload)
        change(broken)
        with pytest.raises(ValueError):
            pp.validate(broken)


# ------------------------------------------------------------------ words

def fit_with(p05, p95, estimate=0.8, half_life=(0.8, 1.6), at_bound=0.0):
    return {"one_year_share": {"estimate": estimate, "p05": p05, "p10": p05, "p50": estimate, "p90": p95, "p95": p95},
            "half_life_quarters": 1.2, "half_life_range": {"p05": half_life[0], "p95": half_life[1]},
            "share_of_resamples_at_rho_upper_bound": at_bound, "n_rows": 68, "n_quarters": 31, "n_quarters_effective": 16.0}


def test_headline_goes_neutral_when_the_range_is_wide():
    names = ["United", "Delta", "American"]
    narrow = pp.headline(fit_with(0.70, 0.90), names)
    assert "has made up 80%" in narrow and "70% to 90%" in narrow and "1.2 quarters" in narrow
    wide = pp.headline(fit_with(0.40, 1.10), names)
    assert "does not pin down" in wide and "between 40% and 110%" in wide and "80%" not in wide
    none = pp.headline(fit_with(-0.20, 0.90), names)
    assert "cannot tell" in none and "-20% to 90%" in none and "80%" not in none
    slow = pp.headline(fit_with(0.70, 0.90, half_life=(0.4, 13.5), at_bound=0.2), names)
    assert "how fast is not pinned down" in slow and "has made up 80%" in slow
    assert pp.is_wide(fit_with(0.40, 1.10)["one_year_share"]) and not pp.is_wide(fit_with(0.70, 0.90)["one_year_share"])


def test_comparison_reports_whether_pooling_narrowed_the_range():
    pooled = {**fit_with(0.60, 0.90), "L": 0.9}
    carriers = {"table": [{"carrier": "UAL", "one_year_share": fit_with(0.5, 1.0)["one_year_share"], "n_rows": 30,
                           "range_width_p05_p95": 0.5, "half_life_quarters": 1.0, "half_life_range": {}}]}
    united = {"one_year_share": {"estimate": 0.75, "p05": 0.5, "p10": 0.55, "p50": 0.7, "p90": 0.9, "p95": 1.0},
              "half_life_quarters": 1.0, "half_life_range": {}, "L": 0.8, "n": 30, "field": "x", "path": "y", "sha256": "z"}
    out = pp.comparison(pooled, carriers, united)
    assert out["narrowed"] is True and out["width_change_share"] == pytest.approx(-0.4)
    assert out["range_width_united_only"] == pytest.approx(0.5) and out["range_width_pooled"] == pytest.approx(0.3)
    assert out["united_only_refitted_here"]["pooled_width_over_this"] == pytest.approx(0.6)
    missing = pp.comparison(pooled, carriers, None)
    assert missing["one_year_share_united_only"] is None and missing["narrowed"] is None and missing["reason"]
