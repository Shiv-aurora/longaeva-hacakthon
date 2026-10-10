"""Forward call: one dated forecast for United's third quarter of 2026, frozen before the results.

The forecast is the track record's chain (app.lab.track_record) run on the quarter
that has not been reported yet, with the variant the replay chose. Nothing here is
a second method: `forecast()` is the same function that produced every replayed
quarter. The chain has four links, each with P10, P50 and P90 and management's own
number beside it:

  1. fuel price paid per gallon   cost clock refitted through the latest report; range from its real misses
  2. recovery share               share of the fuel surprise that comes back in the quarter
  3. unit revenue (TRASM) change  the fare clock's equation, one quarter ahead
  4. adjusted EPS                 United's midpoint marked to (1) with (2), plus the spread of its earlier misses

Freezing: everything that is the forecast sits in one `forecast` block. Its
canonical JSON (sorted keys, no spaces) is hashed with SHA-256 and the same bytes
are written to data/lab/q3_call_frozen.json. `score()` later fills `outcome` and
`scores` beside the block and never changes it.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np

from app.lab import cost_clock as cc
from app.lab import fare_clock as fc
from app.lab import stats
from app.lab import track_record as tr

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "app" / "static" / "data" / "q3_call.json"
FROZEN = ROOT / "data" / "lab" / "q3_call_frozen.json"
TRACK_RECORD = tr.OUTPUT
FILINGS_INDEX = ROOT / "data" / "lab" / "ual_filings_index.csv"
DEMAND_SIGNAL = ROOT / "app" / "static" / "data" / "demand_signal.json"
META = ROOT / "app" / "static" / "data" / "meta.json"
TARGET = "2026Q3"
REPORT_EXPECTED_ON = "2026-10-20"      # given by the project owner; used when meta.json carries no report date
N_DRAWS = 4000
LEVELS = (0.01, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.99)
BASELINES = ("guidance_mid", "consensus", "costs_only", "management")


def canonical(block: dict) -> bytes:
    """The bytes that are hashed and frozen: JSON with sorted keys and fixed separators."""
    return json.dumps(block, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(block: dict) -> str:
    return hashlib.sha256(canonical(block)).hexdigest()


def _usd(value: float) -> str:
    return f"-${abs(value):.2f}" if value < 0 else f"${value:.2f}"


def _pct(value: float) -> str:
    return f"{100 * value:.0f}%"


def _date(value) -> str:
    day = tr._day(value)
    return f"{day.day} {day.strftime('%B %Y')}"


# ------------------------------------------------------------------ what is known

def chosen_variant(world: tr.World, track_record: Path) -> dict:
    """The variant the published replay chose, checked against a fresh selection on the quarters before 2024."""
    published = json.loads(Path(track_record).read_text())
    if published.get("fixture") or not str(published.get("test_split", "")).startswith("computed once"):
        raise ValueError("Build the track record (scripts/lab/build_track_record.py) before the forward call")
    chosen = published["selection"]["chosen"]
    fresh = tr.select_variant(tr.replay(world, evaluate_test=False))["chosen"]
    if chosen != fresh or chosen not in tr.VARIANTS:
        raise ValueError(f"The track record chose {chosen!r} but its inputs now give {fresh!r}; rebuild the track record")
    return {"variant": chosen, "scores": published["scores"], "selection": published["selection"]}


def guidance_updates(world: tr.World, after: date, until: date, index: Path = FILINGS_INDEX) -> dict:
    """Anything United published after the guidance date that the committed files hold."""
    records = [r for r in world.guidance if after.isoformat() < r["issued_on"] <= until.isoformat()
               and r["period"] in (TARGET, TARGET[:4] + "FY")]
    with open(index, newline="") as stream:
        rows = list(csv.DictReader(stream))
    filed = [r for r in rows if after.isoformat() < r["filed_on"] <= until.isoformat()]
    return {"window": [(after + timedelta(days=1)).isoformat(), until.isoformat()],
            "guidance_records_found": [r["id"] for r in records],
            "filings_found": sorted({f"{r['filed_on']} {r['form']} {r['accession']}" for r in filed}),
            "latest_filing_in_index": max(r["filed_on"] for r in rows),
            "latest_guidance_record": max(r["issued_on"] for r in world.guidance),
            "used": bool(records),
            "note": "The index lists earnings releases and investor updates (Form 8-K items 2.02, 7.01 and 8.01) "
                    "fetched on the build day. A statement made only in the press or at a conference would not be in it."}


def comparators(meta: Path = META) -> dict:
    """The consensus estimate, the prediction-market price and the report date as meta.json holds them.

    This module does not read the web. meta.json is written by the meta builder, which read each figure
    from a named page on a named day; a figure is taken only with its value, its as-of date and its URL,
    and is null with the reason otherwise.
    """
    def number(value) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    def absent(reason: str) -> dict:
        return {"consensus_eps": {"value": None, "as_of": None, "source_url": None, "reason": reason},
                "market_odds_beat": {"value": None, "as_of": None, "volume_usd": None, "source_url": None, "reason": reason},
                "report_date": None, "report_date_source_url": None}

    if not Path(meta).is_file():
        return absent("app/static/data/meta.json has not been built, and a search on the build day did not confirm a figure.")
    data = json.loads(Path(meta).read_text())
    if data.get("fixture"):
        return absent("app/static/data/meta.json is a placeholder.")
    found, out = data.get("comparators") or {}, absent("meta.json does not give this figure with a value, a date and a URL.")
    consensus, market = found.get("consensus_eps") or {}, found.get("market_odds_beat") or {}
    if number(consensus.get("value")) and consensus.get("as_of") and consensus.get("source_url"):
        out["consensus_eps"] = {
            **{k: consensus.get(k) for k in ("value", "as_of", "source_url", "quote", "provider_note", "also_stated_by", "read_via")},
            "taken_from": tr.shown(meta),
            "caution": "Read from the web by the meta builder, not machine-checked. A separate search made for this "
                       "forecast on the same day returned different consensus figures from other providers; none of "
                       "those is recorded."}
    if number(market.get("value")) and market.get("as_of") and market.get("source_url"):
        out["market_odds_beat"] = {
            **{k: market.get(k) for k in ("value", "as_of", "volume_usd", "source_url", "question", "threshold_eps", "price_basis",
                                         "best_bid", "best_ask", "last_trade", "note", "read_via")},
            "taken_from": tr.shown(meta)}
    if isinstance(data.get("report_date"), str):
        out["report_date"] = data["report_date"]
        out["report_date_source_url"] = (data.get("report_date_source") or {}).get("source_url")
    return out


def last_date(path: Path) -> date:
    with open(path, newline="") as stream:
        return max(date.fromisoformat(row["date"]) for row in csv.DictReader(stream))


# ------------------------------------------------------------------ link 1: fuel price paid

def fuel_link(world: tr.World, row: dict, cutoff: date, seed: int, n_draws: int) -> dict:
    """The cost clock's quarter estimate with a range widened by its real misses on earlier quarters."""
    clock, inp = world.clock, row["inputs"]
    errors = clock.past_errors(cutoff)
    values = np.array([e["error"] for e in errors], dtype=float)
    centred = values - np.median(values)
    point = row["fuel"]["usd_gal"]
    p10, p90 = (float(point + v) for v in np.quantile(centred, [0.1, 0.9]))
    model = clock.model(inp["quarter_end"])
    own = cc.predicted_price_paid(TARGET, clock.dates, clock.values, as_of=inp["quarter_end"], model=model,
                                  n_draws=n_draws, seed=seed)
    stated = inp["assumed"]["usd_gal"]
    return {"link": "fuel_price_paid_usd_gal", "unit": "USD per gallon", "p10": p10, "p50": point, "p90": p90,
            "management": stated, "management_evidence_id": inp["assumed"]["evidence_id"],
            "p50_minus_management": point - stated,
            "method": "Cost clock refitted on every clean quarter reported before the quarter's end "
                      f"({model.n} quarters, latest report {model.last_filed_on.isoformat()}), at the quarter's actual daily fuel "
                      "prices. P10 and P90 are the estimate plus the 10th and 90th percentile of the clock's own "
                      "misses on earlier quarters, each made with the clock as it stood at that quarter's end, less "
                      "their middle value.",
            "fuel_prices_complete": bool(row["fuel"]["complete"]), "last_fuel_price_used": row["fuel"]["last_print"].isoformat(),
            "clock_own_range": {"p10": own["p10"], "p50": own["p50"], "p90": own["p90"],
                                "note": "The clock's own range from its regression error. It held the reported price in "
                                        "too few past quarters, so it is not used."},
            "past_misses": {"n": len(errors), "first": errors[0]["quarter"], "last": errors[-1]["quarter"],
                            "median_usd_gal": float(np.median(values)), "mean_abs_usd_gal": float(np.abs(values).mean()),
                            "reported_above_estimate_in": int((values > 0).sum()),
                            "note": "Reported price minus the clock's estimate. The middle value is shown, not added "
                                    "to the P50: the replay did not add it either."},
            "past_misses_by_quarter": [{"period": e["quarter"], "error": e["error"]} for e in errors]}


