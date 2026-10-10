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
Quarters after the last stated one hold its value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
FARE_CLOCK = ROOT / "app" / "static" / "data" / "fare_clock.json"
QUARTERS = 5
RESPONSE_STREAM = 0x52455350  # "RESP"
MANAGEMENT_DRAWS = 2000


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

    def sample(self, paths: int, seed: int) -> np.ndarray:
        """One quarterly curve per path, shape (paths, 5)."""
        if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)) or seed < 0:
            raise ValueError("seed must be a non-negative integer")
        rng = np.random.default_rng([RESPONSE_STREAM, int(seed)])
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


def management(promises, draws: int = MANAGEMENT_DRAWS, seed: int = 0, source: dict | None = None) -> Response:
    """Stated recovery ranges by quarter after the shock, sampled uniformly within each range.

    `promises` is a sequence in quarter order of (low, high) pairs, single shares, or
    dicts with low/high (or recovery_low/recovery_high) and an optional quarters_after.
    """
    ranges = np.array(_ranges(promises))
    position = np.random.default_rng([RESPONSE_STREAM, int(seed)]).uniform(size=(draws, 1))
    values = ranges[:, 0] + position * (ranges[:, 1] - ranges[:, 0])
    return Response("management", values,
                    {"kind": "stated ranges", "ranges": [{"quarters_after": q, "low": low, "high": high} for q, (low, high) in enumerate(ranges.tolist())],
                     **({} if source is None else source)},
                    "Uniform within each stated range; one position per draw shared by all quarters; later quarters hold the last stated range.")


def measured(path: Path | None = None) -> Response:
    """The fitted recovery curve and its joint draws from the fare clock output."""
    path = FARE_CLOCK if path is None else Path(path)
    shown = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
    if not path.is_file():
        raise NotComputed(f"{shown} has not been built yet; the fare clock writes `curve` and `curve_draws`")
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
    return Response("measured", data["curve_draws"],
                    {"kind": "measured", "path": shown, "sha256": hashlib.sha256(raw).hexdigest(),
                     "as_of": data.get("as_of"), "published_curve": curve},
                    "Fitted by the fare clock; one joint draw per simulated path.")
