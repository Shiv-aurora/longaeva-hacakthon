import csv
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import re

import httpx
import numpy as np
import pytest

from app.lab import filings
from app.source_store import byte_hash
from scripts.lab import fetch_ual_filings as fetch

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "lab_filings"
RELEASES = {"2013-03-31": "release_2013q1.htm", "2015-06-30": "release_2015q2.htm", "2017-12-31": "release_2017q4.htm",
            "2020-06-30": "release_2020q2.htm", "2022-09-30": "release_2022q3.htm", "2026-06-30": "release_2026q2.htm"}
NO_ADJUSTED_PRETAX = filings.expected_quarters("2013-03-31", "2014-09-30")
NO_PROFIT_SHARING = ["2021-03-31", "2021-06-30", "2023-03-31", "2023-06-30"]


def fixture(name):
    return (FIXTURES / name).read_bytes()


def table(path):
    with open(path, newline="") as stream:
        return list(csv.DictReader(stream))


@pytest.fixture(scope="module")
def quarterly():
    return table(filings.QUARTERLY_CSV)


@pytest.fixture(scope="module")
def sources():
    return json.loads(filings.SOURCES_JSON.read_text())


@pytest.fixture(scope="module")
def index():
    return table(filings.INDEX_CSV)


@pytest.fixture(scope="module")
def manifest():
    return json.loads(fetch.MANIFEST.read_text())


def values(name):
    return {field: cell["value"] for field, cell in filings.parse_release(fixture(name))["cells"].items()}


def test_fixtures_are_the_recorded_cuts_of_real_documents():
    records = {r["file"]: r for r in json.loads((FIXTURES / "sources.json").read_text())}
    assert set(records) == {p.name for p in FIXTURES.iterdir()} - {"sources.json"}
    for name, record in records.items():
        content = fixture(name)
        assert (byte_hash(content), len(content)) == (record["sha256"], record["bytes"])
        assert record["url"].startswith("https://www.sec.gov/Archives/edgar/data/100517/")
        assert b"@" not in content


def test_number_reads_brackets_dashes_and_leaves_text_alone():
    assert filings.number("17,672") == Decimal("17672")
    assert filings.number("(465") == filings.number("(465)") == Decimal("-465")
    assert filings.number("$ (1.87)") == Decimal("-1.87")
    assert filings.number("(0.2%)") == Decimal("-0.2")
    assert filings.number("—") == 0
    assert all(filings.number(text) is None for text in ("NM", "$", ")", "%", "9:52", "pts.", "#DIV/0!", ""))


def test_labels_match_without_quotes_or_footnote_marks():
    assert filings.norm('Total revenue per available seat mile ("TRASM") (cents)') == \
        "total revenue per available seat mile (trasm) (cents)"
    assert filings.norm("CASM-ex (cents) (h)") == "casm-ex (cents)"
    assert filings.norm("Passenger: (A)") == "passenger"
    assert filings.norm("CASM (GAAP)4") == "casm (gaap)"


@pytest.mark.parametrize("period, name", sorted(RELEASES.items()))
def test_release_fixture_parses_to_the_committed_row_and_sources(period, name, quarterly, sources):
    parsed = filings.parse_release(fixture(name))
    row = next(r for r in quarterly if r["period_end"] == period)
    assert parsed["period_end"] == period == filings.release_period(fixture(name))
    assert {f: parsed["cells"].get(f, {}).get("value", "") for f in filings.VALUE_COLUMNS} == \
        {f: row[f] for f in filings.VALUE_COLUMNS}
    for field, cell in parsed["cells"].items():
        assert {k: v for k, v in sources[period][field].items() if k not in ("accession", "filename")} == cell


