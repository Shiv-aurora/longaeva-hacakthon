"""Daily view: the third-quarter estimate marked to fuel each day since guidance, and where the business goes next.

`daily` re-runs the track record's chain (app.lab.track_record) once per trading
day from United's guidance day to the last fuel price, with the cost clock's
estimate of the quarter's fuel price as it stood that day. It is the forward
call's method evaluated on earlier dates, not another forecast: on the last day
its EPS figures equal the frozen call's. Beside it is United's share price move
since guidance with the market's move taken out.

`forward` is one run of the business simulator from today's fuel price: the next
four quarters under each behaviour mode.

No trading rule is run here. `backtest` points to the market clock's result and to
the track record's results-day test.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re

from app.lab import cost_clock as cc
from app.lab import forward_call as fw
from app.lab import market_clock, market_data
from app.lab import track_record as tr

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "app" / "static" / "data" / "signal.json"
MARKET_CLOCK = ROOT / "app" / "static" / "data" / "market_clock.json"
TARGET = fw.TARGET


def _usd(value: float) -> str:
    return f"-${abs(value):.2f}" if value < 0 else f"${value:.2f}"


def _musd(value: float) -> str:
    return f"-${abs(value):,.0f} million" if value < 0 else f"${value:,.0f} million"


def clock_on(clock: tr.Clock, model, day: date) -> float:
    """The cost clock's expected price paid for the quarter from fuel prices up to `day`, with one model throughout.

    The model's latest report came out with the guidance, after the market closed on the guidance
    day. To keep one model for every row, prints after `day` are cut off here and the as-of date
    passed on is the following day, which the cost clock accepts for that model.
    """
    keep = [i for i, d in enumerate(clock.dates) if d <= day]
    dates, values = [clock.dates[i] for i in keep], clock.values[keep]
    got = cc.predicted_price_paid(TARGET, dates, values, as_of=max(day, model.last_filed_on + timedelta(days=1)),
                                  model=model, n_draws=32, seed=tr.SEED)
    return float(got["point"])


def daily_rows(world: tr.World, variant: str, cutoff: date) -> tuple[list[dict], dict]:
    """One row per trading day from the guidance day to the last fuel price."""
    quarter_end = cc.quarter_bounds(TARGET)[1]
    row = tr.forecast(world, TARGET, cutoff, cutoff + timedelta(days=1), as_of=cutoff if cutoff < quarter_end else None)
    inp, clock = row["inputs"], world.clock
    model = clock.model(inp["quarter_end"])
    sessions, returns, _ = market_data.aligned_log_returns([market_clock.TARGET, market_clock.MARKET], prices_path=world.paths.prices)
    excess, _ = market_clock.excess_returns(returns[:, 0], returns[:, 1])
    spot = dict(zip(clock.dates, clock.values))
    first, last = inp["guided_on"], clock.dates[-1]
    residuals = row["residuals"]
    at_mid = clock_on(clock, model, inp["late_from"])
    rows, total = [], 0.0
    for i, day in enumerate(sessions):
        if day < first or day > last:
            continue
        if day > first:
            total += float(excess[i])
        expected = clock_on(clock, model, min(day, inp["quarter_end"]))
        late = expected - at_mid if day > inp["late_from"] else 0.0
        steps = tr.point_calls(inp, expected, late)
        ours = tr.with_range(steps["calls"][variant], residuals)
        rows.append({"date": day.isoformat(), "fuel": float(spot[day]) if day in spot else None,
                     "fuel_paid_expected": expected, "eps_p10": ours["p10"], "eps_p50": ours["p50"], "eps_p90": ours["p90"],
                     "eps_costs_only": steps["calls"]["costs_only"], "eps_management": steps["calls"]["management"],
                     "stock_excess_since_guidance": total, "settled": day >= inp["quarter_end"]})
    return rows, row


def forward_block() -> dict:
    """The next four quarters from today's fuel price under each behaviour mode, from the business simulator."""
    from app.lab import scenario
    today = scenario.today_request()
    if today.get("status") != "ok":
        return {"status": "not_computed", "reason": today.get("reason") or "; ".join(today.get("errors", []))}
    run = scenario.run_scenario(today["request"])
    if run.get("status") != "ok":
        return {"status": "not_computed", "reason": "; ".join(run.get("errors", []))}
    modes = {}
    for name, mode in run["modes"].items():
        if mode.get("status") != "ok":
            modes[name] = {"status": "not_computed", "reason": mode.get("reason")}
            continue
        modes[name] = {"status": "ok",
                       "quarters": [{k: q[k] for k in ("period", "operating_income_musd", "operating_margin", "fcf_musd",
                                                       "operating_income_change_musd", "eps_change")} for q in mode["quarters"][:4]],
                       "fcf_negative_12m": mode["prob"]["fcf_negative_12m"]}
    return {"status": "ok", "request": today["request"], "basis": today["basis"], "fuel": run["fuel"], "run_id": run["run_id"],
            "paths": run["paths"], "starts_from": run["as_of"]["information_cutoff"], "shock_starts": run["as_of"]["shock_starts"],
            "modes": modes, "inputs": run["inputs"],
            "definition": "One run of the business simulator with today's fuel price held for twelve months, against the "
                          "same simulated worlds with fuel at its second-quarter 2026 level. operating_income_change_musd "
                          "and eps_change are against that reference; eps_change is after tax only.",
            "assumptions": today["assumptions"] + run["assumptions"][:4]}


