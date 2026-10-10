import csv
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np
import pytest

from app.data import fuel_volatility
from app.lab import range_check as RC
from app.lab import response as R
from app.lab import scenario as S

ROOT = Path(__file__).resolve().parents[1]
BRITISH = re.compile(r"\b(behaviours?|travellers?|modell(ed|ing)|favou?rable|coloured|colours?|centre|analyse[ds]?|summaris\w+)\b", re.I)


def q(name: str) -> int:
    return int(name[:4]) * 4 + int(name[5]) - 1


def seasonal(first="2013Q1", last="2026Q2", growth=.01) -> dict[int, float]:
    """A level with a fixed seasonal pattern and steady growth: nothing a naive seasonal forecast would miss."""
    return {i: (.1, -.05, .2, 0)[i % 4] + growth * (i - q(first)) for i in range(q(first), q(last) + 1)}


def synthetic_curve() -> dict:
    """Evenly spaced placeholder recovery curve with a common +/-0.2 shift. Not an estimate of anything."""
    shift = np.random.default_rng(0).uniform(-.2, .2, size=(500, 1))
    return {"measured": R.from_draws(np.array([.2, .4, .6, .8, 1.0]) + shift, name="measured",
                                     source={"kind": "synthetic", "detail": "placeholder curve for tests"}, note="Synthetic.")}


# ------------------------------------------------------------------ the statistic

def test_seasonality_and_steady_growth_are_not_surprises_and_a_level_shift_is_one():
    flat = seasonal()
    for ahead in RC.HORIZONS:
        assert all(abs(row["value"]) < 1e-12 for row in RC.surprises(flat, ahead))
    shifted = {i: v + (.05 if i >= q("2017Q1") else 0) for i, v in flat.items()}
    found = {row["quarter"]: round(row["value"], 12) for row in RC.surprises(shifted, 2) if abs(row["value"]) > 1e-12}
    # Missed for the two quarters a two-quarter-old forecast could not know, then mirrored a year later through the growth it carries.
    assert found == {"2017Q1": .05, "2017Q2": .05, "2018Q1": -.05, "2018Q2": -.05}
    assert RC.surprises(shifted, 2)[0]["value"] == pytest.approx(
        shifted[q("2014Q3")] - shifted[q("2014Q1")] - (shifted[q("2013Q3")] - shifted[q("2013Q1")]))