# ------------------------------------------------------------------ link 2: recovery share

def share_link(world: tr.World, row: dict, variant: str) -> dict:
    """The share of the fuel surprise that comes back in the quarter, as the chosen variant applies it."""
    inp, steps = row["inputs"], row["primary"]
    draws = np.array(world.fare["curve_draws"], dtype=float)[:, 0]
    low, median, high = (float(v) for v in np.quantile(draws, [0.1, 0.5, 0.9]))
    share, surprise, late = inp["curve"]["share"], steps["surprise_usd_gal"], steps["late_usd_gal"]
    curve = {"p10": min(low, share), "p50": share, "p90": max(high, share), "draws_median": median,
             "fitted_through": inp["curve"]["fitted_through"],
             "note": "p50 is the least-squares share for the quarter of a fuel change, the figure the replay uses "
                     "(smooth.expanding in fare_clock.json). p10 and p90 are from the fare clock's resampled curves; "
                     "their middle value is lower than the least-squares share because that share leans on one "
                     "quarter, 2026Q2."}
    scale = (surprise - late) / surprise if variant == "late_moves" and surprise else 1.0
    effective = sorted(curve[k] * scale for k in ("p10", "p50", "p90"))
    stated = inp["management"]
    out = {"link": "recovery_share", "unit": "share of the fuel surprise recovered in the quarter",
           "p10": effective[0], "p50": share * scale, "p90": effective[2],
           "management": None if stated is None else stated["stated_value"],
           "management_evidence_id": None if stated is None else stated["evidence_id"],
           "curve_same_quarter_share": curve,
           "fuel_surprise_usd_gal": surprise, "arrived_by_mid_quarter_usd_gal": surprise - late,
           "arrived_after_mid_quarter_usd_gal": late, "mid_quarter_date": inp["late_from"].isoformat(),
           "definition": world.fare["recovery_definition"].split(" It is fitted")[0],
           "management_note": "United never defined 'recover'. Its 80% to 90% is a statement about the whole 2026 fuel "
                              "increase in the third quarter, with no formula and no baseline. The management call "
                              "applies it to the fuel surprise as it stands; the two shares are not the same measure.",
           "one_quarter_after_a_shock": {k: world.fare["curve"][1][k] for k in ("p10", "p50", "p90", "estimate")},
           "one_quarter_after_note": "For the fuel increase that began in spring 2026 the third quarter is one quarter "
                                     "after the shock; this is the fare clock's share there, the closest like-for-like "
                                     "figure to United's statement."}
    if variant == "late_moves":
        out["method"] = ("The chosen variant gives the curve's same-quarter share to the part of the surprise that had "
                         "arrived by the middle of the quarter and nothing to the part that arrived later. p10, p50 and "
                         "p90 are that share as a fraction of the whole surprise.")
    else:
        out["method"] = "The curve's same-quarter share applied to the whole surprise."
    return out