def backtest_block(track: dict, market_path: Path = MARKET_CLOCK) -> dict:
    """Pointers to the two results that bear on trading; no rule is run here."""
    market = json.loads(Path(market_path).read_text()) if Path(market_path).is_file() else {}
    rule = market.get("trading_rule") or {}
    day = track["results_day_test"]
    return {"definition": "No trading rule is run on this page. Two results elsewhere bear on whether the daily estimate "
                          "could be traded: the market clock's test of a delayed share price reaction to fuel, and the "
                          "track record's results-day test.",
            "costs_bps_per_side": None,
            "costs_note": "Null on purpose: nothing is traded here, so no trading cost applies. The market clock's own rule "
                          "used the cost shown under market_clock.",
            "market_clock": {"source": "app/static/data/market_clock.json", "verdict": (market.get("verdict") or {}).get("label"),
                             "cost_bps_per_side": rule.get("cost_bps_per_side"),
                             "trading_rule_verdict": rule.get("verdict"), "survives_costs": rule.get("survives_costs"),
                             "statement": rule.get("statement"),
                             "plain": "No tradable lag was found: United's shares move with fuel on the same day, with no "
                                      "drift or rebound afterwards that survives costs." if rule else None},
            "results_day_test": {"source": "app/static/data/track_record.json", "question": day["question"],
                                 "by_split": day["by_split"]},
            "by_split": {name: {"n_quarters": part["n"], "return_per_dollar_of_gap": part["slope"], "range_low": part["lo"],
                                "range_high": part["hi"], "same_sign_share": part["hit_rate"]}
                         for name, part in day["by_split"].items() if name != "all"},
            "by_split_note": "The results-day test by period (see track_record.json), not trades. "
                             "return_per_dollar_of_gap is the two-day market-hedged return per dollar of gap between the "
                             "fitted call and United's midpoint (0.08 means eight percentage points per dollar); range_low "
                             "and range_high are its 90% range; same_sign_share is the share of quarters where the gap and "
                             "the return had the same sign.",
            "variants_tried": ["None here. The market clock lists the rules it tried in its own file."]}


