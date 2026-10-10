"""Daily view tests. Committed inputs only; the simulator runs once; no network."""
from datetime import timedelta
import json

import numpy as np
import pytest

from app.lab import forward_call as fw
from app.lab import market_clock, market_data, outlook
from app.lab import track_record as tr
from scripts.lab import build_signal

STAMP = "2026-10-10T00:00:00Z"


@pytest.fixture(scope="module")
def payload():
    if not fw.TRACK_RECORD.is_file():
        pytest.skip("app/static/data/track_record.json has not been built")
    return outlook.build(generated_at=STAMP)


def test_daily_runs_from_the_guidance_day_to_the_last_fuel_price(payload):
    rows = payload["daily"]
    fuel_dates, _ = market_data.load_fuel_daily()
    sessions, _, _ = market_data.aligned_log_returns([market_clock.TARGET, market_clock.MARKET])
    wanted = [d.isoformat() for d in sessions if payload["guidance"]["made_on"] <= d.isoformat() <= fuel_dates[-1].isoformat()]
    assert [row["date"] for row in rows] == wanted and rows[0]["date"] == "2026-07-15"
    assert payload["as_of"] == rows[-1]["date"] == fuel_dates[-1].isoformat()
    spot = {d.isoformat(): float(v) for d, v in zip(*market_data.load_fuel_daily())}
    for row in rows:
        assert row["fuel"] == (pytest.approx(spot[row["date"]]) if row["date"] in spot else None)
        assert row["eps_p10"] <= row["eps_p50"] <= row["eps_p90"]
        assert {"date", "fuel", "eps_p10", "eps_p50", "eps_p90", "stock_excess_since_guidance"} <= set(row)


def test_each_day_is_the_chain_at_that_day_s_fuel(payload):
    g = payload["guidance"]
    world = tr.World()
    cutoff = tr._day(payload["as_of"])
    inp = tr.forecast(world, outlook.TARGET, cutoff, cutoff + timedelta(days=1))["inputs"]
    for row in payload["daily"]:
        steps = tr.point_calls(inp, row["fuel_paid_expected"], 0.0)
        assert row["eps_costs_only"] == pytest.approx(steps["calls"]["costs_only"], abs=1e-5)
        assert row["eps_management"] == pytest.approx(g["eps_mid"] - (1 - g["management_share"]) * (g["eps_mid"] - row["eps_costs_only"]), abs=1e-5)
        # Fuel above the assumption lowers the costs-only call; the stated share keeps management's nearer the midpoint.
        assert (row["fuel_paid_expected"] > g["fuel_assumed_usd_gal"]) == (row["eps_costs_only"] < g["eps_mid"])
        assert abs(row["eps_management"] - g["eps_mid"]) <= abs(row["eps_costs_only"] - g["eps_mid"]) + 1e-9
    first = payload["daily"][0]
    assert abs(first["fuel_paid_expected"] - g["fuel_assumed_usd_gal"]) < 0.05        # the clock starts near United's own figure


def test_no_day_uses_a_later_fuel_price(payload, tmp_path):
    """Cut the fuel file at a day in the quarter: that day's row must not change."""
    day = "2026-08-20"
    full = tr.Paths()
    lines = full.fuel.read_text().splitlines()
    short = tmp_path / "fuel.csv"
    short.write_text("\n".join([lines[0]] + [line for line in lines[1:] if line.split(",")[0] <= day]) + "\n")
    world = tr.World(tr.Paths(fuel=short))
    rows, _ = outlook.daily_rows(world, payload["variant"], tr._day(day))
    assert rows[-1]["date"] == day
    then = next(row for row in payload["daily"] if row["date"] == day)
    for key in ("fuel", "fuel_paid_expected", "eps_p50", "eps_costs_only", "eps_management", "stock_excess_since_guidance"):
        assert rows[-1][key] == pytest.approx(then[key], abs=1e-6)


def test_estimate_is_settled_once_the_quarter_has_closed(payload):
    assert payload["quarter_end"] == "2026-09-30" and payload["settled_from"] == "2026-09-30"
    assert "settled" in " ".join(payload["plain_summary"]) and "cannot move" in payload["settled_note"]
    settled = [row for row in payload["daily"] if row["settled"]]
    assert len(settled) >= 2 and all(row["date"] >= "2026-09-30" for row in settled)
    assert len({row["eps_p50"] for row in settled}) == 1 and len({row["fuel_paid_expected"] for row in settled}) == 1
    moving = [row["eps_p50"] for row in payload["daily"] if not row["settled"]]
    assert len(set(moving)) > 10