# ------------------------------------------------------------------ link 3: unit revenue

def trasm_link(world: tr.World, row: dict, fuel: dict) -> dict:
    """Year-over-year TRASM change from the fare clock's published equation, in its one-quarter-ahead form."""
    fare, inp = world.fare, row["inputs"]
    record = next((r for r in reversed(world.records("trasm_guidance", TARGET))
                   if r["issued_on"] <= row["cutoff"].isoformat()), None)
    management = None if record is None else {"qualifier": record.get("qualifier"), "value": record["value"],
                                              "evidence_id": record["id"]}
    words = {"more_than": "above", "approximately": "about", None: ""}
    stated = None if record is None else f"{words.get(record.get('qualifier'), record.get('qualifier'))} {record['value']}".strip()
    null = {"link": "trasm_yoy", "unit": "fraction, year over year", "p10": None, "p50": None, "p90": None,
            "management": stated, "management_detail": management}
    fit = fare["smooth"]["through_latest"]
    scores = fare["oos_test"]["smooth_fit_only"]["scores"]
    select = {h: scores[h]["select"]["mae_cents_per_asm"]["smooth_fit_only"] for h in ("h1", "h4")}
    form = min(select, key=select.get)
    capacity = [r for r in world.records("capacity_growth", TARGET) if r["issued_on"] <= row["cutoff"].isoformat()]
    if not capacity:
        return {**null, "reason": "United gave no capacity figure for the quarter, which the fare clock's equation needs."}
    table = fc.load_ual_quarterly(world.paths.quarterly)
    panel = fc.united_panel(table)
    quarters = [fc.quarter_label(int(q)) for q in panel["q"]]
    year_ago, previous = cc.quarter_label(cc.quarter_index(TARGET) - 4), cc.quarter_label(cc.quarter_index(TARGET) - 1)
    if quarters[-1] != previous or year_ago not in quarters:
        return {**null, "reason": f"The quarterly table does not end in {previous}."}
    back = quarters.index(year_ago)
    growth = 100.0 * tr._mid(capacity[-1]["value"])
    price_ratio = fuel["p50"] / table["fuel_price_usd_gal"][back]
    d_fuel = np.append(panel["d_fuel"], panel["fuel_cpasm"][back] * (price_ratio - 1.0))
    cap = np.append(panel["cap"], growth)
    d_rev = np.append(panel["d_rev"], np.nan)
    q = np.append(panel["q"], panel["q"][-1] + 1)
    a, c = fit["intercept_cents"], fit["capacity_coef_cents_per_point"]
    term = fc.fuel_term(d_fuel, fit["curve_estimate"])
    level = a + c * cap + term
    ahead = fc._lagged(d_rev, 1) + c * (cap - fc._lagged(cap, 1)) + term - fc._lagged(term, 1)
    central = {"h1": float(ahead[-1]), "h4": float(level[-1])}
    usable = np.isfinite(d_rev) & np.isfinite(ahead if form == "h1" else level) & ~fc.pandemic_touched(q, 9 if form == "h1" else 8)
    errors = (d_rev - (ahead if form == "h1" else level))[usable]
    centred = errors - np.median(errors)
    base = panel["unit_rev"][back]
    p10, p90 = (float((central[form] + v) / base) for v in np.quantile(centred, [0.1, 0.9]))
    names = {"h1": "one quarter ahead: last quarter's reported year-over-year change, plus the change in the fuel term and in the capacity term",
             "h4": "from the year-ago level: the fitted constant, the capacity term and the fuel term"}
    return {**null, "p10": p10, "p50": central[form] / base, "p90": p90,
            "form": form, "form_description": names[form],
            "form_rule": "The fare clock scored two forms of its equation; the one with the lower miss on 2020-2023 "
                         f"is used ({select['h1']:.2f} against {select['h4']:.2f} cents per seat mile, in its own file).",
            "other_form_p50": central["h4" if form == "h1" else "h1"] / base,
            "change_cents_per_asm": central[form], "year_ago_trasm_cents": float(base),
            "inputs": {"fuel_price_usd_gal": fuel["p50"], "year_ago_fuel_price_usd_gal": float(table["fuel_price_usd_gal"][back]),
                       "fuel_cost_change_cents_per_asm": float(d_fuel[-1]), "capacity_growth_pct": growth,
                       "capacity_evidence_id": capacity[-1]["id"], "capacity_as_stated": capacity[-1]["value"],
                       "previous_quarter_trasm_change_cents": float(panel["d_rev"][-1]),
                       "long_run_share": fit["L"], "rho": fit["rho"]},
            "range_from": {"n": int(usable.sum()), "mean_abs_miss_cents": float(np.abs(errors).mean()),
                           "note": "The same form's misses on every earlier quarter with no pandemic level behind it, "
                                   "less their middle value. The curve's coefficients were fitted on those quarters, "
                                   "so the range is too narrow."},
            "assumptions": ["Gallons burned per seat mile are the same as in the third quarter a year earlier.",
                            "Capacity growth is the middle of what United stated in April 2026; no later figure is in "
                            "the guidance file."],
            "not_used_by_eps_link": True}


# ------------------------------------------------------------------ link 4: adjusted EPS

