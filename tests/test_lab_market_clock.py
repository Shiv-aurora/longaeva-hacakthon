from datetime import date, timedelta
import json
import math
from pathlib import Path
import sys

import numpy as np
import pytest

from app.lab import market_clock as mc
from app.lab import stats
from scripts.lab import build_market_clock

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "app" / "static" / "data" / "market_clock.json"
SPLITS = {"fit": ("2013-01-01", "2015-12-31"), "select": ("2016-01-01", "2019-12-31"), "test": ("2020-01-01", None)}
LONG = date(2022, 12, 30)
STAMP = "2026-01-01T00:00:00Z"


def sessions(start: date, end: date) -> tuple:
    days, day = [], start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return tuple(days)


def synthetic_panel(seed: int = 7, start=date(2012, 1, 2), end=date(2018, 12, 31), drift: float = 0.0, jump_every: int = 0,
                    fuel_beta: float = -0.1, noise: float = 0.012, fuel_noise: float = 0.012) -> mc.Panel:
    """Random-walk levels. With jump_every, HO=F jumps 15% on those sessions and the
    target then earns `drift` a session for the next 20 sessions."""
    rng = np.random.default_rng(seed)
    dates = sessions(start, end)
    n = len(dates)
    market = rng.normal(0.0003, 0.009, n)
    fuel = rng.normal(0.0, 1.0, n) * fuel_noise
    extra = np.zeros(n)
    if jump_every:
        for e in range(300, n - 30, jump_every):
            fuel[e] += 0.15
            extra[e + 1:e + 21] += drift
    levels = {mc.MARKET: 100 * np.exp(np.cumsum(market)), mc.HO: 2 * np.exp(np.cumsum(fuel)),
              mc.FUEL: 2.1 * np.exp(np.cumsum(fuel + rng.normal(0, 0.004, n)))}
    for i, ticker in enumerate(mc.AIRLINES):
        own = (1.1 + 0.1 * i) * market + fuel_beta * fuel + rng.normal(0, noise, n) + (extra if ticker == mc.TARGET else 0.0)
        levels[ticker] = 50 * np.exp(np.cumsum(own))
    levels["JETS"][:200] = np.nan            # a late listing
    levels[mc.HO][[410, 733]] = np.nan       # sessions the future did not print
    levels[mc.FUEL][[55, 412, 900]] = np.nan
    return mc.Panel(dates, levels)


def bumped(panel: mc.Panel, after: int, seed: int = 99) -> mc.Panel:
    """The same panel with every level after index `after` multiplied by fresh noise."""
    rng = np.random.default_rng(seed)
    levels = {}
    for name, values in panel.levels.items():
        changed = values.copy()
        changed[after + 1:] *= np.exp(np.cumsum(rng.normal(0, 0.05, len(values) - after - 1)))
        levels[name] = changed
    return mc.Panel(panel.dates, levels)


def same(a, b) -> bool:
    return np.array_equal(np.asarray(a), np.asarray(b), equal_nan=True)


# ---------------------------------------------------------------- timing and look-ahead

def test_fuel_moves_skip_rolls_and_gaps_without_filling():
    dates = sessions(date(2024, 1, 29), date(2024, 2, 9))        # 1 February is the fourth session
    level = np.array([2.0, 2.2, 2.2, 3.0, 3.3, np.nan, 3.63, 3.63, 3.63, 3.993])
    moves = mc.fuel_moves(level, dates, skip_roll=True)
    step = math.log(1.1)
    assert math.isnan(moves.one_day[0]) and moves.one_day[1] == pytest.approx(step)
    assert math.isnan(moves.one_day[3]) and moves.step[3] == 0.0                    # the roll session is not a fuel move
    assert moves.one_day[4] == pytest.approx(step)                                   # and is the base for the next one
    assert math.isnan(moves.one_day[5]) and moves.step[5] == 0.0 and not moves.observed[5]
    assert math.isnan(moves.one_day[6]) and moves.step[6] == pytest.approx(step)     # two sessions: in sums, not a one-day return
    trailing = moves.trailing(5)
    assert np.isnan(trailing[:5]).all() and math.isnan(trailing[5])
    assert trailing[6] == pytest.approx(2 * step) and trailing[9] == pytest.approx(2 * step)
    raw = mc.fuel_moves(level, dates, skip_roll=False)
    assert raw.one_day[3] == pytest.approx(math.log(3.0 / 2.2))