def build(paths: tr.Paths | None = None, track_record: Path = fw.TRACK_RECORD, q3_call: Path = fw.OUTPUT,
          generated_at: str | None = None, forward: bool = True) -> dict:
    world = tr.World(paths)
    track = json.loads(Path(track_record).read_text())
    variant = fw.chosen_variant(world, track_record)["variant"]
    cutoff = max(fw.last_date(world.paths.fuel), world.reports[-1]["filed_on"])
    rows, row = daily_rows(world, variant, cutoff)
    inp = row["inputs"]
    first, last = rows[0], rows[-1]
    low_fuel, high_fuel = min(rows, key=lambda r: r["fuel_paid_expected"]), max(rows, key=lambda r: r["fuel_paid_expected"])
    settled = next((r["date"] for r in rows if r["settled"]), None)
    frozen = None
    if Path(q3_call).is_file():
        call = json.loads(Path(q3_call).read_text())
        if not call.get("fixture"):
            frozen = {"sha256": call["sha256"], "eps_p50": call["forecast"]["chain"][3]["p50"],
                      "matches_last_row": abs(call["forecast"]["chain"][3]["p50"] - last["eps_p50"]) < 5e-6}
    headline = (f"Marked to fuel each day, the estimate of United's third-quarter adjusted EPS moved from {_usd(first['eps_p50'])} "
                f"a share on {fw._date(first['date'])} to {_usd(last['eps_p50'])} by {fw._date(last['date'])}, against a guided "
                f"{_usd(inp['guidance_low'])} to {_usd(inp['guidance_high'])}; United's shares moved "
                f"{100 * math.expm1(last['stock_excess_since_guidance']):+.1f}% against the market over the same days.")
    summary = [headline,
               f"The expected price paid for the quarter's fuel went from {_usd(first['fuel_paid_expected'])} a gallon on the "
               f"guidance day to {_usd(last['fuel_paid_expected'])}; United assumed about {_usd(inp['assumed']['usd_gal'])}. Its "
               f"lowest reading was {_usd(low_fuel['fuel_paid_expected'])} on {fw._date(low_fuel['date'])} and its highest "
               f"{_usd(high_fuel['fuel_paid_expected'])} on {fw._date(high_fuel['date'])}.",
               f"On the last day the estimate is {_usd(last['eps_p50'])} a share (P10 {_usd(last['eps_p10'])}, P90 "
               f"{_usd(last['eps_p90'])}); with no recovery it would be {_usd(last['eps_costs_only'])}, and with the share "
               f"United stated, {_usd(last['eps_management'])}.",
               f"The quarter closed on {fw._date(inp['quarter_end'])}. From {fw._date(settled)} the estimate is settled: every "
               "fuel price of the quarter is known, so it does not move again until United reports."
               if settled else "The quarter has not closed in this data.",
               "This is the forward call's method on earlier dates, not a second forecast, and no trading rule is tested here."]
    block = forward_block() if forward else {"status": "not_computed", "reason": "not requested"}
    if block.get("status") == "ok":
        quarter = block["modes"]["measured"]["quarters"][0] if block["modes"].get("measured", {}).get("status") == "ok" else None
        if quarter:
            summary.append(f"From today's fuel price ({_usd(block['fuel']['level_usd_gal'])} a gallon paid, against "
                           f"{_usd(block['fuel']['reference_usd_gal'])} in the second quarter), the simulator puts "
                           f"{quarter['period']} operating income at {_musd(quarter['operating_income_musd']['p50'])} "
                           f"(P10 {_musd(quarter['operating_income_musd']['p10'])}, P90 "
                           f"{_musd(quarter['operating_income_musd']['p90'])}) with measured behaviour.")
    limits = [
        "Each day's estimate uses the cost clock refitted through the second-quarter 2026 report, which came out with the "
        "guidance on 15 July after the market closed; the first row therefore uses a report published that evening.",
        "The range is the same on every day: the spread of United's earlier misses against its own midpoint. It does not "
        "narrow as the quarter's fuel prices come in.",
        "The stock series is United's log return minus beta times the S&P 500 fund's, summed from the day after guidance, "
        f"with beta from the previous {market_clock.BETA_WINDOW} sessions. It is shown beside the estimate, not explained by it.",
        "Days on which no fuel price was published show no spot price; the estimate then uses prices up to the day before.",
        "The forward block is the simulator's view under stated assumptions (today's fuel held for twelve months, demand at "
        "plan), not a forecast of reported results.",
        "No delayed share price reaction to fuel was found by the market clock, so the gap between the two lines is not a "
        "trading signal."]
    payload = {
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": last["date"],
        "inputs": tr.inputs_list((world.paths.guidance, world.paths.quarterly, world.paths.fuel, world.paths.prices,
                                  world.paths.fare_clock, world.paths.literature, Path(track_record),
                                  *((Path(q3_call),) if frozen else ()), *((MARKET_CLOCK,) if MARKET_CLOCK.is_file() else ()))),
        "headline": headline, "plain_summary": summary, "limits": limits,
        "period": TARGET, "variant": variant,
        "guidance": {"made_on": inp["guided_on"].isoformat(), "eps_low": inp["guidance_low"], "eps_high": inp["guidance_high"],
                     "eps_mid": inp["mid"], "fuel_assumed_usd_gal": inp["assumed"]["usd_gal"],
                     "management_share": None if inp["management"] is None else inp["management"]["share"]},
        "quarter_end": inp["quarter_end"].isoformat(), "settled_from": settled,
        "settled_note": "The quarter closed on " + inp["quarter_end"].isoformat() + ". From then until United reports, the "
                        "estimate cannot move: every fuel price of the quarter is known.",
        "frozen_call": frozen,
        "daily_definition": {
            "fuel": "Gulf Coast jet fuel spot price that day, US dollars a gallon; null when none was published",
            "fuel_paid_expected": "the cost clock's expected price United pays per gallon over the whole quarter, from fuel "
                                  "prices up to that day and the last price held for the days still to come",
            "eps_p10, eps_p50, eps_p90": "United's midpoint marked to that fuel price with measured behaviour (the "
                                         "track record's chosen variant), US dollars a share",
            "eps_costs_only": "the same with no fuel cost recovered", "eps_management": "the same with the share United stated",
            "stock_excess_since_guidance": "United's market-hedged log return summed since the guidance day's close",
            "settled": "true once the quarter has closed"},
        "daily": rows, "forward": block, "backtest": backtest_block(track)}
    return cc._clean(payload)


