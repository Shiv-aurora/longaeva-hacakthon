"""Download daily jet fuel, daily prices and monthly airfare CPI, or rebuild offline.

Every response body is kept as received under data/lab_raw/market/<sha256>.<ext>
and indexed in data/lab/manifests/market.json. `--offline` rebuilds the tidy CSVs
from that store through the manifest without network. Days a source did not
print stay absent: nothing is forward filled and no weekend price is invented.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
import io
import json
import math
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab.market_data import CPI_CSV, CPI_SERIES, FUEL_CSV, PRICES_CSV, TICKERS
from app.source_store import byte_hash

RAW = ROOT / "data" / "lab_raw" / "market"
MANIFEST = ROOT / "data" / "lab" / "manifests" / "market.json"
LEGACY_FUEL = ROOT / "data" / "eia_gulf_coast_2026.csv"
FUEL_START, PRICE_START, CPI_START = date(2010, 1, 1), date(2012, 1, 1), date(2010, 1, 1)
EIA_SOURCEKEY = "EER_EPJK_PF4_RGC_DPG"
FRED_FUEL = "DJFUELUSGULF"
FRED_CPI = {"CUSR0000SETG01": "CUSR0000SETG01", "CUUR0000SETG01": "CUUR0000SETG01", "CUSR0000SA0": "CPIAUCSL"}
BLS_BODY = {"seriesid": list(CPI_SERIES), "startyear": "2024", "endyear": "2026"}
AGENT = "UAL Counterfactual Lab research"
YAHOO_AGENT = "Mozilla/5.0"  # Yahoo answers 429 to tool user agents and to some long browser strings.
PUBLIC = "US government statistical data in the public domain; cite the agency as the source."
TERMS = {
    "eia": "U.S. Energy Information Administration. " + PUBLIC,
    "fred": "U.S. agency data redistributed by FRED (Federal Reserve Bank of St. Louis). " + PUBLIC
            + " FRED's own terms of use apply to its service.",
    "bls": "U.S. Bureau of Labor Statistics public API v2, unregistered. " + PUBLIC,
    "yahoo": "Unofficial Yahoo Finance chart endpoint: undocumented, no service guarantee, and covered by Yahoo's "
             "own terms of use, which this project has not cleared for redistribution. Research use only; flag "
             "under project limits.",
}
NOTES = {
    "fuel": "Current history, not a real-time vintage: earlier prints may have been revised by the publisher.",
    "yahoo": "close is Yahoo's split-adjusted close; adj_close also folds in dividends and is restated after "
             "each one, so it depends on the retrieval date. Yahoo computes adj_close in 32-bit floats: two "
             "retrievals minutes apart on 2026-10-09 had identical closes but adj_close values about one part "
             "in a million apart, so rebuild from the stored raw file when an exact match matters.",
    "HO=F": " Continuous front-month NY Harbor ULSD future as Yahoo defines it; Yahoo does not document roll "
            "dates or whether a close is the exchange settlement.",
    "CUSR0000SETG01": "Seasonally adjusted: BLS revises recent years annually, so this is the current vintage, "
                      "not the figures first published.",
    "CUUR0000SETG01": "Not seasonally adjusted.",
    "CUSR0000SA0": "FRED id CPIAUCSL is BLS series CUSR0000SA0 (all items, seasonally adjusted) and is stored "
                   "under the BLS id. Current vintage, revised annually.",
    "bls": "Cross-check only; no tidy file is built from this response.",
}


def sources(now: int) -> list[dict]:
    fred = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="
    yahoo = "https://query1.finance.yahoo.com/v8/finance/chart/{}?period1={}&period2={}&interval=1d&events=div%2Csplit"
    start = int(datetime(PRICE_START.year, 1, 1, tzinfo=timezone.utc).timestamp())
    return ([{"source": "eia_fuel", "url": "https://www.eia.gov/dnav/pet/hist_xls/EER_EPJK_PF4_RGC_DPGd.xls",
              "ext": "xls", "terms_note": TERMS["eia"], "data_note": NOTES["fuel"]},
             {"source": "fred_fuel", "url": fred + FRED_FUEL, "ext": "csv", "fred_id": FRED_FUEL,
              "terms_note": TERMS["fred"], "data_note": "Cross-check only. " + NOTES["fuel"]}]
            + [{"source": f"yahoo:{t}", "url": yahoo.format(quote(t, safe=""), start, now), "ext": "json",
                "ticker": t, "terms_note": TERMS["yahoo"], "data_note": NOTES["yahoo"] + NOTES.get(t, "")}
               for t in TICKERS]
            + [{"source": f"fred_cpi:{s}", "url": fred + FRED_CPI[s], "ext": "csv", "series_id": s,
                "fred_id": FRED_CPI[s], "terms_note": TERMS["fred"], "data_note": NOTES[s]} for s in CPI_SERIES]
            + [{"source": "bls_api", "url": "https://api.bls.gov/publicAPI/v2/timeseries/data/", "ext": "json",
                "method": "POST", "request_body": BLS_BODY, "terms_note": TERMS["bls"], "data_note": NOTES["bls"]}])


def checked(rows: list[tuple], label: str) -> list[tuple]:
    """Ascending unique dates and finite positive values, or refuse the source."""
    if not rows:
        raise ValueError(f"{label}: no observations")
    for previous, current in zip(rows, rows[1:]):
        if current[0] <= previous[0]:
            raise ValueError(f"{label}: dates out of order or repeated at {current[0]}")
    if any(not (isinstance(v, float) and math.isfinite(v) and v > 0) for row in rows for v in row[1:]):
        raise ValueError(f"{label}: non-numeric, non-finite or non-positive value")
    return rows


def eia_grid(content: bytes) -> tuple[list[list], int]:
    try:
        import xlrd  # EIA still serves BIFF .xls.
    except ImportError as exc:
        raise ValueError("Reading the EIA .xls needs xlrd: pip install -r requirements-research.txt") from exc
    book = xlrd.open_workbook(file_contents=content)
    sheet = book.sheet_by_name("Data 1")
    return [sheet.row_values(r) for r in range(sheet.nrows)], book.datemode


def parse_eia_grid(grid: list[list], datemode: int = 0) -> list[tuple[date, float]]:
    """Cell values of the EIA 'Data 1' sheet -> (date, usd_per_gallon)."""
    if datemode != 0 or len(grid) < 4 or list(grid[1][:2]) != ["Sourcekey", EIA_SOURCEKEY] or grid[2][0] != "Date":
        raise ValueError("EIA sheet is not the Gulf Coast jet fuel daily layout")
    rows = []
    for serial, value in (row[:2] for row in grid[3:]):
        if value in ("", None):
            continue  # A day without a print is left out, never carried forward.
        if not isinstance(serial, float) or serial != int(serial) or not isinstance(value, float):
            raise ValueError(f"EIA row has an unexpected date or price cell: {serial!r}, {value!r}")
        rows.append((date(1899, 12, 30) + timedelta(days=int(serial)), value))
    return checked(rows, "EIA fuel")


def parse_fred_csv(content: bytes, fred_id: str) -> list[tuple[date, float]]:
    lines = [line for line in csv.reader(io.StringIO(content.decode("utf-8-sig"))) if line]
    if not lines or lines[0][1:] != [fred_id] or lines[0][0] not in ("observation_date", "DATE"):
        raise ValueError(f"FRED CSV header is not a date column plus {fred_id}")
    # FRED lists market holidays and unpublished months with an empty value.
    return checked([(date.fromisoformat(day), float(text)) for day, text in lines[1:] if text.strip() not in ("", ".")],
                   f"FRED {fred_id}")


def parse_yahoo_chart(content: bytes, ticker: str, retrieved_at: str) -> list[tuple[date, float, float]]:
    """Yahoo chart JSON -> (session date, close, adj_close).

    Bars with no close are dropped, as is any bar dated on or after the retrieval
    day in the exchange's time zone, because that session may not have settled.
    Close is rounded to 4 and adj_close to 6 decimals: Yahoo serves 32-bit floats.
    """
    chart = json.loads(content)["chart"]
    if chart.get("error") or not chart.get("result"):
        raise ValueError(f"Yahoo returned no chart for {ticker}: {chart.get('error')}")
    result = chart["result"][0]
    meta = result["meta"]
    if meta["symbol"] != ticker or meta["dataGranularity"] != "1d":
        raise ValueError(f"Yahoo chart is not daily {ticker}")
    zone = ZoneInfo(meta["exchangeTimezoneName"])
    cutoff = datetime.fromisoformat(retrieved_at).astimezone(zone).date()
    stamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    adjusted = result["indicators"]["adjclose"][0]["adjclose"]
    if not len(stamps) == len(closes) == len(adjusted):
        raise ValueError(f"Yahoo arrays differ in length for {ticker}")
    rows = {}
    for stamp, close, adjusted_close in zip(stamps, closes, adjusted):
        day = datetime.fromtimestamp(stamp, zone).date()
        if (close is None and adjusted_close is None) or day >= cutoff:
            continue
        if close is None or adjusted_close is None:
            raise ValueError(f"Yahoo {ticker} has only one of close and adjusted close on {day}")
        row = (day, round(float(close), 4), round(float(adjusted_close), 6))
        if rows.setdefault(day, row) != row:
            raise ValueError(f"Yahoo {ticker} has two different bars for {day}")
    return checked(sorted(rows.values()), f"Yahoo {ticker}")


def parse_bls_json(content: bytes) -> dict[str, list[tuple[date, float]]]:
    blob = json.loads(content)
    if blob.get("status") != "REQUEST_SUCCEEDED" or not blob.get("Results", {}).get("series"):
        raise ValueError(f"BLS API request failed: {blob.get('status')} {blob.get('message')}")
    result = {}
    for series in blob["Results"]["series"]:
        # "-" marks a month BLS did not publish (October 2025); M13 is an annual average.
        rows = sorted((date(int(item["year"]), int(item["period"][1:]), 1), float(item["value"]))
                      for item in series["data"]
                      if re.fullmatch(r"M(0[1-9]|1[0-2])", item["period"]) and item["value"].strip() not in ("-", ""))
        result[series["seriesID"]] = checked(rows, f"BLS {series['seriesID']}")
    return result


def cross_check(a: list[tuple], b: list[tuple], tolerance: float, start: date | None = None,
                end: date | None = None) -> dict:
    """Compare two dated series on the dates they share, and list the dates only one has."""
    def clip(rows):
        return {r[0]: r[1] for r in rows if (start is None or r[0] >= start) and (end is None or r[0] <= end)}
    left, right = clip(a), clip(b)
    common = sorted(left.keys() & right.keys())
    gaps = {d: abs(left[d] - right[d]) for d in common}
    worst = max(gaps.values(), default=None)
    return {"overlap": len(common),
            "first": common[0].isoformat() if common else None, "last": common[-1].isoformat() if common else None,
            "max_abs_diff": None if worst is None else round(worst, 6),
            "max_abs_diff_date": next((d.isoformat() for d in common if worst and gaps[d] == worst), None),
            "tolerance": tolerance,
            "mismatches": [{"date": d.isoformat(), "a": left[d], "b": right[d]} for d in common if gaps[d] > tolerance],
            "only_in_a": [d.isoformat() for d in sorted(left.keys() - right.keys())],
            "only_in_b": [d.isoformat() for d in sorted(right.keys() - left.keys())]}


def exact(value: float, places: int) -> str:
    if abs(value - round(value, places)) > 1e-9:
        raise ValueError(f"{value!r} carries more than {places} decimals; refusing to round a source value")
    return f"{value:.{places}f}"


def fuel_lines(fuel: list[tuple]) -> list[tuple]:
    return [(d.isoformat(), exact(v, 3)) for d, v in fuel if d >= FUEL_START]


def price_lines(by_ticker: dict[str, list[tuple]]) -> list[tuple]:
    return sorted((d.isoformat(), ticker, f"{close:.4f}", f"{adjusted:.6f}")
                  for ticker, rows in by_ticker.items() for d, close, adjusted in rows if d >= PRICE_START)


def cpi_lines(by_series: dict[str, list[tuple]]) -> list[tuple]:
    return sorted((d.isoformat(), series_id, exact(v, 3))
                  for series_id, rows in by_series.items() for d, v in rows if d >= CPI_START)


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)


def span(days: list[str]) -> dict:
    return {"rows": len(days), "first": days[0], "last": days[-1]}


def write_csv(path: Path, header: tuple, lines: list[tuple]) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(lines)
    return {"path": relative(path), "sha256": byte_hash(path.read_bytes()), **span([line[0] for line in lines])}


def store_raw(content: bytes, ext: str, raw: Path = RAW) -> Path:
    if not content:
        raise ValueError("Empty response")
    raw.mkdir(parents=True, exist_ok=True)
    target = raw / f"{byte_hash(content)}.{ext}"
    if target.exists():
        if target.read_bytes() != content:
            raise ValueError(f"Raw file {target.name} no longer matches its hash")
    else:
        with target.open("xb") as stream:
            stream.write(content)
    return target


def read_raw(entry: dict) -> bytes:
    content = (ROOT / entry["path"]).read_bytes()
    if byte_hash(content) != entry["sha256"] or len(content) != entry["bytes"]:
        raise ValueError(f"Raw file for {entry['source']} failed its checksum")
    return content


def parse_entry(entry: dict, content: bytes):
    kind = entry["source"].split(":")[0]
    if kind == "eia_fuel":
        return parse_eia_grid(*eia_grid(content))
    if kind in ("fred_fuel", "fred_cpi"):
        return parse_fred_csv(content, entry["fred_id"])
    if kind == "yahoo":
        return parse_yahoo_chart(content, entry["ticker"], entry["retrieved_at"])
    if kind == "bls_api":
        return parse_bls_json(content)
    raise ValueError(f"Unknown source {entry['source']}")


def download(previous: dict | None = None, client=None, raw: Path = RAW, pause: float = 1.5) -> dict:
    """Fetch every source. One that fails or does not parse is listed under failures and keeps its previous entry."""
    import httpx
    kept = {e["source"]: e for e in (previous or {}).get("entries", [])}
    entries, failures = [], []
    client = client or httpx.Client(timeout=60, follow_redirects=True)
    with client:
        for source in sources(int(time.time())):
            ext = source.pop("ext")
            headers = {"User-Agent": YAHOO_AGENT if "ticker" in source else AGENT}
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            stored = None
            try:
                for attempt in range(3):
                    try:
                        response = (client.post(source["url"], json=source["request_body"], headers=headers)
                                    if source.get("method") == "POST" else client.get(source["url"], headers=headers))
                        response.raise_for_status()
                        break
                    except httpx.HTTPError:
                        if attempt == 2:
                            raise
                        time.sleep(2 * pause * (attempt + 1))
                content = response.content
                stored = relative(store_raw(content, ext, raw))
                entry = {**source, "retrieved_at": stamp, "sha256": byte_hash(content), "bytes": len(content),
                         "path": stored}
                parse_entry(entry, content)  # A payload that does not parse never replaces a good one.
                entries.append(entry)
            except Exception as exc:
                failures.append({"source": source["source"], "url": source["url"], "attempted_at": stamp,
                                 "error": f"{type(exc).__name__}: {exc}"[:400], "raw_kept_at": stored,
                                 "kept_previous_entry": source["source"] in kept})
                if source["source"] in kept:
                    entries.append(kept[source["source"]])
            if "ticker" in source:
                time.sleep(pause)
    return {"area": "market", "builder": "scripts/lab/fetch_market.py", "raw_dir": relative(raw),
            "raw_note": "Each raw file is the response body as received (after HTTP content decoding), named by its SHA-256.",
            "entries": entries, "failures": failures}


def read_legacy_fuel(path: Path = LEGACY_FUEL) -> list[tuple[date, float]]:
    with path.open(newline="") as stream:
        return [(date.fromisoformat(r["date"]), float(r["usd_per_gallon"])) for r in csv.DictReader(stream)]


def build(manifest: dict, fuel_csv: Path = FUEL_CSV, prices_csv: Path = PRICES_CSV, cpi_csv: Path = CPI_CSV) -> dict:
    """Write each tidy CSV whose sources are all in the manifest, and run the cross-checks. No network."""
    parsed = {e["source"]: parse_entry(e, read_raw(e)) for e in manifest["entries"]}
    outputs, checks, not_built = [], {}, []

    if "eia_fuel" in parsed:
        fuel = [r for r in parsed["eia_fuel"] if r[0] >= FUEL_START]
        outputs.append(write_csv(fuel_csv, ("date", "usd_per_gallon"), fuel_lines(fuel)))
        if "fred_fuel" in parsed:
            # Both publish to a tenth of a cent, so a gap above 0.0005 is a real disagreement.
            checks["eia_vs_fred_fuel"] = cross_check(fuel, parsed["fred_fuel"], 0.0005, start=FUEL_START)
        if LEGACY_FUEL.exists():
            legacy = read_legacy_fuel()
            checks["fuel_vs_eia_gulf_coast_2026_csv"] = {
                "legacy_rows": len(legacy),
                **cross_check(fuel, legacy, 0.005, start=legacy[0][0], end=legacy[-1][0])}
    else:
        not_built.append({"path": relative(fuel_csv), "missing": ["eia_fuel"]})

    missing = [f"yahoo:{t}" for t in TICKERS if f"yahoo:{t}" not in parsed]
    if missing:
        not_built.append({"path": relative(prices_csv), "missing": missing})
    else:
        lines = price_lines({t: parsed[f"yahoo:{t}"] for t in TICKERS})
        outputs.append({**write_csv(prices_csv, ("date", "ticker", "close", "adj_close"), lines),
                        "by_ticker": {t: span([line[0] for line in lines if line[1] == t]) for t in TICKERS}})

    missing = [f"fred_cpi:{s}" for s in CPI_SERIES if f"fred_cpi:{s}" not in parsed]
    if missing:
        not_built.append({"path": relative(cpi_csv), "missing": missing})
    else:
        by_series = {s: parsed[f"fred_cpi:{s}"] for s in CPI_SERIES}
        lines = cpi_lines(by_series)
        outputs.append({**write_csv(cpi_csv, ("date", "series_id", "value"), lines),
                        "by_series": {s: {**span([line[0] for line in lines if line[1] == s]), "fred_id": FRED_CPI[s]}
                                      for s in CPI_SERIES}})
        for series_id, bls in parsed.get("bls_api", {}).items():
            if series_id in by_series:
                fred = by_series[series_id]
                checks.setdefault("bls_vs_fred_cpi", {})[series_id] = {
                    **cross_check(bls, fred, 0.0005, start=bls[0][0]),
                    "latest": {"date": bls[-1][0].isoformat(), "bls": bls[-1][1], "fred": dict(fred).get(bls[-1][0])}}
    return {"outputs": outputs, "cross_checks": checks, "not_built": not_built}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true",
                        help="Rebuild the tidy CSVs from the raw store through the manifest; no network")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args()
    previous = json.loads(args.manifest.read_text()) if args.manifest.exists() else None
    if args.offline:
        if previous is None:
            raise SystemExit(f"No manifest at {args.manifest}; run once with network first")
        manifest = previous
    else:
        manifest = download(previous)
    result = build(manifest)
    if not args.offline:
        manifest.update(result)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    print(json.dumps({**result, "failures": manifest.get("failures", [])}, indent=2))
    if manifest.get("failures") or result["not_built"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