def test_signals_betas_and_excess_returns_do_not_see_the_future():
    panel = synthetic_panel()
    cut = 900
    a, b = mc.prepare(panel), mc.prepare(bumped(panel, cut))
    for name in a.signals:
        assert a.signals[name][1] == (1 if name.startswith("eia") else 0)
        assert same(a.signals[name][0][:cut + 1], b.signals[name][0][:cut + 1])
        assert not same(a.signals[name][0][cut + 1:], b.signals[name][0][cut + 1:])
    for ticker in mc.AIRLINES:
        assert same(a.beta[ticker][:cut + 2], b.beta[ticker][:cut + 2])            # beta[t] stops at t-1
        assert same(a.excess[ticker][:cut + 1], b.excess[ticker][:cut + 1])
        assert not same(a.beta[ticker][cut + 2:], b.beta[ticker][cut + 2:])
    neutral = mc.prepare(panel, fuel_net_of_market=True), mc.prepare(bumped(panel, cut), fuel_net_of_market=True)
    assert same(neutral[0].signals["ho_5d"][0][:cut + 1], neutral[1].signals["ho_5d"][0][:cut + 1])
    assert np.isfinite(neutral[0].signals["ho_5d"][0]).sum() > 1000


def test_beta_for_a_session_ignores_that_session():
    panel = synthetic_panel()
    t = 700
    levels = {k: v.copy() for k, v in panel.levels.items()}
    levels[mc.TARGET][t:] *= 1.2                                                   # a 20% jump on session t
    shocked = mc.prepare(mc.Panel(panel.dates, levels), tickers=(mc.TARGET,))
    plain = mc.prepare(panel, tickers=(mc.TARGET,))
    assert shocked.beta[mc.TARGET][t] == plain.beta[mc.TARGET][t]
    assert shocked.excess[mc.TARGET][t] == pytest.approx(plain.excess[mc.TARGET][t] + math.log(1.2))


def test_event_definition_uses_only_earlier_sessions():
    panel = synthetic_panel(jump_every=90)
    cut = 1000
    for spec in (mc.EventSpec(), mc.EventSpec(overlap="drop_later"), mc.EventSpec(fuel=mc.FUEL), mc.EventSpec(threshold_window=300)):
        a, b = mc.events_for(panel, spec), mc.events_for(bumped(panel, cut), spec)
        assert same(a.threshold[:cut + 2], b.threshold[:cut + 2])                  # threshold[t] stops at t-1
        assert same(a.crossing[:cut + 1], b.crossing[:cut + 1])
        early_a, early_b = a.episodes <= cut, b.episodes <= cut
        assert same(a.episodes[early_a], b.episodes[early_b]) and len(a.episodes[early_a]) > 3
        assert same(a.kept[early_a], b.kept[early_b]) and same(a.side[early_a], b.side[early_b])
    events = mc.events_for(panel, mc.EventSpec())
    t = int(events.episodes[2])
    signal = events.signal.copy()
    signal[t] *= 10
    assert mc.event_threshold(signal, 0.95, 252)[t] == events.threshold[t]
    assert events.threshold[t] == pytest.approx(np.quantile(np.abs(events.signal[:t][np.isfinite(events.signal[:t])]), 0.95))
    assert np.isnan(events.threshold[:252]).all()


