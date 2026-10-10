"""The single definition of a pricing response to a fuel-cost shock.

A response is a recovery curve: the share of an incremental fuel cost that United
wins back through unit revenue in quarters 0..4 after the fuel price changes
(quarter 0 is the quarter of the change). Shares may be negative or exceed 1.
After quarter 4 the last value is held. Uncertainty is carried as joint draws,
one row per draw, so the quarters stay correlated the way they were estimated.

Monthly rule (`monthly_curve`): a step function, no smoothing. Month k after the
change (k = 0 is the month of the change) takes the value of quarter
min(k // 3, 4). A constant shock that begins in the first month of a quarter
therefore recovers exactly the quarterly share in every quarter.

Sampling rule (`Response.sample`): one row of `draws` per simulated path, chosen
with replacement by a generator seeded from (RESPONSE_STREAM, seed). It never
touches the generator that draws the simulator's fuel and demand worlds, so one
seed gives the same worlds under every response.

Management ranges (`management`): each draw takes one uniform position u in
[0, 1] and uses low + u * (high - low) in every stated quarter. That is uniform
within each stated range and keeps a draw equally optimistic in all quarters.
Quarters after the last stated one hold its value. The named paths in
MANAGEMENT_PATHS are United's own statements about one shock, the 2026 fuel
increase, placed on quarters after that shock by MANAGEMENT_MAPPING.

The same container carries the measured capacity response (`capacity`): change in
capacity growth, in percentage points per 10% fuel price increase, by quarter
after the shock. It is sampled on its own random stream (CAPACITY_STREAM).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
FARE_CLOCK = ROOT / "app" / "static" / "data" / "fare_clock.json"
CAPACITY_RESPONSE = ROOT / "app" / "static" / "data" / "capacity_response.json"
LITERATURE = ROOT / "data" / "lab" / "literature.json"
QUARTERS = 5
RESPONSE_STREAM = 0x52455350  # "RESP"
CAPACITY_STREAM = 0x43415041  # "CAPA"
MANAGEMENT_DRAWS = 2000
# United's statements about the 2026 fuel increase, as (low, high) shares by quarter
# after the shock. Entry ids are those of data/lab/literature.json, which holds the quotes.
MANAGEMENT_MAPPING = ("The fuel increase began in spring 2026, so 2026Q2 is quarter 0 of that shock, 2026Q3 quarter 1 "
                      "and 2026Q4 quarter 2. Later quarters are taken as full recovery.")
MANAGEMENT_PATHS = {
    "july_2026": {
        "ranges": ((.40, .50), (.80, .90), (1.0, 1.0), (1.0, 1.0), (1.0, 1.0)),
        "literature_ids": ("ual-2026q1-investor-update", "ual-2026q2-investor-update", "ual-2026q2-earnings-release"),
        "stated": "April 2026 guidance assumed 40-50% recovered in 2026Q2 and the July release says about half was; "
                  "July 2026 guidance: 80-90% in 2026Q3 and 100% by 2026Q4."},
    "april_2026": {
        "ranges": ((.40, .50), (.70, .80), (.85, 1.0), (1.0, 1.0), (1.0, 1.0)),
        "literature_ids": ("ual-2026q1-investor-update",),
        "stated": "April 2026 guidance assumed 40-50% recovered in 2026Q2, 70-80% in 2026Q3 and 85-100% in 2026Q4."},
}


class NotComputed(RuntimeError):
    """The measured response has not been built yet."""


def monthly_curve(curve, months: int) -> np.ndarray:
    """Quarterly shares (last axis, length 5) to monthly shares by months since the change."""
    curve = np.asarray(curve, dtype=float)
    if curve.shape[-1] != QUARTERS:
        raise ValueError(f"A recovery curve has {QUARTERS} quarterly values (quarters 0..4 after the shock)")
    if isinstance(months, bool) or not isinstance(months, (int, np.integer)) or months < 1:
        raise ValueError("months must be a positive integer")
    return curve[..., np.minimum(np.arange(months) // 3, QUARTERS - 1)]


@dataclass(frozen=True, eq=False)
class Response:
    name: str
    draws: np.ndarray
    source: dict = field(default_factory=dict)
    note: str = ""

    def __post_init__(self):
        draws = np.array(self.draws, dtype=float)
        if draws.ndim != 2 or draws.shape[1] != QUARTERS or not len(draws):
            raise ValueError(f"Recovery draws must be a non-empty array with {QUARTERS} columns (quarters 0..4 after the shock)")
        if not np.isfinite(draws).all():
            raise ValueError("Recovery draws contain non-finite values")
        draws.setflags(write=False)
        object.__setattr__(self, "draws", draws)

    @property
    def fingerprint(self) -> str:
        """Identity of the curve and of where it came from."""
        label = json.dumps([self.name, self.source], sort_keys=True, default=str).encode()
        return hashlib.sha256(label + b"\0" + np.ascontiguousarray(self.draws).tobytes()).hexdigest()

    def curve(self, quantile: float = .5) -> list[float]:
        return [round(float(v), 6) for v in np.quantile(self.draws, quantile, axis=0)]

    def sample(self, paths: int, seed: int, stream: int = RESPONSE_STREAM) -> np.ndarray:
        """One quarterly curve per path, shape (paths, 5)."""
        if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)) or seed < 0:
            raise ValueError("seed must be a non-negative integer")
        rng = np.random.default_rng([stream, int(seed)])
        return self.draws[rng.integers(0, len(self.draws), size=paths)]

    def monthly_shares(self, paths: int, months: int, seed: int) -> np.ndarray:
        """One monthly curve per path, shape (paths, months); column k is k months after a price change."""
        return monthly_curve(self.sample(paths, seed), months)

    def describe(self) -> dict:
        return {"name": self.name, "curve_p10": self.curve(.1), "curve_p50": self.curve(.5), "curve_p90": self.curve(.9),
                "draws": len(self.draws), "source": self.source, "note": self.note}


def from_draws(draws, name: str = "custom", source: dict | None = None, note: str = "") -> Response:
    return Response(name, draws, {"kind": "supplied draws"} if source is None else source, note)


def costs_only() -> Response:
    return Response("costs_only", np.zeros((1, QUARTERS)), {"kind": "definition"},
                    "No revenue response: the fuel cost falls through to profit.")


def flat(share: float) -> Response:
    """The same recovery share in every quarter on every path."""
    return Response("recovery_share", np.full((1, QUARTERS), float(share)), {"kind": "flat override", "share": float(share)},
                    "Every quarter's recovery share is set to this value on all paths.")


def _ranges(promises) -> list[tuple[float, float]]:
    stated = {}
    for i, item in enumerate(promises):
        if isinstance(item, dict):
            quarter = item.get("quarters_after", i)
            low = item.get("low", item.get("recovery_low"))
            high = item.get("high", item.get("recovery_high"))
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            quarter, low, high = i, item, item
        else:
            quarter, (low, high) = i, item
        if isinstance(quarter, bool) or not isinstance(quarter, int) or not 0 <= quarter < QUARTERS or quarter in stated:
            raise ValueError("Each promise needs its own quarters_after between 0 and 4")
        for value in (low, high):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value):
                raise ValueError("A promised recovery range needs finite low and high shares")
        if low > high:
            raise ValueError("A promised recovery range needs low <= high")
        stated[quarter] = (float(low), float(high))
    if 0 not in stated:
        raise ValueError("Promises must state the quarter of the shock (quarters_after 0)")
    ranges = []
    for quarter in range(QUARTERS):
        ranges.append(stated.get(quarter, ranges[-1] if ranges else None))
    return ranges


def management(promises="july_2026", draws: int = MANAGEMENT_DRAWS, seed: int = 0, source: dict | None = None) -> Response:
    """Stated recovery ranges by quarter after the shock, sampled uniformly within each range.

    `promises` is the name of a path in MANAGEMENT_PATHS, or a sequence in quarter
    order of (low, high) pairs, single shares, or dicts with low/high (or
    recovery_low/recovery_high) and an optional quarters_after.
    """
    if isinstance(promises, str):
        if promises not in MANAGEMENT_PATHS:
            raise ValueError(f"Unknown management path {promises!r}; choose from {sorted(MANAGEMENT_PATHS)}")
        named = MANAGEMENT_PATHS[promises]
        source = {"path_name": promises, "literature_ids": list(named["literature_ids"]), "stated": named["stated"],
                  "mapping": MANAGEMENT_MAPPING, **_cited(LITERATURE), **({} if source is None else source)}
        promises = named["ranges"]
    ranges = np.array(_ranges(promises))
    position = np.random.default_rng([RESPONSE_STREAM, int(seed)]).uniform(size=(draws, 1))
    values = ranges[:, 0] + position * (ranges[:, 1] - ranges[:, 0])
    return Response("management", values,
                    {"kind": "stated ranges", "ranges": [{"quarters_after": q, "low": low, "high": high} for q, (low, high) in enumerate(ranges.tolist())],
                     **({} if source is None else source)},
                    "Uniform within each stated range; one position per draw shared by all quarters; later quarters hold the last stated range.")


def _shown(path: Path) -> str:
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)


def _cited(path: Path) -> dict:
    return {"path": _shown(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} if path.is_file() else {}


def _curve_file(path: Path, name: str, builder: str, note: str) -> Response:
    shown = _shown(path)
    if not path.is_file():
        raise NotComputed(f"{shown} has not been built yet; {builder} writes `curve` and `curve_draws`")
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("fixture"):
        raise NotComputed(f"{shown} is a placeholder, not a measured response")
    if not data.get("curve") or not data.get("curve_draws"):
        raise NotComputed(f"{shown} does not carry both `curve` and `curve_draws` yet")
    curve = data["curve"]
    if not isinstance(curve, list) or not all(isinstance(row, dict) for row in curve):
        raise ValueError(f"{shown}: `curve` must be a list of objects")
    if [row.get("quarters_after") for row in curve] != list(range(QUARTERS)):
        raise ValueError(f"{shown}: `curve` must list quarters_after 0..4 in order")
    for row in curve:
        if not all(isinstance(row.get(k), (int, float)) and np.isfinite(row[k]) for k in ("p10", "p50", "p90")):
            raise ValueError(f"{shown}: `curve` needs finite p10, p50 and p90 in every quarter")
    return Response(name, data["curve_draws"],
                    {"kind": "measured", "path": shown, "sha256": hashlib.sha256(raw).hexdigest(),
                     "as_of": data.get("as_of"), "published_curve": curve}, note)


def measured(path: Path | None = None) -> Response:
    """The fitted recovery curve and its joint draws from the fare clock output."""
    return _curve_file(FARE_CLOCK if path is None else Path(path), "measured", "the fare clock",
                       "Fitted by the fare clock; one joint draw per simulated path.")


def capacity(path: Path | None = None) -> Response:
    """The fitted capacity response: percentage points of capacity growth per 10% fuel price increase."""
    return _curve_file(CAPACITY_RESPONSE if path is None else Path(path), "capacity", "the capacity response builder",
                       "Change in capacity growth, percentage points per 10% fuel price increase, by quarter after the shock.")