def eps_link(row: dict, variant: str, treatment: str = "primary") -> dict:
    steps, inp = row[treatment], row["inputs"]
    ours = steps["ranges"][variant]
    members = None if ours["members"] is None else sorted(float(v) for v in ours["members"])
    return {"link": "adj_eps_diluted", "unit": "USD per diluted share", "p10": ours["p10"], "p50": ours["p50"], "p90": ours["p90"],
            "management": [inp["guidance_low"], inp["guidance_high"]], "management_evidence_id": inp["guidance_evidence_id"],
            "guidance_mid": inp["mid"], "fuel_surprise_usd_gal": steps["surprise_usd_gal"],
            "fuel_surprise_musd": steps["surprise_musd"], "fuel_surprise_per_share": steps["surprise_per_share"],
            "unrecovered_per_share": inp["mid"] - steps["calls"][variant] + (steps["habit_shift"] if variant == "habit" else 0.0),
            "gallons_m": inp["gallons_m"], "gallons_quarter": inp["gallons_quarter"],
            "tax_rate": inp["per_share"]["tax_rate"], "diluted_shares_m": inp["per_share"]["diluted_shares_m"],
            "tax_and_shares_quarter": inp["per_share"]["quarter"],
            "profit_sharing_rate": inp["profit_sharing_rate"] if treatment == "primary" else None,
            "earlier_misses_n": len(inp["misses"]), "members": members}


def sensitivities(world: tr.World, row: dict, variant: str, fuel: dict) -> list[dict]:
    """The central call with one input changed at a time. None of these is the forecast."""
    inp, at_end, late = row["inputs"], row["fuel"]["usd_gal"], row["fuel"]["late_usd_gal"]

    def call(share: float | None = None, price_shift: float = 0.0) -> float:
        changed = inp if share is None else {**inp, "curve": {**inp["curve"], "share": share}}
        return float(tr.point_calls(changed, at_end + price_shift, late)["calls"][variant])

    median = float(np.median(np.array(world.fare["curve_draws"], dtype=float)[:, 0]))
    before = float(world.expanding[cc.quarter_label(cc.quarter_index(TARGET) - 1)]["curve_p50"][0])
    shift = fuel["past_misses"]["median_usd_gal"]
    return [{"what": "same-quarter share at the middle of the fare clock's resampled curves instead of the least-squares share",
             "share": median, "eps_p50": call(share=median), "note": "Not the forecast."},
            {"what": "recovery curve fitted without 2026Q2 (as it stood one quarter earlier)",
             "share": before, "eps_p50": call(share=before), "note": "Not the forecast."},
            {"what": "fuel price raised by the middle value of the cost clock's past misses",
             "fuel_price_usd_gal": at_end + shift, "eps_p50": call(price_shift=shift),
             "note": "Not the forecast. The replay tried this correction on the quarters before 2024 and it scored worse there."}]


def simulator_cross_check(surprise: float, share: float, curve: list[float], chain: dict) -> dict:
    """The same fuel step in the business simulator's first quarter, beside the closed-form chain."""
    from app.lab import response as R
    from app.lab import scenario
    reference = scenario.baseline_fuel_usd_gal()
    request = {"fuel_shock_months": 3, "horizon_months": 12, "seed": 1}
    plain = scenario.run_scenario({**request, "fuel_usd_gal": reference["usd_gal"]},
                                  response_modes={"replay_curve": R.from_draws([curve], name="replay_curve")})
    moved = scenario.run_scenario({**request, "fuel_usd_gal": round(reference["usd_gal"] + surprise, 4)},
                                  response_modes={"replay_curve": R.from_draws([curve], name="replay_curve")})
    if plain.get("status") != "ok" or moved.get("status") != "ok":
        return {"computed": False, "reason": "; ".join(moved.get("errors") or plain.get("errors") or ["the simulator did not run"])}
    out = {"computed": True, "used_in_forecast": False, "closed_form_chain": chain,
           "period": moved["modes"]["measured"]["quarters"][0]["period"],
           "fuel_step_usd_gal": moved["fuel"]["shock_usd_gal"], "reference_usd_gal": reference["usd_gal"],
           "paths": moved["paths"], "run_ids": [plain["run_id"], moved["run_id"]], "modes": {}}
    for name in ("measured", "replay_curve", "costs_only", "management"):
        first, zero = moved["modes"][name]["quarters"][0], plain["modes"][name]["quarters"][0]
        out["modes"][name] = {"operating_income_change_musd": first["operating_income_change_musd"],
                              "eps_change_after_tax_only": first["eps_change"],
                              "operating_income_p50_difference_between_runs_musd":
                                  round(first["operating_income_musd"]["p50"] - zero["operating_income_musd"]["p50"], 1),
                              "share_in_first_quarter_p50": moved["response_used"][name]["curve_p50"][0]}
    per_share = moved["per_share"]
    out["per_share_rule"] = f"tax rate {per_share['tax_rate']:.4f}, {per_share['diluted_shares_m']} million diluted shares, no profit sharing"
    out["note"] = ("Two simulator runs on the same 10,000 worlds that differ only in the fuel price level, held for three "
                   f"months. The simulator starts from United's business as of {scenario.INFORMATION_CUTOFF} and its first "
                   "whole quarter is the fourth quarter of 2026, so it cannot produce the third quarter; the forecast "
                   "therefore uses the closed-form chain from the replay. `replay_curve` gives the simulator the same "
                   f"same-quarter share the chain uses ({share:.0%}); `measured` draws from the fare clock's resampled "
                   "curves, whose middle same-quarter share is lower.")
    return out


# ------------------------------------------------------------------ the forecast block