def test_current_format_release_gives_every_column():
    assert values("release_2026q2.htm") == {
        "asm_m": "87279", "rpm_m": "72765", "total_revenue_musd": "17672", "passenger_revenue_musd": "16100",
        "trasm_cents": "20.25", "prasm_cents": "18.45", "casm_cents": "18.99", "casm_ex_cents": "13.12",
        "fuel_gallons_m": "1219", "fuel_price_usd_gal": "4.19", "fuel_expense_musd": "5110",
        "pretax_income_musd": "1026", "adj_pretax_income_musd": "843", "net_income_musd": "805",
        "gaap_eps_diluted": "2.46", "adj_eps_diluted": "1.99", "diluted_shares_m": "326.6",
        "profit_sharing_musd": "90"}  # The CASM table's "Profit sharing" row reads 0.10 cents.


def test_sectioned_release_takes_consolidated_rows_and_converts_cents():
    cells = filings.parse_release(fixture("release_2013q1.htm"))["cells"]
    assert (cells["asm_m"]["value"], cells["rpm_m"]["value"]) == ("57372", "46544")  # Mainline: 49,654 and 40,292.
    assert cells["fuel_price_usd_gal"] == {
        "value": "3.301", "label": "Average aircraft fuel price per gallon (cents) (c)",
        "column": "Three Months Ended March 31, 2013", "printed": "330.1", "note": "printed in cents, divided by 100"}
    assert (cells["gaap_eps_diluted"]["label"], cells["gaap_eps_diluted"]["value"]) == \
        ("Loss per share, basic and diluted", "-1.26")
    assert "adj_pretax_income_musd" not in cells  # Only a twelve-month figure is printed, in the ROIC table.


def test_year_row_shifted_against_the_period_row_still_reads_the_current_year():
    cells = filings.parse_release(fixture("release_2015q2.htm"))["cells"]
    assert {f: cells[f]["value"] for f in ("adj_pretax_income_musd", "adj_eps_diluted", "profit_sharing_musd")} == \
        {"adj_pretax_income_musd": "1264", "adj_eps_diluted": "3.31", "profit_sharing_musd": "198"}
    assert cells["adj_pretax_income_musd"]["column"] == "Three Months Ended June 30, 2015"


def test_fourth_quarter_release_takes_the_quarter_not_the_year():
    cells = filings.parse_release(fixture("release_2017q4.htm"))["cells"]
    assert cells["total_revenue_musd"]["value"] == "9438"  # The year column reads 37,736.
    assert {cell["column"] for cell in cells.values()} == {"Three Months Ended December 31, 2017"}
    assert cells["adj_eps_diluted"]["value"] == "1.40" and cells["gaap_eps_diluted"]["value"] == "1.99"


def test_losses_split_across_cells_and_a_dash_for_nil():
    cells = filings.parse_release(fixture("release_2020q2.htm"))["cells"]
    assert [cells[f]["value"] for f in ("pretax_income_musd", "net_income_musd", "gaap_eps_diluted", "adj_eps_diluted")] \
        == ["-2003", "-1627", "-5.79", "-9.31"]
    assert (cells["profit_sharing_musd"]["value"], cells["profit_sharing_musd"]["printed"]) == ("0", "—")


def test_three_year_columns_read_the_first_year():
    cells = filings.parse_release(fixture("release_2022q3.htm"))["cells"]
    assert cells["trasm_cents"]["column"] == "Three Months Ended September 30, 2022"
    assert (cells["asm_m"]["value"], cells["trasm_cents"]["value"], cells["fuel_price_usd_gal"]["value"]) == \
        ("67695", "19.02", "3.81")


def test_untouched_vendor_markup_reads_the_same():
    old = filings.release_tables(fixture("verbatim_2013q1_statistics.htm"))[0]
    new = filings.release_tables(fixture("verbatim_2026q2_statistics.htm"))[0]
    seats = next(line for line in old if line["label"] == "Available seat miles (millions)")
    assert seats["scope"] == "consolidated"
    assert filings.quarter_value(seats, "2013-03-31") == (Decimal("57372"), "57,372", "Three Months Ended March 31, 2013")
    assert filings.quarter_value(seats, "2013-06-30") is None
    seats = next(line for line in new if line["label"] == "ASMs (millions) (c)")
    assert filings.quarter_value(seats, "2026-06-30") == (Decimal("87279"), "87,279", "Three Months Ended June 30, 2026")