def test_pandemic_quarters_are_left_out_and_the_two_periods_never_mix():
    levels = seasonal()
    low, high = q(RC.PANDEMIC[0]), q(RC.PANDEMIC[1])
    for ahead in RC.HORIZONS:
        rows = RC.surprises(levels, ahead)
        fit, check = ([r["quarter"] for r in rows if r["split"] == split] for split in ("fit", "check"))
        assert fit[0] == RC.label(q("2013Q1") + 4 + ahead) and fit[-1] == "2019Q4"
        assert check[0] == RC.label(q("2022Q1") + 4 + ahead) and check[-1] == "2026Q2"
        assert len(fit) + len(check) == len(rows)
        for row in rows:
            used = [q(row["quarter"]) - back for back in (0, ahead, 4, 4 + ahead)]
            assert not any(low <= i <= high for i in used)
            assert all(i // 4 <= 2019 for i in used) or all(i // 4 >= 2022 for i in used)
        without = RC.surprises(levels, ahead, skip=("2022Q1",))
        assert len(without) == len(rows) - 1 and check[0] not in {r["quarter"] for r in without}
    assert [len([r for r in RC.surprises(levels, h) if r["split"] == "fit"]) for h in RC.HORIZONS] == [22, 21, 20]
    assert [len([r for r in RC.surprises(levels, h) if r["split"] == "check"]) for h in RC.HORIZONS] == [12, 11, 10]
    holed = {i: v for i, v in levels.items() if i != q("2016Q2")}
    assert not {"2016Q2", "2016Q4", "2017Q2", "2017Q4"} & {r["quarter"] for r in RC.surprises(holed, 2)}


def test_history_is_read_from_the_quarterly_table(tmp_path):
    table = tmp_path / "quarters.csv"
    table.write_text("period_end,total_revenue_musd,asm_m,adj_pretax_income_musd,fuel_price_usd_gal,fuel_expense_musd\n"
                     "2015-03-31,9000,60000,,2.00,1800\n2015-06-30,10000,62500,500,,2000\n2015-09-30,,64000,600,1.50,\n")
    levels, last = RC.load_history(table)
    assert last == "2015-09-30"
    assert levels["unit_revenue"] == {q("2015Q1"): pytest.approx(math.log(.15)), q("2015Q2"): pytest.approx(math.log(.16))}
    assert levels["operating_margin"] == {q("2015Q2"): .05}  # blank income, and a quarter without revenue, are left out
    assert levels["fuel_price"] == {q("2015Q1"): pytest.approx(math.log(2)), q("2015Q3"): pytest.approx(math.log(1.5))}
    assert levels["fuel_share"] == {q("2015Q1"): .2, q("2015Q2"): .2}


# ------------------------------------------------------------------ fit and coverage

def test_fit_finds_the_noise_that_reproduces_the_spread_and_stops_at_the_grid_ends():
    def squares(sd):
        return [.02 ** 2 + (k * sd) ** 2 for k in (1.0, 1.2, 1.4)]

    spread = [{"demand_monthly_log_sd": sd, "mean_square": squares(sd)} for sd in RC.GRID]
    counts = dict(zip(RC.HORIZONS, (22, 21, 20)))

    def history(sd, scale=1.0):
        return {h: [scale * math.sqrt(square) * (-1) ** i for i in range(counts[h])] for h, square in zip(RC.HORIZONS, squares(sd))}

    exact = RC.fit(history(.0125), spread)
    assert exact["value"] == .0125 and exact["observations"] == 63 and not exact["at_lower_bound"] and not exact["at_upper_bound"]
    assert exact["target_rms"] == pytest.approx(math.sqrt(sum(n * s for n, s in zip(counts.values(), squares(.0125))) / 63))
    assert RC.fit(history(.0111), spread)["value"] == pytest.approx(.0111, abs=2e-4)  # between grid points
    low, high = RC.fit(history(0, scale=.5), spread), RC.fit(history(.06, scale=3), spread)
    assert low["value"] == 0 and low["at_lower_bound"] and high["value"] == RC.GRID[-1] and high["at_upper_bound"]
    with pytest.raises(ValueError, match="must rise"):
        RC.fit(history(.01), [{"demand_monthly_log_sd": sd, "mean_square": [1e-4] * 3} for sd in RC.GRID])


def test_coverage_counts_real_outcomes_inside_each_horizons_range():
    rows = {h: [{"quarter": "2015Q1", "split": "fit", "value": v} for v in (-.03, -.01, 0, .01, .03)]
               + [{"quarter": "2024Q1", "split": "check", "value": v} for v in (.02, .5)] for h in RC.HORIZONS}
    found = RC.coverage(rows, [(-.01, .01), (-.02, .02), (-.05, .05)], "fit")
    assert found == {"inside": 3 + 3 + 5, "of": 15, "share": round(11 / 15, 3),
                     "by_horizon": [{"quarters_ahead": h, "inside": n, "of": 5} for h, n in zip(RC.HORIZONS, (3, 3, 5))]}
    assert RC.coverage(rows, [(-.01, .01), (-.02, .02), (-.05, .05)], "check")["inside"] == 2
    assert RC.coverage({h: [] for h in RC.HORIZONS}, [(-1, 1)] * 3, "fit")["share"] is None
    assert RC.roughly(.7) and RC.roughly(.8) and RC.roughly(.9)
    assert not RC.roughly(.61) and not RC.roughly(1.0) and not RC.roughly(None)
    assert [RC.reading(share) for share in (.61, .76, .85, 1.0)] == ["too narrow", "about right", "about right", "too wide"]


# ------------------------------------------------------------------ the simulator's side

def test_simulated_quarters_are_the_runners_own_and_the_gap_compares_two_worlds():
    modes = synthetic_curve()
    sim = RC.simulate(.03, paths=2000, response_modes=modes)
    assert sim["periods"] == ["2026Q4", "2027Q1", "2027Q2"] and all(sim[name].shape == (2000, 3) for name in (*RC.QUANTITIES, "operating_income_musd"))
    run = S.run_scenario({"fuel_shock_pct": 0, "paths": 2000, "demand_monthly_log_sd": .03}, modes)
    for i, quarter in enumerate(run["modes"]["measured"]["quarters"][:3]):
        assert quarter["period"] == sim["periods"][i]
        assert quarter["operating_income_musd"]["p50"] == round(float(np.median(sim["operating_income_musd"][:, i])), 1)
        assert quarter["operating_margin"]["p90"] == round(float(np.quantile(sim["operating_margin"][:, i], .9)), 4)
    gaps = RC.gap(sim["unit_revenue"])
    assert np.array_equal(gaps[:1000], -gaps[1000:]) and (gaps != 0).all()
    for (low, high), square in zip(RC.bands(sim, "unit_revenue"), RC.mean_squares(sim)):
        assert low == pytest.approx(-high) and 0 < high < 3 * math.sqrt(square)
    again = RC.simulate(.03, paths=2000, response_modes=modes)
    assert all(np.array_equal(sim[name], again[name]) for name in RC.QUANTITIES)
    with pytest.raises(ValueError, match="demand_monthly_log_sd must be between"):
        RC.simulate(.5, paths=2000, response_modes=modes)


def test_demand_noise_is_the_only_source_of_revenue_spread_when_fares_ignore_fuel():
    settings = {"mode": "costs_only", "paths": 2000}
    none, some, twice = (RC.mean_squares(RC.simulate(sd, **settings)) for sd in (0, .01, .02))
    assert none == [0, 0, 0]
    assert [b / a for a, b in zip(some, twice)] == pytest.approx([4, 4, 4], rel=.03)  # the gap scales with the noise
    # With fares following fuel, fuel noise alone spreads revenue, and demand noise adds to it.
    modes = synthetic_curve()
    floor, more = (RC.mean_squares(RC.simulate(sd, paths=2000, response_modes=modes)) for sd in (0, .02))
    assert all(0 < a < b for a, b in zip(floor, more))
    still = RC.simulate(.02, paths=2000, response_modes=modes, fuel_noise=False)
    assert not RC.gap(still["fuel_price"]).any() and RC.gap(still["unit_revenue"]).any()
    assert all(a < b for a, b in zip(RC.mean_squares(still), more))
    spread = [{"demand_monthly_log_sd": sd, "mean_square": RC.mean_squares(RC.simulate(sd, paths=2000, response_modes=modes))} for sd in (0, .02)]
    assert 0 < RC.simulation_error(RC.simulate(.01, paths=2000, response_modes=modes), [22, 21, 20], spread, .01) < .01


def test_fuel_window_is_the_engines_and_is_ranked_against_the_longer_history(tmp_path):
    rng = np.random.default_rng(11)
    days = [d for d in (date(2018, 1, 1) + timedelta(days=i) for i in range(3200)) if d.weekday() < 5 and d <= date(2026, 7, 16)]
    steps = np.where(np.array([d >= date(2026, 4, 1) for d in days]), .03, .01) * rng.standard_normal(len(days))
    prices = 2 * np.exp(np.cumsum(steps))
    rows = [{"date": d.isoformat(), "usd_per_gallon": float(round(p, 6))} for d, p in zip(days, prices)]
    known = [r for r in rows if r["date"] >= "2026-01-01"]
    history = tmp_path / "fuel_daily.csv"
    history.write_text("date,usd_per_gallon\n" + "".join(f"{r['date']},{r['usd_per_gallon']}\n" for r in rows) + "2026-07-17,9.99\n")
    found = RC.fuel_window(known, history)
    changes = np.diff(np.log([r["usd_per_gallon"] for r in rows]))
    monthly = float(changes[-65:].std(ddof=1)) * math.sqrt(21)
    assert found["trading_days"] == 65 and (found["from"], found["to"]) == (known[-66]["date"], "2026-07-16")
    assert found["monthly_log_sd"] == round(monthly, 4) == round(fuel_volatility(known) * math.sqrt(21), 4)
    against = found["against_history"]
    calm = float(np.std([c for r, c in zip(rows[1:], changes) if r["date"] <= "2019-12-31"], ddof=1)) * math.sqrt(21)
    assert against["status"] == "ok" and against["fit_years_monthly_log_sd"] == round(calm, 4) and against["times_fit_years"] == round(monthly / calm, 2)
    assert 2 < against["times_fit_years"] < 4 and against["windows_since"] == "2013-01-01"
    rolling = [float(changes[i - 65:i].std(ddof=1)) * math.sqrt(21) for i in range(65, len(changes) + 1)]
    assert against["windows"] == len(rolling) and against["share_of_windows_less_volatile"] == round(float(np.mean(np.array(rolling) < rolling[-1])), 3) > .95
    assert against["source"] == {"path": str(history), "sha256": hashlib.sha256(history.read_bytes()).hexdigest()}
    # A history that is not the engine's series, or no history, gives the window without a ranking.
    other = tmp_path / "other.csv"
    other.write_text("date,usd_per_gallon\n" + "".join(f"{r['date']},{r['usd_per_gallon'] * (1 + .01 * (i % 3))}\n" for i, r in enumerate(rows)))
    assert RC.fuel_window(known, other)["against_history"] == {"status": "not_computed", "reason": "other.csv does not match the engine's fuel prices over the window"}
    assert RC.fuel_window(known, tmp_path / "absent.csv")["against_history"]["status"] == "not_computed"
    assert RC.fuel_window(known, tmp_path / "absent.csv")["monthly_log_sd"] == found["monthly_log_sd"]


# ------------------------------------------------------------------ the published file

@pytest.fixture(scope="module")
def published():
    return json.loads(RC.OUTPUT.read_text())


@pytest.fixture(scope="module")
def rebuilt(published):
    return RC.build(generated_at=published["generated_at"])


def test_published_file_is_what_the_builder_writes_from_the_committed_inputs(published, rebuilt):
    assert rebuilt == published
    assert RC.OUTPUT.read_text() == json.dumps(rebuilt, indent=1, allow_nan=False) + "\n"
    for item in published["inputs"]:
        assert hashlib.sha256((ROOT / item["path"]).read_bytes()).hexdigest() == item["sha256"], item["path"]
    assert {"data/lab/ual_quarterly.csv", "data/lab/fuel_daily.csv"} <= {item["path"] for item in published["inputs"]}
    assert not any(item["path"].endswith("range_check.json") for item in published["inputs"])  # it does not cite itself


def test_published_file_has_the_agreed_fields_in_plain_american_sentences(published):
    RC.validate(published)
    assert set(RC.REQUIRED) == set(published) and published["question"] == "Is the simulator's range the right width?"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", published["generated_at"])
    with RC.QUARTERLY.open(newline="") as stream:
        assert published["as_of"] == max(row["period_end"] for row in csv.DictReader(stream))
    assert published["plain_summary"][0] == published["headline"] and len(published["plain_summary"]) >= 8
    for sentence in (*published["plain_summary"], *published["limits"], published["runner"]["statement"]):
        assert sentence.endswith(".") and len(sentence) < 520 and not BRITISH.search(sentence), sentence
    assert any("seasonality" in limit for limit in published["limits"])
    assert any("fuel volatility window is short and recent" in limit for limit in published["limits"])
    assert set(published["quantities"]) == set(RC.QUANTITIES) and published["compared"]["quantity"] == published["check"]["quantity"] == "unit_revenue"
    assert [h["quarters_ahead"] for h in published["compared"]["horizons"]] == list(RC.HORIZONS)
    for broken in ({k: v for k, v in published.items() if k != "limits"}, {**published, "inputs": [{"path": "x", "sha256": "abc"}]},
                   {**published, "headline": "/Users/someone/file"}, {**published, "plain_summary": "one string"},
                   {**published, "runner": {**published["runner"], "demand_monthly_log_sd": published["fitted_variant"]["fitted"]}},
                   {**published, "runner": {**published["runner"], "fitted_variant": {**published["runner"]["fitted_variant"], "used": True}}}):
        with pytest.raises(ValueError):
            RC.validate(broken)


def test_history_figures_in_the_file_come_straight_from_the_quarterly_table(published):
    with RC.QUARTERLY.open(newline="") as stream:
        table = {row["period_end"]: row for row in csv.DictReader(stream)}
    unit = {int(day[:4]) * 4 + (int(day[5:7]) - 1) // 3: math.log(float(r["total_revenue_musd"]) / float(r["asm_m"])) for day, r in table.items()}
    for split, years in (("fit", range(2013, 2020)), ("check", range(2022, 2100))):
        for row in published["quantities"]["unit_revenue"]["history"][split]:
            h = row["quarters_ahead"]
            misses = [100 * (unit[t] - unit[t - h] - unit[t - 4] + unit[t - 4 - h]) for t in sorted(unit)
                      if all(i in unit and i // 4 in years for i in (t, t - h, t - 4, t - 4 - h))]
            assert row["n"] == len(misses) and row["rms"] == pytest.approx(math.sqrt(sum(m * m for m in misses) / len(misses)), abs=1e-3)
            # The published range holds the published count of these outcomes (edges allow for the three-decimal rounding).
            for which in ("assumed", "fitted"):
                band = next(b for b in published["quantities"]["unit_revenue"]["simulator"][which] if b["quarters_ahead"] == h)
                counted = next(c for c in published["quantities"]["unit_revenue"]["coverage"][which][split]["by_horizon"] if c["quarters_ahead"] == h)
                assert band["p10"] == -band["p90"] and counted["of"] == len(misses)
                assert (sum(band["p10"] + 1e-3 <= m <= band["p90"] - 1e-3 for m in misses) <= counted["inside"]
                        <= sum(band["p10"] - 1e-3 <= m <= band["p90"] + 1e-3 for m in misses))


def test_the_assumption_is_what_is_tested_and_the_fitted_value_stays_on_file_as_the_variant_not_used(published):
    check, variant, runner, unit = published["check"], published["fitted_variant"], published["runner"], published["quantities"]["unit_revenue"]
    assumed, fitted = S.MonthlyScenario.demand_monthly_log_sd, variant["fitted"]
    assert runner["demand_monthly_log_sd"] == check["assumed"]["demand_monthly_log_sd"] == variant["assumed"] == assumed == RC.ASSUMED
    assert runner["status"] == "assumed" and check["kept"] == "assumed" and runner["fitted_variant"]["used"] is False
    assert check["fitted"]["demand_monthly_log_sd"] == runner["fitted_variant"]["demand_monthly_log_sd"] == fitted and 0 <= fitted <= RC.GRID[-1]
    for which in ("assumed", "fitted"):
        share = check[which]["share"]
        assert {k: v for k, v in check[which].items() if k != "demand_monthly_log_sd"} == unit["coverage"][which]["check"]
        assert check["passes"][which] == RC.roughly(share) and share == round(check[which]["inside"] / check[which]["of"], 3)
        for split in ("fit", "check"):
            assert variant["coverage"][which][split] == {k: unit["coverage"][which][split][k] for k in ("inside", "of", "share")}
    assert runner["coverage"] == variant["coverage"]["assumed"] and runner["fitted_variant"]["coverage"] == variant["coverage"]["fitted"]
    assert runner["check_passed"] == check["passes"]["assumed"] and runner["fitted_variant"]["check_passed"] == check["passes"]["fitted"]
    assert variant["status"] == ("not adopted" if check["passes"]["fitted"] else "rejected")
    # The headline and the runner's statement are these numbers, and say what follows from them.
    a, f = check["assumed"], check["fitted"]
    assert f"held {a['inside']} of United's {a['of']} unit-revenue outcomes" in published["headline"] and f"({a['share']:.0%}, against a nominal 80%)" in published["headline"]
    assert f"({fitted:.2%}) held {f['inside']} ({f['share']:.0%})" in published["headline"]
    assert published["headline"].endswith("so the assumption is kept.") == (check["passes"]["assumed"] and not check["passes"]["fitted"])
    assert f"an assumed {assumed:.0%} a month" in runner["statement"] and f"{a['inside']} of {a['of']} outcomes" in runner["statement"]
    assert f"({a['share']:.0%})" in runner["statement"] and f"{fitted:.2%}" not in runner["statement"]
    # The variant: fitted on the earlier years only, with its uncertainty.
    assert variant["fit_years"] == list(RC.FIT_YEARS) == [2013, 2019]
    assert variant["simulated_rms_at_fitted_pct"] == pytest.approx(variant["target_rms_pct"], rel=.01) or variant["at_lower_bound"] or variant["at_upper_bound"]
    assert variant["range_leaving_one_year_out"] == [min(variant["leave_one_year_out"].values()), max(variant["leave_one_year_out"].values())]
    assert set(variant["leave_one_year_out"]) == {str(year) for year in range(2014, 2020)}
    assert variant["range_leaving_one_year_out"][0] <= fitted <= variant["if_all_surprise_were_demand"]["fitted"]
    assert 0 < variant["simulation_standard_error"] < .01
    assert [row["simulated_rms_pct"] for row in variant["curve"]] == sorted(row["simulated_rms_pct"] for row in variant["curve"])
    # Less demand noise cannot widen the simulator's range for revenue, and it leaves fuel alone.
    for low, high in zip(unit["simulator"]["fitted"], unit["simulator"]["assumed"]):
        assert (low["p90"] <= high["p90"]) == (fitted <= assumed)
    assert published["quantities"]["fuel_price"]["simulator"]["fitted"] == published["quantities"]["fuel_price"]["simulator"]["assumed"]
    for name in ("operating_margin", "unit_revenue"):
        for split in ("fit", "check"):
            cover = published["quantities"][name]["coverage"]["assumed"][split]
            assert RC.reading(cover["share"]) in " ".join(published["plain_summary"])


def test_width_is_measured_with_one_noise_off_at_a_time_and_fuel_noise_names_its_window(published):
    width, fuel, runner = published["simulator"]["width"], published["simulator"]["fuel_noise"], published["runner"]
    assert runner["width"] == width and width["period"] == published["compared"]["horizons"][0]["simulator_period"]
    assert width["share_left_with_demand_noise_off"] == round(width["demand_noise_off_musd"] / width["musd"], 3)
    assert width["share_left_with_fuel_noise_off"] == round(width["fuel_noise_off_musd"] / width["musd"], 3)
    assert 0 < width["demand_noise_off_musd"] <= width["musd"] and 0 < width["fuel_noise_off_musd"] <= width["musd"]
    assert width["with_fitted_demand_noise_musd"] <= width["musd"]
    # The same width as the runner's own first no-shock quarter.
    first = published["simulator"]["no_shock"]["assumed"]["modes"]["measured"]["quarters"][0]["operating_income_musd"]
    assert width["musd"] == pytest.approx(first["p90"] - first["p10"], abs=.11)
    fuel_led = width["share_left_with_demand_noise_off"] > max(.5, width["share_left_with_fuel_noise_off"])
    assert ("sets most of the range's width" in runner["statement"]) == fuel_led
    assert any(s.startswith("Fuel volatility, not demand noise, sets most of the width.") for s in published["plain_summary"]) == fuel_led
    assert f"{width['share_left_with_demand_noise_off']:.0%} of the width" in runner["statement"]
    # Fuel noise is the engine's: the trading days before the cutoff, compared with the longer history.
    known = [r for r in S._context()["fuel"] if r["date"] <= S.INFORMATION_CUTOFF]
    assert fuel == RC.fuel_window(known) and fuel["to"] == known[-1]["date"] <= S.INFORMATION_CUTOFF
    assert fuel["monthly_log_sd"] == round(fuel_volatility(known) * math.sqrt(21), 4) and fuel["from"] == known[-fuel["trading_days"] - 1]["date"]
    against = fuel["against_history"]
    assert against["status"] == "ok" and against["times_fit_years"] == round(fuel["monthly_log_sd"] / against["fit_years_monthly_log_sd"], 2) == pytest.approx(
        runner["fuel_noise"]["times_fit_years"])
    assert f"the {fuel['trading_days']} trading days before the cutoff" in runner["statement"]
    assert ("an unusually volatile stretch" in runner["statement"]) == (against["share_of_windows_less_volatile"] >= .9)
    assert f"more volatile than {against['share_of_windows_less_volatile']:.0%} of such windows" in runner["statement"]


def test_the_check_years_do_not_move_the_fitted_value(tmp_path):
    with RC.QUARTERLY.open(newline="") as stream:
        table = list(csv.DictReader(stream))

    def rewritten(name, years):
        """The table with revenue in those years knocked about by 6% a quarter (unseasonal, so the statistic sees it)."""
        knocks = np.random.default_rng(7).normal(0, .06, size=len(table))
        path = tmp_path / name
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(table[0]))
            writer.writeheader()
            for row, knock in zip(table, knocks):
                noisy = int(row["period_end"][:4]) in years
                writer.writerow({**row, "total_revenue_musd": float(row["total_revenue_musd"]) * (1 + (knock if noisy else 0))})
        return path

    stamp = "2026-01-01T00:00:00Z"
    base = RC.build(paths=1000, generated_at=stamp)
    check_changed = RC.build(quarterly=rewritten("check.csv", range(2022, 2027)), paths=1000, generated_at=stamp)
    fit_changed = RC.build(quarterly=rewritten("fit.csv", range(2014, 2020)), paths=1000, generated_at=stamp)
    for key in ("fitted", "leave_one_year_out", "target_rms_pct", "if_all_surprise_were_demand", "curve"):
        assert check_changed["fitted_variant"][key] == base["fitted_variant"][key], key
    spread = lambda built, split: [row["rms"] for row in built["quantities"]["unit_revenue"]["history"][split]]
    assert spread(check_changed, "fit") == spread(base, "fit") and all(a > b for a, b in zip(spread(check_changed, "check"), spread(base, "check")))
    assert check_changed["check"]["assumed"]["inside"] < base["check"]["assumed"]["inside"]  # the check does see them
    assert fit_changed["fitted_variant"]["fitted"] > base["fitted_variant"]["fitted"] + .01 and spread(fit_changed, "check") == spread(base, "check")
    # Whatever is fitted, the runner's value in the file is the assumed one.
    assert {built["runner"]["demand_monthly_log_sd"] for built in (base, check_changed, fit_changed)} == {RC.ASSUMED}
    assert base["method"]["paths"] == 1000 and base["simulator"]["no_shock"]["fitted"]["demand_monthly_log_sd"] == base["fitted_variant"]["fitted"]


def test_the_runner_keeps_the_assumed_value_and_quotes_the_published_check(published):
    assumed, fitted = published["check"]["assumed"]["demand_monthly_log_sd"], published["fitted_variant"]["fitted"]
    noise = S.demand_noise()
    assert noise["monthly_log_sd"] == assumed == S.MonthlyScenario.demand_monthly_log_sd and noise["source"] == "engine_default"
    assert noise["check"] == {**published["runner"], "path": "app/static/data/range_check.json"}
    request = {"fuel_shock_pct": 0, "horizon_months": 12}
    for which, run in (("assumed", S.run_scenario(request)), ("fitted", S.run_scenario({**request, "demand_monthly_log_sd": fitted}))):
        assert run["request"]["demand_monthly_log_sd"] == (assumed if which == "assumed" else fitted)
        block = run["monte_carlo"]["demand_noise"]
        assert block["source"] == ("engine_default" if which == "assumed" else "request") and run["assumptions"][1].endswith(block["note"])
        assert block["check"] == noise["check"] and S.validate_response(run) == []
        saved = published["simulator"]["no_shock"][which]["modes"]
        assert set(saved) == {name for name, mode in run["modes"].items() if mode["status"] == "ok"} and saved
        for name, mode in saved.items():
            assert mode["fcf_negative_12m"] == run["modes"][name]["prob"]["fcf_negative_12m"]
            assert [row["operating_income_musd"] for row in mode["quarters"]] == [row["operating_income_musd"] for row in run["modes"][name]["quarters"][:4]]
    default = S.run_scenario(request)
    assert default["monte_carlo"]["demand_noise"]["note"] == published["runner"]["statement"]
    assert not any(item["path"].endswith("range_check.json") for item in default["inputs"])  # no number depends on it