def test_stock_line_is_the_hedged_return_summed_from_the_day_after_guidance(payload):
    sessions, returns, _ = market_data.aligned_log_returns([market_clock.TARGET, market_clock.MARKET])
    excess, _ = market_clock.excess_returns(returns[:, 0], returns[:, 1])
    by_day = {d.isoformat(): float(e) for d, e in zip(sessions, excess)}
    rows = payload["daily"]
    assert rows[0]["stock_excess_since_guidance"] == 0
    total = 0.0
    for row in rows[1:]:
        total += by_day[row["date"]]
        assert row["stock_excess_since_guidance"] == pytest.approx(total, abs=1e-5)


def test_last_day_equals_the_frozen_call(payload):
    if not fw.OUTPUT.is_file():
        pytest.skip("the forward call has not been frozen yet")
    call = json.loads(fw.OUTPUT.read_text())
    eps = call["forecast"]["chain"][3]
    last = payload["daily"][-1]
    assert payload["frozen_call"]["matches_last_row"] is True and payload["frozen_call"]["sha256"] == call["sha256"]
    assert [last["eps_p10"], last["eps_p50"], last["eps_p90"]] == pytest.approx([eps["p10"], eps["p50"], eps["p90"]], abs=1e-5)
    assert last["fuel_paid_expected"] == pytest.approx(call["forecast"]["chain"][0]["p50"], abs=1e-5)
    assert last["eps_costs_only"] == pytest.approx(call["forecast"]["scoring"]["baseline_calls"]["costs_only"], abs=1e-5)


def test_forward_block_is_one_simulator_run_from_today(payload):
    from app.lab import scenario
    forward = payload["forward"]
    assert forward["status"] == "ok" and forward["request"] == scenario.today_request()["request"]
    run = scenario.run_scenario(forward["request"])
    assert forward["run_id"] == run["run_id"] and set(forward["modes"]) == {"costs_only", "management", "measured"}
    for name, mode in forward["modes"].items():
        assert mode["status"] == "ok" and len(mode["quarters"]) == 4
        assert [q["period"] for q in mode["quarters"]] == [q["period"] for q in run["modes"][name]["quarters"][:4]]
        assert mode["quarters"][0]["operating_income_musd"] == run["modes"][name]["quarters"][0]["operating_income_musd"]
    first = {name: mode["quarters"][0]["operating_income_change_musd"]["p50"] for name, mode in forward["modes"].items()}
    assert first["costs_only"] < first["measured"] < first["management"] < 0          # fuel is above the reference today


def test_backtest_points_elsewhere_and_runs_no_rule(payload):
    back = payload["backtest"]
    track = json.loads(fw.TRACK_RECORD.read_text())
    assert "No trading rule" in back["definition"] and back["costs_bps_per_side"] is None
    assert back["results_day_test"]["by_split"] == track["results_day_test"]["by_split"]
    assert back["market_clock"]["survives_costs"] is False and "No tradable lag" in back["market_clock"]["plain"]
    assert set(back["by_split"]) == {"fit", "select", "test"}
    assert back["by_split"]["test"]["n_quarters"] == track["results_day_test"]["by_split"]["test"]["n"]
    assert back["variants_tried"] and "gap" not in payload["daily"][0]


def test_output_is_valid_plain_and_deterministic(payload, tmp_path):
    outlook.validate(payload)
    assert outlook.build(generated_at=STAMP, forward=False)["daily"] == payload["daily"]
    for key in ("generated_at", "as_of", "inputs", "headline", "plain_summary", "limits", "daily", "forward", "backtest"):
        assert payload[key] not in (None, "", [])
    assert isinstance(payload["plain_summary"], list) and payload["plain_summary"][0] == payload["headline"]
    assert all(not item["path"].startswith("/") and len(item["sha256"]) == 64 for item in payload["inputs"])
    assert "/Users/" not in json.dumps(payload) and not payload.get("fixture")
    out = tmp_path / "signal.json"
    build_signal.write(payload, out)
    assert json.loads(out.read_text()) == payload
    broken = json.loads(json.dumps(payload))
    broken["daily"][-1]["eps_p50"] += 0.2
    with pytest.raises(ValueError):
        outlook.validate(broken)


def test_published_file_is_the_rebuild_of_its_inputs(payload):
    if not outlook.OUTPUT.is_file():
        pytest.skip("app/static/data/signal.json has not been built")
    published = json.loads(outlook.OUTPUT.read_text())
    outlook.validate(published)
    if published["inputs"] != payload["inputs"]:
        pytest.skip("an input changed since app/static/data/signal.json was built; rebuild it")
    assert {**published, "generated_at": STAMP} == payload
