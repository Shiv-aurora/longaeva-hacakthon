from app.fare_scope import geographic_index, endpoint_assignment, geographic_market, coverage


def test_no_nearest_quarter_or_city_fill_and_no_conflict_majority_choice():
    row = {"quarter": "2013-Q1", "airport": "JFK", "city_market_id": 31703, "wac": 22,
        "market": "domestic", "assignment_status": "unambiguous"}
    index = geographic_index([row])
    assert endpoint_assignment(index, "2013-Q2", "JFK", 31703) == (None, "missing_endpoint")
    assert endpoint_assignment(index, "2013-Q1", "JFK", 99999) == (None, "missing_endpoint")
    index = geographic_index([row, {**row, "wac": 23}])
    assert endpoint_assignment(index, "2013-Q1", "JFK", 31703) == (None, "conflicting_wac")


def test_domestic_latin_and_pacific_scope_preserve_cross_region_ambiguity():
    assert geographic_market("domestic", "latin_america") == "latin_america"
    assert geographic_market("pacific", "domestic") == "pacific"
    assert geographic_market("latin_america", "pacific") == "cross_region"


def test_coverage_keeps_overlapping_cohorts_separate_and_missing_rows_visible():
    geography = [{"quarter": "2013-Q1", "airport": airport, "city_market_id": city, "market": market,
        "assignment_status": "unambiguous"} for airport, city, market in (("JFK", 31703, "domestic"), ("SJU", 34819, "latin_america"))]
    fare = {"quarter": "2013-Q1", "carrier": "UA", "cohort": "mainline_online", "origin": "JFK",
        "origin_city_market_id": 31703, "destination": "SJU", "destination_city_market_id": 34819}
    result = coverage([fare, {**fare, "cohort": "ticketed_online"}, {**fare, "destination": "MISSING"}], geography)
    assert result["route_cells"] == 3 and sum(r["route_cells"] for r in result["cells"]) == 3
    assert len(result["cells"]) == 3 and len(result["unassigned_or_cross_region_examples"]) == 1
    assert sum(r["geographic_market"] == "latin_america" for r in result["cells"]) == 2
