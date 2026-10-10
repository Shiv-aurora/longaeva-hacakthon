"""Track record: the simulator's behaviour chain replayed on past quarters.

The business simulator (app/monthly.py) is calibrated to United as it stood in July
2026, so it cannot be re-run in 2023. What can be replayed is the chain its
"measured" mode rests on, one quarter at a time, with only what was public then:

  1. United's adjusted EPS range for the quarter and the fuel price behind it
     (United's stated assumption, or the cost clock's estimate on the guidance date
     when United stated none).
  2. The fuel price the cost clock expects for the quarter at quarter end.
  3. The fuel surprise in dollars: (2) minus (1), times the gallons of the same
     quarter a year earlier, after tax and profit sharing, per diluted share.
  4. The share of that surprise that comes back through revenue in the same
     quarter: the fare clock's recovery curve as it would have been estimated then
     (`smooth.expanding`).

Rival calls on each quarter:
  guidance_mid  the middle of United's range (the surprise is fully recovered)
  costs_only    the middle minus the whole surprise (nothing is recovered)
  management    the middle minus the part United said it would not recover (2026 only)
  fitted        the middle minus the part the recovery curve says is not recovered

Three more pre-listed variants of `fitted` are scored on the quarters before 2024
only (VARIANTS). The one with the lowest score there is named in `selection` and
is the only variant scored on 2024 onward. The range around `fitted` comes from how
far United's earlier results landed from its own midpoints.

No look-ahead: every read of a dated input goes through `Known`, which refuses
anything dated after the forecast date and lists what was read.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re

import numpy as np

from app.lab import cost_clock as cc
from app.lab import filings, market_clock, market_data, stats

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "data" / "lab"
OUTPUT = ROOT / "app" / "static" / "data" / "track_record.json"

SPLITS = cc.SPLITS                     # fit 2013-2019, select 2020-2023, test 2024-2026
MIN_HISTORY = 3                        # earlier quarters needed before a range or a habit is estimated
MIN_CLOCK_QUARTERS = 8                 # reports behind a cost clock estimate before it is used in a check
LEVEL = 0.90
SEED = 20261010
N_BOOT = 2000
BASE = "fitted"
VARIANTS = (BASE, "habit", "late_moves", "clock_bias")
VARIANT_NOTES = {
    BASE: "Midpoint minus the part of the fuel surprise that the recovery curve says does not come back in the "
          "same quarter.",
    "habit": "The same, plus United's habit of landing above or below its own midpoint: the middle value of its "
             f"earlier misses, once at least {MIN_HISTORY} exist.",
    "late_moves": "The same, but fuel that moved after the middle of the quarter (or after the guidance date, if "
                  "later) is treated as not recovered at all in that quarter.",
    "clock_bias": "The same, with the cost clock's fuel price shifted by the middle value of its own earlier "
                  f"misses against reported prices (at least {MIN_HISTORY}), where United stated a fuel price.",
}
VARIANT_LABELS = {BASE: "the plain fitted call", "habit": "the fitted call with United's habit added",
                  "late_moves": "the fitted call with late fuel moves treated as not recovered",
                  "clock_bias": "the fitted call with the cost clock's own misses added"}
MODELS = ("guidance_mid", "costs_only", "management", BASE)
QUARTERLY = re.compile(r"^\d{4}Q[1-4]$")


@dataclass(frozen=True)
class Paths:
    guidance: Path = LAB / "ual_guidance.json"
    quarterly: Path = filings.QUARTERLY_CSV
    fuel: Path = market_data.FUEL_CSV
    prices: Path = market_data.PRICES_CSV
    fare_clock: Path = ROOT / "app" / "static" / "data" / "fare_clock.json"
    literature: Path = LAB / "literature.json"
    dal_check: Path = LAB / "dal_check.json"

    def all(self) -> tuple[Path, ...]:
        return (self.guidance, self.quarterly, self.fuel, self.prices, self.fare_clock, self.literature, self.dal_check)


class LookAhead(RuntimeError):
    """A forecast asked for something dated after its forecast date."""


def _day(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _mid(value) -> float:
    return float(np.mean(value)) if isinstance(value, (list, tuple)) else float(value)


def shown(path: Path) -> str:
    """A path as the outputs print it: relative to the repository, or the bare file name outside it."""
    path = Path(path).resolve()
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name


def inputs_list(paths) -> list[dict]:
    return [{"path": shown(p), "sha256": cc._sha(p)} for p in paths]


# ------------------------------------------------------------------ the cost clock, as it stood on a date

class Clock:
    """The cost clock's expected price paid for a quarter as of a date, with the model refitted as of a date."""

    def __init__(self, quarterly: Path, fuel: Path):
        self.quarterly, self.fuel = Path(quarterly), Path(fuel)
        self.dates, self.values = market_data.load_fuel_daily(self.fuel)
        self.rows = cc.united_rows(self.quarterly)
        self._models: dict = {}
        self._estimates: dict = {}

    def model(self, as_of: date):
        """cost_clock.model_as_of(as_of), cached by the latest report it can use; None when it cannot be fitted."""
        key = max((row["filed_on"] for row in self.rows if row["filed_on"] < as_of), default=None)
        if key not in self._models:
            try:
                self._models[key] = cc.model_as_of(as_of, self.quarterly, self.fuel)
            except (ValueError, np.linalg.LinAlgError):
                self._models[key] = None
        return self._models[key]

    def estimate(self, quarter: str, as_of: date, model_as_of: date | None = None) -> dict | None:
        """Expected price paid in `quarter` from fuel prices to `as_of` and the model as of `model_as_of`."""
        key = (quarter, as_of, model_as_of or as_of)
        if key not in self._estimates:
            self._estimates[key] = self._estimate(quarter, as_of, model_as_of or as_of)
        return self._estimates[key]

    def _estimate(self, quarter: str, as_of: date, model_date: date) -> dict | None:
        model = self.model(model_date)
        if model is None or not any(d <= as_of for d in self.dates[:1]):
            return None
        try:
            got = cc.predicted_price_paid(quarter, self.dates, self.values, as_of=as_of, model=model, n_draws=32, seed=SEED)
        except ValueError:
            return None
        return {"usd_gal": got["point"], "as_of": as_of, "last_print": _day(got["last_print"]), "complete": got["complete"],
                "weekdays_remaining": got["weekdays_remaining"], "model_quarters": model.n,
                "model_last_report": model.last_filed_on}

    def past_errors(self, before: date) -> list[dict]:
        """Reported price minus the clock's quarter-end estimate, for clean quarters reported before `before`.

        Each estimate uses the model as it stood at that quarter's end, so every error is out of sample.
        Pandemic quarters are left out, as in the cost clock's own scores.
        """
        out = []
        for row in self.rows:
            if row["filed_on"] >= before or cc.in_pandemic(row["quarter"]):
                continue
            end = cc.quarter_bounds(row["quarter"])[1]
            got = self.estimate(row["quarter"], end)
            if got is None or got["model_quarters"] < MIN_CLOCK_QUARTERS:
                continue
            out.append({"quarter": row["quarter"], "reported": row["price"], "estimate": got["usd_gal"],
                        "error": row["price"] - got["usd_gal"], "filed_on": row["filed_on"]})
        return out