def known_before_freeze(world: tr.World, row: dict, updates: dict, cutoff: date) -> tuple[list[str], dict]:
    data = json.loads(world.paths.dal_check.read_text())
    guide, actual = data["guidance_sep_quarter"], data["actuals_sep_quarter"]
    known = [f"Delta reported its September quarter on {actual['reported_on']}: adjusted EPS {_usd(actual['adj_eps_diluted'])} "
             f"against a guided {_usd(guide['eps_low'])} to {_usd(guide['eps_high'])}; it paid {_usd(actual['fuel_price_adj_usd_gal'])} "
             f"a gallon against an assumed {_usd(guide['fuel_price_assumed_usd_gal'])}; adjusted unit revenue rose "
             f"{actual['yoy_stated']['adj_trasm_yoy_pct']}% year over year. No Delta figure enters any number in this forecast.",
             f"Daily Gulf Coast jet fuel prices through {row['fuel']['last_print'].isoformat()} for the quarter itself; the "
             f"quarter ended on {row['inputs']['quarter_end'].isoformat()}, so its fuel prices are all known.",
             f"United's results through the second quarter of 2026, published on {world.reports[-1]['filed_on'].isoformat()}."]
    if updates["used"]:
        known.append("United updated its guidance after July: " + ", ".join(updates["guidance_records_found"]) + ".")
    else:
        known.append(f"No guidance update from United between {updates['window'][0]} and {updates['window'][1]} is in the "
                     f"committed filings (latest filing in the index: {updates['latest_filing_in_index']}).")
    demand = None
    if DEMAND_SIGNAL.is_file():
        signal = json.loads(DEMAND_SIGNAL.read_text())
        quarter = next((q for q in signal.get("quarterly", []) if q.get("period") == TARGET and isinstance(q.get("yoy"), (int, float))), None)
        if quarter and not signal.get("fixture"):
            demand = {"period": TARGET, "yoy": quarter["yoy"], "complete": quarter.get("complete"), "as_of": signal.get("as_of")}
            known.append(f"Airport security counts for the quarter: travellers {100 * quarter['yoy']:+.1f}% against a year "
                         "earlier (national, all airlines). Not an input to the forecast.")
    return known, {"delta": {"reported_on": actual["reported_on"], "adj_eps": actual["adj_eps_diluted"],
                             "guided": [guide["eps_low"], guide["eps_high"]],
                             "fuel_price_adj_usd_gal": actual["fuel_price_adj_usd_gal"],
                             "fuel_price_assumed_usd_gal": guide["fuel_price_assumed_usd_gal"],
                             "adj_trasm_yoy_pct": actual["yoy_stated"]["adj_trasm_yoy_pct"]},
                   "travellers": demand}