def test_exhibit_kind_follows_the_title_rules():
    release = fixture("release_2026q2.htm")
    assert filings.classify_exhibit(release, "2.02,7.01,9.01") == "earnings_release"
    assert filings.classify_exhibit(release, "7.01,9.01") == "other"
    assert filings.classify_exhibit(fixture("verbatim_investor_update_2014.htm"), "2.02,7.01,9.01") == "investor_update"
    assert filings.classify_exhibit(fixture("verbatim_webcast_2013.htm"), "2.02,9.01") == "other"
    assert filings.title_text(fixture("verbatim_investor_update_2014.htm")).startswith("Investor Update Issue Date:")


def test_primary_document_kind_needs_guidance_in_its_own_words():
    assert filings.classify_primary(fixture("verbatim_8k_2019_guidance.htm")) == "investor_update"
    assert "continues to expect second-quarter 2019" in filings.item_text(fixture("verbatim_8k_2019_guidance.htm"))
    assert filings.classify_primary(fixture("verbatim_8k_2013_cover.htm")) == "other"  # It points at an exhibit.


def test_quarterly_table_is_complete_unique_and_ordered(quarterly):
    assert tuple(quarterly[0]) == filings.QUARTERLY_COLUMNS
    ends = [row["period_end"] for row in quarterly]
    assert ends == filings.expected_quarters("2013-03-31", "2026-06-30") and len(ends) == 54
    for row in quarterly:
        assert 10 <= (date.fromisoformat(row["filed_on"]) - date.fromisoformat(row["period_end"])).days <= 45
        assert re.fullmatch(r"\d{10}-\d{2}-\d{6}", row["accession"])


def test_quarterly_table_passes_the_arithmetic_checks(quarterly):
    checks = filings.arithmetic_checks(quarterly)
    assert {c["check"] for c in checks} == {"fuel_expense = gallons x price", "trasm = total_revenue / asm",
                                            "prasm = passenger_revenue / asm"}
    assert len(checks) == 3 * len(quarterly)
    assert [c for c in checks if abs(c["difference"]) > 0.02] == []
    report = filings.arithmetic_report(quarterly)
    assert all(entry["compared"] == 54 and entry["outside"] == [] for entry in report.values())


def test_arithmetic_check_flags_a_wrong_cell_and_skips_a_blank_one(quarterly):
    row = dict(quarterly[-1], fuel_gallons_m="1000", trasm_cents="")
    (flagged,) = filings.arithmetic_report([row])["fuel_expense = gallons x price"]["outside"]
    assert flagged["period_end"] == "2026-06-30" and flagged["difference"] == pytest.approx(4190 / 5110 - 1, abs=1e-5)
    assert set(filings.arithmetic_report([row])) == {"fuel_expense = gallons x price", "prasm = passenger_revenue / asm"}


def test_blank_cells_are_the_ones_the_releases_leave_out(quarterly):
    blank = {f: [r["period_end"] for r in quarterly if r[f] == ""] for f in filings.VALUE_COLUMNS}
    assert {f: ends for f, ends in blank.items() if ends} == \
        {"adj_pretax_income_musd": NO_ADJUSTED_PRETAX, "profit_sharing_musd": NO_PROFIT_SHARING}
    assert filings.coverage(quarterly)["asm_m"] == 54 and filings.coverage(quarterly)["adj_pretax_income_musd"] == 47


def test_every_filled_cell_has_a_source_and_no_blank_one_does(quarterly, sources):
    assert list(sources) == [row["period_end"] for row in quarterly]
    for row in quarterly:
        cells = sources[row["period_end"]]
        assert set(cells) == {f for f in filings.VALUE_COLUMNS if row[f] != ""}
        for field, cell in cells.items():
            assert cell["accession"] == row["accession"] and cell["filename"] in row["source_note"]
            assert cell["value"] == row[field] and cell["label"] and cell["column"].startswith("Three Months Ended")
            assert date.fromisoformat(row["period_end"]).strftime("%B %-d, %Y") in cell["column"]
            printed = filings.number(cell["printed"])
            assert Decimal(cell["value"]) == (printed / 100 if "note" in cell else printed)