# ------------------------------------------------------------------ inputs

class World:
    """The committed inputs, loaded once."""

    def __init__(self, paths: Paths | None = None):
        self.paths = paths or Paths()
        self.guidance = [r for r in json.loads(self.paths.guidance.read_text()) if r.get("verified")]
        self.reports = filings.load_ual_quarterly(self.paths.quarterly)
        self.by_quarter = {row["quarter"]: row for row in self.reports}
        self.fare = json.loads(self.paths.fare_clock.read_text())
        self.expanding = {e["as_of_period"]: e for e in self.fare["smooth"]["expanding"]}
        self.literature = json.loads(self.paths.literature.read_text())
        self.clock = Clock(self.paths.quarterly, self.paths.fuel)
        self.profit_sharing = cc.profit_sharing_rate(self.literature)

    def records(self, claim: str, quarter: str) -> list[dict]:
        return sorted((r for r in self.guidance if r["claim"] == claim and r["period"] == quarter),
                      key=lambda r: (r["issued_on"], r["id"]))

    def eps_range(self, quarter: str, before: date) -> dict | None:
        """The latest quarterly EPS range for `quarter` issued before `before`."""
        found = [r for r in self.records("adj_eps_guidance", quarter)
                 if isinstance(r["value"], list) and _day(r["issued_on"]) < before]
        return found[-1] if found else None

    def guided_quarters(self) -> list[str]:
        """Quarters for which United gave a quarterly EPS range, oldest first."""
        quarters = {r["period"] for r in self.guidance if r["claim"] == "adj_eps_guidance"
                    and QUARTERLY.match(r["period"]) and isinstance(r["value"], list)}
        return sorted(quarters, key=cc.quarter_index)

    def replay_quarters(self) -> list[str]:
        """Guided quarters whose adjusted EPS has been reported and whose range was issued before the results."""
        return [q for q in self.guided_quarters() if q in self.by_quarter
                and self.by_quarter[q]["adj_eps_diluted"] is not None
                and self.eps_range(q, self.by_quarter[q]["filed_on"]) is not None]


class Known:
    """What was public on `cutoff`. Every dated read is checked and logged; a later one raises LookAhead."""

    def __init__(self, world: World, cutoff: date):
        self.world, self.cutoff, self.uses = world, cutoff, []

    def log(self, what: str, dated: date, **more) -> None:
        if dated > self.cutoff:
            raise LookAhead(f"{what} is dated {dated}, after the forecast date {self.cutoff}")
        self.uses.append({"what": what, "dated": dated.isoformat(), **more})

    def report(self, quarter: str, what: str) -> dict:
        row = self.world.by_quarter[quarter]
        self.log(f"{what}: results for {quarter}", row["filed_on"])
        return row

    def record(self, record: dict, what: str) -> dict:
        self.log(f"{what}: guidance record {record['id']}", _day(record["issued_on"]))
        return record

    def clock(self, quarter: str, as_of: date, what: str, model_as_of: date | None = None) -> dict | None:
        if as_of > self.cutoff or (model_as_of or as_of) > self.cutoff:
            raise LookAhead(f"{what} asks the cost clock for {as_of}, after the forecast date {self.cutoff}")
        got = self.world.clock.estimate(quarter, as_of, model_as_of)
        if got is not None:
            self.log(f"{what}: last fuel price used", got["last_print"])
            self.log(f"{what}: latest report inside the cost clock", got["model_last_report"])
        return got

    def per_share(self, on: date) -> dict | None:
        """Tax rate and diluted shares of the latest profitable quarter reported on or before `on` (the simulator's rule)."""
        for row in reversed(self.world.reports):
            if row["filed_on"] > on:
                continue
            pretax, net, shares = row["pretax_income_musd"], row["net_income_musd"], row["diluted_shares_m"]
            if None in (pretax, net, shares) or pretax <= 0 or shares <= 0 or not 0 <= 1 - net / pretax < 0.6:
                continue
            self.log(f"tax rate and share count: results for {row['quarter']}", row["filed_on"])
            return {"tax_rate": 1 - net / pretax, "diluted_shares_m": shares, "quarter": row["quarter"]}
        return None

    def recovery_share(self, quarter: str) -> dict | None:
        """The same-quarter share of the recovery curve as estimated before `quarter`."""
        entry = self.world.expanding.get(quarter)
        if entry is None:
            return None
        through = entry["fitted_through"]
        if cc.quarter_index(through) >= cc.quarter_index(quarter):
            raise LookAhead(f"The recovery curve for {quarter} is fitted through {through}")
        self.log(f"recovery curve fitted through {through}", self.world.by_quarter[through]["filed_on"])
        return {"share": float(entry["curve_p50"][0]), "fitted_through": through, "n": entry["n"]}

    def misses(self, before_quarter: str) -> list[dict]:
        """Actual minus midpoint for every replayed quarter before `before_quarter` whose results are public."""
        out = []
        for quarter in self.world.replay_quarters():
            row = self.world.by_quarter[quarter]
            if cc.quarter_index(quarter) >= cc.quarter_index(before_quarter) or row["filed_on"] > self.cutoff:
                continue
            guide = self.world.eps_range(quarter, row["filed_on"])
            out.append({"quarter": quarter, "miss": row["adj_eps_diluted"] - _mid(guide["value"]), "filed_on": row["filed_on"]})
        if out:
            self.log(f"United's earlier misses against its midpoint, through {out[-1]['quarter']}",
                     max(item["filed_on"] for item in out))
        return out

    def clock_errors(self) -> list[dict]:
        out = self.world.clock.past_errors(self.cutoff)
        if out:
            self.log(f"the cost clock's earlier misses, through {out[-1]['quarter']}", max(e["filed_on"] for e in out))
        return out


# ------------------------------------------------------------------ the chain

