import csv
from datetime import date
import json
import math
from pathlib import Path

import httpx
import numpy as np
import pytest

from app.lab import market_data
from app.source_store import byte_hash
from scripts.lab import fetch_market
from scripts.lab.fetch_market import (cpi_lines, cross_check, exact, fuel_lines, parse_bls_json, parse_eia_grid,
                                      parse_fred_csv, parse_yahoo_chart, price_lines, write_csv)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "lab_market"
LATER = "2026-10-10T05:16:07+00:00"
JUNETEENTH, JULY_FOURTH = date(2025, 6, 19), date(2025, 7, 4)


def eia_rows():
    blob = json.loads((FIXTURES / "eia_data1_rows.json").read_text())
    return parse_eia_grid(blob["rows"], blob["datemode"])


def fred_fuel_rows():
    return parse_fred_csv((FIXTURES / "fred_DJFUELUSGULF.csv").read_bytes(), "DJFUELUSGULF")


def yahoo_rows(name, ticker, retrieved_at=LATER):
    return parse_yahoo_chart((FIXTURES / f"yahoo_chart_{name}.json").read_bytes(), ticker, retrieved_at)


@pytest.fixture()
def tidy(tmp_path):
    cpi = parse_fred_csv((FIXTURES / "fred_CUSR0000SETG01.csv").read_bytes(), "CUSR0000SETG01")
    paths = {"fuel": tmp_path / "fuel.csv", "prices": tmp_path / "prices.csv", "cpi": tmp_path / "cpi.csv"}
    write_csv(paths["fuel"], ("date", "usd_per_gallon"), fuel_lines(eia_rows()))
    write_csv(paths["prices"], ("date", "ticker", "close", "adj_close"),
              price_lines({"UAL": yahoo_rows("UAL", "UAL"), "HO=F": yahoo_rows("HO_F", "HO=F")}))
    write_csv(paths["cpi"], ("date", "series_id", "value"), cpi_lines({"CUSR0000SETG01": cpi}))
    return paths


def test_eia_grid_keeps_holidays_and_weekends_missing():
    rows = eia_rows()
    dates = [d for d, _ in rows]
    assert len(rows) == 15 and rows[0] == (date(2025, 6, 16), 2.197) and rows[-1] == (date(2025, 7, 8), 2.266)
    assert JUNETEENTH not in dates and JULY_FOURTH not in dates and date(2025, 7, 3) in dates
    assert all(d.weekday() < 5 for d in dates)
    assert fuel_lines(rows)[3] == ("2025-06-20", "2.349") and len(fuel_lines(rows)) == 15


def test_eia_grid_skips_a_blank_price_and_rejects_other_layouts():
    blob = json.loads((FIXTURES / "eia_data1_rows.json").read_text())
    blanked = [list(r) for r in blob["rows"]]
    blanked[5][1] = ""
    assert [d for d, _ in parse_eia_grid(blanked)] == [d for d, _ in eia_rows() if d != date(2025, 6, 18)]
    wrong_series = [list(r) for r in blob["rows"]]
    wrong_series[1][1] = "EER_EPD2F_PF4_Y35NY_DPG"
    text_price = [list(r) for r in blob["rows"]]
    text_price[4][1] = "NA"
    repeated = blob["rows"] + [blob["rows"][-1]]
    for grid, mode in ((wrong_series, 0), (text_price, 0), (repeated, 0), (blob["rows"], 1), (blob["rows"][:3], 0)):
        with pytest.raises(ValueError):
            parse_eia_grid(grid, mode)


def test_fred_csv_drops_blank_days_instead_of_filling_them():
    rows = fred_fuel_rows()
    assert len(rows) == 15 and JUNETEENTH not in dict(rows) and JULY_FOURTH not in dict(rows)
    assert dict(rows)[date(2025, 6, 18)] == 2.35 and dict(rows)[date(2025, 6, 20)] == 2.349
    cpi = parse_fred_csv((FIXTURES / "fred_CUSR0000SETG01.csv").read_bytes(), "CUSR0000SETG01")
    assert [d.isoformat() for d, _ in cpi] == ["2025-07-01", "2025-08-01", "2025-09-01", "2025-11-01", "2025-12-01", "2026-01-01"]
    assert cpi_lines({"CUSR0000SETG01": cpi})[3] == ("2025-11-01", "CUSR0000SETG01", "253.063")
    with pytest.raises(ValueError):
        parse_fred_csv((FIXTURES / "fred_CUSR0000SETG01.csv").read_bytes(), "CPIAUCSL")
    with pytest.raises(ValueError):
        parse_fred_csv(b"<html>blocked</html>", "DJFUELUSGULF")