def test_second_quarter_2026_matches_the_repository_anchor(quarterly):
    anchor = json.loads((ROOT / "data" / "ual_q2_2026.json").read_text())
    row = next(r for r in quarterly if r["period_end"] == anchor["period_end"])
    pairs = {"asm_m": "asm_million", "rpm_m": "rpm_million", "fuel_gallons_m": "fuel_gallons_million",
             "fuel_price_usd_gal": "average_paid_fuel_usd_per_gallon", "trasm_cents": "trasm_cents_per_asm",
             "prasm_cents": "prasm_cents_per_asm", "casm_cents": "casm_cents_per_asm",
             "passenger_revenue_musd": "passenger_revenue_million", "total_revenue_musd": "total_operating_revenue_million",
             "fuel_expense_musd": "reported_fuel_expense_million"}
    assert {field: Decimal(row[field]) for field in pairs} == {f: Decimal(str(anchor[key])) for f, key in pairs.items()}
    assert row["filed_on"] == anchor["published_on"] and fetch.anchor_check(quarterly)["all_match"]


def test_index_lists_each_release_once_and_only_known_kinds(index, quarterly):
    assert tuple(index[0]) == filings.INDEX_COLUMNS
    assert {row["kind"] for row in index} == {"earnings_release", "investor_update", "other"}
    assert len({(row["accession"], row["filename"]) for row in index}) == len(index)
    assert [row["filed_on"] for row in index] == sorted(row["filed_on"] for row in index)
    releases = {row["period_end"]: row for row in index if row["kind"] == "earnings_release"}
    assert len(releases) == sum(row["kind"] == "earnings_release" for row in index)
    assert sorted(releases)[1:] == [row["period_end"] for row in quarterly]  # The first reports 2012Q4.
    for row in quarterly:
        assert releases[row["period_end"]]["accession"] == row["accession"]
        assert releases[row["period_end"]]["filed_on"] == row["filed_on"]
    for row in index:
        assert re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) and row["path"].startswith("data/lab_raw/sec/")
        assert row["url"].endswith("/" + row["filename"]) and row["accession"].replace("-", "") in row["url"]
        assert (row["period_end"] != "") == (row["kind"] == "earnings_release")
        assert row["exhibit"] == row["form"] or re.fullmatch(r"EX-99(\.\d+)?", row["exhibit"])


def test_index_selection_follows_the_stated_rule(index):
    by_filing = {}
    for row in index:
        by_filing.setdefault(row["accession"], []).append(row)
    for rows in by_filing.values():
        assert "2.02" in rows[0]["items"].split(",") or any(r["kind"] == "investor_update" for r in rows)
        for row in rows:
            if row["exhibit"] == row["form"]:  # A primary document: guidance in its own text, or nothing else filed.
                assert row["kind"] == "investor_update" or len(rows) == 1
    updates = {row["filed_on"][:4] for row in index if row["kind"] == "investor_update"}
    assert updates == {str(year) for year in range(2013, 2027)}


def test_manifest_points_at_every_indexed_file_and_holds_no_contact_address(manifest, index):
    entries = {entry["path"]: entry for entry in manifest["entries"]}
    assert len(entries) == len(manifest["entries"]) and manifest["failures"] == []
    for entry in manifest["entries"]:
        assert {"kind", "url", "retrieved_at", "sha256", "bytes", "path"} <= set(entry)
        assert entry["kind"] in {"submissions", "index", "filing_index", "primary", "exhibit"}
        assert entry["url"].startswith(("https://www.sec.gov/Archives/edgar/data/100517/", "https://data.sec.gov/"))
        assert not any("agent" in key.lower() for key in entry)
    for row in index:
        assert entries[row["path"]]["sha256"] == row["sha256"] and entries[row["path"]]["url"] == row["url"]
    assert not any("agent" in key.lower() for key in manifest)
    for path in (fetch.MANIFEST, filings.INDEX_CSV, filings.QUARTERLY_CSV, filings.SOURCES_JSON):
        assert "@" not in path.read_text()  # No address of any kind reaches a tracked output.