REQUIRED = ("generated_at", "as_of", "inputs", "headline", "plain_summary", "limits", "daily", "forward", "backtest")


def validate(payload: dict) -> None:
    missing = [key for key in REQUIRED if key not in payload]
    if missing:
        raise ValueError(f"signal.json lacks {missing}")
    if payload.get("fixture"):
        raise ValueError("signal.json must not be a fixture")
    text = json.dumps(payload, allow_nan=False)
    if str(ROOT) in text or "/Users/" in text:
        raise ValueError("signal.json contains an absolute local path")
    if not all(set(item) >= {"path", "sha256"} and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in payload["inputs"]):
        raise ValueError("inputs must list path and sha256")
    rows = payload["daily"]
    if not rows or [r["date"] for r in rows] != sorted(r["date"] for r in rows):
        raise ValueError("daily must be a non-empty list in date order")
    if rows[0]["date"] != payload["guidance"]["made_on"] or rows[0]["stock_excess_since_guidance"] != 0:
        raise ValueError("daily must start on the guidance day with a zero stock move")
    for row in rows:
        if row["eps_p10"] is not None and not row["eps_p10"] <= row["eps_p50"] <= row["eps_p90"]:
            raise ValueError(f"{row['date']}: EPS quantiles are out of order")
    settled = [r for r in rows if r["settled"]]
    if settled and len({round(r["eps_p50"], 6) for r in settled}) != 1:
        raise ValueError("the estimate moves after the quarter closed")