def test_yahoo_chart_parser_drops_empty_bars_and_the_retrieval_day():
    ual, heating_oil = yahoo_rows("UAL", "UAL"), yahoo_rows("HO_F", "HO=F")
    assert len(ual) == 15 and ual[0] == (date(2025, 6, 16), 78.52, 78.519997) and ual[-1][0] == date(2025, 7, 8)
    assert JUNETEENTH not in {r[0] for r in ual} | {r[0] for r in heating_oil}
    # The futures bar on the equity holiday is Yahoo's own print and is kept; the null bar is not replaced.
    assert len(heating_oil) == 16 and dict((r[0], r[1]) for r in heating_oil)[JULY_FOURTH] == 2.409
    # 12:00 UTC on 8 July is 8 July in New York; 03:00 UTC is still 7 July there.
    assert yahoo_rows("UAL", "UAL", "2025-07-08T12:00:00+00:00")[-1][0] == date(2025, 7, 7)
    assert yahoo_rows("UAL", "UAL", "2025-07-08T03:00:00+00:00")[-1][0] == date(2025, 7, 3)
    with pytest.raises(ValueError):
        yahoo_rows("UAL", "DAL")
    with pytest.raises(ValueError):
        parse_yahoo_chart(b'{"chart": {"result": null, "error": {"code": "Not Found"}}}', "UAL", LATER)
    blob = json.loads((FIXTURES / "yahoo_chart_UAL.json").read_text())
    blob["chart"]["result"][0]["indicators"]["adjclose"][0]["adjclose"][2] = None
    with pytest.raises(ValueError):
        parse_yahoo_chart(json.dumps(blob).encode(), "UAL", LATER)


def test_price_lines_are_sorted_by_date_then_ticker():
    lines = price_lines({"UAL": yahoo_rows("UAL", "UAL"), "HO=F": yahoo_rows("HO_F", "HO=F")})
    assert lines == sorted(lines) and len(lines) == 31 and len({line[:2] for line in lines}) == 31
    assert lines[0] == ("2025-06-16", "HO=F", "2.3933", "2.393300") and lines[1] == ("2025-06-16", "UAL", "78.5200", "78.519997")
    assert [line[1] for line in lines if line[0] == "2025-07-04"] == ["HO=F"]


def test_bls_parser_skips_the_unpublished_month():
    series = parse_bls_json((FIXTURES / "bls_timeseries.json").read_bytes())
    assert set(series) == set(market_data.CPI_SERIES)
    airfare = series["CUSR0000SETG01"]
    assert len(airfare) == 6 and date(2025, 10, 1) not in dict(airfare) and airfare[-1] == (date(2026, 1, 1), 279.693)
    fred = parse_fred_csv((FIXTURES / "fred_CUSR0000SETG01.csv").read_bytes(), "CUSR0000SETG01")
    check = cross_check(airfare, fred, 0.0005)
    assert check["overlap"] == 6 and check["max_abs_diff"] == 0 and not check["mismatches"]
    with pytest.raises(ValueError):
        parse_bls_json(b'{"status": "REQUEST_NOT_PROCESSED", "message": ["daily threshold reached"], "Results": {}}')


def test_cross_check_counts_overlap_and_lists_every_disagreement():
    eia, fred = eia_rows(), fred_fuel_rows()
    clean = cross_check(eia, fred, 0.0005)
    assert clean["overlap"] == 15 and clean["max_abs_diff"] == 0 and clean["max_abs_diff_date"] is None
    assert clean["mismatches"] == clean["only_in_a"] == clean["only_in_b"] == []
    shifted = [(d, v + 0.004 if d == date(2025, 6, 24) else v) for d, v in fred if d != date(2025, 7, 8)]
    check = cross_check([r for r in eia if r[0] != date(2025, 6, 16)], shifted, 0.0005)
    assert check["overlap"] == 13 and check["max_abs_diff"] == 0.004 and check["max_abs_diff_date"] == "2025-06-24"
    assert check["mismatches"] == [{"date": "2025-06-24", "a": 2.11, "b": 2.114}]
    assert check["only_in_a"] == ["2025-07-08"] and check["only_in_b"] == ["2025-06-16"]
    # The same 0.4 cent gap agrees "to the cent".
    assert cross_check(eia, shifted, 0.005)["mismatches"] == []
    clipped = cross_check(eia, fred, 0.0005, start=date(2025, 7, 1), end=date(2025, 7, 7))
    assert clipped["overlap"] == 4 and (clipped["first"], clipped["last"]) == ("2025-07-01", "2025-07-07")


