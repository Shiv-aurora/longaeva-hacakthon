"""Offline tests for the 2022 daily fare index.

No row of the Kaggle dataset is used. `fixtures/lab_fares_2022/header.csv` is the
header line of the real itineraries.csv (column names only); every quote below is
synthetic, written in that 27-column schema with a price path the test knows.
"""
from argparse import Namespace
import csv
from datetime import date, timedelta
import json
import math
from pathlib import Path

import duckdb
import numpy as np
import pytest

from app.lab import fares_2022 as fx
from scripts.lab import build_fares_2022 as builder

FIXTURES = Path(__file__).parent / "fixtures" / "lab_fares_2022"
START = date(2022, 4, 18)  # a Monday, so synthetic weeks are whole
ROUTES = {("EWR", "SFO"): ("UA", "DL"), ("ORD", "DEN"): ("UA", "DL", "AA")}
LEVEL = {"UA": 200.0, "DL": 180.0, "AA": 160.0}
NAME = {"UA": "United", "DL": "Delta", "AA": "American Airlines"}
LEADS = range(7, 49)


def quote(search, flight, origin, dest, carrier, fare, basic=False, nonstop=True, cabin="coach"):
    twice = lambda value: value if nonstop else f"{value}||{value}"
    return ["0" * 32, search.isoformat(), flight.isoformat(), origin, dest, "QA7NA0MC", "PT2H30M", "0",
            str(basic), "False", str(nonstop), f"{fare / 1.15:.2f}", f"{fare:.2f}", "7", "947",
            twice("1650214620"), twice("2022-04-17T12:57:00.000-04:00"), twice("1650223560"),
            twice("2022-04-17T15:26:00.000-04:00"), twice(dest), twice(origin), twice(NAME[carrier]), twice(carrier),
            twice("Airbus A321"), twice("8940"), twice("947"), twice(cabin)]


def base_fare(route, carrier, lead):
    """A different level for every route, carrier and day to departure."""
    return LEVEL[carrier] * (1.5 if route[0] == "EWR" else 1.0) * (1.0 + 0.004 * (48 - lead))


def world(n_days, log_path=lambda t, carrier: 0.0, extras=lambda t, route, carrier: 1, basic=lambda t: False,
          listed=lambda t, route, carrier, lead: True, skip_days=()):
    """Synthetic quotes: per search day, route, carrier and flight date one cheapest fare on the known path, some
    dearer itineraries, and rows the index must ignore (connections, first class, leads outside 7-48)."""
    rows = []
    for t in range(n_days):
        if t in skip_days:
            continue
        search = START + timedelta(days=t)
        for route, carriers in ROUTES.items():
            for carrier in carriers:
                for lead in LEADS:
                    if not listed(t, route, carrier, lead):
                        continue
                    flight = search + timedelta(days=lead)
                    fare = base_fare(route, carrier, lead) * math.exp(log_path(t, carrier))
                    rows.append(quote(search, flight, *route, carrier, fare))
                    rows += [quote(search, flight, *route, carrier, fare * (1.15 + 0.1 * k))
                             for k in range(extras(t, route, carrier))]
                    if basic(t):
                        rows.append(quote(search, flight, *route, carrier, fare * 0.8, basic=True))
            ten = search + timedelta(days=10)
            rows.append(quote(search, ten, *route, "UA", 49.0, nonstop=False))
            rows.append(quote(search, ten, *route, "UA", 999.0, cabin="first"))
            rows.append(quote(search, search + timedelta(days=3), *route, "UA", 5.0))
            rows.append(quote(search, search + timedelta(days=55), *route, "UA", 5.0))
    return rows