def test_loader_returns_floats_and_none_for_blank_cells():
    rows = filings.load_ual_quarterly()
    assert len(rows) == 54 and (rows[0]["quarter"], rows[-1]["quarter"]) == ("2013Q1", "2026Q2")
    last = rows[-1]
    assert (last["period_end"], last["filed_on"]) == (date(2026, 6, 30), date(2026, 7, 15))
    assert (last["asm_m"], last["fuel_price_usd_gal"], last["adj_eps_diluted"]) == (87279.0, 4.19, 1.99)
    assert rows[0]["adj_pretax_income_musd"] is None and rows[0]["profit_sharing_musd"] == 0.0
    ends, series = filings.quarterly_series("adj_pretax_income_musd")
    assert ends[0] == date(2013, 3, 31) and int(np.isnan(series).sum()) == 7 and series[-1] == 843.0
    with pytest.raises(ValueError, match="Unknown quarterly column"):
        filings.quarterly_series("period_end")


def test_loader_refuses_repeated_or_unordered_quarters(tmp_path, quarterly):
    for name, rows in (("repeated.csv", quarterly[:2] + quarterly[1:3]), ("unordered.csv", quarterly[:3][::-1])):
        fetch.write_csv(tmp_path / name, filings.QUARTERLY_COLUMNS, rows)
        with pytest.raises(ValueError, match="not unique, ordered"):
            filings.load_ual_quarterly(tmp_path / name)
    (tmp_path / "header.csv").write_text("period_end,asm_m\n2013-03-31,1\n")
    with pytest.raises(ValueError, match="header"):
        filings.load_ual_quarterly(tmp_path / "header.csv")


def test_candidates_are_recent_8ks_with_a_results_or_disclosure_item():
    def page(rows):
        keys = ("accessionNumber", "filingDate", "reportDate", "form", "items")
        return {key: [row[i] for row in rows] for i, key in enumerate(keys)}
    recent = {"filings": {"recent": page([("0000100517-26-000135", "2026-07-15", "2026-07-15", "8-K", "2.02,7.01,9.01"),
                                           ("0000100517-26-000140", "2026-07-16", "2026-06-30", "10-Q", ""),
                                           ("0000100517-26-000090", "2026-05-01", "2026-05-01", "8-K", "5.02"),
                                           ("0000100517-24-000093", "2024-05-28", "2024-05-28", "8-K", "7.01")])}}
    older = page([("0001193125-12-000001", "2012-12-20", "2012-12-20", "8-K", "2.02"),
                  ("0001193125-13-132548", "2013-03-28", "2013-03-28", "8-K/A", "8.01,9.01"),
                  ("0000100517-24-000093", "2024-05-28", "2024-05-28", "8-K", "7.01")])
    assert [row["accessionNumber"] for row in fetch.candidates([recent, older], "2013-01-01")] == \
        ["0001193125-13-132548", "0000100517-24-000093", "0000100517-26-000135"]


def test_filing_documents_are_typed_by_the_index_page():
    page, listing = fixture("verbatim_filing_index_2026.html"), fixture("index_2026.json")
    assert [(d["kind"], d["exhibit"], d["filename"]) for d in fetch.filing_documents(page, listing, "8-K")] == [
        ("primary", "8-K", "ual-20260715.htm"), ("exhibit", "EX-99.1", "ual_erx06302026xex991.htm"),
        ("exhibit", "EX-99.2", "ual_erx06302026xex992.htm")]
    with pytest.raises(ValueError, match="exactly one primary"):
        fetch.filing_documents(page, listing, "8-K/A")
    blob = json.loads(listing)
    blob["directory"]["item"] = [i for i in blob["directory"]["item"] if i["name"] != "ual_erx06302026xex992.htm"]
    with pytest.raises(ValueError, match="not a plain file name in index.json"):
        fetch.filing_documents(page, json.dumps(blob).encode(), "8-K")