def test_overlap_rules_keep_non_overlapping_windows():
    events = np.array([10, 40, 80, 140, 150, 300])
    greedy = mc.thin(events, 65, "greedy")
    assert greedy["kept"].tolist() == [10, 80, 150, 300]                           # 140 falls within 65 sessions of kept 80
    assert np.diff(greedy["kept"]).min() > 65
    assert mc.thin(events, 65, "drop_later")["kept"].tolist() == stats.thin_events(events, 65, "drop_later")["kept"].tolist() == [10, 300]
    assert mc.thin(events[:4], 65, "greedy")["kept"].tolist() == greedy["kept"].tolist()[:2]   # later events change nothing earlier


def test_forward_windows_start_after_the_signal_and_end_inside_the_split():
    x = np.arange(10, dtype=float)
    assert mc.forward_sum(x, 3)[2] == 3 + 4 + 5 and mc.forward_sum(x, 3, skip=1)[2] == 4 + 5 + 6
    assert np.isnan(mc.forward_sum(x, 3)[7:]).all() and mc.forward_sum(x, 3)[6] == 7 + 8 + 9
    x[5] = np.nan
    assert np.isnan(mc.forward_sum(x, 3)[2:5]).all() and mc.forward_sum(x, 3)[5] == 6 + 7 + 8
    rows = mc.rows_in(100, (20, 59), reach=10)
    assert rows[20] and rows[49] and not rows[50] and not rows[19]
    dates = sessions(date(2012, 1, 2), LONG)
    bounds = mc.split_bounds(dates, SPLITS)
    assert dates[bounds["fit"][1]] <= date(2015, 12, 31) < dates[bounds["select"][0]] and bounds["test"][1] == len(dates) - 1
    assert "test" not in mc.split_bounds(dates[:1500], SPLITS)


def test_predictive_regressions_wait_for_publication():
    panel = synthetic_panel()
    prep = mc.prepare(panel, tickers=(mc.TARGET,))
    samples = {"s": (300, len(panel.dates) - 1)}
    for signal, wait in (("ho_1d", 0), ("eia_1d", 1)):
        values = prep.signals[signal][0]
        for shift, expected in ((wait + 1, 1.0), (wait, 0.0)):                     # the first tradable session, and one too early
            excess = np.full(len(values), np.nan)
            excess[shift:] = values[:len(values) - shift]
            excess = np.where(np.isfinite(excess), excess, 0.0)
            fake = mc.Prepared(prep.dates, prep.ret, {mc.TARGET: excess}, prep.beta, prep.same_day, prep.signals)
            got = mc.analyse_lagged(fake, mc.TARGET, samples, signal, horizons=(1,))["s"][0]
            assert got["coef"] == pytest.approx(expected, abs=0.08), (signal, shift)
    row = mc.analyse_lagged(prep, mc.TARGET, samples, "ho_5d", horizons=(20,))["s"][0]
    assert row["hac_lag"] >= 20 and row["hac_lag"] == mc.lagged_hac_lag(row["n"], 20)


def test_rule_enters_after_the_event_and_pays_costs_on_both_legs():
    n = 40
    dates = sessions(date(2024, 1, 1), date(2024, 3, 1))[:n]
    asset, market, beta = np.full(n, math.log(1.01)), np.full(n, math.log(1.002)), np.full(n, 1.5)
    rule = mc.RuleSpec("up", "rebound", hold=3, delay=1)
    run = mc.rule_returns(asset, market, beta, [10, 12, 30], [1, 1, 1], [39, 39, 32], rule, dates)
    daily, cost = 0.01 - 1.5 * 0.002, 5e-4 * 2.5
    assert not run["net"][:12].any() and not run["net"][15:].any()                 # nothing on the event day or the next
    assert run["gross"][12:15] == pytest.approx([daily] * 3)
    assert run["net"][12:15] == pytest.approx([daily - cost, daily, daily - cost])
    assert [t["event"] for t in run["trades"]] == [dates[10].isoformat()]          # busy at 12; 30 cannot close by 32
    assert run["trades"][0]["entry"] == dates[11].isoformat() and run["trades"][0]["exit"] == dates[14].isoformat()
    drift = mc.rule_returns(asset, market, beta, [10], [1], [39], mc.RuleSpec("up", "drift", 3, 0), dates)
    assert drift["gross"][11:14] == pytest.approx([-daily] * 3) and drift["trades"][0]["position"] == "short"
    assert len(mc.rule_grid()) == 144 and len(set(mc.rule_grid())) == 144