def write_quotes(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(fx.EXPECTED_COLUMNS)
        writer.writerows(rows)
    return path


def build(tmp_path, rows, spec=fx.IndexSpec(), name="quotes.csv"):
    return fx.build_index(duckdb.connect(), fx.quotes_sql(write_quotes(tmp_path / name, rows)), spec)


def series(built, carrier, bucket="all", variant=fx.PRIMARY):
    """{day offset from START: (index, n_cells, n_quotes)}"""
    return {(date.fromisoformat(r[0]) - START).days: (float(r[3]), r[4], r[5])
            for r in built["rows"] if (r[1], r[2], r[6]) == (carrier, bucket, variant)}


def path(t, carrier):
    """United ramps and then jumps 20%; Delta drifts down; American is flat."""
    return {"UA": 0.01 * t + (0.2 if t >= 10 else 0.0), "DL": -0.005 * t, "AA": 0.0}[carrier]


def test_header_of_the_real_file_is_the_schema_the_module_reads(tmp_path):
    assert tuple(fx.read_header(FIXTURES / "header.csv")) == fx.EXPECTED_COLUMNS
    assert len(fx.EXPECTED_COLUMNS) == 27 and {name for name, _ in fx.TYPED} <= set(fx.EXPECTED_COLUMNS)
    broken = tmp_path / "broken.csv"
    broken.write_text(",".join(c for c in fx.EXPECTED_COLUMNS if c != "totalFare") + "\n")
    with pytest.raises(ValueError, match="totalFare"):
        fx.read_header(broken)


def test_index_recovers_the_injected_price_path_for_each_carrier(tmp_path):
    built = build(tmp_path, world(28, path))
    for carrier in ("UA", "DL", "AA"):
        for bucket in ("all", "7-13", "14-20", "21-34", "35-48"):
            got = series(built, carrier, bucket)
            assert sorted(got) == list(range(28))
            for t, (index, n_cells, _) in got.items():
                assert index == pytest.approx(100.0 * math.exp(path(t, carrier) - path(0, carrier)), abs=0.02)
                assert n_cells == len([r for r, c in ROUTES.items() if carrier in c]) * (4 if bucket == "all" else 1)
    # All carriers: every cell has the same weight, and there are 8 United, 8 Delta and 4 American cells.
    for t, (index, n_cells, n_quotes) in series(built, fx.ALL).items():
        expected = (8 * path(t, "UA") + 8 * path(t, "DL") + 4 * path(t, "AA")) / 20
        assert index == pytest.approx(100.0 * math.exp(expected), abs=0.02)
        assert n_cells == 20 and n_quotes == 5 * len(LEADS) * 2   # 5 route-carriers, 42 flight dates, 2 quotes each
    assert built["spec"]["buckets"] == ((7, 13), (14, 20), (21, 34), (35, 48))


def test_a_pure_mix_shift_does_not_move_the_index(tmp_path):
    # Prices never change. Later days list many more dear itineraries on the dear route, and basic economy appears.
    rows = world(28, extras=lambda t, route, carrier: 0 if t < 14 else (6 if route[0] == "EWR" else 0),
                 basic=lambda t: t >= 14)
    def naive(day):
        fares = [float(r[12]) for r in rows if r[1] == day.isoformat() and r[10] == "True" and r[26] == "coach"
                 and 7 <= (date.fromisoformat(r[2]) - day).days <= 48 and r[8] == "False"]
        return sum(fares) / len(fares)
    assert naive(START + timedelta(days=27)) > 1.25 * naive(START)   # the average quoted fare rises by over a quarter
    built = build(tmp_path, rows)
    main = [r for r in built["rows"] if r[6] == fx.PRIMARY]
    assert len(main) == 28 * 4 * 5 and {r[3] for r in main} == {"100.0000"}
    # The variant that takes the cheapest fare of either kind does move when basic economy appears: that is a
    # change of fare class, which is why it is kept apart from the primary index.
    either = series(built, "UA", variant="any_economy")
    assert either[13][0] == pytest.approx(100.0, abs=0.01) and either[27][0] == pytest.approx(80.0, abs=0.02)
    # Basic economy alone is quoted on half the days, so no cell of it is observed on nearly every day.
    assert not [r for r in built["rows"] if r[6] == "basic_economy"]
    assert built["cell_counts"]["basic_economy"]["ALL|all"]["cells_kept"] == 0


def test_cells_that_disappear_do_not_move_the_index(tmp_path):
    # The dear route stops being quoted half way through; American on ORD-DEN vanishes for two days and returns.
    def listed(t, route, carrier, lead):
        return not (route[0] == "EWR" and t >= 14) and not (carrier == "AA" and t in (5, 6))
    rows = world(28, path, listed=listed)
    loose = build(tmp_path, rows, fx.IndexSpec(min_coverage=0.4))     # keeps the cells that go missing
    strict = build(tmp_path, rows)                                   # drops them: they are not seen nearly every day
    for built in (loose, strict):
        for carrier in ("UA", "DL"):
            got = series(built, carrier)
            assert sorted(got) == list(range(28))
            for t, (index, _, _) in got.items():
                assert index == pytest.approx(100.0 * math.exp(path(t, carrier) - path(0, carrier)), abs=0.02)
    assert [series(loose, "UA")[t][1] for t in (13, 14)] == [8, 4]
    assert {v[1] for v in series(strict, "UA").values()} == {4} and not series(strict, "AA")
    assert sorted(series(loose, "AA")) == [t for t in range(28) if t not in (5, 6)]
    # With every cell kept, the all-carrier level on a day when all 12 remaining cells are quoted is exactly the
    # average of their own changes, whatever went missing before.
    t = 27
    expected = (4 * path(t, "UA") + 4 * path(t, "DL") + 4 * path(t, "AA")) / 12
    before = (8 * path(13, "UA") + 8 * path(13, "DL") + 4 * path(13, "AA")) / 20
    after = before + (4 * (path(t, "UA") - path(13, "UA")) + 4 * (path(t, "DL") - path(13, "DL"))) / 12
    assert series(loose, fx.ALL)[13][0] == pytest.approx(100.0 * math.exp(before), abs=0.02)
    assert series(loose, fx.ALL)[t][0] == pytest.approx(100.0 * math.exp(after), abs=0.03)
    assert expected != after   # the level keeps what the vanished cells did while they were quoted


def test_chained_index_carries_missing_cells_at_the_index_movement():
    g = np.cumsum(np.r_[0.0, np.full(9, 0.03)])
    steady = np.column_stack([np.log(100.0) + g, np.log(900.0) + g])
    gone = steady.copy()
    gone[5:, 1] = np.nan                       # the dear cell disappears for good
    away = steady.copy()
    away[3:7, 1] = np.nan                      # ... or is away for four days and comes back on the common path
    for x in (steady, gone, away):
        level, used, mask = fx.chained_index(x)
        assert level == pytest.approx(g) and used.tolist() == np.isfinite(x).sum(axis=1).tolist()
        assert (mask == np.isfinite(x)).all()
    # A cell that moved differently while away is corrected on its return, so the level matches the plain average.
    rng = np.random.default_rng(3)
    x = np.log(200.0) + rng.normal(0, 0.05, size=(30, 6)).cumsum(axis=0)
    full = x.copy()
    x[4:9, 2] = x[11:13, 0] = x[20:25, 5] = np.nan
    level, _, _ = fx.chained_index(x)
    assert level[-1] == pytest.approx((full[-1] - full[0]).mean()) and level[3] == pytest.approx((full[3] - full[0]).mean())
    # A day with no cell has no value and the chain resumes after it; a late entrant starts from its first price.
    x = np.array([[1.0, np.nan], [np.nan, np.nan], [1.1, 5.0], [1.2, 5.3]])
    level, used, _ = fx.chained_index(x)
    assert math.isnan(level[1]) and level[[0, 2, 3]] == pytest.approx([0.0, 0.1, 0.3]) and used.tolist() == [1, 0, 1, 2]
    with pytest.raises(ValueError):
        fx.chained_index(x, np.array([1.0, 0.0]))


def test_scraper_gaps_and_thin_cells_are_handled_by_rule(tmp_path):
    # Days 8 and 9 were scraped only 20 days ahead; day 15 is missing; on day 3 one cell has 3 of its 7 departure days.
    def listed(t, route, carrier, lead):
        return not (t in (8, 9) and lead > 20) and not (t == 3 and carrier == "AA" and 10 <= lead <= 13)
    built = build(tmp_path, world(28, path, listed=listed, skip_days=(15,)))
    united = series(built, "UA")
    assert sorted(united) == [t for t in range(28) if t != 15]
    for t, (index, n_cells, _) in united.items():
        assert index == pytest.approx(100.0 * math.exp(path(t, "UA") - path(0, "UA")), abs=0.02)
        assert n_cells == (4 if t in (8, 9) else 8)
    assert sorted(series(built, "UA", "21-34")) == [t for t in range(28) if t not in (8, 9, 15)]
    assert sorted(series(built, "AA", "7-13")) == [t for t in range(28) if t not in (3, 15)]
    assert built["cell_counts"][fx.PRIMARY]["UA|all"] == {"cells_seen": 8, "cells_kept": 8, "routes_kept": 2,
                                                         "carriers_kept": ["UA"]}
    with pytest.raises(ValueError):
        fx.IndexSpec(buckets=((7, 13), (13, 20))).check()


def test_profile_and_parquet_extract_agree_with_the_csv(tmp_path):
    rows = world(6, path, skip_days=(4,))
    source = write_quotes(tmp_path / "quotes.csv", rows)
    con = duckdb.connect()
    extracted = fx.extract_slim(con, source, fx.read_header(source), tmp_path / "slim.parquet")
    assert extracted["rows_written"] == len(rows) and extracted["rows_rejected_by_csv_reader"] == 0
    profile = fx.profile(con, fx.quotes_sql(tmp_path / "slim.parquet"))
    assert profile == fx.profile(duckdb.connect(), fx.quotes_sql(source))
    assert profile["rows"] == len(rows) and profile["rows_unparsed_dates"] == profile["rows_unparsed_fares"] == 0
    assert profile["search_dates"]["first"] == START.isoformat() and profile["search_dates"]["days_with_quotes"] == 5
    assert profile["search_dates"]["missing_days"] == [(START + timedelta(days=4)).isoformat()]
    assert profile["airports"] == {"origins": ["EWR", "ORD"], "destinations": ["DEN", "SFO"], "directional_routes": 2}
    assert profile["days_to_departure"] == {"min": 3, "max": 55}
    shares = {r["key"]: r["share"] for r in profile["rows_by_carrier"]}
    assert set(shares) == {"UA", "DL", "AA"} and sum(shares.values()) == pytest.approx(1.0, abs=1e-5)
    assert profile["nonstop_share"] == pytest.approx(1 - 2 * 5 / len(rows), abs=1e-6)
    from_parquet = fx.build_index(con, fx.quotes_sql(tmp_path / "slim.parquet"))
    assert from_parquet["rows"] == fx.build_index(duckdb.connect(), fx.quotes_sql(source))["rows"]


def test_weekly_levels_and_changes_need_enough_days():
    monday = date(2022, 5, 2)
    days = [monday + timedelta(days=i) for i in range(21)]
    values = [100.0] * 7 + [110.0] * 7 + [121.0] * 7
    levels = fx.weekly_levels(days, values, "mean", 5)
    assert fx.weekly_changes(levels) == pytest.approx({monday + timedelta(days=7): math.log(1.1),
                                                       monday + timedelta(days=14): math.log(1.1)})
    assert monday not in fx.weekly_levels(days[3:], values[3:], "mean", 5)       # four days are not a week
    last = fx.weekly_levels(days[:10], values[:10], "last")                    # second week ends on a Wednesday
    assert list(last) == [monday] and fx.week_of(date(2022, 5, 8)) == monday
    with pytest.raises(ValueError):
        fx.weekly_levels(days, values, "median")


def test_lag_tools_find_an_injected_two_week_lag():
    rng = np.random.default_rng(11)
    weeks = [date(2022, 1, 3) + timedelta(weeks=i) for i in range(60)]
    fuel = dict(zip(weeks, rng.normal(0, 0.05, 60)))
    noise = rng.normal(0, 0.002, 60)
    fares = {w: 0.4 * fuel[w - timedelta(weeks=2)] + noise[i] for i, w in enumerate(weeks) if 20 <= i < 43}
    scan = fx.lead_lag(fares, fuel, seed=5, n_boot=300)
    assert [r["lag_weeks"] for r in scan] == list(range(-8, 13)) and {r["n"] for r in scan} == {23}
    best = max(scan, key=lambda r: abs(r["corr"]))
    assert best["lag_weeks"] == 2 and best["corr"] > 0.95 and best["lo"] > 0.8
    assert scan == fx.lead_lag(fares, fuel, seed=5, n_boot=300)
    fit = fx.distributed_lag_fit(fares, fuel, 4)
    assert fit["n_weeks"] == 23 and fit["parameters"] == 6 and fit["df_resid"] == 17
    assert [c["coef"] for c in fit["coefs"]] == pytest.approx([0, 0, 0.4, 0, 0], abs=0.03)
    assert fit["long_run"]["lo"] < 0.4 < fit["long_run"]["hi"] and fit["long_run"]["reading"] == "positive"
    assert fit["cumulative"][1]["sum"] == pytest.approx(0.0, abs=0.03) and fit["control"] is None
    controlled = fx.distributed_lag_fit(fares, fuel, 4, dict(zip(weeks, rng.normal(0, 0.03, 60))))
    assert controlled["parameters"] == 7 and controlled["control"]["lo"] < 0 < controlled["control"]["hi"]
    with pytest.raises(ValueError):                      # a fare week is missing from the middle of the window
        fx.distributed_lag_fit({w: v for w, v in fares.items() if w != weeks[30]}, fuel, 4)
    assert fx.reading(-0.1, 0.2) == "not distinguishable from zero" and fx.reading(-0.3, -0.1) == "negative"


@pytest.fixture()
def case(tmp_path):
    """A complete synthetic case: 11 weeks of quotes whose fares follow fuel two weeks earlier with elasticity 0.5."""
    rng = np.random.default_rng(7)
    fuel_days = [d for d in (date(2021, 11, 1) + timedelta(days=i) for i in range(480))
                 if d.weekday() < 5 and d != date(2022, 5, 30)]   # no print on Memorial Day
    fuel_values = np.round(3.0 * np.exp(np.cumsum(rng.normal(0, 0.012, len(fuel_days)))), 3)
    with (tmp_path / "fuel.csv").open("w") as stream:
        stream.write("date,usd_per_gallon\n" + "".join(f"{d.isoformat()},{v:.3f}\n" for d, v in zip(fuel_days, fuel_values)))
    tsa_days = [date(2022, 1, 1) + timedelta(days=i) for i in range(365)]
    with (tmp_path / "tsa.csv").open("w") as stream:
        stream.write("date,travelers\n" + "".join(
            f"{d.isoformat()},{int(2_000_000 * (1 + 0.1 * math.sin(i / 9.0) + 0.05 * (d.weekday() == 4)))}\n"
            for i, d in enumerate(tsa_days)))
    weekly_fuel = fx.weekly_levels(fuel_days, fuel_values, "mean", 3)
    def fare_path(t, carrier):
        week = fx.week_of(START + timedelta(days=t))
        return 0.5 * (weekly_fuel[week - timedelta(weeks=2)] - weekly_fuel[START - timedelta(weeks=2)])
    quotes = write_quotes(tmp_path / "quotes.csv", world(77, fare_path, extras=lambda t, route, carrier: 0))
    slim = tmp_path / "raw" / "slim.parquet"
    slim.parent.mkdir()
    fx.extract_slim(duckdb.connect(), quotes, fx.read_header(quotes), slim)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"entries": [{**builder.SOURCE, "retrieved_at": "2026-01-01T00:00:00+00:00",
                                                 "sha256": "0" * 64, "bytes": 1, "path": "synthetic/archive.zip"}],
                                    "derived": [{"sha256": fx.file_sha256(slim)}]}))
    args = Namespace(manifest=manifest, slim=slim, fares=tmp_path / "fares.csv", fuel=tmp_path / "fuel.csv",
                     tsa=tmp_path / "tsa.csv", out=tmp_path / "daily_fares_2022.json", memory="1GB", threads=2,
                     generated_at="2026-01-01T00:00:00Z", seed=5, n_boot=200)
    return args, weekly_fuel


