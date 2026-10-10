import csv
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re

import numpy as np
import pytest

from app.lab import tsa
from app.source_store import byte_hash
from scripts.lab import fetch_tsa
from scripts.lab.fetch_tsa import BASE, merge_pages, parse_page

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "lab_tsa"
CUTS = {BASE: "page_current_2026.html", f"{BASE}/2025": "page_2025.html", f"{BASE}/2024": "page_2024.html",
        f"{BASE}/2020": "page_2020.html", f"{BASE}/2019": "page_2019.html"}
TODAY = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc)
ARCHIVE_NOTE = "Archived Data has been updated including Known Crewmembers and later flight travel numbers."
# The body tsa.gov's edge returned with HTTP 403 to Python httpx on 2026-10-09.
DENIED = (b'<HTML><HEAD>\n<TITLE>Access Denied</TITLE>\n</HEAD><BODY>\n<H1>Access Denied</H1>\n \nYou don\'t have '
          b'permission to access "http&#58;&#47;&#47;www&#46;tsa&#46;gov&#47;travel&#47;passenger&#45;volumes" on this '
          b'server.<P>\nReference&#32;&#35;18&#46;aee82d17&#46;1791610469&#46;a20ed6a1\n</BODY>\n</HTML>\n')
UAL_HEADER = ("period_end,filed_on,accession,asm_m,rpm_m,total_revenue_musd,passenger_revenue_musd,trasm_cents,"
              "prasm_cents,casm_cents,casm_ex_cents,fuel_gallons_m,fuel_price_usd_gal,fuel_expense_musd,"
              "pretax_income_musd,adj_pretax_income_musd,net_income_musd,gaap_eps_diluted,adj_eps_diluted,"
              "diluted_shares_m,profit_sharing_musd,source_note").split(",")