def local_manifest(tmp_path, documents):
    entries = []
    for accession, filed_on, items, kind, exhibit, name in documents:
        target = tmp_path / "raw" / accession / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fixture(name))
        entries.append({"kind": kind, "accession": accession, "filed_on": filed_on, "form": "8-K", "items": items,
                        "exhibit": exhibit, "filename": name, "url": f"https://www.sec.gov/Archives/x/{name}",
                        "sha256": byte_hash(fixture(name)), "bytes": len(fixture(name)),
                        "path": f"raw/{accession}/{name}"})
    return {"entries": entries}


def test_index_and_quarterly_rebuild_offline_from_a_manifest(tmp_path, monkeypatch, quarterly):
    monkeypatch.setattr(fetch, "ROOT", tmp_path)
    manifest = local_manifest(tmp_path, [
        ("0000100517-26-000135", "2026-07-15", "2.02,7.01,9.01", "primary", "8-K", "verbatim_8k_2013_cover.htm"),
        ("0000100517-26-000135", "2026-07-15", "2.02,7.01,9.01", "exhibit", "EX-99.1", "release_2026q2.htm"),
        ("0000100517-26-000135", "2026-07-15", "2.02,7.01,9.01", "exhibit", "EX-99.2", "verbatim_investor_update_2014.htm"),
        ("0001104659-19-030655", "2019-05-21", "7.01", "primary", "8-K", "verbatim_8k_2019_guidance.htm"),
        ("0001193125-13-402459", "2013-10-17", "8.01,9.01", "primary", "8-K", "verbatim_8k_2013_cover.htm"),
        ("0001193125-13-402459", "2013-10-17", "8.01,9.01", "exhibit", "EX-99.1", "verbatim_webcast_2013.htm"),
        ("0001193125-13-172096", "2013-01-24", "2.02,9.01", "exhibit", "EX-99.1", "release_2013q1.htm")])
    manifest["entries"][-1]["filename"] = "d526478dex991.htm"
    rows, content = fetch.build_index(manifest)
    assert [(r["filed_on"], r["exhibit"], r["kind"], r["period_end"]) for r in rows] == [
        ("2013-01-24", "EX-99.1", "earnings_release", "2013-03-31"),
        ("2019-05-21", "8-K", "investor_update", ""),
        ("2026-07-15", "EX-99.1", "earnings_release", "2026-06-30"),
        ("2026-07-15", "EX-99.2", "investor_update", "")]
    content[("0000100517-26-999999", "release_2026q2.htm")] = fixture("release_2026q2.htm")
    table_rows, cell_sources, notes = fetch.build_quarterly(rows + [dict(rows[2], accession="0000100517-26-999999")], content)
    assert [r["period_end"] for r in table_rows] == ["2013-03-31", "2026-06-30"]
    assert table_rows[1] == quarterly[-1] | {"source_note": "EX-99.1 release_2026q2.htm"}
    assert cell_sources["2026-06-30"]["fuel_price_usd_gal"]["accession"] == "0000100517-26-000135"
    assert notes == ["0000100517-26-999999 release_2026q2.htm: second release for 2026-06-30, not used"]
    manifest["entries"][1]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="failed its checksum"):
        fetch.build_index(manifest)