def chain_inputs(world: World, quarter: str, known: Known, results_before: date) -> dict:
    """Everything the chain needs for `quarter` that does not depend on where fuel ended up."""
    first, last = cc.quarter_bounds(quarter)
    guide = known.record(world.eps_range(quarter, results_before), "EPS range")
    guided_on = _day(guide["issued_on"])
    stated = [r for r in world.records("fuel_assumed_usd_gal", quarter) if r["issued_on"] == guide["issued_on"]]
    if stated:
        record = known.record(stated[-1], "fuel price assumed")
        assumed = {"usd_gal": _mid(record["value"]), "source": "stated", "evidence_id": record["id"],
                   "stated_value": record["value"]}
    else:
        got = known.clock(quarter, guided_on, "fuel price assumed (cost clock on the guidance date)")
        assumed = {"usd_gal": None if got is None else got["usd_gal"], "source": "cost_clock", "evidence_id": None,
                   "stated_value": None}
    year_ago = cc.quarter_label(cc.quarter_index(quarter) - 4)
    gallons = known.report(year_ago, "gallons of the same quarter a year earlier")["fuel_gallons_m"]
    per_share = known.per_share(guided_on)
    curve = known.recovery_share(quarter)
    promises = [r for r in world.records("fuel_recovery_share", quarter) if _day(r["issued_on"]) <= known.cutoff]
    management = None
    if promises:
        record = known.record(promises[-1], "recovery share United stated")
        management = {"share": _mid(record["value"]), "stated_value": record["value"], "evidence_id": record["id"],
                      "issued_on": record["issued_on"]}
    later = [r for r in world.records("fuel_assumed_usd_gal", quarter)
             if guide["issued_on"] < r["issued_on"] <= known.cutoff.isoformat()]
    misses = known.misses(quarter)
    errors = known.clock_errors()
    middle = first + timedelta(days=(last - first).days // 2)
    sharing = world.profit_sharing
    return {
        "quarter": quarter, "quarter_start": first, "quarter_end": last, "guided_on": guided_on,
        "guidance_low": float(guide["value"][0]), "guidance_high": float(guide["value"][1]), "mid": _mid(guide["value"]),
        "guidance_evidence_id": guide["id"], "assumed": assumed, "gallons_m": gallons, "gallons_quarter": year_ago,
        "per_share": per_share, "profit_sharing_rate": sharing["rate"] if sharing else None,
        "curve": curve, "management": management, "misses": misses, "clock_errors": errors,
        "late_from": max(middle, guided_on),
        "later_fuel_statements": [{"issued_on": r["issued_on"], "value": r["value"], "guidance_record_id": r["id"]} for r in later]}


def middle_value(values) -> float | None:
    """The median, once there are at least MIN_HISTORY values."""
    return float(np.median(values)) if len(values) >= MIN_HISTORY else None


def spread(misses: list[dict]) -> np.ndarray | None:
    """Earlier misses with their middle value taken out; None until MIN_HISTORY exist."""
    values = np.array([m["miss"] for m in misses], dtype=float)
    return values - np.median(values) if len(values) >= MIN_HISTORY else None


def point_calls(inp: dict, realised: float, late: float, with_profit_sharing: bool = True) -> dict:
    """Every model's central call for the quarter, given where the cost clock puts the fuel price.

    `late` is the part of the clock's estimate that arrived after inp["late_from"]. Returns the calls
    and the dollar steps behind them. Sign: fuel above the assumption is a positive surprise and
    lowers earnings.
    """
    share, tax = inp["curve"]["share"], inp["per_share"]["tax_rate"]
    keep = (1 - tax) * (1 - (inp["profit_sharing_rate"] or 0.0 if with_profit_sharing else 0.0))
    per_gallon = inp["gallons_m"] * keep / inp["per_share"]["diluted_shares_m"]      # USD per share per USD a gallon
    surprise = realised - inp["assumed"]["usd_gal"]
    habit = middle_value([m["miss"] for m in inp["misses"]]) or 0.0
    bias = middle_value([e["error"] for e in inp["clock_errors"]]) or 0.0
    bias = bias if inp["assumed"]["source"] == "stated" else 0.0
    mid = inp["mid"]
    calls = {"guidance_mid": mid, "costs_only": mid - surprise * per_gallon,
             "management": None if inp["management"] is None else mid - (1 - inp["management"]["share"]) * surprise * per_gallon,
             BASE: mid - (1 - share) * surprise * per_gallon,
             "habit": mid - (1 - share) * surprise * per_gallon + habit,
             "late_moves": mid - ((1 - share) * (surprise - late) + late) * per_gallon,
             "clock_bias": mid - (1 - share) * (surprise + bias) * per_gallon}
    return {"calls": calls, "surprise_usd_gal": surprise, "surprise_musd": surprise * inp["gallons_m"],
            "surprise_per_share": surprise * per_gallon, "usd_per_share_per_usd_gal": per_gallon,
            "habit_shift": habit, "clock_bias_usd_gal": bias, "late_usd_gal": late}


def with_range(point: float, residuals: np.ndarray | None) -> dict:
    """A central call and, where earlier misses exist, its P10-P90 range and the members behind it."""
    if residuals is None:
        return {"p10": None, "p50": point, "p90": None, "members": None}
    members = point + residuals
    p10, p90 = np.quantile(members, [0.1, 0.9])
    return {"p10": float(p10), "p50": float(point), "p90": float(p90), "members": members}


def crps(call: dict, actual: float) -> float:
    """CRPS of a call with members; the absolute error of a point call."""
    if call["members"] is None:
        return abs(call["p50"] - actual)
    return float(stats.crps_ensemble(call["members"], actual))


def realised_fuel(inp: dict, known: Known, as_of: date | None = None) -> dict | None:
    """The cost clock's estimate for the quarter as of `as_of` (default: quarter end) and its late part."""
    end = inp["quarter_end"] if as_of is None else min(as_of, inp["quarter_end"])
    model_date = end if as_of is None else as_of
    got = known.clock(inp["quarter"], end, "fuel price at quarter end (cost clock)", model_date)
    if got is None:
        return None
    late = 0.0
    if end > inp["late_from"]:
        earlier = known.clock(inp["quarter"], inp["late_from"], "cost clock at the middle of the quarter", model_date)
        if earlier is None:     # the model's latest report came out after that date: use the model as of then
            earlier = known.clock(inp["quarter"], inp["late_from"], "cost clock at the middle of the quarter")
        late = got["usd_gal"] - earlier["usd_gal"] if earlier is not None else 0.0
    return {**got, "late_usd_gal": late}


def forecast(world: World, quarter: str, cutoff: date, results_before: date, as_of: date | None = None) -> dict:
    """The whole chain for one quarter from what was public on `cutoff`: inputs, calls of every variant, ranges."""
    known = Known(world, cutoff)
    inp = chain_inputs(world, quarter, known, results_before)
    missing = [name for name, value in (("a fuel price assumption", inp["assumed"]["usd_gal"]),
                                        ("a tax rate and share count", inp["per_share"]),
                                        ("a recovery curve estimated before the quarter", inp["curve"]),
                                        ("gallons of the same quarter a year earlier", inp["gallons_m"])) if value is None]
    fuel = None if missing else realised_fuel(inp, known, as_of)
    if fuel is None and not missing:
        missing.append("a cost clock estimate for the quarter")
    if missing:
        return {"quarter": quarter, "computed": False, "reason": "No " + ", no ".join(missing), "inputs": inp, "uses": known.uses}
    residuals = spread(inp["misses"])
    out = {"quarter": quarter, "computed": True, "inputs": inp, "fuel": fuel, "uses": known.uses, "cutoff": cutoff,
           "residuals": residuals}
    for label, sharing in (("primary", True), ("tax_only", False)):
        steps = point_calls(inp, fuel["usd_gal"], fuel["late_usd_gal"], sharing)
        steps["ranges"] = {name: with_range(steps["calls"][name], residuals) for name in VARIANTS}
        out[label] = steps
    return out


# ------------------------------------------------------------------ the replay

def replay(world: World, evaluate_test: bool = True) -> list[dict]:
    """One forecast per replayed quarter, made at quarter end, with the outcome beside it."""
    rows = []
    for quarter in world.replay_quarters():
        split = cc.split_of(quarter)
        if split == "test" and not evaluate_test:
            continue
        report = world.by_quarter[quarter]
        row = forecast(world, quarter, cc.quarter_bounds(quarter)[1], report["filed_on"])
        row.update(split=split, results_on=report["filed_on"], actual=report["adj_eps_diluted"],
                   fuel_reported=report["fuel_price_usd_gal"])
        rows.append(row)
    return rows


def variant_scores(rows: list[dict], treatment: str = "primary") -> dict:
    """Mean CRPS and mean absolute error of each variant over `rows`."""
    out = {}
    for name in VARIANTS:
        calls = [(row[treatment]["ranges"][name], row["actual"]) for row in rows if row["computed"]]
        out[name] = {"crps": float(np.mean([crps(c, a) for c, a in calls])) if calls else None,
                     "mae": float(np.mean([abs(c["p50"] - a) for c, a in calls])) if calls else None, "n": len(calls)}
    return out


def select_variant(rows: list[dict]) -> dict:
    """The variant with the lowest mean CRPS on the quarters before the test years; the base call keeps ties."""
    pool = [row for row in rows if row["split"] in ("fit", "select") and row["computed"]]
    table = variant_scores(pool)
    chosen = BASE
    for name in VARIANTS[1:]:
        if table[name]["crps"] is not None and table[name]["crps"] < table[chosen]["crps"] - 1e-9:
            chosen = name
    by_split = {split: len([row for row in pool if row["split"] == split]) for split in ("fit", "select")}
    return {"chosen": chosen, "table": table, "n": len(pool), "n_by_split": by_split,
            "quarters": [row["quarter"] for row in pool],
            "rule": "Lowest average CRPS over the replayed quarters of 2013-2023; the plain fitted call keeps ties. "
                    "Decided before any quarter from 2024 onward was scored, and identical whether or not those "
                    "quarters are loaded."}


def model_call(row: dict, model: str, chosen: str, treatment: str = "primary") -> dict | None:
    """A model's call on a row as {p10, p50, p90, members}; `fitted` is the chosen variant."""
    if model == BASE:
        return row[treatment]["ranges"][chosen]
    value = row[treatment]["calls"][model]
    return None if value is None else {"p10": None, "p50": value, "p90": None, "members": None}


def score_table(rows: list[dict], chosen: str, treatment: str = "primary") -> list[dict]:
    out = []
    for split in ("fit", "select", "test"):
        part = [row for row in rows if row["split"] == split and row["computed"]]
        for model in MODELS:
            calls = [(model_call(row, model, chosen, treatment), row["actual"]) for row in part]
            calls = [(c, a) for c, a in calls if c is not None]
            ranged = [(c, a) for c, a in calls if c["p10"] is not None]
            out.append({"model": model, "split": split, "n": len(calls),
                        "mae": float(np.mean([abs(c["p50"] - a) for c, a in calls])) if calls else None,
                        "crps": float(np.mean([crps(c, a) for c, a in calls])) if calls else None,
                        "inside_p10_p90": float(np.mean([c["p10"] <= a <= c["p90"] for c, a in ranged])) if ranged else None,
                        "n_with_range": len(ranged)})
    return out


def head_to_head(rows: list[dict], chosen: str, seed: int) -> dict:
    """Fitted against the midpoint on the same quarters: mean CRPS difference with a bootstrap range, and wins."""
    out = {}
    for i, split in enumerate(("select", "test")):
        part = [row for row in rows if row["split"] == split and row["computed"]]
        if not part:
            out[split] = {"n": 0}
            continue
        ours = np.array([crps(model_call(row, BASE, chosen), row["actual"]) for row in part])
        mid = np.array([abs(row["primary"]["calls"]["guidance_mid"] - row["actual"]) for row in part])
        closer = [abs(model_call(row, BASE, chosen)["p50"] - row["actual"]) < abs(row["inputs"]["mid"] - row["actual"]) for row in part]
        diff = stats.paired_loss_diff(ours, mid, N_BOOT, seed + i, LEVEL)
        ranged = [crps(with_range(row["inputs"]["mid"], row["residuals"]), row["actual"]) for row in part]
        biggest = sorted(part, key=lambda row: -abs(row["primary"]["surprise_per_share"]))[:2]
        out[split] = {"n": len(part), "fitted_minus_midpoint_crps": diff["mean_diff"],
                      "lo": None if math.isnan(diff["lo"]) else diff["lo"], "hi": None if math.isnan(diff["hi"]) else diff["hi"],
                      "level": LEVEL, "quarters_fitted_closer": int(sum(closer)),
                      "note": "Negative favours the fitted call. The range is a bootstrap over quarters and is too "
                              "narrow with this few quarters.",
                      "like_for_like": {
                          "fitted_crps": float(ours.mean()), "midpoint_with_the_same_range_crps": float(np.mean(ranged)),
                          "midpoint_as_a_single_number_crps": float(mid.mean()),
                          "note": "CRPS rewards a call for carrying a range. Here the midpoint is given the same range as "
                                  "the fitted call (the same earlier misses, centred on the midpoint), so the two differ "
                                  "only in the fuel marking. This line is a reading aid added after the test quarters "
                                  "were scored; it is not one of the pre-listed models."},
                      "largest_fuel_surprises": [
                          {"period": row["quarter"], "fuel_surprise_per_share": row["primary"]["surprise_per_share"],
                           "actual_eps": row["actual"], "guidance_mid": row["inputs"]["mid"],
                           "fitted_p50": model_call(row, BASE, chosen)["p50"],
                           "closer": "fitted" if abs(model_call(row, BASE, chosen)["p50"] - row["actual"])
                                     < abs(row["inputs"]["mid"] - row["actual"]) else "guidance_mid"} for row in biggest]}
    return out


# ------------------------------------------------------------------ side checks

def fuel_price_test(world: World, evaluate_test: bool) -> dict:
    """United's first stated fuel price for a quarter against the cost clock at quarter end, both against the reported price."""
    rows = []
    for report in world.reports:
        quarter, split = report["quarter"], cc.split_of(report["quarter"])
        if report["fuel_price_usd_gal"] is None or (split == "test" and not evaluate_test):
            continue
        stated = [r for r in world.records("fuel_assumed_usd_gal", quarter) if _day(r["issued_on"]) < report["filed_on"]
                  and _day(r["issued_on"]) <= cc.quarter_bounds(quarter)[1]]
        if not stated:
            continue
        record, end = stated[0], cc.quarter_bounds(quarter)[1]
        marked = world.clock.estimate(quarter, end)
        if marked is None or marked["model_quarters"] < MIN_CLOCK_QUARTERS:
            continue
        early = world.clock.estimate(quarter, _day(record["issued_on"]))
        early = early if early is not None and early["model_quarters"] >= MIN_CLOCK_QUARTERS else None
        price = report["fuel_price_usd_gal"]
        rows.append({"period": quarter, "split": split, "pandemic": cc.in_pandemic(quarter), "stated_on": record["issued_on"],
                     "guidance_record_id": record["id"], "fuel_guided": _mid(record["value"]), "fuel_marked": marked["usd_gal"],
                     "fuel_clock_on_statement_date": None if early is None else early["usd_gal"], "fuel_reported": price,
                     "error_guided": _mid(record["value"]) - price, "error_marked": marked["usd_gal"] - price,
                     "error_clock_on_statement_date": None if early is None else early["usd_gal"] - price})

    def summary(part: list[dict]) -> dict:
        mean = lambda key: float(np.mean([abs(r[key]) for r in part if r[key] is not None])) if part else None
        return {"n": len(part), "mae_guided": mean("error_guided"), "mae_marked": mean("error_marked"),
                "mae_clock_on_statement_date": mean("error_clock_on_statement_date"),
                "marked_closer_in": int(sum(abs(r["error_marked"]) < abs(r["error_guided"]) for r in part))}

    clean = [r for r in rows if not r["pandemic"]]
    return {**summary(clean),
            "definition": "mae_guided: average distance between the first fuel price United stated for a quarter and the "
                          "price per gallon it later reported. mae_marked: the same for the cost clock's estimate at "
                          "quarter end, with the clock refitted only on reports published before that date. "
                          "mae_clock_on_statement_date: the clock on the day United stated its price, a like-for-like "
                          "comparison. US dollars per gallon.",
            "unit": "USD per gallon",
            "by_split": {split: summary([r for r in clean if r["split"] == split]) for split in ("fit", "select", "test")},
            "with_pandemic_quarters": summary(rows),
            "rule": f"Quarters where United stated a fuel price before the quarter ended and the cost clock had at least "
                    f"{MIN_CLOCK_QUARTERS} reports behind it. 2020Q1 to 2021Q4 are left out of the headline numbers, as in "
                    "the cost clock's own scores, and kept in `quarters`.",
            "note": "The two are not given the same information: United states its price about three weeks into the "
                    "quarter, the clock at quarter end has seen the whole quarter's fuel prices. That is the point of "
                    "marking guidance to fuel.",
            "quarters": rows}


def delta_check(world: World) -> dict:
    """Delta's September 2026 quarter as a worked example of the same chain, with Delta's reported fuel price."""
    data = json.loads(world.paths.dal_check.read_text())
    guide, actual, june = data["guidance_sep_quarter"], data["actuals_sep_quarter"], data["actuals_jun_quarter"]
    tax = data["derived"]["actuals_jun_quarter"]["adj_effective_tax_rate_pct"] / 100.0
    shares, gallons = june["diluted_shares_m"], actual["prior_year"]["fuel_gallons_m"]
    assumed, price = guide["fuel_price_assumed_usd_gal"], actual["fuel_price_adj_usd_gal"]
    mid = (guide["eps_low"] + guide["eps_high"]) / 2.0
    per_share = (price - assumed) * gallons * (1 - tax) / shares
    entry = world.expanding.get(guide["period"])
    share = float(entry["curve_p50"][0]) if entry else None
    draws = np.array(world.fare["curve_draws"], dtype=float)[:, 0]
    low, high = (float(v) for v in np.quantile(draws, [0.1, 0.9]))
    fitted = None if share is None else {"p10": mid - (1 - min(low, share)) * per_share, "p50": mid - (1 - share) * per_share,
                                         "p90": mid - (1 - max(high, share)) * per_share}
    eps = actual["adj_eps_diluted"]
    ids = [guide["evidence_ids"][k] for k in ("eps_low", "eps_high", "fuel_price_assumed_usd_gal")]
    ids += [actual["evidence_ids"][k] for k in ("adj_eps_diluted", "fuel_price_adj_usd_gal")]
    more = [actual["prior_year"]["evidence_ids"]["fuel_gallons_m"], june["evidence_ids"]["diluted_shares_m"]]
    return {"period": guide["period"], "airline": "DAL", "guided_on": guide["made_on"], "reported_on": actual["reported_on"],
            "guidance_low": guide["eps_low"], "guidance_high": guide["eps_high"], "actual_eps": eps,
            "fuel_assumed": assumed, "fuel_realized": price, "fuel_realized_source": "reported (adjusted price per gallon)",
            "fuel_surprise_usd_gal": price - assumed, "gallons_m": gallons,
            "gallons_quarter": cc.quarter_label(cc.quarter_index(guide["period"]) - 4),
            "fuel_surprise_musd": (price - assumed) * gallons, "tax_rate": tax, "diluted_shares_m": shares,
            "profit_sharing_rate": None,
            "profit_sharing_note": "Delta's profit-sharing rate is not in the committed files, so the per-share figure is "
                                   "after tax only and overstates the hit to earnings.",
            "fuel_surprise_per_share": per_share, "recovery_share_used": share,
            "recovery_share_range_used": [low, high],
            "calls": {"guidance_mid": mid, "costs_only": mid - per_share, "management": None, "fitted": fitted},
            "implied_recovery_share": 1 - (mid - eps) / per_share,
            "implied_recovery_share_note": "The share that would make the chain land on Delta's reported EPS, if fuel were "
                                           "the only reason for the miss. It is arithmetic on one quarter, not an estimate.",
            "management_note": "Delta stated no recovery share, so there is no management call.",
            "note": "A worked example, not a forecast: Delta reported on " + actual["reported_on"] + ", before this file was "
                    "built. The fuel price is the one Delta reported, the recovery share is United's same-quarter share as "
                    "estimated for 2026Q3, and the range uses only the spread of that share (P10 to P90 of the fare "
                    "clock's draws).",
            "evidence_ids": ids, "other_record_ids": more,
            "ids_note": "evidence_ids: Delta's range, assumed fuel price, reported EPS and reported fuel price. "
                        "other_record_ids: the year-ago gallons and the June-quarter share count, which are records in "
                        "data/lab/dal_check.json."}


def results_day_test(world: World, rows: list[dict], chosen: str) -> dict:
    """Does the gap between the fitted call and the midpoint at quarter end line up with the share price move on results?"""
    dates, returns, _ = market_data.aligned_log_returns([market_clock.TARGET, market_clock.MARKET], prices_path=world.paths.prices)
    excess, _ = market_clock.excess_returns(returns[:, 0], returns[:, 1])
    index = {d: i for i, d in enumerate(dates)}
    points = []
    for row in rows:
        if not row["computed"]:
            continue
        i = index.get(row["results_on"])
        if i is None or i + 1 >= len(dates) or not np.isfinite(excess[i:i + 2]).all():
            continue
        gap = model_call(row, BASE, chosen)["p50"] - row["inputs"]["mid"]
        points.append({"period": row["quarter"], "split": row["split"], "results_on": row["results_on"].isoformat(),
                       "next_session": dates[i + 1].isoformat(), "gap_usd_per_share": gap,
                       "hedged_return": float(excess[i] + excess[i + 1])})

    def summary(part: list[dict]) -> dict:
        n = len(part)
        out = {"n": n, "slope": None, "sign": None, "lo": None, "hi": None, "hit_rate": None, "n_hits": None, "n_nonzero": None}
        if not n:
            return out
        x, y = np.array([p["gap_usd_per_share"] for p in part]), np.array([p["hedged_return"] for p in part])
        moved = (x != 0) & (y != 0)
        out.update(n_nonzero=int(moved.sum()), n_hits=int((np.sign(x[moved]) == np.sign(y[moved])).sum()),
                   hit_rate=float((np.sign(x[moved]) == np.sign(y[moved])).mean()) if moved.any() else None)
        if n >= 3 and np.ptp(x) > 0:
            fit = stats.ols(x, y)
            out.update(slope=float(fit.beta[1]), sign="positive" if fit.beta[1] > 0 else "negative")
            if n >= 5:
                robust = stats.newey_west(fit, None, small_sample=True)
                lo, hi = stats.conf_int(fit.beta[1], robust.se[1], LEVEL, df=fit.df_resid)
                out.update(lo=lo, hi=hi)
            else:
                out["interval_note"] = "Fewer than five quarters: no interval is given."
        return out

    by_split = {split: summary([p for p in points if p["split"] == split]) for split in ("fit", "select", "test")}
    by_split["all"] = summary(points)
    return {"question": "Across the replayed quarters, does the fitted call minus United's midpoint at quarter end predict "
                        "United's share price move on the results day and the day after, with the market's move taken out?",
            "definition": "gap_usd_per_share is the fitted P50 minus the guidance midpoint, known at quarter end. "
                          "hedged_return is United's log return minus beta times the S&P 500 fund's, summed over the "
                          "results day and the next session, with beta from the previous "
                          f"{market_clock.BETA_WINDOW} sessions only. slope is the change in that return per one dollar "
                          "of gap (0.05 means five percentage points per dollar), with a "
                          f"{LEVEL:.0%} Newey-West range. hit_rate is the share of quarters where the two have the same sign.",
            "pre_specified": True, "by_split": by_split, "points": points,
            "note": "United reports after the market closes, so the reaction is mostly in the next session; both days "
                    "are summed as specified. Reported whatever it shows."}


# ------------------------------------------------------------------ wording generated from the numbers

def _usd(value: float) -> str:
    return f"-${abs(value):.2f}" if value < 0 else f"${value:.2f}"


def _score(scores: list[dict], model: str, split: str) -> dict:
    return next(s for s in scores if s["model"] == model and s["split"] == split)


def headline(scores: list[dict] | None, selection: dict, evaluate_test: bool) -> str:
    if not evaluate_test:
        table, n = selection["table"], selection["n"]
        return (f"On the {n} guided quarters before 2024 the fitted call missed reported earnings by "
                f"{_usd(table[selection['chosen']]['mae'])} a share on average; the quarters from 2024 onward are withheld "
                "in this build.")
    ours, mid, costs = (_score(scores, m, "test") for m in (BASE, "guidance_mid", "costs_only"))
    verdict = ("closer than" if ours["mae"] < mid["mae"] - 0.005 else "no closer than" if ours["mae"] <= mid["mae"] + 0.005
               else "further off than")
    return (f"In the {ours['n']} guided quarters from 2024 to mid-2026, marking United's midpoint to fuel with the measured "
            f"recovery share missed reported earnings by {_usd(ours['mae'])} a share on average, {verdict} the midpoint "
            f"itself ({_usd(mid['mae'])}) and against {_usd(costs['mae'])} if no fuel cost were recovered.")


# ------------------------------------------------------------------ the output file

def public_row(row: dict, chosen: str, show_variants: bool) -> dict:
    """One replayed quarter as the page reads it."""
    inp = row["inputs"]
    base = {"period": row["quarter"], "split": row["split"], "guided_on": inp["guided_on"].isoformat(),
            "guidance_low": inp["guidance_low"], "guidance_high": inp["guidance_high"], "guidance_mid": inp["mid"],
            "guidance_evidence_id": inp["guidance_evidence_id"], "forecast_as_of": row["cutoff"].isoformat() if row["computed"] else None,
            "results_on": row["results_on"].isoformat(), "actual_eps": row["actual"],
            "miss_against_midpoint": row["actual"] - inp["mid"], "computed": row["computed"]}
    if not row["computed"]:
        return {**base, "reason": row["reason"], "calls": None}
    main, plain, fuel = row["primary"], row["tax_only"], row["fuel"]

    def calls(steps: dict) -> dict:
        ours = steps["ranges"][chosen]
        return {"guidance_mid": steps["calls"]["guidance_mid"], "costs_only": steps["calls"]["costs_only"],
                "management": steps["calls"]["management"], "fitted": {k: ours[k] for k in ("p10", "p50", "p90")}}

    ours = main["ranges"][chosen]
    out = {**base,
           "fuel_assumed": inp["assumed"]["usd_gal"], "fuel_assumed_source": inp["assumed"]["source"],
           "fuel_assumed_evidence_id": inp["assumed"]["evidence_id"], "fuel_assumed_as_stated": inp["assumed"]["stated_value"],
           "fuel_realized": fuel["usd_gal"], "fuel_realized_source": "cost clock at quarter end, refitted on reports published before then",
           "fuel_reported": row["fuel_reported"],
           "fuel_surprise_usd_gal": main["surprise_usd_gal"], "gallons_m": inp["gallons_m"], "gallons_quarter": inp["gallons_quarter"],
           "fuel_surprise_musd": main["surprise_musd"], "tax_rate": inp["per_share"]["tax_rate"],
           "diluted_shares_m": inp["per_share"]["diluted_shares_m"], "tax_and_shares_quarter": inp["per_share"]["quarter"],
           "profit_sharing_rate": inp["profit_sharing_rate"],
           "fuel_surprise_per_share": main["surprise_per_share"],
           "fuel_surprise_per_share_without_profit_sharing": plain["surprise_per_share"],
           "recovery_share": inp["curve"]["share"], "recovery_curve_fitted_through": inp["curve"]["fitted_through"],
           "management_share": None if inp["management"] is None else inp["management"]["share"],
           "management_evidence_id": None if inp["management"] is None else inp["management"]["evidence_id"],
           "earlier_misses_n": len(inp["misses"]),
           "range_source": "earlier misses against the midpoint" if ours["p10"] is not None
                           else f"none: fewer than {MIN_HISTORY} earlier guided quarters",
           "calls": calls(main), "calls_without_profit_sharing": calls(plain),
           "error": {model: None if model_call(row, model, chosen) is None else model_call(row, model, chosen)["p50"] - row["actual"]
                     for model in MODELS},
           "crps_fitted": crps(ours, row["actual"]),
           "inside_p10_p90": None if ours["p10"] is None else bool(ours["p10"] <= row["actual"] <= ours["p90"]),
           "later_fuel_statements": inp["later_fuel_statements"], "uses": row["uses"]}
    if show_variants:
        out["variants"] = {name: {k: main["ranges"][name][k] for k in ("p10", "p50", "p90")} for name in VARIANTS}
    return out


def variants_tried(selection: dict, evaluate_test: bool) -> list[str]:
    out = []
    for name in VARIANTS:
        score = selection["table"][name]
        picked = name == selection["chosen"]
        tail = ("Chosen" + ("; scored once on 2024 onward." if evaluate_test else "; the test quarters are withheld in this build.")
                if picked else "Not chosen; never scored on 2024 onward.")
        if score["crps"] is None:
            out.append(f"{name}: {VARIANT_NOTES[name]} No quarter before 2024 could be scored. {tail}")
            continue
        out.append(f"{name}: {VARIANT_NOTES[name]} On the {score['n']} guided quarters before 2024: average CRPS "
                   f"{score['crps']:.3f} and average miss {score['mae']:.3f} US dollars a share. {tail}")
    out.append("Profit sharing: every call is also computed after tax only (calls_without_profit_sharing, "
               "scores_without_profit_sharing). That is a second accounting treatment of the same calls, not a variant "
               "that could be chosen.")
    return out


def build(paths: Paths | None = None, evaluate_test: bool = True, seed: int = SEED, generated_at: str | None = None) -> dict:
    """Everything track_record.json carries. `evaluate_test=False` never loads a quarter from 2024 onward."""
    world = World(paths)
    rows = replay(world, evaluate_test)
    selection = select_variant(rows)
    chosen = selection["chosen"]
    scores = score_table(rows, chosen)
    plain_scores = score_table(rows, chosen, "tax_only")
    if not evaluate_test:
        scores = [s for s in scores if s["split"] != "test"]
        plain_scores = [s for s in plain_scores if s["split"] != "test"]
    by_split = lambda table: {split: [s for s in table if s["split"] == split] for split in ("fit", "select", "test")
                              if evaluate_test or split != "test"}
    fuel = fuel_price_test(world, evaluate_test)
    delta = delta_check(world) if evaluate_test else None
    day = results_day_test(world, rows, chosen)
    duel = head_to_head(rows, chosen, seed)
    guided = world.guided_quarters()
    replayed = [row["quarter"] for row in rows]
    sharing = world.profit_sharing
    stated = [row["quarter"] for row in rows if row["computed"] and row["inputs"]["assumed"]["source"] == "stated"]
    dates_used = [_day(use["dated"]) for row in rows for use in row["uses"]] + [row["results_on"] for row in rows]
    dates_used += [_day(p["next_session"]) for p in day["points"]]
    if delta:
        dates_used.append(_day(delta["reported_on"]))
    n_by = {split: len([r for r in rows if r["split"] == split]) for split in ("fit", "select", "test")}

    summary = [headline(scores, selection, evaluate_test)]
    summary.append(f"United gave a quarterly EPS range for {len(guided)} quarters, the first in {guided[0]}; "
                   f"{len(replayed)} of them have reported results and are replayed here "
                   f"({n_by['fit']} in 2013-2019, {n_by['select']} in 2020-2023, {n_by['test']} from 2024 onward).")
    summary.append(f"The method was chosen on the {selection['n']} quarters before 2024: {VARIANT_LABELS[chosen]} had the lowest "
                   f"score (average CRPS {selection['table'][chosen]['crps']:.3f} US dollars a share, against "
                   f"{selection['table'][BASE]['crps']:.3f} for the plain fitted call). So few quarters cannot tell close "
                   "variants apart: the choice is a rule fixed in advance, not evidence that the variant is better.")
    if evaluate_test:
        test = {m: _score(scores, m, "test") for m in MODELS}
        t = duel["test"]
        summary.append(f"From 2024 onward ({test[BASE]['n']} quarters) the average miss was {_usd(test[BASE]['mae'])} a share "
                       f"for the fitted call, {_usd(test['guidance_mid']['mae'])} for United's midpoint and "
                       f"{_usd(test['costs_only']['mae'])} for costs only. The fitted call was closer than the midpoint in "
                       f"{t['quarters_fitted_closer']} of {t['n']} quarters.")
        like = t["like_for_like"]
        summary.append(f"CRPS, which also rewards a sensible range, was {like['fitted_crps']:.2f} for the fitted call and "
                       f"{like['midpoint_as_a_single_number_crps']:.2f} for the midpoint as a single number. Give the midpoint "
                       f"the same range and it scores {like['midpoint_with_the_same_range_crps']:.2f}: the range does the work, "
                       "not the fuel marking.")
        for item in t["largest_fuel_surprises"]:
            way = "more" if item["fuel_surprise_per_share"] > 0 else "less"
            summary.append(f"{item['period']}: fuel cost {_usd(abs(item['fuel_surprise_per_share']))} a share {way} than "
                           f"assumed. United reported {_usd(item['actual_eps'])}; its midpoint was {_usd(item['guidance_mid'])}, "
                           f"the fitted call {_usd(item['fitted_p50'])}. The "
                           f"{'fitted call' if item['closer'] == 'fitted' else 'midpoint'} was closer.")
        if test[BASE]["inside_p10_p90"] is not None:
            summary.append(f"United's result fell inside the fitted call's P10-P90 range in "
                           f"{round(test[BASE]['inside_p10_p90'] * test[BASE]['n_with_range'])} of {test[BASE]['n_with_range']} "
                           "quarters; a well-sized range would hold about 8 in 10.")
        if test["management"]["n"]:
            summary.append(f"United stated a recovery share for {test['management']['n']} past quarter; using it missed by "
                           f"{_usd(test['management']['mae'])} a share there.")
        if fuel["n"]:
            summary.append(f"Fuel price check ({fuel['n']} quarters): United's stated price was {100 * fuel['mae_guided']:.0f} "
                           f"cents a gallon from the price it later reported on average; the cost clock at quarter end was "
                           f"{100 * fuel['mae_marked']:.0f} cents away.")
        r = day["by_split"]["test"]
        if r["slope"] is not None:
            where = "spans zero" if r["lo"] is not None and r["lo"] <= 0 <= r["hi"] else "does not span zero"
            rng = f" ({LEVEL:.0%} range {100 * r['lo']:+.1f} to {100 * r['hi']:+.1f}, which {where})" if r["lo"] is not None else ""
            summary.append(f"Results-day check, 2024 onward ({r['n']} quarters): one dollar of gap between the fitted call "
                           f"and the midpoint went with a {100 * r['slope']:+.1f} percentage point move in United's "
                           f"market-hedged shares over the results day and the next{rng}; the two had the same sign in "
                           f"{r['n_hits']} of {r['n_nonzero']} quarters.")
        summary.append(f"Delta's September 2026 quarter: guided {_usd(delta['guidance_low'])} to {_usd(delta['guidance_high'])} "
                       f"at about {_usd(delta['fuel_assumed'])} a gallon, paid {_usd(delta['fuel_realized'])}, reported "
                       f"{_usd(delta['actual_eps'])}. Costs only gives {_usd(delta['calls']['costs_only'])}; the fitted share "
                       f"gives {_usd(delta['calls']['fitted']['p50'])}.")
    limits = [
        f"United gave quarterly EPS ranges only from {guided[0]}. Before that it guided a pre-tax margin, which this replay "
        f"does not convert. So there are no replayed quarters in 2013-2019, {n_by['select']} in 2020-2023 and {n_by['test']} "
        "from 2024 onward: every score here rests on few quarters.",
        "Guided quarters without a replay row: " + (", ".join(q for q in guided if q not in replayed) or "none")
        + " (not yet reported). 2023Q4 has no verified quarterly EPS range in data/lab/ual_guidance.json and is not replayed.",
        "This replays the chain the simulator's measured mode uses, not the simulator: the simulator is calibrated to the "
        "2026 business and cannot be started in an earlier year.",
        f"United stated a fuel price for {len(stated)} of the replayed quarters ({', '.join(stated) or 'none'}). For the others "
        "the assumption is the cost clock's estimate on the guidance date, so the surprise there is only the move in spot "
        "fuel after guidance.",
        "The fuel price at quarter end is the cost clock's estimate, not the reported price, because the reported price "
        "comes out with the results. The clock's coefficients are re-estimated only on reports published before each date, "
        "but its specification and starting year were chosen once on 2013-2023, so replays of 2022 and 2023 use a "
        "specification picked with those years in view.",
        ("Profit sharing: " + f"{sharing['rate']:.1%} of pre-tax earnings at the margin, the sum of three tiers read from the "
         "notes on United's July 2026 investor update in data/lab/literature.json. That is a paraphrase, not a verified "
         "quote, and the tiers are the 2026 ones applied to every year. United's own July 2026 arithmetic ($575 million = "
         "$1.12 a share, a verified quote) is consistent with it. Every call and score is also shown after tax only."
         if sharing else "Profit sharing: no rate was found, so every call is after tax only."),
        "Tax rate and share count: the latest profitable quarter reported on or before the guidance date, tax rate = 1 - net "
        "income / pre-tax income of that quarter (the simulator's rule). The guidance comes out in the same release as that "
        "quarter's results.",
        f"The range around the fitted call is the spread of United's earlier misses against its own midpoint, centred on "
        f"the call. It needs {MIN_HISTORY} earlier guided quarters, so the first {MIN_HISTORY} replayed quarters have no "
        "range, and early ranges rest on three or four misses.",
        "The recovery curve's two-parameter form was chosen by the fare clock after it had seen 2024-2026. Each quarter's "
        "curve here is estimated only on earlier quarters, but the form itself is not free of hindsight.",
        "Everything that moves earnings other than fuel after guidance (demand, storms, labour deals, one-off items) is in "
        "the misses, not in the chain."]
    payload = {
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": max(dates_used).isoformat(),
        "inputs": inputs_list(world.paths.all() if evaluate_test else world.paths.all()[:-1]),
        "seed": seed, "test_split": "computed once, after the method was chosen on 2020-2023" if evaluate_test else "withheld",
        "splits": {name: list(years) for name, years in SPLITS.items()},
        "headline": headline(scores, selection, evaluate_test), "plain_summary": summary,
        "method": {
            "what_is_replayed": "The full simulator cannot be re-run in past years: it is calibrated to United's business "
                                "as of July 2026. This replay uses the same chain the simulator's measured mode uses: fuel "
                                "price paid from the cost clock, the same-quarter recovery share from the fare clock as it "
                                "would have been estimated at the time, then tax, profit sharing and share count.",
            "steps": [
                "Guidance: the latest quarterly adjusted EPS range United issued before the quarter's results, and the fuel "
                "price stated with it. Where none was stated, the cost clock's estimate on the guidance date.",
                "Fuel at quarter end: the cost clock's estimate with every fuel price of the quarter, refitted on reports "
                "published before quarter end.",
                "Fuel surprise: quarter-end price minus assumed price, times the gallons of the same quarter a year "
                "earlier, times (1 - tax rate) and (1 - profit-sharing rate), over diluted shares.",
                "Calls: guidance_mid = midpoint; costs_only = midpoint minus the surprise; management = midpoint minus "
                "(1 - stated share) x surprise; fitted = midpoint minus (1 - measured same-quarter share) x surprise.",
                f"Range: the fitted call plus each earlier miss of United against its own midpoint, less their middle "
                f"value; P10 and P90 are read off those values once {MIN_HISTORY} exist.",
                "Scores: average miss (MAE) and CRPS in US dollars a share; for a single-number call CRPS is the miss."],
            "forecast_date": "Each forecast is made at the quarter's last day, about three weeks before results.",
            "variants": dict(VARIANT_NOTES)},
        "profit_sharing": ({"rate": sharing["rate"], "tiers": sharing["tiers"], "source_id": sharing["source_id"],
                            "source": "notes field of that entry in data/lab/literature.json",
                            "verified_quote": False, "caveat": sharing["caveat"],
                            "treatment": "Applied at the margin to every quarter; results are shown with and without it."}
                           if sharing else None),
        "quarters": [public_row(row, chosen, row["split"] != "test") for row in rows],
        "scores": [s for s in scores if s["split"] == "test"], "scores_by_split": by_split(scores),
        "scores_without_profit_sharing": [s for s in plain_scores if s["split"] == "test"],
        "scores_without_profit_sharing_by_split": by_split(plain_scores),
        "scores_note": "`scores` holds the quarters from 2024 onward, the only ones the method never saw; "
                       "`scores_by_split` holds every period. mae and crps are in US dollars a share. inside_p10_p90 is "
                       "the share of quarters with a range (n_with_range) whose result fell inside it; null for "
                       "single-number calls. `fitted` is the chosen variant.",
        "fitted_against_midpoint": duel,
        "selection": {k: selection[k] for k in ("chosen", "rule", "n", "n_by_split", "quarters", "table")},
        "variants_tried": variants_tried(selection, evaluate_test),
        "fuel_price_test": fuel, "delta_check": delta, "results_day_test": day, "limits": limits}
    return cc._clean(payload)


REQUIRED = ("generated_at", "as_of", "inputs", "headline", "plain_summary", "limits", "method", "quarters", "scores",
            "scores_by_split", "selection", "variants_tried", "fuel_price_test", "delta_check", "results_day_test")


def validate(payload: dict) -> None:
    """Shape checks the builder runs before writing; raises ValueError naming the first problem."""
    missing = [key for key in REQUIRED if key not in payload]
    if missing:
        raise ValueError(f"track_record.json lacks {missing}")
    if payload.get("fixture"):
        raise ValueError("track_record.json must not be a fixture")
    text = json.dumps(payload, allow_nan=False)
    if str(ROOT) in text or "/Users/" in text:
        raise ValueError("track_record.json contains an absolute local path")
    if not all(set(item) >= {"path", "sha256"} and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in payload["inputs"]):
        raise ValueError("inputs must list path and sha256")
    if not payload["headline"] or not isinstance(payload["plain_summary"], list) or not payload["plain_summary"] or not payload["limits"]:
        raise ValueError("headline, plain_summary and limits must not be empty")
    if payload["selection"]["chosen"] not in VARIANTS:
        raise ValueError("selection.chosen must be one of the pre-listed variants")
    for row in payload["quarters"]:
        if not row["computed"]:
            continue
        if not row["forecast_as_of"] < row["results_on"]:
            raise ValueError(f"{row['period']}: the forecast date is not before the results date")
        late = [use for use in row["uses"] if use["dated"] > row["forecast_as_of"]]
        if late:
            raise ValueError(f"{row['period']} reads {late[0]['what']} dated {late[0]['dated']}, after its forecast date")
        ours = row["calls"]["fitted"]
        if ours["p10"] is not None and not ours["p10"] <= ours["p50"] <= ours["p90"]:
            raise ValueError(f"{row['period']}: fitted quantiles are out of order")
    for score in [s for part in payload["scores_by_split"].values() for s in part]:
        if score["n"] and not (score["mae"] is not None and score["mae"] >= 0):
            raise ValueError("a score with quarters needs a mean absolute error")
    if payload["scores"] != payload["scores_by_split"].get("test", []):
        raise ValueError("scores must be the test rows of scores_by_split")
