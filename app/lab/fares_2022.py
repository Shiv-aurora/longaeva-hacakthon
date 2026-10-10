"""A daily matched fare index from 2022 Expedia search quotes, set next to daily jet fuel.

One episode, described; not a model. The source is the public Kaggle dataset
`dilwong/flightprices` (one-way Expedia quotes collected by repeated searches,
2022-04-16 to 2022-10-05). It is third-party data: it is read only as CSV text
through duckdb, never imported or executed.

What a row is. One itinerary offered on one search date for one flight date,
with the fare shown for it. These are prices quoted to a shopper, not tickets
sold, and there is no quantity.

Index design (every choice is also written into the output file)
----------------------------------------------------------------
- Quotes kept: nonstop, one carrier code, cabin code `coach`, positive fare,
  7 to 48 days before departure.
- Elementary price: the cheapest quote of a fare class for one route (directional
  airport pair) x carrier x flight date on one search date. A minimum does not
  move when more or fewer itineraries are listed at higher prices. The primary
  fare class is economy that is not basic economy, at total fare.
- Cell: route x carrier x days-to-departure bucket. Every bucket is a whole
  number of weeks wide, so each cell holds every departure weekday equally often
  on every search day. The cell's log price on a search day is the mean log of
  its elementary prices; the cell counts that day only if at least
  `min_lead_share` of the bucket's departure days have a quote.
- Kept cells: the scraper's horizon varies by day, so a bucket is "scraped" on a
  search day when any of its cells counts. A cell is kept when it counts on at
  least `min_coverage` of the days its bucket was scraped.
- Index: each kept cell carries a log price (observed, or while missing its last
  price moved with the index). A day's change is the equal-weighted mean of
  (observed - carried) over the cells observed that day; the index is 100 x exp of
  the running sum (a chained matched-cell, Jevons-type index). Only a cell's own
  earlier price is ever compared with its later price, so a change in which
  routes, carriers or booking windows were scraped cannot move the index.

Statistics come from app.lab.stats; this module arranges data and timing.
"""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import zipfile

import numpy as np

from app.lab import stats
from app.lab.market_data import FUEL_CSV, load_fuel_daily

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "lab_raw" / "fares2022"
ARCHIVE = RAW_DIR / "archive.zip"
MEMBER = "itineraries.csv"
SLIM = RAW_DIR / "itineraries_slim.parquet"
FARES_CSV = ROOT / "data" / "lab" / "fares_daily_2022.csv"
TSA_CSV = ROOT / "data" / "lab" / "tsa_daily.csv"
MANIFEST = ROOT / "data" / "lab" / "manifests" / "fares_2022.json"
OUT = ROOT / "app" / "static" / "data" / "daily_fares_2022.json"

# The 27 columns the dataset's own description lists; the header of the file is checked against this.
EXPECTED_COLUMNS = (
    "legId", "searchDate", "flightDate", "startingAirport", "destinationAirport", "fareBasisCode", "travelDuration",
    "elapsedDays", "isBasicEconomy", "isRefundable", "isNonStop", "baseFare", "totalFare", "seatsRemaining",
    "totalTravelDistance", "segmentsDepartureTimeEpochSeconds", "segmentsDepartureTimeRaw",
    "segmentsArrivalTimeEpochSeconds", "segmentsArrivalTimeRaw", "segmentsArrivalAirportCode",
    "segmentsDepartureAirportCode", "segmentsAirlineName", "segmentsAirlineCode", "segmentsEquipmentDescription",
    "segmentsDurationInSeconds", "segmentsDistance", "segmentsCabinCode")
TYPED = (("searchDate", "DATE"), ("flightDate", "DATE"), ("startingAirport", "VARCHAR"),
         ("destinationAirport", "VARCHAR"), ("isBasicEconomy", "BOOLEAN"), ("isRefundable", "BOOLEAN"),
         ("isNonStop", "BOOLEAN"), ("baseFare", "DOUBLE"), ("totalFare", "DOUBLE"), ("seatsRemaining", "INTEGER"),
         ("totalTravelDistance", "DOUBLE"), ("segmentsAirlineCode", "VARCHAR"), ("segmentsCabinCode", "VARCHAR"))
CARRIERS = (("UA", "United"), ("DL", "Delta"), ("AA", "American"))
ALL = "ALL"
ALL_BUCKETS = "all"


def _lit(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def file_sha256(path: Path, chunk: int = 1 << 22) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def check_header(header) -> list[str]:
    """Return the header as a list, or refuse a file that lacks a column this module reads."""
    header = [str(name) for name in header]
    missing = [name for name, _ in TYPED if name not in header]
    if missing or len(set(header)) != len(header):
        raise ValueError(f"Quote file header is not usable: missing {missing}, columns {header}")
    return header


def read_header(path: Path, member: str | None = None) -> list[str]:
    """First line of a CSV file, or of one member of a zip archive, split as CSV."""
    if member is None:
        with Path(path).open("rb") as stream:
            line = stream.readline(1 << 16)
    else:
        with zipfile.ZipFile(path) as archive, archive.open(member) as stream:
            line = stream.readline(1 << 16)
    return check_header(next(csv.reader([line.decode("utf-8-sig").rstrip("\r\n")])))


def quotes_sql(source, header=None) -> str:
    """SQL for the typed quote relation over a slim Parquet file or a CSV in the dataset's schema.

    A CSV is read with every column as text and cast with try_cast, so a value that
    does not parse becomes NULL (and is counted by `profile`) instead of being guessed.
    """
    if str(source).endswith(".parquet"):
        return f"SELECT * FROM read_parquet({_lit(source)})"
    header = check_header(header if header is not None else read_header(Path(source)))
    columns = ", ".join(f"{_lit(name)}: 'VARCHAR'" for name in header)
    typed = ", ".join(f'"{name}"' if kind == "VARCHAR" else f'try_cast("{name}" AS {kind}) AS "{name}"'
                      for name, kind in TYPED)
    return (f"SELECT {typed} FROM read_csv({_lit(source)}, header=true, auto_detect=false, delim=',', quote='\"', "
            f"escape='\"', columns={{{columns}}}, store_rejects=true, parallel=false)")


def extract_slim(con, csv_source, header, target: Path) -> dict:
    """One pass over the full CSV (a path or /dev/stdin): keep the 13 columns used, every row, as Parquet."""
    target = Path(target)
    partial = target.with_suffix(".partial.parquet")
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"COPY ({quotes_sql(csv_source, header)}) TO {_lit(partial)} (FORMAT parquet, COMPRESSION zstd)")
    rejected = con.execute("SELECT count(*) FROM reject_errors").fetchone()[0]
    partial.replace(target)
    rows = con.execute(f"SELECT count(*) FROM read_parquet({_lit(target)})").fetchone()[0]
    return {"rows_written": int(rows), "rows_rejected_by_csv_reader": int(rejected), "columns_kept": [n for n, _ in TYPED]}


# ---------------------------------------------------------------- what the file holds

def _pairs(rows) -> list[dict]:
    total = sum(r[1] for r in rows) or 1
    return [{"key": r[0], "rows": int(r[1]), "share": round(r[1] / total, 6)} for r in rows]