def cut(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def page(rows, header=("Date", "Numbers"), extra="") -> bytes:
    """A made-up page in the layout of the real ones. Its numbers are test inputs, not TSA counts."""
    head = "".join(f'<th style="text-align:center;">{h}</th>' for h in header)
    body = "".join("<tr>" + "".join(f'<td class="text-align-center">{c}</td>' for c in row) + "</tr>" for row in rows)
    return (f"<!DOCTYPE html><html><head><title>Made-up page</title></head><body><article><p>Made-up note.</p>"
            f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{extra}</article></body></html>").encode()


def listing(rows) -> list[tuple[str, str]]:
    return [(f"{d.month}/{d.day}/{d.year}", f"{v:,}") for d, v in rows]


def made_up_count(day: date) -> int:
    """Test input: a level per year with a pandemic dip, plus a weekday and a seasonal pattern."""
    level = {2019: 1.00, 2020: 0.40, 2021: 0.70, 2022: 0.90, 2023: 1.02, 2024: 1.06, 2025: 1.07, 2026: 1.05}[day.year]
    weekday = 0.05 * (day.weekday() in (3, 4, 6))
    return int(2_000_000 * level * (1 + weekday + 0.02 * math.sin(day.timetuple().tm_yday / 58)))


def days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def made_up_history(last: date = date(2026, 10, 8)) -> dict[str, bytes]:
    """URL -> page for 2019-01-01..last: per-year pages ascending, the running year descending."""
    served = {f"{BASE}/{year}": page(listing((d, made_up_count(d)) for d in days(date(year, 1, 1), date(year, 12, 31))))
              for year in range(2019, last.year)}
    served[BASE] = page(listing((d, made_up_count(d)) for d in reversed(days(date(last.year, 1, 1), last))))
    return served


def serve(pages: dict[str, bytes], calls: list | None = None):
    def get(url):
        if calls is not None:
            calls.append(url)
        if url in pages:
            return 200, pages[url]
        return (404, b"<html><body>Page not found</body></html>") if url == f"{BASE}/2026" else (403, DENIED)
    return get


def fetch(pages, raw, previous=None, today=TODAY):
    return fetch_tsa.download(previous, get=serve(pages), raw=raw, pause=0, today=today, sleep=lambda s: None)


def write_tidy(path: Path, rows) -> Path:
    fetch_tsa.write_csv(path, ("date", "travelers"), [(d.isoformat(), str(v)) for d, v in rows])
    return path


def write_united(path: Path, rows: list[dict]) -> Path:
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=UAL_HEADER, restval="", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def united_rows() -> list[dict]:
    """Made-up United rows for 2018Q4..2026Q2 with one blank cell and one quarter filed twice."""
    rows = []
    for year in range(2018, 2027):
        for quarter, end in enumerate(("03-31", "06-30", "09-30", "12-31"), start=1):
            if (year, quarter) < (2018, 4) or (year, quarter) > (2026, 2):
                continue
            step = 4 * (year - 2018) + quarter
            rows.append({"period_end": f"{year}-{end}", "filed_on": f"{year + (quarter == 4)}-{(3 * quarter) % 12 + 1:02d}-20",
                         "asm_m": str(60000 + 700 * step + 300 * (step % 3)), "rpm_m": str(50000 + 500 * step + 900 * (step % 2)),
                         "total_revenue_musd": str(9000 + 100 * step), "trasm_cents": f"{15 + 0.1 * (step % 5):.2f}"})
    next(r for r in rows if r["period_end"] == "2025-03-31")["trasm_cents"] = ""
    rows.append({**next(r for r in rows if r["period_end"] == "2024-06-30"), "filed_on": "2024-09-01", "asm_m": "99999"})
    return rows


def check_payload(payload: dict, first_quarter: str | None = "2019Q1") -> None:
    """The shape every demand_signal.json must have."""
    json.dumps(payload, allow_nan=False)
    assert "fixture" not in payload
    assert datetime.fromisoformat(payload["generated_at"]).utcoffset() == timedelta(0)
    assert date.fromisoformat(payload["as_of"]) == date.fromisoformat(payload["coverage"]["last"])
    assert payload["inputs"] and all(set(i) >= {"path", "sha256"} and re.fullmatch(r"[0-9a-f]{64}", i["sha256"])
                                     for i in payload["inputs"])
    for key in ("headline", "plain_summary"):
        assert isinstance(payload[key], str) and payload[key]
        assert not re.search(r"\b(strong|weak|sharp|robust|significant|soft|healthy|solid)", payload[key], re.I)
    daily = payload["daily"]
    assert all(set(row) == {"date", "travelers_7d", "yoy_7d"} for row in daily)
    assert [r["date"] for r in daily] == sorted({r["date"] for r in daily})
    assert not daily or daily[0]["date"] >= "2025-01-01"
    for name, label in (("quarterly", "period"), ("monthly_control", "month")):
        rows = payload[name]
        assert rows and all({label, "mean_daily_travelers", "yoy", "days_used", "pandemic"} <= set(r) for r in rows)
        assert all(isinstance(r["pandemic"], bool) and 0 < r["days_used"] <= r["days_in_period"] for r in rows)
        assert [r[label] for r in rows] == sorted({r[label] for r in rows})
    if first_quarter:
        assert payload["quarterly"][0]["period"] == first_quarter and payload["monthly_control"][0]["month"] == "2019-01"
    note = payload["current_quarter_note"]
    assert {"2026Q3", "2026Q2", "text"} <= set(note) and note["2026Q3"]["months"] == "July to September 2026"
    assert len(payload["limits"]) >= 6 and all(isinstance(text, str) and text for text in payload["limits"])
    assert (payload["versus_united"] is None) == isinstance(payload["versus_united_reason"], str)


def test_parser_reads_cuts_of_the_real_pages_in_each_layout():
    current, info = parse_page(cut("page_current_2026.html"))
    assert info["listed_order"] == "descending" and [d for d, _ in current] == sorted(d for d, _ in current)
    assert len(current) == 11 and current[0] == (date(2026, 1, 1), 2334465) and current[-1] == (date(2026, 10, 8), 2901564)
    assert info["page_title"] == "TSA checkpoint travel numbers | Transportation Security Administration"
    assert info["page_notes"][0].startswith("Passenger travel numbers are updated Monday through Friday")

    rows_2025, info_2025 = parse_page(cut("page_2025.html"), 2025)  # <table class="table">, oldest first.
    assert info_2025["listed_order"] == "ascending" and info_2025["page_notes"] == [ARCHIVE_NOTE]
    assert len(rows_2025) == 13 and rows_2025[0] == (date(2025, 1, 1), 2317817) and rows_2025[-1] == (date(2025, 12, 31), 2291360)

    rows_2024 = dict(parse_page(cut("page_2024.html"), 2024)[0])  # Bare <table>, leap year.
    assert rows_2024[date(2024, 2, 29)] == 2551534 and rows_2024[date(2024, 3, 1)] == 2602686
    rows_2020 = dict(parse_page(cut("page_2020.html"), 2020)[0])  # Six-digit counts.
    assert rows_2020[date(2020, 4, 14)] == 113147 and rows_2020[date(2020, 12, 31)] == 914456
    rows_2019, info_2019 = parse_page(cut("page_2019.html"), 2019)
    assert rows_2019 == [(date(2019, 1, 1), 2201765), (date(2019, 1, 2), 2424225),
                         (date(2019, 12, 30), 2572305), (date(2019, 12, 31), 2073706)]
    assert info_2019 == {"rows": 4, "first": "2019-01-01", "last": "2019-12-31", "listed_order": "ascending",
                         "page_title": "2019 TSA checkpoint travel numbers | Transportation Security Administration",
                         "page_notes": [ARCHIVE_NOTE]}


def test_fixture_provenance_lists_every_cut_and_its_dates():
    provenance = json.loads((FIXTURES / "provenance.json").read_text())["fixtures"]
    assert set(provenance) == set(CUTS.values())
    for url, name in CUTS.items():
        rows, _ = parse_page(cut(name))
        kept = [date(int(y), int(m), int(d)) for m, d, y in (text.split("/") for text in provenance[name]["kept_dates"])]
        assert provenance[name]["url"] == url and sorted(kept) == [d for d, _ in rows]
        assert re.fullmatch(r"[0-9a-f]{64}", provenance[name]["raw_sha256"])


def test_parser_refuses_what_it_does_not_recognise():
    good = [("10/7/2026", "2,377,978"), ("10/8/2026", "2,901,564")]
    assert parse_page(page(good))[0] == [(date(2026, 10, 7), 2377978), (date(2026, 10, 8), 2901564)]
    # A second <title> (the real pages label SVG icons with one) does not leak into the page title.
    assert parse_page(page(good, extra="<svg><title>Lock</title></svg>"))[1]["page_title"] == "Made-up page"
    # The same row twice is one day; the same date with two numbers is not resolved by guessing.
    assert parse_page(page(good + [good[0]]))[0] == parse_page(page(good))[0]
    refused = [
        page(good + [("10/7/2026", "2,377,979")]),                        # one date, two numbers
        page([("10/8/2026", "2,901,564", "2,800,000")]),                  # three cells
        page(good, header=("Date", "2026", "2025")),                      # another table layout
        page([("2026-10-08", "2,901,564")]),                              # another date format
        page([("2/30/2026", "2,901,564")]),                               # impossible date
        page([("10/8/2026", "N/A")]), page([("10/8/2026", "")]), page([("10/8/2026", "0")]),
        page([("10/8/2026", "2,901,564.5")]), page([("10/8/2026", "-5")]),
        page([]),                                                         # table without rows
        page(good) + page(good),                                          # two tables
        DENIED, b"\xff\xfe not utf-8",
    ]
    for content in refused:
        with pytest.raises(ValueError):
            parse_page(content)
    with pytest.raises(ValueError):
        parse_page(cut("page_2019.html"), 2020)  # A per-year page may only list its own year.
    with pytest.raises(ValueError):
        parse_page(page(good + [("1/1/2027", "5")]), 2026)


def entry(source, seen, sha, year=None):
    return {"source": source, "year": year, "sha256": sha, "last_seen_at": seen, "retrieved_at": seen}


def test_overlapping_pages_follow_the_revision_rule():
    d1, d2, d3 = date(2025, 12, 30), date(2025, 12, 31), date(2026, 1, 1)
    running = (entry("tsa:current", "2026-01-05T09:00:00+00:00", "a" * 64), [(d1, 100), (d2, 200), (d3, 300)])
    archive = (entry("tsa:2025", "2026-01-05T09:00:03+00:00", "b" * 64, 2025), [(d1, 100), (d2, 210)])
    # Same run: the per-year page was requested later and wins; agreement on d1 is not a revision.
    for order in ([running, archive], [archive, running]):
        rows, revisions, origin = merge_pages(order)
        assert rows == [(d1, 100), (d2, 210), (d3, 300)]
        assert revisions == [{"date": "2025-12-31", "travelers": 210,
                              "taken_from": {"source": "tsa:2025", "sha256": "b" * 64, "last_seen_at": "2026-01-05T09:00:03+00:00"},
                              "not_used": [{"travelers": 200, "source": "tsa:current", "sha256": "a" * 64,
                                            "last_seen_at": "2026-01-05T09:00:00+00:00"}]}]
        assert origin == {d1: "b" * 64, d2: "b" * 64, d3: "a" * 64}
    # An exact tie in time also goes to the per-year page.
    tied = (entry("tsa:current", "2026-01-05T09:00:03+00:00", "a" * 64), running[1])
    assert merge_pages([archive, tied])[0][1] == (d2, 210) and merge_pages([tied, archive])[0][1] == (d2, 210)
    # A later run: the page seen last wins whichever kind it is, and every other number is listed.
    later = (entry("tsa:current", "2026-01-06T09:00:00+00:00", "c" * 64), [(d2, 220), (d3, 305)])
    rows, revisions, origin = merge_pages([later, running, archive])
    assert rows == [(d1, 100), (d2, 220), (d3, 305)] and origin[d1] == "b" * 64 and origin[d3] == "c" * 64
    assert [(r["date"], r["travelers"], sorted(n["travelers"] for n in r["not_used"])) for r in revisions] == [
        ("2025-12-31", 220, [200, 210]), ("2026-01-01", 305, [300])]
    # A date only an older copy lists is kept as that copy has it; no date is invented.
    assert [d for d, _ in merge_pages([later])[0]] == [d2, d3]


def test_download_paces_requests_and_reports_refusals(tmp_path):
    calls, sleeps = [], []
    pages = {url: cut(name) for url, name in CUTS.items()}
    manifest = fetch_tsa.download(None, get=serve(pages, calls), raw=tmp_path / "raw", pause=1.0, today=TODAY,
                                  sleep=sleeps.append)
    assert calls == [BASE] + [f"{BASE}/{year}" for year in range(2026, 2018, -1)] and sleeps == [1.0] * 8
    assert [e["source"] for e in manifest["entries"]] == ["tsa:current", "tsa:2025", "tsa:2024", "tsa:2020", "tsa:2019"]
    for item in manifest["entries"]:
        stored = Path(item["path"])
        assert stored.name == item["sha256"] + ".html" and byte_hash(stored.read_bytes()) == item["sha256"]
        assert item["bytes"] == stored.stat().st_size and item["url"] in CUTS and item["http_status"] == 200
        assert datetime.fromisoformat(item["retrieved_at"]).utcoffset() == timedelta(0)
    # The running year has no page of its own yet: recorded, not a failure. A refusal is reported as returned.
    assert [(n["source"], n["http_status"]) for n in manifest["not_published"]] == [("tsa:2026", 404)]
    assert [f["source"] for f in manifest["failures"]] == ["tsa:2023", "tsa:2022", "tsa:2021"]
    refusal = manifest["failures"][0]
    assert refusal["http_status"] == 403 and refusal["returned_bytes"] == len(DENIED) and "HTTP 403" in refusal["error"]
    assert refusal["returned_excerpt"].startswith("<HTML><HEAD> <TITLE>Access Denied</TITLE>")
    assert refusal["returned_sha256"] == byte_hash(DENIED) and refusal["kept_previous_entry"] is False
    assert manifest["revision_rule"] == fetch_tsa.REVISION_RULE and "No browser is imitated" in manifest["access_note"]

    # Same bytes again: no new entry. New bytes for one page: one more entry, and the older copy stays indexed.
    again = fetch(pages, tmp_path / "raw", manifest)
    assert len(again["entries"]) == 5 and all(e["last_seen_at"] >= e["retrieved_at"] for e in again["entries"])
    changed = {**pages, BASE: page([("10/8/2026", "2,901,565"), ("10/9/2026", "2,500,000")])}
    third = fetch(changed, tmp_path / "raw", again)
    assert [e["source"] for e in third["entries"]].count("tsa:current") == 2 and len(third["entries"]) == 6
    # A page that is refused or does not parse leaves the stored entries alone and says what happened.
    broken = fetch({**pages, BASE: page([("10/11/2026", "2,500,000")]), f"{BASE}/2025": DENIED}, tmp_path / "raw", third)
    assert [(e["source"], e["sha256"]) for e in broken["entries"]] == [(e["source"], e["sha256"]) for e in third["entries"]]
    errors = {f["source"]: f for f in broken["failures"]}
    assert "after the retrieval date" in errors["tsa:current"]["error"] and errors["tsa:current"]["kept_previous_entry"]
    assert "0 tables" in errors["tsa:2025"]["error"] and errors["tsa:2025"]["raw_kept_at"].endswith(".html")


def test_download_retries_a_server_error_but_not_a_refusal(tmp_path):
    answers = {BASE: [(503, b"busy"), (503, b"busy"), (200, cut("page_current_2026.html"))]}
    calls, sleeps = [], []

    def get(url):
        calls.append(url)
        return answers[url].pop(0) if answers.get(url) else (403, DENIED)

    manifest = fetch_tsa.download(None, get=get, raw=tmp_path / "raw", pause=1.0, today=TODAY, sleep=sleeps.append)
    assert calls.count(BASE) == 3 and sleeps[:2] == [2.0, 4.0] and [e["source"] for e in manifest["entries"]] == ["tsa:current"]
    assert all(calls.count(f"{BASE}/{year}") == 1 for year in range(2019, 2027))  # 403 is asked once and reported.
    assert len(manifest["failures"]) == 8


def test_offline_build_from_the_real_cuts_invents_no_day(tmp_path):
    manifest = fetch({url: cut(name) for url, name in CUTS.items()}, tmp_path / "raw")
    paths = {"csv_path": tmp_path / "tsa_daily.csv", "json_path": tmp_path / "demand_signal.json",
             "ual_csv": tmp_path / "absent.csv"}
    result = fetch_tsa.build(manifest, now=NOW, **paths)
    cut_rows = sorted(row for name in CUTS.values() for row in parse_page(cut(name))[0])
    dates, values = tsa.load_tsa_daily(paths["csv_path"])
    assert list(zip(dates, values.astype(int))) == cut_rows and len(dates) == 39 == len(set(dates))
    assert paths["csv_path"].read_text().splitlines()[:2] == ["date,travelers", "2019-01-01,2201765"]
    assert result["coverage"]["days"] == 39 and result["revisions"] == [] and result["not_built"] == []
    assert result["coverage"]["missing_ranges"][0] == {"first": "2019-01-03", "last": "2019-12-29", "days": 361}
    assert result["coverage"]["missing_days"] == (date(2026, 10, 8) - date(2019, 1, 1)).days + 1 - 39

    payload = json.loads(paths["json_path"].read_text())
    check_payload(payload)
    assert payload["generated_at"] == "2026-10-09T06:00:00+00:00" and payload["as_of"] == "2026-10-08"
    assert payload["versus_united"] is None and "does not exist" in payload["versus_united_reason"]
    assert payload["inputs"][0]["sha256"] == byte_hash(paths["csv_path"].read_bytes()) == result["outputs"][0]["sha256"]
    assert {i["sha256"] for i in payload["inputs"][1:]} == {e["sha256"] for e in manifest["entries"]}
    # The 7-day average needs all seven calendar days; the growth rate needs the window 364 days earlier too.
    by_day, daily = dict(cut_rows), {row["date"]: row for row in payload["daily"]}
    week = [by_day[date(2026, 10, 2) + timedelta(days=i)] for i in range(7)]
    year_ago = [by_day[date(2025, 10, 3) + timedelta(days=i)] for i in range(7)]
    assert date(2026, 10, 8) - timedelta(days=364) == date(2025, 10, 9)
    assert daily["2026-10-08"] == {"date": "2026-10-08", "travelers_7d": round(sum(week) / 7, 1),
                                   "yoy_7d": round(sum(week) / sum(year_ago) - 1, 6)}
    assert daily["2026-10-07"]["yoy_7d"] is not None and daily["2025-10-09"]["travelers_7d"] is not None
    assert daily["2026-10-06"] == {"date": "2026-10-06", "travelers_7d": None, "yoy_7d": None}  # 30 September is cut.
    assert daily["2025-10-09"]["yoy_7d"] is None and daily["2026-01-03"]["travelers_7d"] is None
    # No count in July to September 2026 in these cuts: the note says so and carries no number.
    assert payload["current_quarter_note"]["2026Q3"]["yoy"] is None and "reason" in payload["current_quarter_note"]["2026Q3"]
    assert "2026Q3 has no year-over-year figure" in payload["headline"]
    assert any(ARCHIVE_NOTE in text for text in payload["limits"])

    # Rebuilding from the same store gives the same files; a raw file that changed is refused.
    before = (paths["csv_path"].read_bytes(), paths["json_path"].read_bytes())
    fetch_tsa.build(json.loads(json.dumps(manifest)), now=NOW, **paths)
    assert (paths["csv_path"].read_bytes(), paths["json_path"].read_bytes()) == before
    Path(manifest["entries"][0]["path"]).write_bytes(cut("page_2025.html"))
    with pytest.raises(ValueError):
        fetch_tsa.build(manifest, now=NOW, **paths)
    assert fetch_tsa.build({"entries": []}, now=NOW, **paths)["not_built"]


def test_builder_works_with_and_without_the_united_file(tmp_path):
    served = made_up_history()
    # The running-year page still lists 31 December 2025 with another number: the per-year page outranks it.
    overlap = date(2025, 12, 31)
    running = [(d, made_up_count(d)) for d in reversed(days(date(2026, 1, 1), date(2026, 10, 8)))] + [(overlap, 1_234_567)]
    served[BASE] = page(listing(running))
    manifest = fetch(served, tmp_path / "raw")
    assert not manifest["failures"] and len(manifest["entries"]) == 8
    paths = {"csv_path": tmp_path / "tsa_daily.csv", "json_path": tmp_path / "demand_signal.json"}

    without = fetch_tsa.build(manifest, ual_csv=tmp_path / "ual_quarterly.csv", now=NOW, **paths)
    dates, values = tsa.load_tsa_daily(paths["csv_path"])
    assert len(dates) == 2838 and without["coverage"]["missing_days"] == 0
    assert dict(zip(dates, values))[overlap] == made_up_count(overlap)
    assert [(r["date"], r["travelers"], r["taken_from"]["source"], r["not_used"][0]["travelers"])
            for r in without["revisions"]] == [("2025-12-31", made_up_count(overlap), "tsa:2025", 1_234_567)]
    payload = json.loads(paths["json_path"].read_text())
    check_payload(payload)
    assert payload["versus_united"] is None and without["outputs"][1]["versus_united"] == payload["versus_united_reason"]
    assert "was not computed" in payload["plain_summary"] and len(payload["inputs"]) == 9

    united_csv = write_united(tmp_path / "ual_quarterly.csv", united_rows())
    with_file = fetch_tsa.build(manifest, ual_csv=united_csv, now=NOW, **paths)
    payload = json.loads(paths["json_path"].read_text())
    check_payload(payload)
    assert with_file["outputs"][1]["versus_united"] == "computed" and payload["versus_united_reason"] is None
    assert payload["inputs"][-1] == {"path": str(united_csv), "sha256": byte_hash(united_csv.read_bytes())}
    table = {row["period"]: row for row in payload["versus_united"]["rows"]}
    assert list(table)[0] == "2020Q1" and list(table)[-1] == "2026Q3" and len(table) == 27
    levels = {r["period_end"]: r for r in united_rows()[:-1]}
    assert table["2025Q2"]["ual_rpm_yoy"] == round(int(levels["2025-06-30"]["rpm_m"]) / int(levels["2024-06-30"]["rpm_m"]) - 1, 6)
    # The quarter filed twice uses the later filing; a blank cell blanks the two growth rates that need it.
    assert table["2024Q2"]["ual_asm_yoy"] == round(99999 / int(levels["2023-06-30"]["asm_m"]) - 1, 6)
    assert table["2025Q1"]["ual_trasm_yoy"] is None and table["2026Q1"]["ual_trasm_yoy"] is None
    assert table["2025Q1"]["ual_asm_yoy"] is not None and table["2026Q3"]["ual_asm_yoy"] is None
    # Made-up levels: 2020-2022 sit below 2019 and 2023 is back above it.
    quarters = {q["period"]: q for q in payload["quarterly"]}
    assert [p for p, q in quarters.items() if q["pandemic"]] == [f"{y}Q{k}" for y in (2020, 2021, 2022) for k in (1, 2, 3, 4)]
    assert [p for p, q in quarters.items() if q["yoy_base_pandemic"]] == ["2023Q1", "2023Q2", "2023Q3", "2023Q4"]
    correlation = payload["versus_united"]["correlation"]
    clean = [r for r in table.values() if not r["pandemic"] and not r["yoy_base_pandemic"] and r["ual_asm_yoy"] is not None]
    assert (correlation["sample"], correlation["n"], correlation["first"], correlation["last"]) == ("non_pandemic", 10, "2024Q1", "2026Q2")
    assert correlation["tsa_vs_ual_asm"] == {
        "r": round(float(np.corrcoef([r["tsa_yoy"] for r in clean], [r["ual_asm_yoy"] for r in clean])[0, 1]), 4), "n": 10}
    assert correlation["tsa_vs_ual_trasm"]["n"] == 8 and correlation["tsa_vs_ual_rpm"]["n"] == 10
    assert [s["n"] for s in payload["versus_united"]["other_samples"]] == [14, 26]
    assert f"(n={correlation['n']})" in payload["plain_summary"]

    # A file without the expected columns is reported, not guessed at.
    united_csv.write_text("period_end,asm_m\n2026-06-30,1\n")
    fetch_tsa.build(manifest, ual_csv=united_csv, now=NOW, **paths)
    payload = json.loads(paths["json_path"].read_text())
    assert payload["versus_united"] is None and "could not be read" in payload["versus_united_reason"]


def test_trailing_average_and_weekday_growth_use_only_past_data():
    dates = days(date(2024, 1, 1), date(2026, 10, 8))
    values = np.array([made_up_count(d) for d in dates], dtype=float)
    average = tsa.trailing_mean(dates, values)
    growth = tsa.growth_vs_days_earlier(dates, average)
    assert np.isnan(average[:6]).all() and average[6] == values[:7].mean() and average[-1] == values[-7:].mean()
    assert np.isnan(growth[:370]).all() and np.isfinite(growth[370:]).all()
    index = {d: i for i, d in enumerate(dates)}
    for i in (370, 500, len(dates) - 1):
        j = index[dates[i] - timedelta(days=364)]
        assert dates[j].weekday() == dates[i].weekday() and growth[i] == average[i] / average[j] - 1
    # Changing every later count leaves each earlier value as it was, and so does cutting the series off.
    cutoff = 700
    changed = values.copy()
    changed[cutoff + 1:] *= 3
    for series in (tsa.trailing_mean(dates, changed), tsa.trailing_mean(dates[:cutoff + 1], values[:cutoff + 1])):
        assert np.array_equal(series[:cutoff + 1], average[:cutoff + 1], equal_nan=True)
    assert np.array_equal(tsa.growth_vs_days_earlier(dates, tsa.trailing_mean(dates, changed))[:cutoff + 1],
                          growth[:cutoff + 1], equal_nan=True)
    # A pure weekday pattern shows no growth at 364 days and a spurious one at 365.
    pattern = np.array([1000.0 + 100 * d.weekday() for d in dates])
    assert np.nanmax(np.abs(tsa.growth_vs_days_earlier(dates, pattern, 364))) == 0
    assert np.nanmax(np.abs(tsa.growth_vs_days_earlier(dates, pattern, 365))) > 0.05
    # A missing day is not bridged: the six averages after it are absent, and so is growth measured against them.
    hole = 400
    gapped_dates, gapped_values = dates[:hole] + dates[hole + 1:], np.delete(values, hole)
    gapped = tsa.trailing_mean(gapped_dates, gapped_values)
    assert np.isnan(gapped[hole:hole + 6]).all() and gapped[hole + 6] == average[hole + 7] and gapped[hole - 1] == average[hole - 1]
    gapped_growth = tsa.growth_vs_days_earlier(gapped_dates, gapped)
    assert np.isnan(gapped_growth[gapped_dates.index(dates[hole + 3] + timedelta(days=364))])
    assert tsa.missing_ranges(gapped_dates) == [{"first": dates[hole].isoformat(), "last": dates[hole].isoformat(), "days": 1}]


def test_month_and_quarter_means_count_the_days_they_use():
    dates = days(date(2019, 1, 1), date(2021, 2, 10))
    # Ten percent more each year on the same calendar day, with a slope inside the year.
    values = np.array([1.1 ** (d.year - 2019) * (1000 + d.timetuple().tm_yday) for d in dates])
    by_day = dict(zip(dates, values))
    months = {m["period"]: m for m in tsa.period_means(dates, values, "month")}
    quarters = {q["period"]: q for q in tsa.period_means(dates, values, "quarter")}
    assert list(months)[0] == "2019-01" and list(months)[-1] == "2021-02" and len(months) == 26 and len(quarters) == 9
    assert months["2019-06"]["yoy"] is None and months["2019-06"]["days_used"] == 30 and months["2019-06"]["complete"]
    assert months["2020-01"]["yoy"] == pytest.approx(0.1) and months["2020-01"]["yoy_basis"] == "whole period a year earlier"
    # Leap year: 29 days against 28, 91 against 90, each mean over the days its own year has.
    february = [by_day[d] for d in days(date(2020, 2, 1), date(2020, 2, 29))]
    assert (months["2020-02"]["days_used"], months["2020-02"]["prior_days_used"]) == (29, 28)
    assert months["2020-02"]["mean_daily_travelers"] == pytest.approx(sum(february) / 29)
    assert (quarters["2020Q1"]["days_used"], quarters["2020Q1"]["prior_days_used"]) == (91, 90)
    assert quarters["2020Q1"]["yoy"] == pytest.approx(
        np.mean([by_day[d] for d in days(date(2020, 1, 1), date(2020, 3, 31))])
        / np.mean([by_day[d] for d in days(date(2019, 1, 1), date(2019, 3, 31))]) - 1)
    # The period the data ends inside is compared with the same days a year earlier, not with the whole period.
    partial = quarters["2021Q1"]
    assert (partial["days_used"], partial["days_in_period"], partial["complete"], partial["prior_days_used"]) == (41, 90, False, 41)
    like_for_like = (np.mean([by_day[d] for d in days(date(2021, 1, 1), date(2021, 2, 10))])
                     / np.mean([by_day[d] for d in days(date(2020, 1, 1), date(2020, 2, 10))]) - 1)
    whole = partial["mean_daily_travelers"] / quarters["2020Q1"]["mean_daily_travelers"] - 1
    assert partial["yoy"] == pytest.approx(like_for_like) == pytest.approx(0.1) and abs(whole - 0.1) > 0.01
    assert partial["yoy_basis"] == "start of period to the same month and day a year earlier"
    assert (months["2021-02"]["days_used"], months["2021-02"]["prior_days_used"], months["2021-02"]["complete"]) == (10, 10, False)
    # A prior-year window that is missing a day gives no growth rate rather than a mean over fewer days.
    keep = [i for i, d in enumerate(dates) if d != date(2020, 1, 15)]
    gapped = {m["period"]: m for m in tsa.period_means([dates[i] for i in keep], values[keep], "month")}
    assert gapped["2021-01"]["yoy"] is None and gapped["2021-01"]["prior_days_used"] is None
    assert gapped["2020-01"]["days_used"] == 30 and not gapped["2020-01"]["complete"] and gapped["2020-02"]["yoy"] is not None
    with pytest.raises(ValueError):
        tsa.period_means(dates, values, "week")


def test_pandemic_quarters_follow_the_stated_rule():
    def quarter(period, mean, complete=True):
        return {"period": period, "mean_daily_travelers": mean, "complete": complete}
    levels = {2019: 100, 2020: 60, 2021: 99.9}
    rows = [quarter(f"{y}Q{k}", levels[y]) for y in (2019, 2020, 2021) for k in (1, 2, 3, 4)]
    rows += [quarter("2022Q1", 150, complete=False), quarter("2022Q2", 100), quarter("2022Q3", 90), quarter("2022Q4", 101),
             quarter("2023Q1", 105), quarter("2023Q2", 105), quarter("2023Q3", 105)]
    marked = {q["period"]: q for q in tsa.mark_pandemic(rows)}
    # 2019 is never marked; an incomplete quarter cannot end the pandemic; the first quarter back at 2019's level does.
    assert [p for p, q in marked.items() if q["pandemic"]] == [f"{y}Q{k}" for y in (2020, 2021) for k in (1, 2, 3, 4)] + ["2022Q1"]
    assert marked["2022Q1"]["vs_2019"] is None and marked["2022Q2"]["vs_2019"] == 1.0 and marked["2020Q3"]["vs_2019"] == 0.6
    # A later dip (2022Q3) is not marked again. Growth measured against a marked quarter is flagged separately.
    assert not marked["2022Q3"]["pandemic"]
    assert [p for p, q in marked.items() if q["yoy_base_pandemic"]] == ["2022Q2", "2022Q3", "2022Q4", "2023Q1"]


def test_loader_refuses_a_file_that_is_not_the_tidy_series(tmp_path):
    good = write_tidy(tmp_path / "good.csv", [(date(2026, 10, 7), 2377978), (date(2026, 10, 8), 2901564)])
    dates, values = tsa.load_tsa_daily(good)
    assert dates == [date(2026, 10, 7), date(2026, 10, 8)] and values.tolist() == [2377978.0, 2901564.0]
    for text in ("date,count\n2026-10-08,1\n", "date,travelers\n", "date,travelers\n2026-10-08,1\n2026-10-08,1\n",
                 "date,travelers\n2026-10-08,1\n2026-10-07,1\n", "date,travelers\n2026-10-08,0\n",
                 "date,travelers\n2026-10-08,2.5\n"):
        bad = tmp_path / "bad.csv"
        bad.write_text(text)
        with pytest.raises(ValueError):
            tsa.load_tsa_daily(bad)


def test_headline_and_summary_are_written_from_the_numbers(tmp_path):
    rows = [(d, made_up_count(d)) for d in days(date(2019, 1, 1), date(2026, 10, 8))]
    tidy = write_tidy(tmp_path / "tsa_daily.csv", rows)
    payload = tsa.demand_signal(tidy, tmp_path / "absent.csv", now=NOW)
    check_payload(payload)
    by_day = dict(rows)

    def growth(first, last):
        current = [by_day[d] for d in days(first, last)]
        before = [by_day[d.replace(year=d.year - 1)] for d in days(first, last)]
        return sum(current) / len(current), sum(current) / sum(before) - 1

    (q3_mean, q3), (q2_mean, q2) = growth(date(2026, 7, 1), date(2026, 9, 30)), growth(date(2026, 4, 1), date(2026, 6, 30))
    note = payload["current_quarter_note"]
    assert note["2026Q3"]["yoy"] == round(q3, 6) and note["2026Q2"]["yoy"] == round(q2, 6)
    assert note["2026Q3"]["mean_daily_travelers"] == round(q3_mean, 1) and note["2026Q3"]["days_used"] == 92
    assert payload["headline"] == (f"TSA daily travelers: {abs(q3) * 100:.2f}% below a year earlier in 2026Q3; "
                                   f"{abs(q2) * 100:.2f}% below a year earlier in 2026Q2.")
    assert f"{q3_mean / 1e6:.2f} million travelers a day, {abs(q3) * 100:.2f}% below" in payload["plain_summary"]
    assert "2019-01-01 to 2026-10-08: 2,838 days, with no date missing." in payload["plain_summary"]
    assert note["text"] in payload["plain_summary"] and "(92 days against 92)" in note["text"]
    # Other numbers give other words; the text is never fixed in advance.
    higher = write_tidy(tmp_path / "higher.csv", [(d, v * 2 if d >= date(2026, 7, 1) else v) for d, v in rows])
    assert "above a year earlier in 2026Q3" in tsa.demand_signal(higher, tmp_path / "absent.csv", now=NOW)["headline"]
    assert tsa._versus(0.00001) == "level with" and tsa._versus(-0.0285) == "2.85% below"
    assert payload["daily"][0]["date"] == "2025-01-01" and len(payload["daily"]) == 646
    assert payload["quarterly"][-1]["period"] == "2026Q4" and not payload["quarterly"][-1]["complete"]
    assert any("2026Q4 is incomplete (8 of 92 days)" in text for text in payload["limits"])
    assert any("cannot inform the 2013-2019 fit period" in text for text in payload["limits"])


def test_committed_csv_json_and_manifest_agree():
    dates, values = tsa.load_tsa_daily(tsa.TSA_CSV)
    payload = json.loads(tsa.DEMAND_JSON.read_text())
    check_payload(payload)
    manifest = json.loads(fetch_tsa.MANIFEST.read_text())
    tidy_hash = byte_hash(tsa.TSA_CSV.read_bytes())
    assert payload["inputs"][0] == {"path": "data/lab/tsa_daily.csv", "sha256": tidy_hash}
    assert manifest["outputs"][0]["sha256"] == tidy_hash and manifest["outputs"][0]["rows"] == len(dates)
    assert payload["as_of"] == dates[-1].isoformat() == manifest["coverage"]["last"] and dates[0] == date(2019, 1, 1)
    assert payload["coverage"] == manifest["coverage"]
    assert payload["coverage"]["missing_days"] == (dates[-1] - dates[0]).days + 1 - len(dates)
    for item in manifest["entries"]:
        assert item["url"].startswith("https://www.tsa.gov/travel/passenger-volumes") and item["bytes"] > 0
        assert item["path"] == f"data/lab_raw/tsa/{item['sha256']}.html" and re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        assert datetime.fromisoformat(item["retrieved_at"]).utcoffset() == timedelta(0)
    assert {i["sha256"] for i in payload["inputs"][1:] if i["path"].startswith("data/lab_raw/tsa/")} <= {
        e["sha256"] for e in manifest["entries"]}
    # Every TSA number in the JSON is what the library computes from the committed CSV today.
    fresh = tsa.demand_signal(tsa.TSA_CSV, ROOT / "data" / "lab" / "no_such_file.csv", now=NOW)
    for key in ("daily", "quarterly", "monthly_control", "current_quarter_note", "coverage", "headline"):
        assert payload[key] == fresh[key], key
    by_day = dict(zip(dates, values))
    if date(2026, 9, 30) in by_day:
        current = [by_day[d] for d in days(date(2026, 7, 1), date(2026, 9, 30))]
        before = [by_day[d] for d in days(date(2025, 7, 1), date(2025, 9, 30))]
        assert payload["current_quarter_note"]["2026Q3"]["yoy"] == round(sum(current) / sum(before) - 1, 6)
    if payload["versus_united"]:
        assert payload["inputs"][-1] == payload["versus_united"]["source"]
        assert payload["versus_united"]["correlation"]["sample"] == "non_pandemic"