def forecast_block(paths: tr.Paths | None = None, track_record: Path = TRACK_RECORD, seed: int = tr.SEED,
                   n_draws: int = N_DRAWS, frozen_at: str | None = None, cross_check: bool = True, meta: Path = META) -> dict:
    """The forecast: everything that is hashed and frozen."""
    world = tr.World(paths)
    picked = chosen_variant(world, track_record)
    variant = picked["variant"]
    outside = comparators(meta)
    consensus, market = outside["consensus_eps"], outside["market_odds_beat"]
    cutoff = max(last_date(world.paths.fuel), tr._day(json.loads(world.paths.dal_check.read_text())["actuals_sep_quarter"]["reported_on"]),
                 world.reports[-1]["filed_on"], *(tr._day(c["as_of"]) for c in (consensus, market) if c["value"] is not None))
    if TARGET in world.by_quarter and world.by_quarter[TARGET]["adj_eps_diluted"] is not None:
        raise ValueError(f"{TARGET} has already been reported; a forecast made now would not be a forecast")
    row = tr.forecast(world, TARGET, cutoff, cutoff + timedelta(days=1))
    if not row["computed"]:
        raise ValueError(f"The chain could not be computed for {TARGET}: {row['reason']}")
    inp = row["inputs"]
    updates = guidance_updates(world, inp["guided_on"], cutoff)
    fuel = fuel_link(world, row, cutoff, seed, n_draws)
    share = share_link(world, row, variant)
    trasm = trasm_link(world, row, fuel)
    eps = eps_link(row, variant)
    members = np.array(eps.pop("members"), dtype=float)
    low, high = inp["guidance_low"], inp["guidance_high"]
    calls = row["primary"]["calls"]
    known, context = known_before_freeze(world, row, updates, cutoff)
    plain = eps_link(row, variant, "tax_only")
    plain.pop("members")
    entry = world.expanding[TARGET]
    if consensus["value"] is not None:
        line = (f"Analyst consensus for the quarter's EPS: {_usd(consensus['value'])}, as read on {consensus['as_of']} "
                f"({consensus['source_url']}).")
        if market["value"] is not None:
            line += (f" A prediction market priced a result above {_usd(market.get('threshold_eps') or consensus['value'])} at "
                     f"{_pct(market['value'])} on {market['as_of']}, with about ${market['volume_usd']:,.0f} traded in total.")
        known.append(line + " Neither enters any number in the chain.")
    odds = {"below_range": float((members < low).mean()), "inside_range": float(((members >= low) & (members <= high)).mean()),
            "above_range": float((members > high).mean()),
            "above_consensus": None if consensus["value"] is None else float((members > consensus["value"]).mean()),
            "consensus_used": consensus["value"],
            "above_consensus_reason": None if consensus["value"] is not None else
                                      "No consensus figure is recorded with a value, a date and a URL, so no odds are "
                                      "stated against one. `eps_samples` allows the share above any level to be computed later.",
            "above_guidance_mid": float((members > inp["mid"]).mean()), "n_samples": len(members),
            "note": f"Shares of the {len(members)} sample values: today's central estimate with each of United's "
                    f"{len(members)} earlier misses against its midpoint applied in turn. With this few values the odds move "
                    f"in steps of {100 / len(members):.0f} percentage points."}
    scores = {s["model"]: s for s in picked["scores"] if s["split"] == "test"}
    block = {
        "target": {"company": "UAL", "period": TARGET, "measure": "adjusted diluted EPS, as United reports it",
                   "report_expected_on": outside["report_date"] or REPORT_EXPECTED_ON,
                   "report_date_source": outside["report_date_source_url"] or "given by the project owner; not in the committed filings"},
        "frozen_at": frozen_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_cutoff": cutoff.isoformat(), "last_fuel_price": last_date(world.paths.fuel).isoformat(),
        "method": {"variant": variant, "variant_note": tr.VARIANT_NOTES[variant],
                   "same_as_replay": "tr.forecast: the function that produced every replayed quarter in track_record.json",
                   "chosen_on": picked["selection"]["quarters"], "selection_rule": picked["selection"]["rule"],
                   "fuel_effect_from": "closed_form",
                   "fuel_effect_note": "The closed-form chain from the replay, not the simulator: the simulator's first whole "
                                       "quarter is the fourth quarter of 2026. The simulator is run beside it as a cross-check.",
                   "replay_record_2024_onward": {name: {k: scores[name][k] for k in ("mae", "crps", "inside_p10_p90", "n")}
                                                 for name in tr.MODELS if name in scores}},
        "guidance": {"made_on": inp["guided_on"].isoformat(), "eps_low": low, "eps_high": high,
                     "fuel_assumed_usd_gal": inp["assumed"]["usd_gal"], "fuel_assumed_source": inp["assumed"]["source"],
                     "recovery_stated": None if inp["management"] is None else inp["management"]["stated_value"],
                     "evidence_ids": [i for i in (inp["guidance_evidence_id"], inp["assumed"]["evidence_id"],
                                                  None if inp["management"] is None else inp["management"]["evidence_id"]) if i],
                     "updates_since": updates},
        "known_before_freeze": known, "context": context,
        "chain": [fuel, share, trasm, eps],
        "eps_samples": [float(v) for v in members],
        "eps_samples_quantiles": [[level, float(np.quantile(members, level))] for level in LEVELS],
        "odds": odds,
        "scoring": {"rule": "CRPS", "baselines": list(BASELINES),
                    "definition": "CRPS of `eps_samples` against the adjusted diluted EPS United reports, in US dollars a "
                                  "share; for a single-number baseline CRPS is its absolute miss. Lower is better. The "
                                  "fuel price and unit revenue links are scored by absolute miss and by whether the "
                                  "outcome falls inside P10-P90.",
                    "baseline_calls": {"guidance_mid": calls["guidance_mid"], "consensus": consensus["value"],
                                       "costs_only": calls["costs_only"], "management": calls["management"]},
                    "consensus_note": ("The consensus baseline is the figure in `comparators`, fixed here before the report."
                                       if consensus["value"] is not None else
                                       "No consensus figure was recorded before the freeze; if one is supplied when the "
                                       "result is scored it is recorded beside the outcome, outside this block.")},
        "comparators": {"consensus_eps": consensus, "market_odds_beat": market},
        "without_profit_sharing": {"p10": plain["p10"], "p50": plain["p50"], "p90": plain["p90"],
                                   "fuel_surprise_per_share": plain["fuel_surprise_per_share"],
                                   "costs_only": row["tax_only"]["calls"]["costs_only"],
                                   "management": row["tax_only"]["calls"]["management"],
                                   "note": "The same call after tax only. Not the forecast: United's own July arithmetic "
                                           "($575 million = $1.12 a share) includes profit sharing."},
        "sensitivities": sensitivities(world, row, variant, fuel),
        "simulator_cross_check": (simulator_cross_check(
            row["primary"]["surprise_usd_gal"], inp["curve"]["share"], [float(v) for v in entry["curve_p50"]],
            {"period": TARGET, "fuel_surprise_musd": row["primary"]["surprise_musd"], "share_recovered": share["p50"],
             "operating_income_change_musd": -(1 - share["p50"]) * row["primary"]["surprise_musd"],
             "eps_change_after_tax_only": -(1 - share["p50"]) * row["tax_only"]["surprise_per_share"],
             "note": "The chain's own figures for the third quarter, before profit sharing, for comparison."})
                                  if cross_check else {"computed": False, "reason": "not requested"}),
        "uses": row["uses"],
        "inputs": tr.inputs_list((*world.paths.all(), Path(track_record), FILINGS_INDEX,
                                  *((DEMAND_SIGNAL,) if context["travellers"] else ()),
                                  *((Path(meta),) if consensus["value"] is not None or market["value"] is not None
                                    or outside["report_date"] else ())))}
    return cc._clean(block)


# ------------------------------------------------------------------ the output file

def headline(block: dict) -> str:
    fuel, share, _, eps = block["chain"]
    guide = block["guidance"]
    return (f"Frozen on {_date(block['frozen_at'][:10])}: United's third-quarter adjusted EPS is put at {_usd(eps['p50'])} a share "
            f"(P10 {_usd(eps['p10'])}, P90 {_usd(eps['p90'])}) against its own range of {_usd(guide['eps_low'])} to "
            f"{_usd(guide['eps_high'])}, with fuel at {_usd(fuel['p50'])} a gallon instead of the {_usd(fuel['management'])} United assumed.")


