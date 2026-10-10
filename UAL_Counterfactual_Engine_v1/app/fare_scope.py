"""Quarter-specific geographic coverage diagnostics, not a fare/traffic join."""
from collections import Counter


def geographic_index(rows):
    result = {}
    for row in rows:
        key = row["quarter"], row["airport"], row["city_market_id"]
        result.setdefault(key, []).append(row)
    return result


def endpoint_assignment(index, quarter, airport, city):
    rows = index.get((quarter, airport, city), [])
    if not rows:
        return None, "missing_endpoint"
    if len(rows) != 1 or rows[0]["assignment_status"] == "conflicting_wac":
        return None, "conflicting_wac"
    row = rows[0]
    if row["assignment_status"] != "unambiguous" or row["market"] is None:
        return None, "unmapped_wac"
    return row["market"], None


def geographic_market(origin, destination):
    if origin == destination:
        return origin
    if origin == "domestic":
        return destination
    if destination == "domestic":
        return origin
    return "cross_region"


def coverage(fare_rows, geography_rows):
    index = geographic_index(geography_rows); counts = Counter(); failures = []
    for row in fare_rows:
        origin, a = endpoint_assignment(index, row["quarter"], row["origin"], row["origin_city_market_id"])
        destination, b = endpoint_assignment(index, row["quarter"], row["destination"], row["destination_city_market_id"])
        status = "/".join(sorted({s for s in (a, b) if s})) if a or b else "mapped_endpoints"
        market = "unassigned" if a or b else geographic_market(origin, destination)
        key = row["quarter"], row["carrier"], row["cohort"], market, status
        counts[key] += 1
        if market in {"unassigned", "cross_region"} and len(failures) < 10:
            failures.append({k: row[k] for k in ("quarter", "carrier", "cohort", "origin", "origin_city_market_id", "destination", "destination_city_market_id")})
    return {"route_cells": len(fare_rows), "cells": [{"quarter": q, "carrier": c, "cohort": h,
        "geographic_market": m, "assignment_status": s, "route_cells": n} for (q, c, h, m, s), n in sorted(counts.items())],
        "unassigned_or_cross_region_examples": failures,
        "scope": "Counts of fare cells by declared endpoint geography; overlapping cohorts stay separate. This does not establish matching T-100 travel objects, complete market coverage, a fixed-mix price index or measured carrier revenue yield."}