# ---------------------------------------------------------------- recovery of an injected effect

def test_injected_post_event_drift_is_recovered_and_absent_drift_is_not_invented():
    spec = mc.EventSpec()
    for drift, label in ((-0.004, "under_reaction"), (0.004, "over_reaction"), (0.0, "neither")):
        # Flat fuel apart from the jumps, which fill under 5% of sessions: every jump is an event and nothing else is.
        panel = synthetic_panel(seed=11, end=LONG, drift=drift, jump_every=105, noise=0.006, fuel_noise=0.0)
        prep = mc.prepare(panel, tickers=(mc.TARGET,))
        events = mc.events_for(panel, spec)
        sample = {"s": (253, len(panel.dates) - 1)}
        ups = mc.event_members(events, sample["s"], spec.h, 1, kept_only=True)
        assert len(ups) >= 15 and np.diff(ups).min() > spec.k + spec.h
        got = mc.study(prep.ret[mc.TARGET], prep.ret[mc.MARKET], ups, spec, None, prep.dates, seed=5, n_boot=500)
        post = got["post"]["20"]
        assert post["mean"] == pytest.approx(20 * drift, abs=0.02) and post["n"] == got["n_events"]
        assert mc.classify_after(post) == label
        path = {p["day"]: p for p in got["path"]}
        assert sorted(path) == list(range(-spec.k, spec.h + 1))
        assert path[20]["mean"] - path[0]["mean"] == pytest.approx(post["mean"]) and path[20]["lo"] <= path[20]["mean"] <= path[20]["hi"]
        with_intercept = mc.study(prep.ret[mc.TARGET], prep.ret[mc.MARKET], ups, mc.EventSpec(abnormal="market_model"), None,
                                  prep.dates, seed=5, n_boot=500)
        assert with_intercept["post"]["20"]["mean"] == pytest.approx(20 * drift, abs=0.03)
        lagged = mc.analyse_lagged(prep, mc.TARGET, sample, "ho_5d", horizons=(20,), block=(200, 3))["s"][0]
        assert mc.classify_after(lagged) == label
        if drift:
            assert math.copysign(1, lagged["coef"]) == math.copysign(1, drift) and lagged["ci_block"] is not None
            rule = mc.RuleSpec("up", "drift" if drift < 0 else "rebound", hold=20, delay=0)
            stats_ = mc.run_rule(prep, mc.TARGET, mc.rule_event_sets(panel, spec), sample, rule)["s"]
            assert stats_["sharpe_ann"] > 1.0 and stats_["mean_trade_net"] == pytest.approx(20 * abs(drift), abs=0.03)


# ---------------------------------------------------------------- wording