def test_source_values_are_never_rounded_silently():
    assert exact(2.35, 3) == "2.350"
    with pytest.raises(ValueError):
        exact(2.3504, 3)


def test_loaders_return_dates_and_arrays_without_filling(tidy):
    dates, values = market_data.load_fuel_daily(tidy["fuel"])
    assert isinstance(values, np.ndarray) and len(dates) == len(values) == 15 and dates[0] == date(2025, 6, 16)
    assert JUNETEENTH not in dates and values[3] == 2.349
    dates, close = market_data.load_prices("HO=F", "close", tidy["prices"])
    assert len(dates) == 16 and JULY_FOURTH in dates and close[0] == 2.3933
    dates, adjusted = market_data.load_prices("UAL", path=tidy["prices"])
    assert len(dates) == 15 and adjusted[0] == 78.519997
    dates, index = market_data.load_cpi("CUSR0000SETG01", tidy["cpi"])
    assert date(2025, 10, 1) not in dates and len(dates) == 6 and index[-1] == 279.693
    for call in (lambda: market_data.load_prices("DAL", path=tidy["prices"]),
                 lambda: market_data.load_prices("UAL", "open", tidy["prices"]),
                 lambda: market_data.load_cpi("CUUR0000SETG01", tidy["cpi"]),
                 lambda: market_data.load_fuel_daily(tidy["cpi"])):
        with pytest.raises(ValueError):
            call()


def test_fuel_window_mean_reports_the_prints_it_used(tidy):
    mean, prints = market_data.fuel_window_mean("2025-06-18", date(2025, 6, 22), tidy["fuel"])
    assert prints == 2 and mean == pytest.approx((2.35 + 2.349) / 2)
    empty, prints = market_data.fuel_window_mean(JUNETEENTH, JUNETEENTH, tidy["fuel"])
    assert prints == 0 and math.isnan(empty)
    assert market_data.fuel_window_mean("2025-06-16", "2025-07-08", tidy["fuel"])[1] == 15
    with pytest.raises(ValueError):
        market_data.fuel_window_mean("2025-07-08", "2025-06-16", tidy["fuel"])


def test_quarterly_mean_fuel_splits_the_window_at_quarter_ends(tidy):
    second, third, fourth = market_data.quarterly_mean_fuel("2025-06-16", "2025-10-02", tidy["fuel"])
    assert second == {"period": "2025Q2", "period_start": "2025-04-01", "period_end": "2025-06-30",
                      "usd_per_gallon": pytest.approx(22.053 / 10), "n_prints": 10,
                      "first_print": "2025-06-16", "last_print": "2025-06-30"}
    assert third["period"] == "2025Q3" and third["n_prints"] == 5 and third["last_print"] == "2025-07-08"
    assert third["usd_per_gallon"] == pytest.approx(11.088 / 5)
    assert fourth == {"period": "2025Q4", "period_start": "2025-10-01", "period_end": "2025-12-31",
                      "usd_per_gallon": None, "n_prints": 0, "first_print": None, "last_print": None}
    # A window that stops inside the quarter only uses the prints up to its end.
    assert market_data.quarterly_mean_fuel("2025-07-01", "2025-07-02", tidy["fuel"])[0]["n_prints"] == 2


def test_log_returns_and_alignment_on_common_dates(tidy):
    dates, returns = market_data.daily_log_returns("UAL", prices_path=tidy["prices"])
    assert len(dates) == len(returns) == 14 and dates[0] == date(2025, 6, 17)
    assert returns[0] == pytest.approx(math.log(73.67 / 78.52), abs=1e-6)
    dates, fuel_returns = market_data.daily_log_returns(market_data.FUEL, fuel_path=tidy["fuel"])
    assert dates[0] == date(2025, 6, 17) and fuel_returns[0] == pytest.approx(math.log(2.304 / 2.197))

    names = ["UAL", "HO=F", market_data.FUEL]
    dates, levels = market_data.aligned_levels(names, fuel_path=tidy["fuel"], prices_path=tidy["prices"])
    assert len(dates) == 15 and levels.shape == (15, 3) and JULY_FOURTH not in dates
    dates, returns, gaps = market_data.aligned_log_returns(names, fuel_path=tidy["fuel"], prices_path=tidy["prices"])
    assert returns.shape == (14, 3) and len(dates) == len(gaps) == 14
    by_date = dict(zip(dates, gaps.tolist()))
    assert by_date[date(2025, 6, 20)] == 2 and by_date[date(2025, 6, 23)] == 3 and by_date[date(2025, 7, 7)] == 4
    # Across the 4 July gap every column spans 3 July to 7 July, although heating oil printed in between.
    assert returns[dates.index(date(2025, 7, 7))] == pytest.approx(
        [math.log(81.11 / 82.36), math.log(2.4211 / 2.3698), math.log(2.24 / 2.192)], abs=1e-6)