def test_a_release_before_the_table_starts_is_noted_not_used(monkeypatch):
    monkeypatch.setattr(filings, "FIRST_QUARTER", "2013-06-30")
    entry = {"kind": "earnings_release", "accession": "0001193125-13-172096", "filename": "d526478dex991.htm",
             "filed_on": "2013-04-25", "exhibit": "EX-99.1"}
    rows, _, notes = fetch.build_quarterly([entry], {(entry["accession"], entry["filename"]): fixture("release_2013q1.htm")})
    assert rows == [] and notes == ["0001193125-13-172096 d526478dex991.htm: reports 2013-03-31, before the table starts"]


class Refuse:
    def get(self, url):
        raise AssertionError(f"unexpected request for {url}")


def test_store_fetches_once_then_reads_the_held_file(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "ROOT", tmp_path)

    class Once:
        def get(self, url):
            return b"<html>held</html>", "2026-10-09T00:00:00+00:00"
    url, target = "https://www.sec.gov/Archives/edgar/data/100517/x/a.htm", tmp_path / "raw" / "x" / "a.htm"
    first = fetch.Store(None, Once(), refresh=False)
    assert first.get(url, target, kind="exhibit", accession="x") == b"<html>held</html>" and first.fetched == 1
    assert first.entries == [{"kind": "exhibit", "accession": "x", "url": url, "retrieved_at": "2026-10-09T00:00:00+00:00",
                              "sha256": byte_hash(b"<html>held</html>"), "bytes": 17, "path": "raw/x/a.htm"}]
    again = fetch.Store({"entries": first.entries}, Refuse(), refresh=False)
    assert again.get(url, target, kind="exhibit", accession="x") == b"<html>held</html>" and again.fetched == 0
    assert again.entries == first.entries
    target.write_bytes(b"<html>changed</html>")
    with pytest.raises(ValueError, match="failed its checksum"):
        fetch.Store({"entries": first.entries}, Refuse(), refresh=False).get(url, target)
    with pytest.raises(AssertionError, match="unexpected request"):
        fetch.Store({"entries": first.entries}, Refuse(), refresh=True).get(url, target)


def test_edgar_declares_the_agent_follows_redirects_and_retries(monkeypatch):
    waits, seen = [], []
    monkeypatch.setattr(fetch.time, "sleep", waits.append)
    answers = iter([httpx.Response(429), httpx.Response(503),
                    httpx.Response(301, headers={"Location": "https://www.sec.gov/moved.htm"}),
                    httpx.Response(200, content=b"body")])

    def handler(request):
        seen.append((str(request.url), request.headers["User-Agent"]))
        return next(answers)
    edgar = fetch.Edgar("lab test agent", transport=httpx.MockTransport(handler))
    content, stamp = edgar.get("https://www.sec.gov/a.htm")
    assert content == b"body" and stamp.endswith("+00:00")
    assert [url for url, _ in seen] == ["https://www.sec.gov/a.htm"] * 3 + ["https://www.sec.gov/moved.htm"]
    assert {agent for _, agent in seen} == {"lab test agent"}
    assert [w for w in waits if w >= 1] == [1, 2]  # Doubling waits after 429 and 503; the rest is pacing.
    missing = fetch.Edgar("lab test agent", transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    with pytest.raises(httpx.HTTPStatusError):
        missing.get("https://www.sec.gov/none.htm")


def test_requests_are_spaced_under_four_per_second(monkeypatch):
    clock, waits = [100.0], []
    monkeypatch.setattr(fetch.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(fetch.time, "sleep", lambda seconds: (waits.append(seconds), clock.__setitem__(0, clock[0] + seconds)))
    edgar = fetch.Edgar("lab test agent", transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x")))
    for _ in range(3):
        edgar.get("https://www.sec.gov/a.htm")
    assert waits[1:] == [pytest.approx(fetch.MIN_GAP)] * 2 and fetch.MIN_GAP > 0.25


def test_script_stops_without_a_declared_user_agent(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setattr(fetch.sys, "argv", ["fetch_ual_filings.py"])
    monkeypatch.setattr(fetch, "download", lambda *args: pytest.fail("download reached without a user agent"))
    with pytest.raises(SystemExit, match="SEC_USER_AGENT"):
        fetch.main()