def test_wording_turns_neutral_when_the_interval_spans_zero():
    assert mc.spans_zero([-0.1, 0.2]) and mc.spans_zero(None) and mc.spans_zero([math.nan, 1.0]) and mc.spans_zero([0.0, 0.3])
    assert not mc.spans_zero([0.01, 0.2]) and not mc.spans_zero([-0.3, -0.01])
    clear = {"coef": -0.15, "ci": [-0.22, -0.08], "n": 600}
    vague = {"coef": -0.15, "ci": [-0.32, 0.02], "n": 600}
    said = mc.same_day_sentence(clear, "United", "2024-2026")
    assert "1.5% fall" in said and "-2.2% to -0.8%" in said and "n = 600" in said and "not distinguishable" not in said
    said = mc.same_day_sentence(vague, "United", "2024-2026")
    assert "not distinguishable from zero" in said and "fall in" not in said and "-1.5% per 10% fuel rise" in said
    flat = {h: {"coef": 0.2, "ci": [-0.1, 0.5], "interval": [-0.2, 0.6], "n": 500} for h in (20, 60)}
    said = mc.after_sentence(flat, clear, "United", "2024-2026")
    assert "not distinguishable from zero: neither a continued fall nor a rebound" in said and "reaction)" not in said
    down = {20: {"coef": -0.4, "ci": [-0.7, -0.1], "interval": [-0.75, -0.05], "n": 500}, 60: flat[60]}
    said = mc.after_sentence(down, clear, "United", "2024-2026")
    assert "was negative over the next 20 sessions (a continued fall: under-reaction)" in said
    assert "is not distinguishable from zero over the next 60 sessions" in said and "-4.0%, 95% interval -7.5% to -0.5%" in said
    assert "under-reaction" not in mc.after_sentence(down, vague, "United", "2024-2026")     # no same-day fall to under-react to
    up = {20: {"coef": 0.4, "ci": [0.1, 0.7], "interval": [0.05, 0.75], "n": 500}, 60: flat[60]}
    assert "was positive over the next 20 sessions (a rebound: over-reaction)" in mc.after_sentence(up, clear, "United", "2024-2026")
    assert mc.classify_after(flat[20]) == "neither" and mc.classify_after(down[20]) == "under_reaction"
    assert mc.classify_after({"coef": 0.4, "ci": [0.1, 0.7], "interval": [-0.05, 0.75]}) == "neither"   # the wider interval decides
    assert mc.classify_after(None) == "not_computed"
    assert mc.classify_after({"mean": 0.2, "interval": [0.1, 0.3], "n": 3}) == "too_few"
    assert mc.overall_verdict({"20": "neither", "60": "neither"}) == "neither"
    assert mc.overall_verdict({"20": "under_reaction", "60": "neither"}) == "under_reaction"
    assert mc.overall_verdict({"20": "under_reaction", "60": "over_reaction"}) == "mixed"
    assert mc.make_headline(vague, flat, "United", "2024-2026").count("not distinguishable from zero") == 2
    for sentence in (mc.make_headline(vague, flat, "United", "2024-2026"), mc.make_headline(clear, down, "United", "2024-2026")):
        assert not any(word in sentence.lower() for word in ("strong", "weak", "significant", "sharp", "large ", "small"))


def test_event_and_rule_sentences_report_nulls_as_nulls():
    counts = {"flagged": {"up": 9, "down": 7}}
    few = {"n_events": 3, "post": {"60": {"mean": 0.2, "interval": [0.1, 0.3], "n": 3}}}
    assert "too few to judge" in mc.event_sentence(few, counts, "United", "2024-2026")
    none = {"n_events": 0, "post": {"60": None}}
    assert "0 events" in mc.event_sentence(none, counts, "United", "2024-2026")
    null = {"n_events": 8, "post": {"60": {"mean": -0.03, "interval": [-0.12, 0.06], "n": 8}}}
    said = mc.event_sentence(null, counts, "United", "2024-2026")
    assert "not distinguishable from zero" in said and "-3.0% on average" in said and "9 before thinning" in said
    real = {"n_events": 8, "post": {"60": {"mean": -0.09, "interval": [-0.15, -0.03], "n": 8}}}
    assert "(continued fall)" in mc.event_sentence(real, counts, "United", "2024-2026")
    rule = {"side": "both", "bet": "rebound", "hold": 10, "delay": 1, "events": "all", "cost_bps": 5.0}
    splits = {"fit": {"sharpe_ann": -0.8, "n_trades": 30}, "select": {"sharpe_ann": 1.4, "n_trades": 25},
              "test": {"sharpe_ann": 0.3, "se_ann": 0.6, "ci": [-0.9, 1.5], "n_trades": 12}}
    said = mc.rule_sentence(rule, splits, {"test": {"dsr": 0.04}}, 144)
    assert "nothing survives costs" in said and "Sharpe 0.30 with standard error 0.60 over 12 trades" in said
    assert "deflated Sharpe ratio 0.04" in said and "-0.80 in the fit split and 1.40 in the select split" in said
    assert "long after a large fuel rise and short after a large fuel fall" in said
    assert mc.rule_verdict(splits["test"], {"dsr": 0.04}) == "not_distinguishable"
    good = {**splits, "test": {"sharpe_ann": 2.5, "se_ann": 0.6, "ci": [1.3, 3.7], "n_trades": 40}}
    assert "it survives costs and the haircut for 144 tries" in mc.rule_sentence(rule, good, {"test": {"dsr": 0.97}}, 144)
    said = mc.rule_sentence(rule, good, {"test": {"dsr": 0.80}}, 144)                # positive, but fails the haircut
    assert "under the bar for the best of 144 tries" in said and "survives" not in said
    assert mc.rule_verdict(good["test"], {"dsr": 0.80}) == "positive_below_bar" and mc.rule_verdict(good["test"], None) == "positive_below_bar"
    bad = {**splits, "test": {"sharpe_ann": -2.0, "se_ann": 0.6, "ci": [-3.2, -0.8], "n_trades": 40}}
    assert "the rule lost money after costs" in mc.rule_sentence(rule, bad, {"test": {"dsr": 0.0}}, 144)
    empty = {**splits, "test": {"sharpe_ann": None, "n_trades": 0}}
    assert "too few to compute a Sharpe ratio; nothing survives costs" in mc.rule_sentence(rule, empty, {}, 144)
    assert mc.rule_verdict(empty["test"], None) == "no_trades"