def profile(con, quotes: str) -> dict:
    """Row counts, date ranges, airports and carrier shares of the whole quote file, from the data itself."""
    con.execute(f"CREATE OR REPLACE TEMP VIEW quotes_all AS {quotes}")
    (rows, bad_dates, bad_fares, bad_flags, first, last, n_days, flight_first, flight_last, lead_min, lead_max,
     nonstop, basic, no_distance) = con.execute("""
        SELECT count(*), count(*) FILTER (WHERE searchDate IS NULL OR flightDate IS NULL),
               count(*) FILTER (WHERE baseFare IS NULL OR totalFare IS NULL),
               count(*) FILTER (WHERE isNonStop IS NULL OR isBasicEconomy IS NULL),
               min(searchDate), max(searchDate), count(DISTINCT searchDate), min(flightDate), max(flightDate),
               min(flightDate - searchDate), max(flightDate - searchDate),
               count(*) FILTER (WHERE isNonStop), count(*) FILTER (WHERE isBasicEconomy),
               count(*) FILTER (WHERE totalTravelDistance IS NULL) FROM quotes_all""").fetchone()
    if not rows or first is None:
        raise ValueError("Quote file has no usable rows")
    per_day = con.execute("SELECT searchDate, count(*) FROM quotes_all WHERE searchDate IS NOT NULL "
                          "GROUP BY 1 ORDER BY 1").fetchall()
    seen = {d for d, _ in per_day}
    counts = np.array([n for _, n in per_day], dtype=float)
    median = float(np.median(counts))
    calendar = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    origins = [r[0] for r in con.execute("SELECT DISTINCT startingAirport FROM quotes_all ORDER BY 1").fetchall()]
    destinations = [r[0] for r in con.execute("SELECT DISTINCT destinationAirport FROM quotes_all ORDER BY 1").fetchall()]
    routes = con.execute("SELECT count(*) FROM (SELECT DISTINCT startingAirport, destinationAirport FROM quotes_all)").fetchone()[0]
    by_carrier = con.execute("""
        WITH g AS (SELECT segmentsAirlineCode AS s, count(*) AS n FROM quotes_all GROUP BY 1)
        SELECT CASE WHEN len(list_distinct(string_split(s, '||'))) = 1 THEN string_split(s, '||')[1]
                    ELSE 'more than one carrier' END AS carrier, sum(n)::BIGINT AS n
        FROM g GROUP BY 1 ORDER BY n DESC, carrier""").fetchall()
    nonstop_coach = con.execute("""
        SELECT segmentsAirlineCode, count(*) AS n, count(*) FILTER (WHERE isBasicEconomy) AS basic
        FROM quotes_all WHERE isNonStop AND segmentsCabinCode = 'coach' AND position('||' IN segmentsAirlineCode) = 0
        GROUP BY 1 ORDER BY n DESC, 1""").fetchall()
    cabins = con.execute("SELECT segmentsCabinCode, count(*) AS n FROM quotes_all WHERE isNonStop "
                         "GROUP BY 1 ORDER BY n DESC, 1").fetchall()
    base_fares = con.execute("""
        SELECT segmentsAirlineCode, count(*) AS n, min(baseFare), count(*) FILTER (WHERE baseFare < 15), min(totalFare)
        FROM quotes_all WHERE isNonStop AND segmentsCabinCode = 'coach' AND position('||' IN segmentsAirlineCode) = 0
          AND baseFare > 0 AND totalFare > 0
        GROUP BY 1 ORDER BY n DESC, 1""").fetchall()
    basic_by_month = con.execute("""
        SELECT strftime(searchDate, '%Y-%m') AS month, segmentsAirlineCode, count(*) AS n,
               count(*) FILTER (WHERE isBasicEconomy) AS basic
        FROM quotes_all WHERE isNonStop AND segmentsCabinCode = 'coach' AND segmentsAirlineCode IN ('UA', 'DL', 'AA')
        GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
    return {
        "rows": int(rows), "rows_unparsed_dates": int(bad_dates), "rows_unparsed_fares": int(bad_fares),
        "rows_unparsed_flags": int(bad_flags), "rows_without_distance": int(no_distance),
        "search_dates": {"first": first.isoformat(), "last": last.isoformat(), "days_with_quotes": int(n_days),
                         "calendar_days": len(calendar),
                         "missing_days": [d.isoformat() for d in calendar if d not in seen],
                         "rows_per_day": {"min": int(counts.min()), "median": int(median), "max": int(counts.max())},
                         "thin_days": [{"date": d.isoformat(), "rows": int(n)} for d, n in per_day if n < 0.5 * median]},
        "flight_dates": {"first": flight_first.isoformat(), "last": flight_last.isoformat()},
        "days_to_departure": {"min": int(lead_min), "max": int(lead_max)},
        "airports": {"origins": origins, "destinations": destinations, "directional_routes": int(routes)},
        "nonstop_rows": int(nonstop), "nonstop_share": round(nonstop / rows, 6),
        "basic_economy_rows": int(basic), "basic_economy_share": round(basic / rows, 6),
        "rows_by_carrier": _pairs(by_carrier),
        "rows_by_carrier_note": "A row counts for a carrier when every segment carries that carrier's code.",
        "nonstop_economy_rows_by_carrier": [{"key": c, "rows": int(n), "share": round(n / max(1, sum(r[1] for r in nonstop_coach)), 6),
                                             "basic_economy_share": round(b / n, 6)} for c, n, b in nonstop_coach],
        "nonstop_rows_by_cabin_code": _pairs(cabins),
        "nonstop_economy_base_fare_by_carrier": [
            {"carrier": c, "rows": int(n), "min_base_fare": float(lo), "share_base_fare_under_15": round(k / n, 6),
             "min_total_fare": float(lo_total)} for c, n, lo, k, lo_total in base_fares],
        "basic_economy_share_of_nonstop_economy_rows_by_month": [
            {"month": m, "carrier": c, "rows": int(n), "basic_economy_share": round(b / n, 4)} for m, c, n, b in basic_by_month],
    }


# ---------------------------------------------------------------- matched index

@dataclass(frozen=True)
class IndexSpec:
    buckets: tuple = ((7, 13), (14, 20), (21, 34), (35, 48))  # days to departure, inclusive; widths 7, 7, 14, 14
    min_lead_share: float = 0.85   # share of a bucket's departure days that must be quoted for the cell to count that day
    min_coverage: float = 0.95     # share of the search days its bucket was scraped on which a kept cell must count
    weights: str = "equal"         # one fixed weight per kept cell
    price: str = "minimum"         # cheapest quote per route x carrier x flight date x search date

    def labels(self) -> list[str]:
        return [f"{a}-{b}" for a, b in self.buckets]

    def check(self) -> "IndexSpec":
        if not self.buckets or any(a > b for a, b in self.buckets) or any(
                b1 >= a2 for (_, b1), (a2, _) in zip(self.buckets, self.buckets[1:])):
            raise ValueError("Lead buckets must be ascending, inclusive and non-overlapping")
        if not (0 < self.min_lead_share <= 1 and 0 < self.min_coverage <= 1) or self.weights != "equal":
            raise ValueError("Shares must lie in (0, 1] and weights must be 'equal'")
        return self


# variant -> (price column, quote-count column, what it is)
VARIANTS = {
    "main_economy": ("main", "n_main",
                     "cheapest economy quote that is not basic economy, total fare including taxes and fees"),
    "any_economy": ("any_fare", "n_any",
                    "cheapest economy quote of either kind (basic or not), total fare including taxes and fees"),
    "basic_economy": ("basic", "n_basic", "cheapest basic-economy quote, total fare including taxes and fees"),
    "main_economy_base_fare": ("main_base", "n_main",
                               "cheapest economy quote that is not basic economy, base fare before taxes and fees"),
}
PRIMARY = "main_economy"

def build_lead_table(con, quotes: str, spec: IndexSpec) -> int:
    """Elementary prices: cheapest quote per search date x route x carrier x days to departure, by fare class."""
    lo, hi = spec.check().buckets[0][0], spec.buckets[-1][1]
    con.execute(f"""
        CREATE OR REPLACE TABLE lead_min AS
        SELECT searchDate AS d, startingAirport || '-' || destinationAirport AS route, segmentsAirlineCode AS carrier,
               CAST(flightDate - searchDate AS INTEGER) AS lead,
               min(totalFare) FILTER (WHERE NOT isBasicEconomy) AS main,
               min(totalFare) FILTER (WHERE isBasicEconomy) AS basic,
               min(totalFare) AS any_fare,
               min(baseFare) FILTER (WHERE NOT isBasicEconomy) AS main_base,
               count(*) FILTER (WHERE NOT isBasicEconomy) AS n_main,
               count(*) FILTER (WHERE isBasicEconomy) AS n_basic,
               count(*) AS n_any
        FROM ({quotes})
        WHERE isNonStop AND segmentsCabinCode = 'coach' AND position('||' IN segmentsAirlineCode) = 0
          AND isBasicEconomy IS NOT NULL AND baseFare > 0 AND totalFare > 0
          AND flightDate - searchDate BETWEEN {int(lo)} AND {int(hi)}
        GROUP BY ALL""")
    return int(con.execute("SELECT count(*) FROM lead_min").fetchone()[0])


@dataclass(frozen=True, eq=False)
class Panel:
    days: list            # every search date that has an elementary price, ascending
    cells: list           # (route, carrier, bucket label)
    x: np.ndarray         # [day, cell] mean log elementary price; NaN where the cell does not count that day
    quotes: np.ndarray    # [day, cell] quote rows behind the cell that day (0 where it does not count)
    scraped: dict         # bucket label -> [day] True where the bucket was scraped (some cell counts, any fare class)


def _cell_rows(con, spec: IndexSpec, price: str, count: str) -> list[tuple]:
    """(search date, route, carrier, bucket number, mean log price, quote rows) for every cell-day that counts.

    The mean is taken in days-to-departure order so that the result does not depend on thread scheduling.
    """
    case = " ".join(f"WHEN lead BETWEEN {int(a)} AND {int(b)} THEN {i}" for i, (a, b) in enumerate(spec.buckets))
    need = [math.ceil(spec.min_lead_share * (b - a + 1) - 1e-9) for a, b in spec.buckets]
    rows = con.execute(f"""
        SELECT d, route, carrier, b, sum(ln(p) ORDER BY lead) / count(*) AS x, count(*) AS leads, sum(n)::BIGINT AS n
        FROM (SELECT d, route, carrier, lead, CASE {case} END AS b, {price} AS p, {count} AS n
              FROM lead_min WHERE {price} IS NOT NULL)
        WHERE b IS NOT NULL GROUP BY d, route, carrier, b ORDER BY route, carrier, b, d""").fetchall()
    return [(d, route, carrier, b, x, n) for d, route, carrier, b, x, leads, n in rows if leads >= need[b]]


def cell_panel(con, spec: IndexSpec, variant: str = PRIMARY) -> Panel:
    price, count, _ = VARIANTS[variant]
    labels = spec.check().labels()
    days = [r[0] for r in con.execute("SELECT DISTINCT d FROM lead_min ORDER BY 1").fetchall()]
    day_at = {d: i for i, d in enumerate(days)}
    scraped = {label: np.zeros(len(days), dtype=bool) for label in labels}
    for d, _, _, b, _, _ in _cell_rows(con, spec, "any_fare", "n_any"):
        scraped[labels[b]][day_at[d]] = True
    rows = _cell_rows(con, spec, price, count)
    cells = sorted({(r[1], r[2], r[3]) for r in rows})
    cell_at = {c: i for i, c in enumerate(cells)}
    x = np.full((len(days), len(cells)), np.nan)
    quotes = np.zeros((len(days), len(cells)), dtype=np.int64)
    for d, route, carrier, b, value, n in rows:
        i, j = day_at[d], cell_at[(route, carrier, b)]
        x[i, j], quotes[i, j] = value, n
    return Panel(days=days, cells=[(r, c, labels[b]) for r, c, b in cells], x=x, quotes=quotes, scraped=scraped)


def kept_cells(panel: Panel, spec: IndexSpec) -> np.ndarray:
    """Cells that count on at least min_coverage of the search days on which their lead bucket was scraped.

    The scraper's horizon varies from day to day, so a day on which no cell of a
    bucket counts (in any fare class) is a day that bucket was not scraped, not a
    day the cell vanished.
    """
    present = np.isfinite(panel.x)
    labels = np.array([c[2] for c in panel.cells])
    keep = np.zeros(len(panel.cells), dtype=bool)
    for label, scraped in panel.scraped.items():
        columns = labels == label
        if scraped.any() and columns.any():
            keep[columns] = present[scraped][:, columns].mean(axis=0) >= spec.min_coverage - 1e-12
    return keep


def chained_index(x: np.ndarray, weights: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Chained matched-cell log index over the rows of x (days) and its columns (cells).

    Each cell carries a current log price: the one observed that day, or, while the
    cell is missing, its last price moved by the index (so a missing cell neither
    moves the index nor leaves a gap when it returns). A day's index change is the
    fixed-weight mean, over the cells observed that day that already carry a price,
    of (observed log price - carried log price). A cell seen for the first time
    starts carrying its price and contributes from the next day. When every cell is
    observed on a day, the level equals the plain weighted mean of each cell's log
    change since it entered, whatever was missing in between.

    Returns (log level, cells used, mask of the cells used); the first day with any
    cell is 0. A day on which no observed cell already carries a price has no value.
    """
    x = np.asarray(x, dtype=float)
    n_days, n_cells = x.shape
    weights = np.ones(n_cells) if weights is None else np.asarray(weights, dtype=float)
    if weights.shape != (n_cells,) or not (np.isfinite(weights).all() and (weights > 0).all()):
        raise ValueError("weights must be positive, finite and one per cell")
    present = np.isfinite(x)
    level, used = np.full(n_days, np.nan), np.zeros((n_days, n_cells), dtype=bool)
    carried, current = np.full(n_cells, np.nan), None
    for i in range(n_days):
        known = np.isfinite(carried)
        if current is None:
            if present[i].any():
                level[i], current, used[i] = 0.0, 0.0, present[i]
        else:
            match = present[i] & known
            if match.any():
                step = float(weights[match] @ (x[i, match] - carried[match])) / float(weights[match].sum())
                current += step
                level[i], used[i] = current, match
                carried[known & ~present[i]] += step
        carried[present[i]] = x[i, present[i]]
    return level, used.sum(axis=1), used


def index_rows(panel: Panel, spec: IndexSpec, variant: str) -> tuple[list[tuple], dict]:
    """Tidy rows (date, carrier, lead_bucket, index, n_cells, n_quotes, variant) and the cell counts behind them."""
    keep = kept_cells(panel, spec)
    carriers = np.array([c[1] for c in panel.cells])
    buckets = np.array([c[2] for c in panel.cells])
    rows, counts = [], {}
    for carrier in [code for code, _ in CARRIERS] + [ALL]:
        of_carrier = np.ones(len(panel.cells), dtype=bool) if carrier == ALL else carriers == carrier
        for bucket in spec.labels() + [ALL_BUCKETS]:
            of_bucket = np.ones(len(panel.cells), dtype=bool) if bucket == ALL_BUCKETS else buckets == bucket
            columns = keep & of_carrier & of_bucket
            seen = of_carrier & of_bucket
            counts[f"{carrier}|{bucket}"] = {
                "cells_seen": int(seen.sum()), "cells_kept": int(columns.sum()),
                "routes_kept": len({panel.cells[j][0] for j in np.flatnonzero(columns)}),
                "carriers_kept": sorted({panel.cells[j][1] for j in np.flatnonzero(columns)})}
            if not columns.any():
                continue
            level, used, mask = chained_index(panel.x[:, columns])
            n_quotes = (panel.quotes[:, columns] * mask).sum(axis=1)
            rows += [(day.isoformat(), carrier, bucket, f"{100.0 * math.exp(level[i]):.4f}", int(used[i]), int(n_quotes[i]), variant)
                     for i, day in enumerate(panel.days) if math.isfinite(level[i])]
    return rows, counts


FARES_HEADER = ("date", "carrier", "lead_bucket", "index", "n_cells", "n_quotes", "variant")


def build_index(con, quotes: str, spec: IndexSpec = IndexSpec(), variants=tuple(VARIANTS)) -> dict:
    """Quotes -> tidy index rows for every carrier, lead bucket and variant. Deterministic for a given input."""
    elementary = build_lead_table(con, quotes, spec)
    rows, counts, days = [], {}, []
    for variant in variants:
        panel = cell_panel(con, spec, variant)
        variant_rows, counts[variant] = index_rows(panel, spec, variant)
        rows += variant_rows
        days = panel.days
    rows.sort(key=lambda r: (r[6] != PRIMARY, r[6], r[1], r[2], r[0]))
    return {"rows": rows, "cell_counts": counts, "elementary_prices": elementary,
            "search_days": [d.isoformat() for d in days], "spec": asdict(spec)}


def write_fares_csv(path: Path, rows: list[tuple]) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(FARES_HEADER)
        writer.writerows(rows)
    return {"rows": len(rows), "sha256": file_sha256(path), "bytes": path.stat().st_size}


def load_fares_daily(path: Path = FARES_CSV) -> dict:
    """{(variant, carrier, lead_bucket): {"dates", "index", "n_cells", "n_quotes"}} from the tidy file."""
    with Path(path).open(newline="") as stream:
        rows = list(csv.reader(stream))
    if not rows or tuple(rows[0]) != FARES_HEADER:
        raise ValueError("Unexpected fares_daily_2022.csv header")
    out: dict = {}
    for day, carrier, bucket, index, n_cells, n_quotes, variant in rows[1:]:
        series = out.setdefault((variant, carrier, bucket), {"dates": [], "index": [], "n_cells": [], "n_quotes": []})
        if series["dates"] and date.fromisoformat(day) <= series["dates"][-1]:
            raise ValueError(f"fares_daily_2022.csv dates repeat or are out of order for {variant} {carrier} {bucket}")
        series["dates"].append(date.fromisoformat(day))
        series["index"].append(float(index))
        series["n_cells"].append(int(n_cells))
        series["n_quotes"].append(int(n_quotes))
    for series in out.values():
        series["index"] = np.array(series["index"], dtype=float)
        if not (np.isfinite(series["index"]).all() and (series["index"] > 0).all()):
            raise ValueError("fares_daily_2022.csv holds a non-positive or non-finite index value")
    return out


# ---------------------------------------------------------------- fuel, weeks, leads and lags

DEFAULT_SEED = 20221005
LAGS = tuple(range(-8, 13))          # weeks; positive = the fuel change came that many weeks before the fare change
MAX_LAG = 4                          # weeks of lagged fuel in the primary regression
MAX_LAGS_TRIED = (2, 4, 6)
WEEKLY_METHODS = ("mean", "last")
MIN_WEEK_OBS = {"fare": 5, "fuel": 3, "tsa": 7}
LEVEL = 0.95
BLOCK_LEN = 3
SERIES = (("ua", "UA"), ("dl", "DL"), ("aa", "AA"), ("all", ALL))
NAMES = {"ua": "United", "dl": "Delta", "aa": "American", "all": "all carriers"}


def load_tsa_daily(path: Path = TSA_CSV) -> tuple[list[date], np.ndarray]:
    """TSA checkpoint travelers per day from data/lab/tsa_daily.csv (`date,travelers`)."""
    with Path(path).open(newline="") as stream:
        rows = list(csv.reader(stream))
    if not rows or rows[0] != ["date", "travelers"] or len(rows) < 2:
        raise ValueError("tsa_daily.csv needs the header date,travelers and at least one day")
    dates = [date.fromisoformat(r[0]) for r in rows[1:]]
    values = np.array([float(r[1]) for r in rows[1:]], dtype=float)
    if any(b <= a for a, b in zip(dates, dates[1:])) or not (np.isfinite(values).all() and (values > 0).all()):
        raise ValueError("tsa_daily.csv has repeated or unordered dates, or a non-positive count")
    return dates, values


def describe_path(dates, values, start: date, end: date, places: int = 3) -> dict:
    """First, highest, lowest and last observation dated start..end, the means of the first and last seven
    calendar days of that window, and the percentage changes between them."""
    picked = [(d, float(v)) for d, v in zip(dates, values) if start <= d <= end]
    if not picked:
        raise ValueError("No observation in the window")
    first, last = picked[0], picked[-1]
    peak, low = max(picked, key=lambda p: p[1]), min(picked, key=lambda p: p[1])
    head = [v for d, v in picked if d < start + timedelta(days=7)]
    tail = [v for d, v in picked if d > end - timedelta(days=7)]
    point = lambda p: {"date": p[0].isoformat(), "value": round(p[1], places)}
    pct = lambda a, b: round(100.0 * (b / a - 1.0), 2)
    return {"start": point(first), "peak": point(peak), "low": point(low), "end": point(last), "n": len(picked),
            "first_week_mean": round(float(np.mean(head)), places), "last_week_mean": round(float(np.mean(tail)), places),
            "start_to_peak_pct": pct(first[1], peak[1]), "peak_to_low_pct": pct(peak[1], low[1]) if low[0] > peak[0] else None,
            "peak_to_end_pct": pct(peak[1], last[1]), "start_to_end_pct": pct(first[1], last[1]),
            "first_week_to_last_week_pct": pct(float(np.mean(head)), float(np.mean(tail)))}


def week_of(day: date) -> date:
    """The Monday of the day's ISO week."""
    return day - timedelta(days=day.weekday())


def weekly_levels(dates, values, how: str = "mean", min_obs: int = 1) -> dict:
    """Log level per ISO week (keyed by its Monday).

    "mean": mean of the logs of the week's observations, Monday to Sunday, when
    there are at least min_obs. "last": log of the last observation dated Monday to
    Friday, when that observation falls on a Thursday or Friday. A week that does
    not qualify is absent; nothing is carried forward.
    """
    if how not in WEEKLY_METHODS:
        raise ValueError(f"how must be one of {WEEKLY_METHODS}")
    groups: dict = {}
    for d, v in zip(dates, values):
        groups.setdefault(week_of(d), []).append((d, float(v)))
    out = {}
    for monday, obs in sorted(groups.items()):
        if how == "mean":
            if len(obs) >= min_obs:
                out[monday] = float(np.mean(np.log([v for _, v in obs])))
        else:
            weekdays = [(d, v) for d, v in obs if d.weekday() <= 4]
            if weekdays and weekdays[-1][0].weekday() >= 3:
                out[monday] = math.log(weekdays[-1][1])
    return out


def weekly_changes(levels: dict) -> dict:
    """Log change from the week before, for weeks whose previous week also has a level."""
    return {w: levels[w] - levels[w - timedelta(weeks=1)] for w in sorted(levels) if w - timedelta(weeks=1) in levels}


def _corr(a, b) -> float:
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if a.std() == 0 or b.std() == 0:
        return math.nan
    return float(np.corrcoef(a, b)[0, 1])


def lead_lag(fare_chg: dict, fuel_chg: dict, lags=LAGS, seed: int = DEFAULT_SEED, n_boot: int = 2000,
             block_len: int = BLOCK_LEN, level: float = LEVEL) -> list[dict]:
    """corr(fare change in week t, fuel change in week t - lag) for each lag, with a moving-block bootstrap interval."""
    out = []
    for lag in lags:
        weeks = [w for w in sorted(fare_chg) if w - timedelta(weeks=lag) in fuel_chg]
        a = np.array([fare_chg[w] for w in weeks], dtype=float)
        b = np.array([fuel_chg[w - timedelta(weeks=lag)] for w in weeks], dtype=float)
        row = {"lag_weeks": int(lag), "corr": None, "lo": None, "hi": None, "n": len(weeks)}
        corr = _corr(a, b) if len(weeks) >= max(8, 2 * block_len) else math.nan
        if math.isfinite(corr):
            draws = stats.block_bootstrap(_corr, (a, b), block_len, n_boot, seed)
            draws = draws[np.isfinite(draws)]
            lo, hi = stats.percentile_interval(draws, level)
            row.update({"corr": round(corr, 4), "lo": round(lo, 4), "hi": round(hi, 4)})
        out.append(row)
    return out


def reading(lo: float, hi: float) -> str:
    return "positive" if lo > 0 else "negative" if hi < 0 else "not distinguishable from zero"


def distributed_lag_fit(fare_chg: dict, fuel_chg: dict, max_lag: int = MAX_LAG, control_chg: dict | None = None,
                        level: float = LEVEL) -> dict:
    """Weekly fare change on a constant, the fuel change of the same week and of max_lag earlier weeks, and an
    optional control of the same week. Newey-West errors (stats.distributed_lag); intervals use t(df_resid)."""
    weeks = sorted(fare_chg)
    if len(weeks) < 2:
        raise ValueError("Need at least two weekly fare changes")
    grid = [weeks[0] + timedelta(weeks=i) for i in range(-max_lag, (weeks[-1] - weeks[0]).days // 7 + 1)]
    y = np.array([fare_chg.get(w, math.nan) for w in grid], dtype=float)
    x = np.array([fuel_chg.get(w, math.nan) for w in grid], dtype=float)
    if not (np.isfinite(y[max_lag:]).all() and np.isfinite(x).all()):
        raise ValueError("The regression needs a fare change for every week of the window and a fuel change for every lag")
    controls = None
    if control_chg is not None:
        controls = np.array([control_chg.get(w, math.nan) for w in grid], dtype=float)
        if not np.isfinite(controls[max_lag:]).all():
            raise ValueError("The control is missing for a week of the window")
        controls[:max_lag] = 0.0  # rows before the window are dropped by the lag matrix; the value is never used
    result = stats.distributed_lag(y, x, max_lag, controls=controls)
    fit, hac, cum = result["fit"], result["hac"], result["cumulative"]
    if fit.df_resid <= 0 or fit.rank_deficient:
        raise ValueError("Too few weeks for this many lags")
    interval = lambda est, se: tuple(float(v) for v in stats.conf_int(est, se, level, df=fit.df_resid))
    r4 = lambda v: round(float(v), 4)
    coefs = []
    for j, col in zip(result["lags"], result["lag_cols"]):
        lo, hi = interval(fit.beta[col], hac.se[col])
        coefs.append({"lag_weeks": int(j), "coef": r4(fit.beta[col]), "se": r4(hac.se[col]), "lo": r4(lo), "hi": r4(hi)})
    cumulative = []
    for j, total, se in zip(result["lags"], cum["cumulative"], cum["se"]):
        lo, hi = interval(total, se)
        cumulative.append({"through_lag_weeks": int(j), "sum": r4(total), "se": r4(se), "lo": r4(lo), "hi": r4(hi)})
    last = cumulative[-1]
    out = {"n_weeks": int(fit.n), "max_lag_weeks": int(max_lag), "parameters": int(fit.k), "df_resid": int(fit.df_resid),
           "hac_lag": int(hac.lag), "r2": r4(fit.r2), "level": level, "coefs": coefs, "cumulative": cumulative,
           "long_run": {"sum": last["sum"], "se": last["se"], "lo": last["lo"], "hi": last["hi"],
                        "p_value": r4(stats.p_value(last["sum"] / last["se"], fit.df_resid)) if last["se"] > 0 else None,
                        "reading": reading(last["lo"], last["hi"])},
           "control": None}
    if controls is not None:
        col = fit.k - 1
        lo, hi = interval(fit.beta[col], hac.se[col])
        out["control"] = {"coef": r4(fit.beta[col]), "se": r4(hac.se[col]), "lo": r4(lo), "hi": r4(hi)}
    return out


# ---------------------------------------------------------------- the page document

def _window(series: dict, start: date, end: date) -> tuple[list[date], np.ndarray]:
    keep = [i for i, d in enumerate(series["dates"]) if start <= d <= end]
    return [series["dates"][i] for i in keep], series["index"][keep]


def _fare_changes(series: dict, how: str) -> dict:
    return weekly_changes(weekly_levels(series["dates"], series["index"], how, MIN_WEEK_OBS["fare"]))


def _long_run(fare_chg: dict, fuel_chg: dict, max_lag: int, control: dict | None) -> dict:
    try:
        fit = distributed_lag_fit(fare_chg, fuel_chg, max_lag, control)
    except ValueError as exc:
        return {"not_computed": str(exc)}
    return {"n_weeks": fit["n_weeks"], "df_resid": fit["df_resid"], "r2": fit["r2"],
            **{k: fit["long_run"][k] for k in ("sum", "lo", "hi", "reading")}}


def _method_text(spec: dict, primary: str) -> str:
    buckets = ", ".join(f"{a}-{b}" for a, b in spec["buckets"])
    return " ".join([
        f"Each row of the source is one itinerary Expedia offered on one search date for one flight date; we keep nonstop "
        f"economy itineraries on a single carrier, searched {spec['buckets'][0][0]} to {spec['buckets'][-1][1]} days before "
        f"departure.",
        f"For every search date, route, carrier and flight date we take the {VARIANTS[primary][2]}, so listing more or "
        f"fewer itineraries at higher prices does not move it.",
        f"These prices are grouped into cells of route x carrier x days-to-departure bucket ({buckets} days, each a whole "
        f"number of weeks, so a cell always holds each departure weekday equally often); a cell counts on a search date "
        f"when at least {spec['min_lead_share']:.0%} of its departure days have a quote, and it is kept only if it counts "
        f"on at least {spec['min_coverage']:.0%} of the search dates on which its bucket was scraped.",
        "From one search date to the next the index moves by the equal-weighted average log price change of the kept "
        "cells quoted that day, each measured against that same cell's own earlier price, and the changes are chained "
        "from 100 on the first search date.",
        "A cell that is not quoted on a day is carried at the average movement of the others, so a change in which "
        "routes, carriers or booking windows were scraped cannot move the index, and a cell that disappears simply stops "
        "contributing.",
    ])


def _basic_shift(profile: dict, carrier: str) -> dict | None:
    """Basic-economy share of a carrier's nonstop economy quote rows in the first and last full calendar month."""
    rows = [r for r in profile.get("basic_economy_share_of_nonstop_economy_rows_by_month", []) if r["carrier"] == carrier]
    if len(rows) < 4:
        return None
    first, last = rows[1], rows[-2]   # the first and last months of the window are partial
    return {"carrier": carrier, "first_month": first["month"], "first_share": first["basic_economy_share"],
            "last_month": last["month"], "last_share": last["basic_economy_share"]}


def _price_note(profile: dict) -> str:
    odd = [r for r in profile.get("nonstop_economy_base_fare_by_carrier", []) if r["share_base_fare_under_15"] > 0.01]
    if not odd:
        return "Total fare (with taxes and fees) is the price a shopper sees; base fare is kept as a variant."
    listed = "; ".join(f"{r['carrier']}: lowest base fare ${r['min_base_fare']:.2f}, {r['share_base_fare_under_15']:.0%} of "
                       f"its nonstop economy quotes under $15" for r in odd)
    return ("Total fare (with taxes and fees) is used because base fare is not comparable across carriers. Some carriers "
            f"quote base fares near zero and carry the price in fees ({listed}), which makes log changes in base fare "
            "meaningless for them. Base fare is kept as a variant.")


def _limits(profile: dict, entry: dict, n_weeks: int, counts: dict, no_index: list[str], window: tuple[str, str]) -> list[str]:
    airports = sorted(set(profile["airports"]["origins"]) | set(profile["airports"]["destinations"]))
    ua = counts[PRIMARY][f"UA|{ALL_BUCKETS}"]
    ua_any = counts.get("any_economy", {}).get(f"UA|{ALL_BUCKETS}")
    shift = _basic_shift(profile, "UA")
    limits = [
        "These are prices quoted in Expedia searches, not tickets sold: there are no quantities, no corporate or "
        "direct-channel fares, and a quoted fare may never have been bought.",
        f"One episode: {window[0]} to {window[1]}, {n_weeks} weekly changes. It spans the summer travel peak, so the "
        "seasonal rise and fall of fares overlaps the rise and fall of fuel and this window cannot separate them. Each "
        "search date prices flights 7 to 48 days ahead, so the travel dates themselves move from spring into the "
        "summer peak and on to autumn.",
        f"Routes are limited to nonstop flights among the {len(airports)} airports in the dataset ({', '.join(airports)}); "
        "this is not United's network and has no international or connecting itineraries.",
        f"The United index rests on {ua['cells_kept']} cells on {ua['routes_kept']} routes that were quoted with a "
        "non-basic economy fare nearly every day"
        + (f"; United has kept cells on {ua_any['routes_kept']} routes when basic-economy quotes are also counted" if ua_any else "")
        + ". Routes where the cheapest fare shown was usually basic economy drop out of the primary index.",
        (f"Fare-class mix in the scrape changed during the window: basic economy was {shift['first_share']:.0%} of United's "
         f"nonstop economy quote rows in {shift['first_month']} and {shift['last_share']:.0%} in {shift['last_month']}. The "
         "primary index excludes basic-economy quotes; the any_economy variant includes them and falls further."
         if shift else "The share of quotes that are basic economy may change within the window."),
        (f"No index on {len(no_index)} calendar day(s) inside the window ({', '.join(no_index)}): the scrape has no rows or "
         "only departures less than a week out. " if no_index else "")
        + f"The scraper's horizon varies by day and {len(profile['search_dates']['thin_days'])} search day(s) have fewer than "
          "half the median number of rows; the index is chained across these gaps.",
        "The index follows the cheapest quote in each cell; fares paid, the mix of fare classes sold and seats sold can "
        "move differently. Flight dates near holidays pass through the booking-window buckets and move the index for "
        "reasons unrelated to fuel.",
        "With this few weeks the bootstrap and Newey-West intervals are approximate and tend to be too narrow, and "
        f"{len(LAGS)} leads and lags were scanned, so about one interval excluding zero is expected by chance alone.",
        f"Licence: {entry['licence']}. {entry['attribution']} {entry['terms_note']}",
    ]
    return limits


def _daily(fares: dict, variant: str, bucket: str, fuel: dict | None, days: list[date]) -> list[dict]:
    lookup = {key: dict(zip(fares[(variant, code, bucket)]["dates"], fares[(variant, code, bucket)]["index"]))
              for key, code in SERIES if (variant, code, bucket) in fares}
    rows = []
    for day in days:
        row = {"date": day.isoformat()}
        if fuel is not None:
            row["fuel"] = fuel.get(day)
        for key, _ in SERIES:
            value = lookup.get(key, {}).get(day)
            row[f"index_{key}"] = None if value is None else round(float(value), 2)
        rows.append(row)
    return rows


def _money(value: float) -> str:
    return f"${value:.2f}"


def _signed(value: float, places: int = 2) -> str:
    return f"{value:+.{places}f}"


_VERDICT = {"positive": "the interval lies above zero", "negative": "the interval lies below zero",
            "not distinguishable from zero": "the interval includes zero"}


def _regression_words(name: str, fit: dict, control: bool) -> str:
    run = fit["long_run"]
    return (f"{name} fares, {'with' if control else 'without'} the TSA traveler control: a 1% rise in fuel goes with a "
            f"{_signed(run['sum'])}% change in the fare index summed over the same week and the {fit['max_lag_weeks']} weeks "
            f"after (95% interval {_signed(run['lo'])}% to {_signed(run['hi'])}%, {fit['n_weeks']} weeks, "
            f"{fit['parameters']} parameters); {_VERDICT[run['reading']]}.")


def build_document(fares_csv: Path = FARES_CSV, fuel_csv: Path = FUEL_CSV, tsa_csv: Path | None = TSA_CSV,
                   manifest_path: Path = MANIFEST, generated_at: str = "", seed: int = DEFAULT_SEED,
                   n_boot: int = 2000, relative_to: Path = ROOT) -> dict:
    """Everything the page needs, computed from the tidy index file, daily fuel and (when present) TSA travelers."""
    def shown(path: Path) -> str:
        path = Path(path).resolve()
        return path.relative_to(relative_to).as_posix() if path.is_relative_to(relative_to) else path.as_posix()

    fares = load_fares_daily(fares_csv)
    manifest = json.loads(Path(manifest_path).read_text())
    entry, profile, spec = manifest["entries"][0], manifest["profile"], manifest["index"]["spec"]
    primary, counts = manifest["index"]["primary"], manifest["index"]["cell_counts"]
    if any((primary, code, ALL_BUCKETS) not in fares for _, code in SERIES):
        raise ValueError("fares_daily_2022.csv lacks a primary series for United, Delta, American or all carriers")
    labels = [f"{a}-{b}" for a, b in spec["buckets"]]
    base = fares[(primary, ALL, ALL_BUCKETS)]
    start, end = base["dates"][0], base["dates"][-1]
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    no_index = [d.isoformat() for d in days if d not in set(base["dates"])]

    fuel_dates, fuel_values = load_fuel_daily(Path(fuel_csv))
    fuel_by_day = {d: float(v) for d, v in zip(fuel_dates, fuel_values)}
    fuel_path = describe_path(fuel_dates, fuel_values, start, end, 3)
    fuel_chg = {how: weekly_changes(weekly_levels(fuel_dates, fuel_values, how, MIN_WEEK_OBS["fuel"])) for how in WEEKLY_METHODS}

    inputs = [{"path": shown(fares_csv), "sha256": file_sha256(fares_csv)},
              {"path": shown(fuel_csv), "sha256": file_sha256(fuel_csv)},
              {"path": shown(manifest_path), "sha256": file_sha256(manifest_path)},
              {"path": entry["path"], "sha256": entry["sha256"], "bytes": entry["bytes"],
               "note": "raw archive, untracked; fares_daily_2022.csv was built from it"}]
    tsa_chg = None
    if tsa_csv is not None and Path(tsa_csv).exists():
        tsa_dates, tsa_values = load_tsa_daily(Path(tsa_csv))
        tsa_chg = weekly_changes(weekly_levels(tsa_dates, tsa_values, "mean", MIN_WEEK_OBS["tsa"]))
        inputs.insert(2, {"path": shown(tsa_csv), "sha256": file_sha256(tsa_csv)})

    fare_chg = {key: _fare_changes(fares[(primary, code, ALL_BUCKETS)], "mean") for key, code in SERIES}
    weeks = sorted(fare_chg["all"])
    n_weeks = len(weeks)
    correlations = {key: lead_lag(fare_chg[key], fuel_chg["mean"], LAGS, seed, n_boot) for key, _ in SERIES}
    regressions = {key: {"fuel_only": distributed_lag_fit(fare_chg[key], fuel_chg["mean"], MAX_LAG),
                         "with_demand": None if tsa_chg is None else
                         distributed_lag_fit(fare_chg[key], fuel_chg["mean"], MAX_LAG, tsa_chg)} for key, _ in SERIES}

    all_variants = sorted({k[0] for k in fares}, key=lambda v: (v != primary, v))
    robustness, tried = [], []
    for variant in all_variants:
        for bucket in [ALL_BUCKETS] + labels:
            for how in WEEKLY_METHODS:
                for max_lag in MAX_LAGS_TRIED:
                    if (variant != primary and bucket != ALL_BUCKETS) or (
                            (variant != primary or bucket != ALL_BUCKETS) and (how != "mean" or max_lag != MAX_LAG)):
                        continue
                    tried.append(f"{variant} | lead {bucket} | weekly {how} | fuel lags 0-{max_lag} | "
                                 f"{'with and without' if tsa_chg is not None else 'without'} TSA control")
                    for key, code in SERIES:
                        if (variant, code, bucket) not in fares:
                            continue
                        series = fares[(variant, code, bucket)]
                        changes = _fare_changes(series, how)
                        robustness.append({
                            "variant": variant, "lead_bucket": bucket, "series": key, "weekly": how, "max_lag_weeks": max_lag,
                            "index_days": len(series["dates"]), "index_end": round(float(series["index"][-1]), 2),
                            "index_end_date": series["dates"][-1].isoformat(), "min_cells": int(min(series["n_cells"])),
                            "fuel_only": _long_run(changes, fuel_chg[how], max_lag, None),
                            "with_demand": None if tsa_chg is None else _long_run(changes, fuel_chg[how], max_lag, tsa_chg)})

    fare_paths = {key: describe_path(*_window(fares[(primary, code, ALL_BUCKETS)], start, end), start, end, 2)
                  for key, code in SERIES}
    variants = {}
    for variant in all_variants[1:]:
        cells = counts[variant][f"{ALL}|{ALL_BUCKETS}"]
        variants[variant] = {
            "description": VARIANTS[variant][2], "cells_kept": cells["cells_kept"], "carriers_kept": cells["carriers_kept"],
            "cells_kept_by_series": {key: counts[variant][f"{code}|{ALL_BUCKETS}"]["cells_kept"] for key, code in SERIES},
            "fare_path": {key: describe_path(*_window(fares[(variant, code, ALL_BUCKETS)], start, end), start, end, 2)
                          for key, code in SERIES if (variant, code, ALL_BUCKETS) in fares},
            "daily": _daily(fares, variant, ALL_BUCKETS, None, days)}

    n_quotes = int(sum(base["n_quotes"]))
    ua, everyone = regressions["ua"], regressions["all"]
    scored = [r for r in correlations["ua"] if r["corr"] is not None]
    strongest = max(scored, key=lambda r: abs(r["corr"]))
    same_week = next(r for r in correlations["ua"] if r["lag_weeks"] == 0)
    excluding = {key: sum(1 for r in rows if r["corr"] is not None and (r["lo"] > 0 or r["hi"] < 0))
                 for key, rows in correlations.items()}
    gap = (date.fromisoformat(fare_paths["ua"]["peak"]["date"]) - date.fromisoformat(fuel_path["peak"]["date"])).days
    headline_fit = ua["with_demand"] or ua["fuel_only"]
    run = headline_fit["long_run"]
    united_specs = [r[name] for r in robustness if r["variant"] == primary and r["lead_bucket"] == ALL_BUCKETS
                    and r["series"] == "ua" for name in ("fuel_only", "with_demand") if r[name] and "sum" in r[name]]
    united_zero = sum(1 for fit in united_specs if fit["reading"] == "not distinguishable from zero")
    headline = (
        f"Jet fuel peaked at {_money(fuel_path['peak']['value'])} a gallon on {fuel_path['peak']['date']}; United's matched "
        f"fare index (100 on {start.isoformat()}) peaked at {fare_paths['ua']['peak']['value']:.0f} on "
        f"{fare_paths['ua']['peak']['date']}, {abs(gap)} days {'later' if gap >= 0 else 'earlier'}, and stood at "
        f"{fare_paths['ua']['end']['value']:.0f} on {end.isoformat()}. Across {n_weeks} weekly changes the summed "
        f"{MAX_LAG}-week response of United fares to a 1% fuel move is {_signed(run['sum'])}% (95% interval "
        f"{_signed(run['lo'])}% to {_signed(run['hi'])}%); the interval includes zero in {united_zero} of the "
        f"{len(united_specs)} United specifications tried, and the window spans the summer travel peak.")

    summary = [
        f"Gulf Coast jet fuel: {_money(fuel_path['start']['value'])} on {fuel_path['start']['date']}, a high of "
        f"{_money(fuel_path['peak']['value'])} on {fuel_path['peak']['date']} ({_signed(fuel_path['start_to_peak_pct'], 0)}% "
        f"from the start), a low of {_money(fuel_path['low']['value'])} on {fuel_path['low']['date']}, and "
        f"{_money(fuel_path['end']['value'])} on {fuel_path['end']['date']} ({_signed(fuel_path['start_to_end_pct'], 0)}% from the start).",
        f"Matched nonstop economy fare index, 100 on {start.isoformat()}: " + "; ".join(
            f"{NAMES[key]} high {fare_paths[key]['peak']['value']:.0f} on {fare_paths[key]['peak']['date']}, low "
            f"{fare_paths[key]['low']['value']:.0f} on {fare_paths[key]['low']['date']}, end {fare_paths[key]['end']['value']:.0f} "
            f"(last week against first week {_signed(fare_paths[key]['first_week_to_last_week_pct'], 0)}%)"
            for key, _ in SERIES) + "."]
    shift = _basic_shift(profile, "UA")
    if "any_economy" in variants and "ua" in variants["any_economy"]["fare_path"]:
        other = variants["any_economy"]["fare_path"]["ua"]
        summary.append(
            f"Counting basic-economy quotes too (cheapest economy fare of either kind), the United index peaks at "
            f"{other['peak']['value']:.0f} on {other['peak']['date']} and ends at {other['end']['value']:.0f}"
            + (f"; basic economy went from {shift['first_share']:.0%} of United's nonstop economy quote rows in "
               f"{shift['first_month']} to {shift['last_share']:.0%} in {shift['last_month']}" if shift else "") + ".")
    summary.append(
        f"Weekly changes in the United index against weekly fuel changes at {len(scored)} leads and lags ({LAGS[0]} to "
        f"+{LAGS[-1]} weeks): same-week correlation {_signed(same_week['corr'])} (95% interval {_signed(same_week['lo'])} to "
        f"{_signed(same_week['hi'])}); largest in size {_signed(strongest['corr'])} at {strongest['lag_weeks']:+d} weeks "
        f"(interval {_signed(strongest['lo'])} to {_signed(strongest['hi'])}; a positive lag means fuel moved first). "
        f"{excluding['ua']} of {len(scored)} intervals exclude zero; about one would by chance.")
    summary.append(_regression_words("United", ua["fuel_only"], False))
    if ua["with_demand"] is not None:
        summary.append(_regression_words("United", ua["with_demand"], True))
    summary.append(_regression_words("All-carrier", everyone["with_demand"] or everyone["fuel_only"], everyone["with_demand"] is not None))
    name = "with_demand" if tsa_chg is not None else "fuel_only"
    others = [r for r in robustness if r["variant"] == primary and r["lead_bucket"] == ALL_BUCKETS and r["series"] == "ua"
              and r["weekly"] == "mean" and r["max_lag_weeks"] != MAX_LAG and "sum" in r[name]]
    if others:
        summary.append("The United estimate depends on how many weeks of lagged fuel are allowed: " + "; ".join(
            f"lags 0-{r['max_lag_weeks']}: {_signed(r[name]['sum'])}% ({_signed(r[name]['lo'])}% to {_signed(r[name]['hi'])}%, "
            f"{r[name]['df_resid']} residual degrees of freedom)" for r in others)
            + f". Across lag lengths {', '.join(str(m) for m in MAX_LAGS_TRIED)}, weekly averages or week-end values, with and "
              f"without the traveler control, the interval includes zero in {united_zero} of {len(united_specs)} United "
              "specifications.")
    sums = [r[name]["sum"] for r in robustness if r["series"] == "ua" and "sum" in r[name]] + [run["sum"]]
    if max(sums) > 1.0:
        summary.append("A summed response above 1% is larger than passing the extra fuel cost through in full would "
                       "produce, because fuel is only part of an airline's costs; estimates that large indicate fares and "
                       "fuel moving together for other reasons, such as the summer peak.")
    summary.append(f"This is one episode of {n_weeks} weekly changes, built from {n_quotes:,} search quotes on {len(base['dates'])} "
                   "search days. It describes what happened; it does not by itself measure how fares respond to fuel.")

    return {
        "generated_at": generated_at, "as_of": end.isoformat(), "inputs": inputs,
        "headline": headline, "plain_summary": " ".join(summary),
        "window": {"start": start.isoformat(), "end": end.isoformat(), "calendar_days": len(days),
                   "days_without_index": no_index},
        "n_days": len(base["dates"]), "n_quotes": n_quotes,
        "n_quotes_note": "Quote rows behind the cells of the all-carrier index, summed over search days. The source file "
                         f"holds {profile['rows']:,} rows in total, {profile['nonstop_share']:.1%} of them nonstop.",
        "daily": _daily(fares, primary, ALL_BUCKETS, fuel_by_day, days),
        "daily_note": f"index_* = 100 on {start.isoformat()}. fuel is USD per gallon and null on days without a print; an "
                      "index is null on days the scrape has no usable quotes. Nothing is filled forward.",
        "fuel_path": {**fuel_path, "unit": "USD per gallon", "series": "EIA Gulf Coast kerosene-type jet fuel, daily spot"},
        "fare_path": fare_paths,
        "by_lead_bucket": {label: _daily(fares, primary, label, None, days) for label in labels},
        "variants": variants,
        "lead_lag": {
            "definition": "Pearson correlation of the weekly log change in the fare index with the weekly log change in jet "
                          "fuel lag_weeks earlier. Positive lag_weeks: fuel moved first. Negative: fares moved first.",
            "weekly": f"Weekly level = mean of daily logs over the ISO week (Monday to Sunday); a week needs at least "
                      f"{MIN_WEEK_OBS['fare']} index days or {MIN_WEEK_OBS['fuel']} fuel prints. Fuel changes before and after "
                      "the fare window come from the full fuel history, so every lag uses the same fare weeks.",
            "interval": f"{LEVEL:.0%} percentile interval from a moving-block bootstrap of week pairs (blocks of {BLOCK_LEN} "
                        f"weeks, {n_boot} draws, seed {seed}). Approximate with this few weeks.",
            "first_week": weeks[0].isoformat(), "last_week": weeks[-1].isoformat(), "n_weeks": n_weeks,
            "lags_scanned": len(scored), "intervals_excluding_zero": excluding,
            "correlations": correlations},
        "distributed_lag": {
            "definition": f"Weekly log change in the fare index on a constant and the weekly log change in jet fuel of the same "
                          f"week and each of the {MAX_LAG} weeks before. Coefficients are elasticities (percent per percent); "
                          "`cumulative` sums them through each lag and `long_run` is the full sum. Newey-West standard "
                          "errors; intervals use Student t with the residual degrees of freedom.",
            "demand_control": None if tsa_chg is None else
                "Weekly log change in mean daily TSA checkpoint travelers, same week (data/lab/tsa_daily.csv).",
            "by_series": regressions},
        "robustness": {"note": "The summed fuel coefficient for every specification computed, none selected on its result. "
                               "`not_computed` appears where a series lacks a full run of weeks.",
                       "united_specifications": len(united_specs), "united_intervals_including_zero": united_zero,
                       "rows": robustness},
        "variants_tried": tried,
        "method": _method_text(spec, primary),
        "choices": {"primary_variant": primary, "primary_variant_description": VARIANTS[primary][2],
                    "price_note": _price_note(profile), **spec,
                    "carriers": {code: name for code, name in CARRIERS},
                    "all_carriers": counts[primary][f"{ALL}|{ALL_BUCKETS}"]["carriers_kept"],
                    "cells": {key: counts[primary][f"{code}|{ALL_BUCKETS}"] for key, code in SERIES},
                    "cells_by_lead_bucket": {label: {key: counts[primary][f"{code}|{label}"]["cells_kept"] for key, code in SERIES}
                                             for label in labels},
                    "index_days_by_lead_bucket": {label: len(fares[(primary, ALL, label)]["dates"])
                                                  for label in labels if (primary, ALL, label) in fares},
                    "lags_weeks": list(LAGS), "max_lag_weeks": MAX_LAG, "max_lags_tried": list(MAX_LAGS_TRIED),
                    "weekly_methods": list(WEEKLY_METHODS), "seed": seed, "n_boot": n_boot},
        "dataset": {"name": entry["title"], "source": entry["source"], "url": entry["url"], "licence": entry["licence"],
                    "licence_id": entry["licence_id"], "attribution": entry["attribution"], "archive_sha256": entry["sha256"],
                    "archive_bytes": entry["bytes"], "retrieved_at": entry["retrieved_at"], "rows": profile["rows"],
                    "search_dates": profile["search_dates"], "flight_dates": profile["flight_dates"],
                    "days_to_departure": profile["days_to_departure"], "airports": profile["airports"],
                    "nonstop_share": profile["nonstop_share"], "rows_by_carrier": profile["rows_by_carrier"],
                    "nonstop_economy_rows_by_carrier": profile["nonstop_economy_rows_by_carrier"],
                    "basic_economy_share_of_nonstop_economy_rows_by_month":
                        profile["basic_economy_share_of_nonstop_economy_rows_by_month"]},
        "limits": _limits(profile, entry, n_weeks, counts, no_index, (start.isoformat(), end.isoformat())),
    }


def validate(document: dict) -> list[str]:
    """Problems with a daily_fares_2022.json document; empty when it conforms."""
    problems = []
    for key in ("generated_at", "as_of", "inputs", "headline", "plain_summary", "daily", "lead_lag", "distributed_lag",
                "n_days", "n_quotes", "method", "limits", "fuel_path", "fare_path", "choices", "dataset"):
        if key not in document or document[key] in (None, "", [], {}):
            problems.append(f"missing {key}")
    if problems:
        return problems
    if "fixture" in document:
        problems.append("a built file must not carry a fixture flag")
    try:
        json.dumps(document, allow_nan=False)
    except ValueError:
        problems.append("document holds NaN or infinity")
    if any(not {"path", "sha256"} <= set(i) or len(i["sha256"]) != 64 for i in document["inputs"]):
        problems.append("every input needs a path and a SHA-256")
    daily = document["daily"]
    fields = {"date", "fuel"} | {f"index_{key}" for key, _ in SERIES}
    if any(set(row) != fields for row in daily):
        problems.append("daily rows must hold date, fuel and the four indexes")
    elif [row["date"] for row in daily] != sorted({row["date"] for row in daily}):
        problems.append("daily dates repeat or are out of order")
    elif any(v is not None and not (isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0)
             for row in daily for k, v in row.items() if k != "date"):
        problems.append("daily values must be positive numbers or null")
    elif daily[-1]["date"] != document["as_of"]:
        problems.append("as_of is not the last daily date")
    elif document["n_days"] != sum(1 for row in daily if row["index_all"] is not None):
        problems.append("n_days does not match the days with an all-carrier index")
    correlations = document["lead_lag"].get("correlations", {})
    if set(correlations) != {key for key, _ in SERIES}:
        problems.append("lead_lag.correlations needs ua, dl, aa and all")
    for key, rows in correlations.items():
        if [r["lag_weeks"] for r in rows] != list(LAGS):
            problems.append(f"lead_lag.{key} does not cover lags {LAGS[0]}..{LAGS[-1]}")
        elif any(r["corr"] is not None and not (-1 <= r["lo"] <= r["hi"] <= 1 and -1 <= r["corr"] <= 1) for r in rows):
            problems.append(f"lead_lag.{key} has a correlation or interval outside -1..1")
    fits = document["distributed_lag"].get("by_series", {})
    if set(fits) != {key for key, _ in SERIES}:
        problems.append("distributed_lag.by_series needs ua, dl, aa and all")
    for key, pair in fits.items():
        if pair.get("fuel_only") is None:
            problems.append(f"distributed_lag.{key} lacks the fuel-only regression")
        for name, fit in pair.items():
            if fit is not None and not (fit["long_run"]["lo"] <= fit["long_run"]["sum"] <= fit["long_run"]["hi"]):
                problems.append(f"distributed_lag.{key}.{name} interval does not bracket the estimate")
    if not isinstance(document["method"], str) or not 4 <= document["method"].count(". ") + 1 <= 6:
        problems.append("method must be four to six sentences of text")
    if not isinstance(document["limits"], list) or len(document["limits"]) < 5 or not any("Licence" in t for t in document["limits"]):
        problems.append("limits must be a list of at least five statements, one of them the licence")
    return problems