def plain_summary(block: dict) -> list[str]:
    fuel, share, trasm, eps = block["chain"]
    odds, guide, record = block["odds"], block["guidance"], block["method"]["replay_record_2024_onward"]
    n = odds["n_samples"]
    count = lambda p: round(p * n)
    out = [headline(block),
           f"Fuel: the cost clock puts the price United paid at {_usd(fuel['p50'])} a gallon (P10 {_usd(fuel['p10'])}, P90 "
           f"{_usd(fuel['p90'])}); United assumed about {_usd(fuel['management'])} on {_date(guide['made_on'])}. The gap is "
           f"${eps['fuel_surprise_musd']:,.0f} million before tax, or {_usd(eps['fuel_surprise_per_share'])} a share after tax "
           "and profit sharing.",
           f"Recovery: {_pct(share['p50'])} of that gap is expected back through revenue within the quarter (P10 "
           f"{_pct(share['p10'])}, P90 {_pct(share['p90'])}). United said it expects to recover "
           f"{_pct(share['management'][0])} to {_pct(share['management'][1])} of the fuel increase in the quarter, but never "
           "defined 'recover', so the two figures are not the same measure."]
    if trasm["p50"] is not None:
        detail = trasm["management_detail"]
        said = ("" if detail is None else f"; United said {'more than' if detail['qualifier'] == 'more_than' else 'about'} "
                f"{100 * detail['value']:.1f}%")
        out.append(f"Unit revenue: up {100 * trasm['p50']:.1f}% on a year earlier (P10 {100 * trasm['p10']:.1f}%, P90 "
                   f"{100 * trasm['p90']:.1f}%){said}. This link does not feed the EPS figure.")
    else:
        out.append("Unit revenue: no forecast. " + trasm.get("reason", ""))
    out.append(f"EPS: {_usd(eps['guidance_mid'])} midpoint less {_usd(eps['unrecovered_per_share'])} of fuel cost not recovered "
               f"gives {_usd(eps['p50'])}. Applying each of United's {n} earlier misses against its own midpoint, "
               f"{count(odds['below_range'])} land below the guided range, {count(odds['inside_range'])} inside and "
               f"{count(odds['above_range'])} above.")
    consensus, market = block["comparators"]["consensus_eps"], block["comparators"]["market_odds_beat"]
    if consensus["value"] is not None:
        line = (f"Against the {_usd(consensus['value'])} analyst consensus read on {_date(consensus['as_of'])}, "
                f"{count(odds['above_consensus'])} of the {n} values {'is' if count(odds['above_consensus']) == 1 else 'are'} "
                f"higher ({_pct(odds['above_consensus'])})")
        if market["value"] is not None:
            line += (f"; a thinly traded prediction market put the chance of a result above "
                     f"{_usd(market.get('threshold_eps') or consensus['value'])} at {_pct(market['value'])}")
        out.append(line + ".")
    if record:
        better = record["fitted"]["mae"] < record["guidance_mid"]["mae"] - 0.005
        out.append(f"How this method did on the {record['fitted']['n']} quarters from 2024 to mid-2026: average miss "
                   f"{_usd(record['fitted']['mae'])} a share, against {_usd(record['guidance_mid']['mae'])} for United's own "
                   f"midpoint and {_usd(record['costs_only']['mae'])} for costs only. "
                   + ("It beat the midpoint there." if better else "It did not beat the midpoint there, so this call is a "
                      "test of the method, not a claim that it is better than United's own number."))
    out.append("The forecast block is fixed: its SHA-256 is in `sha256` and the same bytes are in "
               "data/lab/q3_call_frozen.json. The outcome and the scores are added beside it after United reports.")
    return out


def wrap(block: dict, generated_at: str | None = None, outcome=None, scores=None, extra: dict | None = None) -> dict:
    """q3_call.json around a forecast block. Top-level copies of the block's fields are for the page."""
    fuel = block["chain"][0]
    record = block["method"]["replay_record_2024_onward"].get("fitted", {})
    limits = [
        "One forecast, made once. It rests on the same few quarters as the track record: the method was chosen on "
        f"{len(block['method']['chosen_on'])} quarters and tested on {record.get('n', 0)}, where it did not beat United's own "
        "midpoint on average miss." if record.get("mae", 0) >= block["method"]["replay_record_2024_onward"]["guidance_mid"]["mae"] - 0.005
        else "One forecast, made once. It rests on the same few quarters as the track record: the method was chosen on "
        f"{len(block['method']['chosen_on'])} quarters and tested on {record.get('n', 0)}.",
        "The EPS range is the spread of United's earlier misses against its own midpoint, not a product of the link "
        "ranges. Those misses already contain past fuel and revenue surprises.",
        f"The odds are shares of {block['odds']['n_samples']} values, so they are coarse.",
        "The fuel price is an estimate from Gulf Coast spot prices. United buys at many airports and the cost clock ran "
        f"low in {fuel['past_misses']['reported_above_estimate_in']} of {fuel['past_misses']['n']} past quarters even after "
        "refitting; the middle of those misses is shown but not added.",
        "The same-quarter recovery share leans on one quarter, 2026Q2; `sensitivities` shows the call with two lower shares.",
        ("The consensus figure was read from the web by another part of the build and is not machine-checked; a search "
         "made for this forecast on the same day returned different figures from other providers. The prediction market "
         "is thinly traded."
         if block["comparators"]["consensus_eps"]["value"] is not None else
         "No consensus estimate and no prediction-market price is recorded with a value, a date and a URL, so both "
         "comparators are null."),
        "Anything United said outside its SEC filings after 15 July 2026 is not in the inputs.",
        "Profit sharing uses a rate read from a paraphrase; the call after tax only is in `without_profit_sharing`.",
        "The unit revenue link uses coefficients fitted on the same quarters its range is drawn from and assumes "
        "capacity growth from April; it is a side forecast and does not feed the EPS figure."]
    payload = {
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": block["data_cutoff"], "inputs": block["inputs"],
        "headline": headline(block), "plain_summary": plain_summary(block), "limits": limits,
        "frozen_at": block["frozen_at"], "sha256": sha256(block),
        "sha256_of": "the canonical JSON of `forecast` (sorted keys, separators ',' and ':', UTF-8); the same bytes are "
                     "in data/lab/q3_call_frozen.json",
        "frozen_file": tr.shown(FROZEN),
        "timestamp_proof": None,
        "timestamp_proof_note": "Not created by the build. The file to stamp is data/lab/q3_call_frozen.json.",
        "data_cutoff": block["data_cutoff"], "known_before_freeze": block["known_before_freeze"],
        "chain": block["chain"], "eps_samples_quantiles": block["eps_samples_quantiles"], "odds": block["odds"],
        "scoring": block["scoring"],
        "comparators": block["comparators"],
        "forecast": block, "outcome": outcome, "scores": scores}
    if extra:
        payload.update(extra)
    return payload


