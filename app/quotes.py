"""Audited human-supplied ticket quote snapshots. Never synthesize/forward-fill fare observations."""
from __future__ import annotations
import csv
import io
import math
from datetime import date
from pathlib import Path
from statistics import median
from urllib.parse import urlparse

REQUIRED = ["observed_on", "departure_on", "carrier", "origin", "destination", "cabin", "stops", "total_price_usd", "source_url"]
QUOTES_FILE = Path(__file__).resolve().parents[1] / "data" / "user_quotes.csv"


def parse_quotes(text: str) -> list[dict]:
    if len(text.encode("utf-8")) > 2_000_000:
        raise ValueError("Quote CSV must be at most 2 MB")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or any(x not in reader.fieldnames for x in REQUIRED):
        raise ValueError("Missing required columns: " + ", ".join(x for x in REQUIRED if x not in (reader.fieldnames or [])))
    data = []
    for index, row in enumerate(reader, 2):
        if index > 10002:
            raise ValueError("Maximum 10,000 quote rows")
        try:
            obs = date.fromisoformat(row["observed_on"].strip())
            dep = date.fromisoformat(row["departure_on"].strip())
            lead = (dep - obs).days
            if not 1 <= lead <= 365:
                raise ValueError("departure_on must be 1-365 days after observed_on")
            price = float(row["total_price_usd"])
            if not math.isfinite(price) or not 20 <= price <= 25000:
                raise ValueError("total_price_usd outside supported bounds")
            carrier = row["carrier"].strip().upper()
            if carrier != "UA":
                raise ValueError("V1 accepts only UA-marketed quotes")
            orig = row["origin"].strip().upper()
            dest = row["destination"].strip().upper()
            if any(len(x) != 3 or not x.isalpha() for x in (orig, dest)) or orig == dest:
                raise ValueError("origin and destination must be distinct 3-letter IATA codes")
            cabin = row["cabin"].strip().lower()
            if cabin not in {"economy", "premium_economy", "business", "first"}:
                raise ValueError("unsupported cabin")
            stops = int(row["stops"])
            if stops not in (0, 1, 2):
                raise ValueError("stops must be 0, 1, or 2")
            url = row["source_url"].strip()
            parsed_url = urlparse(url)
            if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
                raise ValueError("source_url must be valid http(s) URL")
            data.append({"observed_on": obs.isoformat(), "departure_on": dep.isoformat(), "carrier": carrier,
                         "origin": orig, "destination": dest, "cabin": cabin, "stops": stops,
                         "total_price_usd": price, "source_url": url, "lead_days": lead,
                         "lead_week": lead // 7, "dep_weekday": dep.weekday()})
        except (ValueError, TypeError, KeyError) as e:
            raise ValueError(f"CSV row {index}: {str(e)}") from e
    if not data:
        raise ValueError("The CSV has no quote observations")
    # Fail rather than silently mutating records or overwriting duplicates.
    keys = [(d["observed_on"], d["departure_on"], d["origin"], d["destination"], d["cabin"], d["stops"]) for d in data]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate snapshots for identical observed/departure/route/cabin/stops")
    return sorted(data, key=lambda x: (x["observed_on"], x["origin"], x["destination"], x["departure_on"]))


def save_quotes(text: str) -> int:
    data = parse_quotes(text)
    QUOTES_FILE.write_text(text, encoding="utf-8")
    return len(data)


def read_quotes() -> list[dict]:
    if not QUOTES_FILE.exists(): return []
    try:
        return parse_quotes(QUOTES_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return []


def quote_coverage(data: list[dict]) -> dict:
    routes = sorted({f"{x['origin']}-{x['destination']}" for x in data})
    last = max((x["observed_on"] for x in data), default=None)
    return {"rows": len(data), "routes": routes, "last_observed": last, "status": "user-imported observations" if data else "no fare observations"}


def route_diagnostic(data: list[dict], fuel: list[dict], ual: dict, route: str = "") -> dict:
    """Report only an observable descriptive fare/fuel comparison, never infer UAL causality."""
    if not data: return {"ready": False, "reason": "Import real, sourced UAL ticket quotes to calculate an observed route-level gap."}
    routes = sorted({f"{x['origin']}-{x['destination']}" for x in data})
    if not route: route = routes[0]
    if route not in routes: return {"ready": False, "reason": "No observations for this route", "route": route}
    group = [x for x in data if f"{x['origin']}-{x['destination']}" == route and x["cabin"] == "economy" and x["stops"] == 0 and 21 <= x["lead_days"] <= 35]
    dates = sorted({x["observed_on"] for x in group})
    if len(dates) < 8 or (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days < 10:
        return {"ready": False, "reason": "Need at least 8 distinct days spanning 10+ days of nonstop economy quotes at 21-35 day booking lead, for the selected route.", "route": route, "comparable_quotes": len(group), "comparable_days": len(dates)}
    index = []
    for d in dates:
        prices = [r["total_price_usd"] for r in group if r["observed_on"] == d]
        index.append({"date": d, "median_offered_fare_usd": round(median(prices), 2), "count": len(prices)})
    fuel_map = {r["date"]: r["usd_per_gallon"] for r in fuel}
    last_date = date.fromisoformat(dates[-1])
    eligible = [x for x in index if (last_date - date.fromisoformat(x["date"])).days >= 7 and x["date"] in fuel_map]
    current = index[-1]
    if current["date"] not in fuel_map or not eligible:
        return {"ready": False, "reason": "Need date-matched public fuel observations for current and at least 7-days-earlier quote snapshots.", "route": route, "price_index": index}
    old = eligible[-1]
    fuel_delta = fuel_map[current["date"]] - fuel_map[old["date"]]
    fare_pct_change = 100 * (current["median_offered_fare_usd"] / old["median_offered_fare_usd"] - 1)
    cents_asm_pressure = fuel_delta * (ual["fuel_gallons_million"] / ual["asm_million"]) * 100
    return {"ready": True, "status": "descriptive / unvalidated diagnostic", "route": route,
            "cabin": "economy, nonstop, 21-35 days to departure", "first_date": old["date"], "last_date": current["date"],
            "fare_change_pct": round(fare_pct_change, 3), "spot_fuel_delta_usd_gal": round(fuel_delta, 3),
            "spot_fuel_cost_pressure_cents_per_asm": round(cents_asm_pressure, 3),
            "reference_fare_usd": old["median_offered_fare_usd"], "latest_fare_usd": current["median_offered_fare_usd"],
            "price_index": index, "caution": "Offered fare is not realized yield or PRASM. Source mix, day of week, and departure seasonality may bias the comparison; not a tradable alpha signal."}