def test_builder_is_deterministic_and_the_json_validates(case):
    args, weekly_fuel = case
    outputs = []
    for _ in range(2):
        indexed, published = builder.index(args), builder.publish(args)
        outputs.append((args.fares.read_bytes(), args.out.read_bytes(), args.manifest.read_bytes()))
    assert outputs[0] == outputs[1]
    assert indexed["sha256"] == fx.file_sha256(args.fares) and published["n_days"] == 77

    document = json.loads(args.out.read_text())
    assert fx.validate(document) == [] and "fixture" not in document
    assert document["generated_at"] == "2026-01-01T00:00:00Z" and document["as_of"] == "2022-07-03"
    assert [i["path"].rsplit("/", 1)[-1] for i in document["inputs"]] == ["fares.csv", "fuel.csv", "tsa.csv",
                                                                         "manifest.json", "archive.zip"]
    assert document["inputs"][0]["sha256"] == fx.file_sha256(args.fares)
    assert len(document["daily"]) == 77 and document["n_days"] == 77
    assert document["n_quotes"] == 77 * 5 * len(LEADS)
    no_print = [row["date"] for row in document["daily"] if row["fuel"] is None]
    assert len(no_print) == 22 + 1 and "2022-05-30" in no_print   # weekends and the holiday: no print is invented
    # The daily index is the injected path: half of the log change in weekly fuel two weeks earlier.
    for t in (0, 20, 41, 76):
        week = fx.week_of(START + timedelta(days=t))
        expected = 100.0 * math.exp(0.5 * (weekly_fuel[week - timedelta(weeks=2)] - weekly_fuel[START - timedelta(weeks=2)]))
        assert [document["daily"][t][f"index_{key}"] for key in ("ua", "dl", "aa", "all")] == pytest.approx([expected] * 4, abs=0.03)
    # ... and the lag tools read it back: elasticity 0.5 at a two-week lag, nothing elsewhere.
    for key in ("ua", "all"):
        fit = document["distributed_lag"]["by_series"][key]
        assert [c["coef"] for c in fit["fuel_only"]["coefs"]] == pytest.approx([0, 0, 0.5, 0, 0], abs=0.02)
        assert fit["fuel_only"]["n_weeks"] == 10 and fit["with_demand"]["control"] is not None
        assert fit["with_demand"]["long_run"]["sum"] == pytest.approx(0.5, abs=0.03)
        scan = {r["lag_weeks"]: r for r in document["lead_lag"]["correlations"][key]}
        assert sorted(scan) == list(range(-8, 13)) and scan[2]["corr"] > 0.99
        assert max(scan.values(), key=lambda r: abs(r["corr"]))["lag_weeks"] == 2
    # Words come from the numbers.
    assert f"${document['fuel_path']['peak']['value']:.2f}" in document["headline"]
    assert document["fuel_path"]["peak"]["date"] in document["plain_summary"]
    assert f"{document['fare_path']['ua']['end']['value']:.0f}" in document["headline"]
    assert "CC BY 4.0" in document["limits"][-1] and len(document["limits"]) >= 8
    assert 4 <= document["method"].count(". ") + 1 <= 6 and "7-13, 14-20, 21-34, 35-48" in document["method"]
    assert document["choices"]["primary_variant"] == fx.PRIMARY and document["choices"]["min_coverage"] == 0.95
    assert len(document["variants_tried"]) == len(set(document["variants_tried"])) >= 10

    for damage, word in ((lambda d: d["lead_lag"]["correlations"]["ua"].pop(), "lags"),
                         (lambda d: d["daily"][5].update(index_ua=-1.0), "positive"),
                         (lambda d: d.update(fixture=True), "fixture"),
                         (lambda d: d["daily"][5].update(fuel=float("nan")), "NaN"),
                         (lambda d: d["inputs"][0].update(sha256="abc"), "SHA-256"),
                         (lambda d: d.update(as_of="2022-07-04"), "as_of"),
                         (lambda d: d.update(limits=[]), "missing limits")):
        broken = json.loads(args.out.read_text())
        damage(broken)
        assert any(word in problem for problem in fx.validate(broken)), word


def test_publish_runs_without_the_demand_file_and_index_refuses_a_changed_extract(case):
    args, _ = case
    builder.index(args)
    args.tsa = args.tsa.with_name("absent.csv")
    builder.publish(args)
    document = json.loads(args.out.read_text())
    assert fx.validate(document) == [] and document["distributed_lag"]["demand_control"] is None
    assert all(fit["with_demand"] is None for fit in document["distributed_lag"]["by_series"].values())
    assert len(document["inputs"]) == 4 and "without the TSA traveler control" in document["plain_summary"]
    args.slim.write_bytes(args.slim.read_bytes() + b"x")
    with pytest.raises(SystemExit, match="extract"):
        builder.index(args)