# ---------------------------------------------------------------- the build

@pytest.fixture(scope="module")
def built():
    panel = synthetic_panel(end=LONG, jump_every=45)
    pending = []
    kwargs = dict(inputs=[{"path": "synthetic", "sha256": "0" * 64}], generated_at=STAMP, splits=SPLITS, seed=3,
                  n_boot_events=150, n_boot_block=60)
    document = mc.build(panel, on_frozen=pending.append, **kwargs)
    return panel, kwargs, pending[0], document


def test_build_is_deterministic_and_matches_the_contract(built):
    panel, kwargs, pending, document = built
    assert mc.validate(document) == []
    assert json.dumps(document, allow_nan=False) == json.dumps(mc.build(panel, **kwargs), allow_nan=False)
    other = mc.build(panel, **{**kwargs, "seed": 4})
    assert other["event_study"]["path"] != document["event_study"]["path"]          # the seed drives the bands ...
    assert other["same_day"][0]["beta"] == document["same_day"][0]["beta"]         # ... and nothing else
    assert "fixture" not in document and document["as_of"] == panel.dates[-1].isoformat()
    assert [row["ticker"] for row in document["same_day"]] == list(mc.AIRLINES)
    assert [row["horizon_days"] for row in document["lagged"]] == list(mc.HORIZONS)
    assert [p["day"] for p in document["event_study"]["path"]] == list(range(-5, 61))
    assert set(document["by_split"]) == {"fit", "select", "test"} and 3 <= len(document["plain_summary"]) <= 4
    assert [row["ticker"] for row in document["peers"]] == list(mc.AIRLINES)
    counts = document["event_study"]["counts"]["test"]
    assert counts["thinned"]["up"] <= counts["flagged"]["up"] and document["event_study"]["n_events_flagged"] >= document["event_study"]["n_events"]
    rule = document["trading_rule"]
    assert rule["n_variants"] == 144 and set(rule["by_split"]) == {"fit", "select", "test"}
    assert rule["by_split"]["test"]["deflated"]["n_trials"] == 144
    assert len(document["variants_tried"]) == len(document["variants_detail"]) > 144
    assert sum("Chosen" in text for text in document["variants_tried"]) == 3
    assert mc.validate({k: v for k, v in document.items() if k != "headline"}) == ["missing headline"]
    assert mc.validate({**document, "fixture": True}) == ["fixture flag set"]


def test_headline_and_summary_are_regenerated_from_the_numbers(built):
    check_wording(built[3])