def test_download_stores_raw_bytes_and_records_failures(tmp_path):
    served = {"id=DJFUELUSGULF": "fred_DJFUELUSGULF.csv", "id=CUSR0000SETG01": "fred_CUSR0000SETG01.csv",
              "id=CUUR0000SETG01": "fred_CUSR0000SETG01.csv",  # Wrong series: stored, but must not be accepted.
              "chart/UAL?": "yahoo_chart_UAL.json", "chart/HO%3DF?": "yahoo_chart_HO_F.json",
              "api.bls.gov": "bls_timeseries.json"}
    seen = []

    def handler(request):
        seen.append(request)
        name = next((v for k, v in served.items() if k in str(request.url)), None)
        return httpx.Response(200, content=(FIXTURES / name).read_bytes()) if name else httpx.Response(503)

    old = {"source": "eia_fuel", "url": "u", "retrieved_at": "t", "sha256": "0" * 64, "bytes": 1, "path": "p"}
    manifest = fetch_market.download({"entries": [old]}, httpx.Client(transport=httpx.MockTransport(handler)),
                                     raw=tmp_path / "raw", pause=0)
    accepted = {e["source"]: e for e in manifest["entries"]}
    assert set(accepted) == {"eia_fuel", "fred_fuel", "yahoo:UAL", "yahoo:HO=F", "fred_cpi:CUSR0000SETG01", "bls_api"}
    assert accepted["eia_fuel"] == old
    for source, entry in accepted.items():
        if source == "eia_fuel":
            continue
        assert {"url", "retrieved_at", "sha256", "bytes", "path", "terms_note"} <= set(entry)
        stored = Path(entry["path"])
        assert stored.name == f"{entry['sha256']}.{stored.suffix[1:]}" and stored.parent == tmp_path / "raw"
        assert byte_hash(stored.read_bytes()) == entry["sha256"] and stored.stat().st_size == entry["bytes"]
        assert entry["retrieved_at"].endswith("+00:00")
    assert Path(accepted["yahoo:UAL"]["path"]).read_bytes() == (FIXTURES / "yahoo_chart_UAL.json").read_bytes()
    assert "unofficial" in accepted["yahoo:UAL"]["terms_note"].lower()

    failures = {f["source"]: f for f in manifest["failures"]}
    assert set(failures) == {"eia_fuel", "yahoo:DAL", "yahoo:AAL", "yahoo:LUV", "yahoo:SPY", "yahoo:JETS",
                             "fred_cpi:CUUR0000SETG01", "fred_cpi:CUSR0000SA0"}
    assert failures["eia_fuel"]["kept_previous_entry"] and "503" in failures["eia_fuel"]["error"]
    assert failures["fred_cpi:CUUR0000SETG01"]["raw_kept_at"] and not failures["fred_cpi:CUSR0000SA0"]["raw_kept_at"]
    assert sum("eia.gov" in str(r.url) for r in seen) == 3
    bls = next(r for r in seen if r.method == "POST")
    assert json.loads(bls.content) == fetch_market.BLS_BODY
    assert {r.headers["user-agent"] for r in seen if "yahoo" in str(r.url)} == {"Mozilla/5.0"}

    manifest["entries"] = [e for e in manifest["entries"] if e is not old]
    outputs = [tmp_path / "fuel.csv", tmp_path / "prices.csv", tmp_path / "cpi.csv"]
    result = fetch_market.build(manifest, *outputs)
    assert [item["path"] for item in result["not_built"]] == [str(p) for p in outputs]
    assert result["outputs"] == [] and not any(p.exists() for p in outputs)


def read(path):
    with path.open(newline="") as stream:
        rows = list(csv.reader(stream))
    return tuple(rows[0]), [tuple(r) for r in rows[1:]]