def validate(payload: dict, frozen: Path | None = None) -> None:
    """Shape and integrity checks; raises ValueError naming the first problem."""
    required = ("generated_at", "as_of", "inputs", "headline", "plain_summary", "limits", "frozen_at", "sha256", "data_cutoff",
                "known_before_freeze", "chain", "eps_samples_quantiles", "odds", "scoring", "forecast", "outcome")
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"q3_call.json lacks {missing}")
    if payload.get("fixture"):
        raise ValueError("q3_call.json must not be a fixture")
    text = json.dumps(payload, allow_nan=False)
    if str(ROOT) in text or "/Users/" in text:
        raise ValueError("q3_call.json contains an absolute local path")
    block = payload["forecast"]
    if sha256(block) != payload["sha256"]:
        raise ValueError("sha256 does not match the forecast block")
    if frozen is not None and Path(frozen).read_bytes() != canonical(block):
        raise ValueError("the frozen file does not hold the forecast block's canonical bytes")
    for key in ("chain", "eps_samples_quantiles", "odds", "scoring", "known_before_freeze", "data_cutoff", "comparators"):
        if payload[key] != block[key]:
            raise ValueError(f"top-level {key} differs from the forecast block")
    if [link["link"] for link in block["chain"]] != ["fuel_price_paid_usd_gal", "recovery_share", "trasm_yoy", "adj_eps_diluted"]:
        raise ValueError("the chain must have its four links in order")
    for link in block["chain"]:
        if link["p50"] is not None and not link["p10"] <= link["p50"] <= link["p90"]:
            raise ValueError(f"{link['link']}: quantiles are out of order")
    odds = block["odds"]
    if abs(odds["below_range"] + odds["inside_range"] + odds["above_range"] - 1.0) > 1e-6:
        raise ValueError("odds must add up to one")
    if block["data_cutoff"] >= block["target"]["report_expected_on"]:
        raise ValueError("the data cutoff is not before the expected report date")
    late = [use for use in block["uses"] if use["dated"] > block["data_cutoff"]]
    if late:
        raise ValueError(f"the forecast reads {late[0]['what']} dated after its cutoff")


# ------------------------------------------------------------------ scoring, after the report

def score(payload: dict, actual_eps: float, reported_on: str, fuel_price: float | None = None, trasm_yoy: float | None = None,
          source: str | None = None, consensus: dict | None = None, scored_at: str | None = None) -> dict:
    """q3_call.json with `outcome` and `scores` filled. The forecast block is copied untouched."""
    block = payload["forecast"]
    if sha256(block) != payload["sha256"]:
        raise ValueError("The forecast block no longer matches its sha256; refusing to score it")
    if reported_on <= block["data_cutoff"]:
        raise ValueError("The result is dated on or before the forecast's data cutoff")
    for name, value in (("actual_eps", actual_eps), ("fuel_price", fuel_price), ("trasm_yoy", trasm_yoy)):
        if value is not None and not (isinstance(value, (int, float)) and math.isfinite(value)):
            raise ValueError(f"{name} must be a finite number")
    fuel, share, trasm, eps = block["chain"]
    members = np.array(block["eps_samples"], dtype=float)
    low, high = block["guidance"]["eps_low"], block["guidance"]["eps_high"]
    calls = dict(block["scoring"]["baseline_calls"])
    supplied = consensus if consensus and consensus.get("value") is not None else None
    if supplied and calls.get("consensus") is None:
        calls["consensus"] = float(supplied["value"])
    baselines = {name: {"call": calls.get(name), "abs_error": None if calls.get(name) is None else abs(calls[name] - actual_eps),
                        "crps": None if calls.get(name) is None else abs(calls[name] - actual_eps)} for name in BASELINES}
    ours = float(stats.crps_ensemble(members, float(actual_eps)))
    ranked = sorted([("fitted", ours)] + [(name, b["crps"]) for name, b in baselines.items() if b["crps"] is not None], key=lambda item: item[1])

    def link_score(link: dict, value: float | None) -> dict | None:
        if value is None or link["p50"] is None:
            return None
        return {"actual": value, "error": link["p50"] - value, "abs_error": abs(link["p50"] - value),
                "inside_p10_p90": bool(link["p10"] <= value <= link["p90"]),
                "management_abs_error": abs(link["management"] - value) if isinstance(link["management"], (int, float)) else None}

    per_share = eps["fuel_surprise_per_share"]
    outcome = {"reported_on": reported_on, "adj_eps_diluted": actual_eps, "fuel_price_usd_gal": fuel_price, "trasm_yoy": trasm_yoy,
               "source": source, "scored_at": scored_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "position": "below_range" if actual_eps < low else "above_range" if actual_eps > high else "inside_range"}
    scores = {"rule": "CRPS", "unit": "USD per share",
              "consensus_supplied_after_freeze": supplied,
              "consensus_scored": ("the figure frozen in the block" if block["scoring"]["baseline_calls"].get("consensus") is not None
                                   else "the figure supplied after the freeze" if supplied else None),
              "fitted": {"crps": ours, "abs_error": abs(eps["p50"] - actual_eps), "error": eps["p50"] - actual_eps,
                         "inside_p10_p90": bool(eps["p10"] <= actual_eps <= eps["p90"]),
                         "share_of_samples_below_actual": float((members < actual_eps).mean())},
              "baselines": baselines, "ranking": [name for name, _ in ranked],
              "fuel_price_paid_usd_gal": link_score(fuel, fuel_price), "trasm_yoy": link_score(trasm, trasm_yoy),
              "implied_recovery_share": (None if not per_share else 1 - (eps["guidance_mid"] - actual_eps) / per_share),
              "implied_recovery_share_note": "The share that would make the chain land on the reported EPS if the frozen "
                                             "fuel surprise were the only reason for the gap to the midpoint. Arithmetic on "
                                             "one quarter, not an estimate.",
              "above_consensus_from_frozen_samples": (None if calls.get("consensus") is None
                                                      else float((members > calls["consensus"]).mean()))}
    out = {**payload, "outcome": outcome, "scores": scores}
    if sha256(out["forecast"]) != payload["sha256"]:
        raise ValueError("Scoring changed the forecast block")
    return out