def check_wording(document: dict) -> None:
    test = document["by_split"]["test"]
    same_day = {s: {"coef": v["same_day_ual"]["beta"], "ci": v["same_day_ual"]["ci"], "n": v["same_day_ual"]["n"],
                    "period": f"{v['start'][:4]}-{v['end'][:4]}"} for s, v in document["by_split"].items()}
    after = {h: test["lagged_ual"][str(h)] for h in mc.HEADLINE_HORIZONS}
    period = f"{test['start'][:4]}-{test['end'][:4]}"
    assert document["headline"] == mc.make_headline(same_day["test"], after, "United", period)
    assert spans(document["headline"]) == [mc.spans_zero(same_day["test"]["ci"]),
                                           all(mc.spans_zero(after[h]["interval"]) for h in after)]
    rule = document["trading_rule"]
    model = document["primary_spec"]["event"]["abnormal"]
    summary = mc.make_plain_summary(same_day, after, document["event_study"]["up"]["thinned"][model]["test"],
                                    document["event_study"]["counts"]["test"], rule["chosen"], rule["by_split"],
                                    {s: v["deflated"] for s, v in rule["by_split"].items()}, rule["n_variants"], "United", period)
    assert document["plain_summary"] == summary and rule["statement"] == summary[3]
    verdict = mc.rule_verdict(rule["by_split"]["test"], rule["by_split"]["test"]["deflated"])
    assert rule["verdict"] == verdict and rule["survives_costs"] == (verdict in ("positive_below_bar", "survives"))
    assert rule["clears_selection_haircut"] == (verdict == "survives") == ("it survives costs" in summary[3])
    assert document["verdict"]["by_horizon"] == {str(h): mc.classify_after(after[h]) for h in after}


def spans(headline: str) -> list:
    first, second = headline.split(" After a 10% five-session rise")
    return ["not distinguishable from zero" in first, "not distinguishable from zero: neither" in second]


def test_specification_is_frozen_without_test_data_and_before_the_test_split(built):
    panel, kwargs, pending, document = built
    select_end = mc.split_bounds(panel.dates, SPLITS)["select"][1]
    settings = {k: v for k, v in kwargs.items() if k not in ("inputs", "generated_at")}
    frozen, moved = mc.freeze(panel, **settings), mc.freeze(bumped(panel, select_end), **settings)
    assert frozen["spec_sha256"] == moved["spec_sha256"] == document["spec_sha256"] == pending["spec_sha256"]
    assert frozen["explored"]["chosen_rule"] == moved["explored"]["chosen_rule"] == document["trading_rule"]["chosen"]
    assert mc.variant_strings(frozen["explored"]) == mc.variant_strings(moved["explored"]) == document["variants_tried"]
    assert mc._only(frozen["early"], ("fit", "select")) == mc._only(moved["early"], ("fit", "select"))
    assert pending["primary_spec"] == document["primary_spec"] and "by_split" not in pending and "headline" not in pending
    assert pending["as_of"] == panel.dates[select_end].isoformat() < SPLITS["test"][0]
    assert all(v["test"] == "not computed" or v["id"] in ("primary", "event_primary") or v["rule"] == document["trading_rule"]["chosen"]
               for v in document["variants_detail"])
    with pytest.raises(ValueError, match="after the select split"):
        mc.explore(panel, SPLITS, mc.EventSpec(), 3, 50)
    different = mc.finish(bumped(panel, select_end), frozen, kwargs["inputs"], STAMP)   # other test data, same fit and select
    assert different["by_split"]["fit"] == document["by_split"]["fit"] and different["by_split"]["test"] != document["by_split"]["test"]
    with pytest.raises(RuntimeError, match="changed when test data was added"):
        mc.finish(bumped(panel, select_end - 200), frozen, kwargs["inputs"], STAMP)