def assert_tidy(path, header, key_width):
    found, rows = read(path)
    keys = [r[:key_width] for r in rows]
    assert found == header and rows
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    assert all(len(r) == len(header) and all(cell.strip() == cell and cell for cell in r) for r in rows)
    values = [float(cell) for r in rows for cell in r[key_width:]]
    assert all(math.isfinite(v) and v > 0 for v in values)
    return rows


def test_committed_fuel_csv_is_tidy_and_agrees_with_the_existing_2026_file():
    rows = assert_tidy(market_data.FUEL_CSV, ("date", "usd_per_gallon"), 1)
    days = [date.fromisoformat(r[0]) for r in rows]
    assert date(2010, 1, 1) <= days[0] <= date(2010, 1, 8) and all(d.weekday() < 5 for d in days)
    fuel = {r[0]: float(r[1]) for r in rows}
    _, legacy = read(ROOT / "data" / "eia_gulf_coast_2026.csv")
    assert len(legacy) == 190
    assert [d for d, _ in legacy if d not in fuel] == []
    assert [(d, v, fuel[d]) for d, v in legacy if round(abs(float(v) - fuel[d]), 6) >= 0.005] == []
    assert [d for d in fuel if legacy[0][0] <= d <= legacy[-1][0]] == [d for d, _ in legacy]


def test_committed_price_and_cpi_csvs_are_tidy():
    rows = assert_tidy(market_data.PRICES_CSV, ("date", "ticker", "close", "adj_close"), 2)
    first = {}
    for day, ticker, *_ in rows:
        first.setdefault(ticker, day)
    assert set(first) == set(market_data.TICKERS)
    assert all("2012-01-01" <= day <= "2012-01-10" for ticker, day in first.items() if ticker != "JETS")
    assert first["JETS"] >= "2015-04-30"
    assert all(date.fromisoformat(r[0]).weekday() < 5 for r in rows)

    rows = assert_tidy(market_data.CPI_CSV, ("date", "series_id", "value"), 2)
    assert {r[1] for r in rows} == set(market_data.CPI_SERIES)
    assert rows[0][0] == "2010-01-01" and all(r[0].endswith("-01") for r in rows)


def test_manifest_lists_every_raw_file_and_matches_the_committed_csvs():
    manifest = json.loads(fetch_market.MANIFEST.read_text())
    expected = ({"eia_fuel", "fred_fuel", "bls_api"} | {f"yahoo:{t}" for t in market_data.TICKERS}
                | {f"fred_cpi:{s}" for s in market_data.CPI_SERIES})
    assert {e["source"] for e in manifest["entries"]} == expected
    assert manifest["failures"] == [] and manifest["not_built"] == []
    for entry in manifest["entries"]:
        assert entry["url"].startswith("https://") and entry["retrieved_at"].endswith("+00:00") and entry["bytes"] > 0
        assert entry["terms_note"] and Path(entry["path"]).stem == entry["sha256"]
        assert entry["path"].startswith("data/lab_raw/market/")
        assert ("unofficial" in entry["terms_note"].lower()) == entry["source"].startswith("yahoo:")
    for output in manifest["outputs"]:
        assert byte_hash((ROOT / output["path"]).read_bytes()) == output["sha256"]
    checks = manifest["cross_checks"]
    for check in (checks["eia_vs_fred_fuel"], checks["fuel_vs_eia_gulf_coast_2026_csv"], *checks["bls_vs_fred_cpi"].values()):
        assert check["overlap"] > 0 and check["mismatches"] == []
    assert checks["fuel_vs_eia_gulf_coast_2026_csv"]["overlap"] == 190


def test_offline_rebuild_reproduces_the_committed_csvs(tmp_path):
    pytest.importorskip("xlrd")
    manifest = json.loads(fetch_market.MANIFEST.read_text())
    if not all((ROOT / e["path"]).exists() for e in manifest["entries"]):
        pytest.skip("raw market downloads are not in this checkout (data/lab_raw is untracked)")
    outputs = [tmp_path / "fuel.csv", tmp_path / "prices.csv", tmp_path / "cpi.csv"]
    result = fetch_market.build(manifest, *outputs)
    assert result["not_built"] == [] and result["cross_checks"] == manifest["cross_checks"]
    for rebuilt, committed in zip(outputs, (market_data.FUEL_CSV, market_data.PRICES_CSV, market_data.CPI_CSV)):
        assert rebuilt.read_bytes() == committed.read_bytes()