def write_inputs(folder: Path) -> tuple:
    panel = synthetic_panel(seed=21, end=LONG, jump_every=60)
    fuel, prices = folder / "fuel.csv", folder / "prices.csv"
    fuel.write_text("date,usd_per_gallon\n" + "".join(f"{d.isoformat()},{v:.3f}\n" for d, v in zip(panel.dates, panel.levels[mc.FUEL])
                                                      if math.isfinite(v)))
    rows = [f"{d.isoformat()},{t},{v:.4f},{v:.6f}\n" for t in (mc.MARKET,) + mc.AIRLINES + (mc.HO,)
            for d, v in zip(panel.dates, panel.levels[t]) if math.isfinite(v)]
    prices.write_text("date,ticker,close,adj_close\n" + "".join(sorted(rows)))
    return fuel, prices


def test_builder_script_is_deterministic_and_writes_the_specification_first(tmp_path, monkeypatch, capsys):
    fuel, prices = write_inputs(tmp_path)
    monkeypatch.setattr(mc, "SPLITS", SPLITS)                                      # the real splits need data to 2024
    args = ["build_market_clock.py", "--fuel", str(fuel), "--prices", str(prices), "--seed", "9", "--n-boot-events", "60",
            "--n-boot-block", "30", "--generated-at", STAMP]
    outputs = []
    for name in ("a.json", "b.json"):
        monkeypatch.setattr(sys, "argv", args + ["--out", str(tmp_path / name)])
        build_market_clock.main()
        outputs.append((tmp_path / name).read_bytes())
    assert outputs[0] == outputs[1]
    document = json.loads(outputs[0])
    assert mc.validate(document) == [] and document["generated_at"] == STAMP and document["as_of"] == LONG.isoformat()
    assert [i["path"] for i in document["inputs"]] == [str(fuel.resolve()), str(prices.resolve())]
    assert all(len(i["sha256"]) == 64 for i in document["inputs"])
    assert document["selection_data_end"] == "2019-12-31" and document["spec_frozen_before_test"] is True
    check_wording(document)

    def stop(*_):
        raise RuntimeError("stopped before the test split")
    monkeypatch.setattr(mc, "finish", stop)
    monkeypatch.setattr(sys, "argv", args + ["--out", str(tmp_path / "c.json")])
    with pytest.raises(RuntimeError, match="stopped before the test split"):
        build_market_clock.main()
    pending = json.loads((tmp_path / "c.json").read_text())                        # on disk before any test number exists
    assert pending["spec_sha256"] == document["spec_sha256"] and pending["primary_spec"] == document["primary_spec"]
    assert pending["status"].startswith("specification frozen") and "by_split" not in pending

    monkeypatch.undo()
    monkeypatch.setattr(mc, "SPLITS", SPLITS)
    monkeypatch.setattr(sys, "argv", args + ["--stage", "select", "--out", str(tmp_path / "d.json")])
    build_market_clock.main()
    early = json.loads((tmp_path / "d.json").read_text())
    assert set(early["fit_and_select"]["same_day"][mc.TARGET]) == {"fit", "select"} and early["spec_sha256"] == document["spec_sha256"]
    capsys.readouterr()


def test_committed_output_matches_the_contract_and_its_own_numbers():
    if not OUTPUT.exists():
        pytest.skip("market_clock.json has not been built")
    document = json.loads(OUTPUT.read_text())
    assert mc.validate(document) == []
    assert [i["path"] for i in document["inputs"]] == ["data/lab/fuel_daily.csv", "data/lab/prices_daily.csv"]
    assert all(len(i["sha256"]) == 64 and int(i["sha256"], 16) >= 0 for i in document["inputs"])
    assert document["spec_frozen_before_test"] is True and document["selection_data_end"] <= "2023-12-31"
    assert document["spec_sha256"] == mc.spec_hash(document["primary_spec"])
    check_wording(document)
    for row in document["same_day"]:
        assert set(row["by_split"]) == {"fit", "select", "test", "all"} and row["n"] == row["by_split"]["all"]["n"]
    assert len(document["variants_tried"]) == len(document["variants_detail"])
