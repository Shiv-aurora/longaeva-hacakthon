import csv
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sys

import httpx
import pytest

from app.lab import delta
from scripts.lab import fetch_dal_filings as fetch

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "lab_delta"
CHECK = ROOT / "data" / "lab" / "dal_check.json"
QUARTERLY = ROOT / "data" / "lab" / "dal_quarterly.csv"
MANIFEST = ROOT / "data" / "lab" / "manifests" / "dal_filings.json"
SECTIONS = ("guidance_sep_quarter", "actuals_jun_quarter", "actuals_sep_quarter", "guidance_dec_quarter")
CONTRACT_KEYS = {"id", "claim", "period", "value", "unit", "quote", "doc_url", "doc_sha256", "char_start", "char_end",
                 "picked_by", "verified"}
NORMALISERS = [delta.LOCAL, None]  # None: the project reader's normalisation when it is importable.
OCTOBER_FILING = "0000027904-26-000035"


def snippet(name, spec, normaliser=None, wrap=False):
    """A real excerpt as a document for the rules. `wrap` restores the <table> a row-level cut starts inside."""
    content = (FIXTURES / name).read_bytes()
    return delta.document(b"<table>" + content if wrap else content, normaliser, **spec,
                          url="https://www.sec.gov/Archives/edgar/data/27904/" + name, sha256="0" * 64)


def rule_facts(doc):
    """Facts from every guidance rule whose sentence or table is inside this excerpt."""
    return {(f["_path"], f["_key"]): f for pattern, picks in delta.guidance_rules(doc)
            if re.search(pattern, doc["view"]) for f in delta.take(doc, pattern, picks)}


@pytest.fixture(scope="module")
def check():
    return json.loads(CHECK.read_text())


# ---- parsing

@pytest.mark.parametrize("text,expected", [
    ("$2.00 - $2.50", (2.0, 2.5)), ("$2.00 to $2.50", (2.0, 2.5)), ("11% - 13%", (11, 13)),
    ("11 to 13 percent", (11, 13)), ("$3 - $4", (3, 4)), ("$1.15 – $1.65", (1.15, 1.65)),
    ("Earnings Per Share $5.10 - $5.60", (5.1, 5.6)), ("free cash flow of $3 to $4 billion", (3, 4))])
def test_ranges_parse_as_printed(text, expected):
    assert delta.parse_range(text) == expected


@pytest.mark.parametrize("text", ["$2.50 - $2.00", "Up Mid-Teens", "approximately $3.15 per gallon"])
def test_a_backwards_or_absent_range_is_refused(text):
    with pytest.raises(ValueError):
        delta.parse_range(text)


@pytest.mark.parametrize("text,expected", [
    ("15 percent", 15), ("12.4%", 12.4), ("12.4 %", 12.4), ("(7)%", -7), ("Approx. 20%", 20),
    ("increased 60 percent year-over-year", 60)])
def test_percents_parse_as_printed(text, expected):
    assert delta.parse_percent(text) == expected


@pytest.mark.parametrize("text", ["Up Mid-Teens", "$3.61 per gallon", "nearly one point"])
def test_text_without_a_percentage_is_refused(text):
    with pytest.raises(ValueError):
        delta.parse_percent(text)


def test_printed_numbers_keep_sign_and_type():
    assert delta.number("1,604") == 1604 and isinstance(delta.number("1,604"), int)
    assert delta.number("(405)") == -405 and delta.number("$2.44") == 2.44 and delta.number("13.9%") == 13.9
    assert delta.number("(344 )") == -344
    for bad in ("—", "1,60", "12,34", "Mid-Teens", "1.2.3"):
        with pytest.raises(ValueError):
            delta.number(bad)
    assert delta.half_unit("1,604") == 0.5 and delta.half_unit("3.61") == pytest.approx(0.005)
    assert delta.half_unit("12.4") == pytest.approx(0.05)
    assert [delta.printed(t) for t in ("(344 )", "$ 3.80", "1,146", "(0.51)")] == ["-344", "3.80", "1146", "-0.51"]


def test_numbers_in_a_quote():
    assert delta.quote_values("Non-GAAP $ 1,497 $ (363) $ 1,134 $ 1.72") == [1497, -363, 1134, 1.72]
    assert delta.quote_values("Non-GAAP\t$\t1,497\t$\t(363)\t$\t1,134\t$\t1.72") == [1497, -363, 1134, 1.72]
    assert delta.quote_values("a refinery benefit of 13¢ per gallon", "usd_per_gallon") == [0.13]
    assert delta.quote_values("benefit of 5-cents per gallon; about $3.15 per gallon.", "usd_per_gallon") == [0.05, 3.15]
    assert delta.quote_values("Non-fuel CASM was 14.03¢", "cents") == [14.03]
    assert delta.quote_values("3Q26 Gross Leverage Approx. 2.2x") == [2.2]  # A quarter label is not a number.
    assert delta.quote_dates("as of July 2, 2026, and as of October 2, 2026") == ["2026-07-02", "2026-10-02"]
    assert delta.shift("2026Q3", 1) == "2026Q4" and delta.shift("2026Q3", -4) == "2025Q3"
    assert delta.shift("2026Q4", 1) == "2027Q1"


# ---- normalisation

def test_local_normaliser_is_single_spaced_nfc_text():
    html = "<p>a&nbsp;&nbsp;b</p><table><tr><td>$</td><td>1,604</td></tr></table><script>x()</script>café"
    assert delta.document_text(html) == "a b $ 1,604 café"
    text = delta.document_text((FIXTURES / "july_guidance.html").read_bytes())
    assert text == delta.normalise(text)
    assert ("3Q26 Total Revenue YoY (%) Up Mid-Teens Operating Margin 11% - 13% Earnings Per Share $2.00 - $2.50 "
            "Guidance for the September quarter assumes fuel at the forward curve as of July 2, 2026, and includes a "
            "refinery benefit of 5-cents per gallon. This results in a projected all-in fuel price for the quarter of "
            "approximately $3.15 per gallon.") in text


@pytest.mark.parametrize("normaliser", NORMALISERS)
def test_view_maps_back_to_exact_offsets(normaliser):
    doc = snippet("october_guidance.html", delta.OCTOBER, normaliser)
    assert "  " not in doc["view"] and len(doc["view"]) == len(doc["index"])
    assert all(doc["text"][i] == c or (c == " " and doc["text"][i].isspace())
               for c, i in zip(doc["view"], doc["index"]))
    match = re.search(r"Operating Margin 7% - 9%", doc["view"])
    start, end = doc["index"][match.start()], doc["index"][match.end() - 1] + 1
    assert doc["text"][start:end].split() == ["Operating", "Margin", "7%", "-", "9%"]


# ---- guidance rules on real excerpts

@pytest.mark.parametrize("normaliser", NORMALISERS)
def test_july_guidance_from_real_excerpts(normaliser):
    g = "guidance_sep_quarter"
    table = rule_facts(snippet("july_guidance.html", delta.JULY, normaliser))
    cfo = rule_facts(snippet("july_cfo.html", delta.JULY, normaliser))
    assert [cfo[g, k]["value"] for k in ("eps_low", "eps_high")] == [2.0, 2.5]
    assert [cfo[g, k]["value"] for k in ("operating_margin_low_pct", "operating_margin_high_pct")] == [11, 13]
    assert cfo[g, "eps_low"]["period"] == "2026Q3" and cfo[g, "eps_low"]["unit"] == "usd_per_share"
    assert table[g, "fuel_price_assumed_usd_gal"]["value"] == 3.15
    assert table[g, "fuel_price_assumed_usd_gal"]["_qualifier"] == "approximately"
    assert table[g, "refinery_benefit_usd_gal"]["value"] == 0.05
    assert table[g, "fuel_curve_as_of"]["value"] == "2026-07-02"
    assert table[g, "revenue_growth_text"]["value"] is None and table[g, "revenue_growth_text"]["_text"] == "Up Mid-Teens"
    assert table[g + ".full_year", "gross_leverage_x"]["value"] == 2
    for doc_facts in (table, cfo):
        assert doc_facts and all(f["verified"] and f["picked_by"] == "rules" for f in doc_facts.values())
    assert cfo[g, "eps_low"]["id"] == "dal-2026q2-ex991-2026q3-eps-low"


@pytest.mark.parametrize("normaliser", NORMALISERS)
def test_october_guidance_and_explanation_from_real_excerpts(normaliser):
    g = "guidance_dec_quarter"
    table = rule_facts(snippet("october_guidance.html", delta.OCTOBER, normaliser))
    cfo = rule_facts(snippet("october_cfo.html", delta.OCTOBER, normaliser))
    assert [cfo[g, k]["value"] for k in ("eps_low", "eps_high")] == [1.15, 1.65]
    assert [table[g, k]["value"] for k in ("operating_margin_low_pct", "operating_margin_high_pct")] == [7, 9]
    assert table[g, "fuel_price_assumed_usd_gal"]["value"] == 4.25
    assert table[g, "refinery_benefit_usd_gal"]["value"] == 0.4
    assert table[g, "refinery_benefit_usd_gal"]["_qualifier"] == "approximately"
    assert table[g, "fuel_curve_as_of"]["value"] == "2026-10-02"
    assert table[g + ".full_year", "gross_leverage_x"]["value"] == 2.2
    overrun = cfo["management_explanation", "fuel_cost_above_july_guidance_musd"]
    assert (overrun["value"], overrun["unit"], overrun["_qualifier"]) == (500, "usd_million", "more than")
    assert "compared to our guidance in early July" in overrun["quote"]
    assert all(f["verified"] for f in {**table, **cfo}.values())


def test_a_rule_that_matches_twice_or_not_at_all_is_refused():
    doc = snippet("october_cfo.html", delta.OCTOBER)
    with pytest.raises(ValueError, match="expected one match, found 0"):
        delta.take(doc, r"In the March quarter, we expect", [])
    with pytest.raises(ValueError, match="expected one match, found"):
        delta.take(doc, r"quarter", [])


# ---- the verifier

@pytest.mark.parametrize("normaliser", NORMALISERS)
def test_verifier_rejects_an_altered_quote_value_or_offset(normaliser):
    doc = snippet("july_guidance.html", delta.JULY, normaliser)
    facts = rule_facts(doc)
    record = facts["guidance_sep_quarter", "fuel_price_assumed_usd_gal"]
    text = doc["text"]
    assert delta.verify(record, text) and text[record["char_start"]:record["char_end"]] == record["quote"]
    assert not delta.verify({**record, "quote": record["quote"].replace("$3.15", "$3.05")}, text)
    assert not delta.verify({**record, "value": 3.05}, text)
    assert not delta.verify({**record, "value": True}, text)
    assert not delta.verify({**record, "char_start": record["char_start"] + 1, "char_end": record["char_end"] + 1}, text)
    assert not delta.verify({**record, "quote": record["quote"] + " "}, text)
    assert not delta.verify({**record, "quote": ""}, text)
    assert not delta.verify(record, text.replace("$3.15", "$3.51"))  # The document no longer says it.
    benefit = facts["guidance_sep_quarter", "refinery_benefit_usd_gal"]
    assert delta.verify(benefit, text) and not delta.verify({**benefit, "value": 5}, text)  # Cents are not dollars.
    day = facts["guidance_sep_quarter", "fuel_curve_as_of"]
    assert delta.verify(day, text) and not delta.verify({**day, "value": "2026-07-03"}, text)
    statement = facts["guidance_sep_quarter", "revenue_growth_text"]
    assert delta.verify(statement, text) and not delta.verify({**statement, "value": 15}, text)


# ---- table rows on real excerpts

@pytest.mark.parametrize("normaliser", NORMALISERS)
def test_fuel_rows_from_the_real_statistics_table(normaliser):
    view = snippet("october_stats_fuel_rows.html", delta.OCTOBER, normaliser, wrap=True)["view"]
    rows = {field: delta.row_pattern(label, columns).search(view)
            for field, _, label, columns, _ in delta.ROWS
            if field in ("fuel_gallons_m", "fuel_price_gaap_usd_gal", "fuel_price_adj_usd_gal")}
    assert rows["fuel_gallons_m"].group("c", "p", "y") == ("1,146", "1,138", "1")
    assert rows["fuel_price_gaap_usd_gal"].group("c", "p", "y") == ("3.80", "2.26", "68")
    assert rows["fuel_price_adj_usd_gal"].group("c", "p", "y") == ("3.61", "2.25", "60")
    assert rows["fuel_price_gaap_usd_gal"].group(0) == "Average price per fuel gallon $ 3.80 $ 2.26 68 %"


@pytest.mark.parametrize("normaliser", NORMALISERS)
def test_fuel_reconciliation_keeps_gaap_and_adjusted_apart(normaliser):
    view = snippet("october_fuel_reconciliation.html", delta.OCTOBER, normaliser)["view"]
    assert ("Total fuel expense $ 4,351 $ 2,570 $ 3.80 $ 2.26 Adjusted for: MTM adjustments and settlements on hedges "
            "(208) (5) (0.18) (0.01) Total fuel expense, adjusted $ 4,143 $ 2,565 62 % $ 3.61 $ 2.25 60 %") in view
    hedge = delta.row_pattern("MTM adjustments and settlements on hedges", "c").search(view)
    assert delta.number(hedge["c"]) == -208


def test_history_patterns_read_older_layouts():
    heads = {" Three Months Ended December 31, Year Ended December 31, 2017 2016 Change ": ("December 31", "2017"),
             " Three Months Ended March 31, 2018 2017 Change ": ("March 31", "2018"),
             " Three Months Ended Twelve Months Ended December 31, December 31, 2019 2018 Change ": ("December 31", "2019"),
             " Three Months Ended Six Months Ended June 30, June 30, 2021 2019 Change ": ("June 30", "2021"),
             " Three Months Ended Nine Months Ended September 30, September 30, 2026 2025 Change ": ("September 30", "2026")}
    for text, expected in heads.items():
        assert delta.PERIOD_HEAD.match(text).group("day", "year") == expected
    assert not delta.PERIOD_HEAD.match(" Six Months Ended June 30, 2026 2025 Change ")
    cells = re.search(rf"Non-GAAP(?P<cells>(?: \$ {delta.V}){{2,4}})", "Non-GAAP $ 1,025 $ (344 ) $ 681 $ 0.96 Year")
    assert [delta.printed(c) for c in re.findall(delta.V, cells["cells"])] == ["1025", "-344", "681", "0.96"]


# ---- arithmetic

def printed_fact(key, token):
    return {"id": key, "value": delta.number(token), "_printed": token, "_path": "actuals", "_key": key}


def test_arithmetic_tolerance_is_what_rounding_allows():
    inputs = [printed_fact("fuel_gallons_m", "1,146"), printed_fact("fuel_price_adj_usd_gal", "3.61")]
    held = delta.arithmetic("fuel_expense_adj", "2026Q3", "g * p", printed_fact("e", "4,143"), inputs, lambda g, p: g * p)
    assert held["ok"] and held["computed"] == pytest.approx(4137.06) and held["difference"] == pytest.approx(-5.94)
    assert held["tolerance"] == pytest.approx(0.5 + 1146.5 * 3.615 - 1146 * 3.61)
    broke = delta.arithmetic("fuel_expense_adj", "2026Q3", "g * p", printed_fact("e", "4,160"), inputs, lambda g, p: g * p)
    assert not broke["ok"]
    row = {"period_end": "2026-09-30", "fuel_gallons_m": "1146", "fuel_price_usd_gal": "3.80",
           "fuel_expense_musd": "4351", "net_income_musd": "756", "diluted_shares_m": "659", "gaap_eps_diluted": "1.15"}
    assert [(c["name"], c["ok"]) for c in delta.history_checks(row)] == [("fuel_expense", True), ("gaap_eps", True)]
    assert not delta.history_checks({**row, "gaap_eps_diluted": "1.25"})[1]["ok"]


# ---- data/lab/dal_check.json

def numeric_fields(node, path=""):
    """(dotted path, value, evidence id) for every number that sits beside an evidence_ids map."""
    ids = node.get("evidence_ids", {})
    for key, value in node.items():
        if isinstance(value, dict) and key not in ("evidence_ids", "qualifiers"):
            yield from numeric_fields(value, f"{path}{key}.")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            yield path + key, value, ids.get(key)


def test_check_file_has_the_agreed_shape(check):
    for key in ("generated_at", "as_of", "inputs", *SECTIONS, "management_explanation", "evidence"):
        assert key in check
    assert "fixture" not in check and check["as_of"] == "2026-10-09"
    assert datetime.fromisoformat(check["generated_at"]).utcoffset() == timedelta(0)
    for item in check["inputs"]:
        assert set(item) == {"path", "sha256"} and re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        assert item["path"].startswith("data/lab_raw/sec/")
    assert [(d["release"], d["accession"], d["filed_on"]) for d in check["documents"]] == [
        ("2026Q2", "0000027904-26-000029", "2026-07-10"), ("2026Q3", "0000027904-26-000035", "2026-10-09")]
    assert {d["sha256"] for d in check["documents"]} <= {i["sha256"] for i in check["inputs"]}


def test_every_evidence_record_is_verified_under_the_contract_rule(check):
    documents = {d["sha256"]: d for d in check["documents"]}
    ids = [e["id"] for e in check["evidence"]]
    assert len(ids) == len(set(ids)) > 150
    for record in check["evidence"]:
        assert CONTRACT_KEYS <= set(record), record["id"]
        assert record["picked_by"] == "rules" and record["verified"] is True
        assert record["doc_sha256"] in documents and record["doc_url"] == documents[record["doc_sha256"]]["url"]
        assert record["normalisation"] == documents[record["doc_sha256"]]["normalisation"]
        assert record["doc_url"].startswith("https://www.sec.gov/Archives/edgar/data/27904/")
        assert 0 <= record["char_start"] < record["char_end"] <= documents[record["doc_sha256"]]["normalised_chars"]
        # The value parses from the quote, and the offsets span exactly the quote.
        assert delta.verify(record, " " * record["char_start"] + record["quote"]), record["id"]
        assert re.fullmatch(r"\d{4}(Q[1-4]|FY)", record["period"])


def test_every_number_in_the_fact_sections_cites_a_verified_record(check):
    evidence = {e["id"]: e for e in check["evidence"]}
    seen = 0
    for name in SECTIONS:
        for path, value, evidence_id in numeric_fields(check[name]):
            assert evidence_id in evidence, f"{name}.{path} cites no evidence"
            assert evidence[evidence_id]["verified"] and evidence[evidence_id]["value"] == value, f"{name}.{path}"
            seen += 1
        for node in (check[name], check[name].get("full_year", {})):
            for key, evidence_id in node.get("evidence_ids", {}).items():
                assert evidence_id in evidence and key in node
    assert seen > 120
    for item in check["management_explanation"]:
        record = evidence[item["evidence_id"]]
        assert record["verified"] and record["quote"] == item["quote"] and record["value"] == item["value"]


def test_headline_facts(check):
    guided, june, september = (check[k] for k in ("guidance_sep_quarter", "actuals_jun_quarter", "actuals_sep_quarter"))
    assert (guided["period"], guided["made_on"], guided["eps_low"], guided["eps_high"]) == ("2026Q3", "2026-07-10", 2.0, 2.5)
    assert (guided["operating_margin_low_pct"], guided["operating_margin_high_pct"]) == (11, 13)
    assert (guided["fuel_price_assumed_usd_gal"], guided["refinery_benefit_usd_gal"]) == (3.15, 0.05)
    assert guided["fuel_curve_as_of"] == "2026-07-02" and guided["revenue_growth_text"] == "Up Mid-Teens"
    # What the July release does not state stays null.
    assert guided["revenue_growth_yoy_pct"] is None and guided["capacity_growth_yoy_pct"] is None
    assert guided["fuel_recovery_through_pricing"]["stated"] is False
    assert june["income_tax_rate_pct"] is None and september["income_tax_rate_pct"] is None
    assert (june["adj_eps_diluted"], june["gaap_eps_diluted"]) == (1.56, 2.44)
    assert (june["fuel_price_gaap_usd_gal"], june["fuel_price_adj_usd_gal"]) == (3.66, 3.93)
    assert (september["adj_eps_diluted"], september["gaap_eps_diluted"]) == (1.72, 1.15)
    assert (september["fuel_price_gaap_usd_gal"], september["fuel_price_adj_usd_gal"]) == (3.8, 3.61)
    assert (september["fuel_gallons_m"], september["asm_m"], september["diluted_shares_m"]) == (1146, 79288, 659)
    assert (september["total_revenue_musd"], september["adj_total_revenue_musd"]) == (20186, 17585)
    assert (september["trasm_cents"], september["adj_trasm_cents"], september["casm_ex_cents"]) == (25.46, 22.18, 14.03)
    assert (september["pretax_income_musd"], september["adj_pretax_income_musd"]) == (1074, 1497)
    assert (september["profit_sharing_musd"], september["refinery_benefit_usd_gal"]) == (389, 0.13)
    assert september["yoy_stated"]["adj_trasm_yoy_pct"] == 15 and september["yoy_stated"]["fuel_price_adj_yoy_pct"] == 60
    assert june["yoy_stated"]["adj_trasm_yoy_pct"] == 12.4
    december = check["guidance_dec_quarter"]
    assert (december["period"], december["eps_low"], december["eps_high"]) == ("2026Q4", 1.15, 1.65)
    assert (december["operating_margin_low_pct"], december["operating_margin_high_pct"]) == (7, 9)
    assert (december["fuel_price_assumed_usd_gal"], december["refinery_benefit_usd_gal"]) == (4.25, 0.4)
    assert december["revenue_growth_yoy_pct"] == 20 and december["qualifiers"]["revenue_growth_yoy_pct"] == "approximately"
    assert (december["full_year"]["eps_low"], december["full_year"]["eps_high"]) == (5.1, 5.6)
    assert (guided["full_year"]["eps_low"], guided["full_year"]["eps_high"]) == (6.5, 7.5)
    overrun = next(i for i in check["management_explanation"] if i["topic"] == "fuel_cost_above_july_guidance_musd")
    assert (overrun["value"], overrun["qualifier"]) == (500, "more than")


def test_arithmetic_checks_hold_within_the_stated_tolerance(check):
    block = check["arithmetic_checks"]
    evidence = {e["id"]: e for e in check["evidence"]}
    assert block["total"] == len(block["checks"]) == block["passed"] >= 60
    for item in block["checks"]:
        assert item["ok"] and abs(item["difference"]) <= item["tolerance"], item["name"]
        assert item["computed"] - item["stated"] == pytest.approx(item["difference"], abs=1e-5)
        assert evidence[item["stated_evidence_id"]]["value"] == item["stated"]
        assert all(evidence[i["evidence_id"]]["value"] == i["value"] for i in item["inputs"].values())
    by_name = {(c["period"], c["name"]): c for c in block["checks"]}
    for period in ("2026Q2", "2026Q3"):
        for name in ("fuel_expense_gaap", "fuel_expense_adj", "gaap_eps", "adj_eps", "trasm", "adj_trasm_yoy_pct"):
            assert (period, name) in by_name
    fuel = by_name["2026Q3", "fuel_expense_adj"]
    assert fuel["computed"] == pytest.approx(1146 * 3.61) and fuel["stated"] == 4143
    assert by_name["2026Q3", "adj_eps"]["computed"] == pytest.approx(1134 / 659, abs=1e-6)
    twice = check["printed_twice"]
    assert twice["agree"] == twice["total"] == len(twice["items"]) and all(i["agree"] for i in twice["items"])
    assert all(a["matches"] == 0 for a in check["absence_checks"])


def test_guidance_against_actual_is_arithmetic_on_cited_facts(check):
    block, evidence = check["guidance_vs_actual_sep_quarter"], {e["id"] for e in check["evidence"]}
    assert block["eps"]["position"] == "below_range" and block["eps"]["actual_minus_low"] == pytest.approx(1.72 - 2.0)
    assert block["operating_margin_pct"]["position"] == "below_range"
    fuel = block["fuel_price_usd_gal"]
    assert fuel["actual_adj_minus_assumed"] == pytest.approx(3.61 - 3.15)
    assert fuel["before_refinery_benefit"]["actual_minus_assumed"] == pytest.approx((3.61 + 0.13) - (3.15 + 0.05))
    cost = block["fuel_cost_above_guidance"]
    assert cost["computed_musd"] == pytest.approx((3.61 - 3.15) * 1146, abs=0.05)
    assert cost["consistent_with_statement"] is True and cost["delta_states_more_than_musd"] == 500
    assert cost["per_diluted_share_pretax"] == pytest.approx(cost["computed_musd"] / 659, abs=1e-3)
    for part in block.values():
        if isinstance(part, dict):
            assert set(part["evidence_ids"]) <= evidence


@pytest.mark.parametrize("evidence_id,name,wrap", [
    ("dal-2026q2-ex991-2026q3-fuel-price-assumed-usd-gal", "july_guidance.html", False),
    ("dal-2026q2-ex991-2026q3-revenue-growth-text", "july_guidance.html", False),
    ("dal-2026q2-ex991-2026q3-eps-low", "july_cfo.html", False),
    ("dal-2026q3-ex991-2026q4-eps-high", "october_cfo.html", False),
    ("dal-2026q3-ex991-2026q3-fuel-cost-above-july-guidance-musd", "october_cfo.html", False),
    ("dal-2026q3-ex991-2026q4-fuel-price-assumed-usd-gal", "october_guidance.html", False),
    ("dal-2026q3-ex991-2026q4-operating-margin-low-pct", "october_guidance.html", False),
    ("dal-2026q3-ex991-2026q3-fuel-price-adj-usd-gal", "october_stats_fuel_rows.html", True),
    ("dal-2026q3-ex991-2026q3-fuel-price-gaap-usd-gal", "october_stats_fuel_rows.html", True),
    ("dal-2026q3-ex991-2026q3-fuel-gallons-m", "october_stats_fuel_rows.html", True),
    ("dal-2026q3-ex991-2026q3-fuel-hedge-adjustment-usd-gal", "october_fuel_reconciliation.html", False)])
def test_committed_quotes_are_in_the_real_excerpts(check, evidence_id, name, wrap):
    record = next(e for e in check["evidence"] if e["id"] == evidence_id)
    if record["normalisation"] != delta.LOCAL and delta.shared_reader is None:
        pytest.skip("the project reader that produced these offsets is not importable")
    spec = delta.JULY if name.startswith("july") else delta.OCTOBER
    text = snippet(name, spec, record["normalisation"], wrap)["text"]
    start = text.find(record["quote"])
    assert start >= 0
    assert delta.verify({**record, "char_start": start, "char_end": start + len(record["quote"])}, text)


def test_excerpts_are_the_bytes_recorded_for_them():
    for source in json.loads((FIXTURES / "sources.json").read_text()):
        if "sha256" in source:
            content = (FIXTURES / source["file"]).read_bytes()
            assert hashlib.sha256(content).hexdigest() == source["sha256"] and len(content) == source["bytes"]


# ---- data/lab/dal_quarterly.csv

def test_quarterly_file_keeps_the_contract_columns_and_its_own_checks(check):
    with QUARTERLY.open(newline="") as stream:
        raw_rows = list(csv.DictReader(stream))
    assert tuple(raw_rows[0]) == delta.QUARTERLY_COLUMNS
    assert delta.QUARTERLY_COLUMNS[:len(delta.CONTRACT_COLUMNS)] == delta.CONTRACT_COLUMNS
    assert delta.CONTRACT_COLUMNS[-1] == "source_note" and delta.CONTRACT_COLUMNS[:3] == ("period_end", "filed_on", "accession")
    periods = [row["period_end"] for row in raw_rows]
    assert periods == sorted(set(periods)) and periods[-1] == "2026-09-30" and len(periods) >= 30
    off = []
    for row in raw_rows:
        assert row["period_end"] < row["filed_on"] and re.fullmatch(r"\d{10}-\d{2}-\d{6}", row["accession"])
        tested = delta.history_checks(row)
        assert f"checks {sum(c['ok'] for c in tested)}/{len(tested)}" in row["source_note"]
        off += [(c["period_end"], c["name"]) for c in tested if not c["ok"]]
    # One figure in the source does not recompute, and its row says so.
    assert off == [("2018-09-30", "fuel_expense")]
    assert "fuel_expense off by" in next(r for r in raw_rows if r["period_end"] == "2018-09-30")["source_note"]
    loaded = {row["period_end"]: row for row in delta.load_quarterly(QUARTERLY)}
    for period_end, section in (("2026-06-30", "actuals_jun_quarter"), ("2026-09-30", "actuals_sep_quarter")):
        row, facts = loaded[period_end], check[section]
        same = ("asm_m", "rpm_m", "total_revenue_musd", "passenger_revenue_musd", "trasm_cents", "prasm_cents",
                "casm_cents", "casm_ex_cents", "fuel_gallons_m", "pretax_income_musd", "adj_pretax_income_musd",
                "net_income_musd", "gaap_eps_diluted", "adj_eps_diluted", "diluted_shares_m", "profit_sharing_musd",
                "adj_total_revenue_musd", "adj_trasm_cents")
        assert all(row[column] == facts[column] for column in same)
        # The contract's fuel columns hold the GAAP figures; Delta's adjusted ones have their own columns.
        assert (row["fuel_price_usd_gal"], row["adj_fuel_price_usd_gal"]) == (
            facts["fuel_price_gaap_usd_gal"], facts["fuel_price_adj_usd_gal"])
        assert (row["fuel_expense_musd"], row["adj_fuel_expense_musd"]) == (
            facts["fuel_expense_gaap_musd"], facts["fuel_expense_adj_musd"])
    assert loaded["2020-06-30"]["profit_sharing_musd"] is None  # Printed as a dash, so left blank.


# ---- the downloader, offline

def test_earnings_filings_are_the_item_202_rows():
    blob = json.loads((FIXTURES / "submissions_rows.json").read_text())
    rows = fetch.earnings_filings(blob, "2018-01-01")
    assert [r["accession"] for r in rows] == [OCTOBER_FILING, "0000027904-26-000029", "0001683168-25-005002",
                                              "0001683168-18-003018", "0001683168-18-003013"]
    assert (rows[0]["filed_on"], rows[0]["form"], rows[0]["items"]) == ("2026-10-09", "8-K", "2.02,9.01")
    assert rows[3]["form"] == "8-K/A"
    assert [r["accession"] for r in fetch.earnings_filings(blob, "2026-07-01")] == [OCTOBER_FILING, "0000027904-26-000029"]
    assert [r["filed_on"] for r in fetch.earnings_filings(blob, "2018-01-01", "2018-12-31")] == ["2018-10-11"] * 2
    with pytest.raises(ValueError, match="older than"):
        fetch.earnings_filings(blob, "2016-01-01")
    with pytest.raises(ValueError, match="not Delta"):
        fetch.earnings_filings({**blob, "cik": "0000100517"}, "2018-01-01")


def test_exhibits_are_typed_from_the_header_page():
    documents = fetch.header_documents((FIXTURES / "october_index_headers.html").read_bytes())
    index = json.loads((FIXTURES / "october_index.json").read_text())
    assert fetch.exhibits(index, documents) == [{"type": "EX-99.1", "sequence": 2, "description": "EXHIBIT 99.1",
                                                 "filename": "deltaairlinesannouncessept.htm"}]
    assert {"8-K", "EX-101.SCH", "GRAPHIC"} <= {d["type"] for d in documents}
    with pytest.raises(ValueError, match="absent from index.json"):
        fetch.exhibits({"directory": {"item": []}}, documents)


class Scripted:
    """Stands in for the HTTP client: answers each request with the next scripted status."""

    def __init__(self, statuses):
        self.statuses, self.calls = list(statuses), []

    def get(self, url):
        self.calls.append(url)
        status = self.statuses.pop(0)
        return httpx.Response(status, content=b"body" if status == 200 else b"", request=httpx.Request("GET", url))


@pytest.fixture()
def clock(monkeypatch):
    now, sleeps = [1000.0], []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    monkeypatch.setattr(fetch.time, "sleep", sleep)
    monkeypatch.setattr(fetch.time, "monotonic", lambda: now[0])
    return sleeps


def test_requests_carry_the_declared_agent_are_spaced_and_retried(clock):
    edgar = fetch.Edgar("unit test agent")
    assert edgar.client.headers["User-Agent"] == "unit test agent" and edgar.client.follow_redirects
    edgar.client = Scripted([429, 503, 200, 200])
    content, stamp = edgar.get("https://www.sec.gov/one")
    assert content == b"body" and datetime.fromisoformat(stamp).utcoffset() == timedelta(0)
    assert edgar.client.calls == ["https://www.sec.gov/one"] * 3
    assert 1 in clock and 2 in clock  # Backoff after the 429 and after the 503.
    clock.clear()
    edgar.get("https://www.sec.gov/two")
    assert clock == [pytest.approx(fetch.MIN_GAP)] and fetch.MIN_GAP >= 0.25  # At most four requests a second.


def test_a_client_error_is_not_retried(clock):
    edgar = fetch.Edgar("unit test agent")
    edgar.client = Scripted([404])
    with pytest.raises(httpx.HTTPStatusError):
        edgar.get("https://www.sec.gov/missing")
    assert len(edgar.client.calls) == 1


def test_main_refuses_to_run_without_a_declared_agent(monkeypatch, tmp_path):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setattr(sys, "argv", ["fetch_dal_filings.py", "--manifest", str(tmp_path / "manifest.json")])
    with pytest.raises(SystemExit) as stopped:
        fetch.main()
    assert "SEC_USER_AGENT" in str(stopped.value) and not (tmp_path / "manifest.json").exists()


def test_download_keeps_bytes_and_a_manifest_without_the_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch, "ROOT", tmp_path)
    monkeypatch.setattr(fetch, "RAW", tmp_path / "data" / "lab_raw" / "sec")
    folder = f"{fetch.ARCHIVE}/{OCTOBER_FILING.replace('-', '')}"
    exhibit = (FIXTURES / "october_cfo.html").read_bytes()
    served = {fetch.SUBMISSIONS: (FIXTURES / "submissions_rows.json").read_bytes(),
              f"{folder}/index.json": (FIXTURES / "october_index.json").read_bytes(),
              f"{folder}/{OCTOBER_FILING}-index-headers.html": (FIXTURES / "october_index_headers.html").read_bytes(),
              f"{folder}/deltaairlinesannouncessept.htm": exhibit}
    requested = []

    class Offline:
        def get(self, url):
            requested.append(url)
            return served[url], "2026-10-10T00:00:00+00:00"

    manifest = fetch.download(Offline(), "2026-10-01", None, None)
    assert manifest["failures"] == [] and [e["kind"] for e in manifest["entries"]].count("exhibit") == 1
    entry = next(e for e in manifest["entries"] if e["kind"] == "exhibit")
    assert {"url", "retrieved_at", "sha256", "bytes", "path"} <= set(entry)
    assert entry["path"] == f"data/lab_raw/sec/{OCTOBER_FILING}/deltaairlinesannouncessept.htm"
    assert (entry["exhibit"], entry["form"], entry["filed_on"]) == ("EX-99.1", "8-K", "2026-10-09")
    assert (tmp_path / entry["path"]).read_bytes() == exhibit
    assert entry["sha256"] == hashlib.sha256(exhibit).hexdigest() and entry["bytes"] == len(exhibit)
    assert "agent" not in json.dumps(manifest).lower()
    # A second run asks only for the list of filings; intact files are not fetched again.
    requested.clear()
    again = fetch.download(Offline(), "2026-10-01", None, manifest)
    assert requested == [fetch.SUBMISSIONS] and again["entries"] == manifest["entries"]
    # A filing that cannot be listed is reported, and what is already held is kept.
    wider = fetch.download(Offline(), "2026-07-01", None, manifest)
    assert [f["accession"] for f in wider["failures"]] == ["0000027904-26-000029"]
    assert entry in wider["entries"]


def test_tracked_outputs_carry_no_contact_address_or_agent_field(check):
    manifest = json.loads(MANIFEST.read_text())
    for entry in manifest["entries"]:
        assert {"url", "retrieved_at", "sha256", "bytes", "path"} <= set(entry)
        assert entry["url"].startswith(("https://www.sec.gov/", "https://data.sec.gov/"))
    assert {i["sha256"] for i in check["inputs"]} <= {e["sha256"] for e in manifest["entries"]}
    for path in (MANIFEST, CHECK, QUARTERLY):
        text = path.read_text().lower()
        assert not re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", text), path.name
        assert "user-agent" not in text and "user_agent" not in text, path.name


# ---- against the raw store, where this machine holds it

def test_committed_outputs_rebuild_from_the_raw_store(check, require_local_cache, tmp_path):
    manifest = json.loads(MANIFEST.read_text())
    for entry in manifest["entries"]:
        if entry["kind"] == "exhibit":
            require_local_cache(entry["path"], "docs/lab_contract.md", what="raw SEC exhibit")
    result = delta.build(manifest, tmp_path / "dal_check.json", tmp_path / "dal_quarterly.csv")
    assert result["not_built"] == [] and result["arithmetic_failures"] == []
    rebuilt = json.loads((tmp_path / "dal_check.json").read_text())
    # The shared reader's own findings are reported, not asserted: that module belongs to another part.
    volatile = ("generated_at", "shared_reader")
    stale = "rebuild with: .venv/bin/python scripts/lab/fetch_dal_filings.py --offline"
    assert ({k: v for k, v in rebuilt.items() if k not in volatile}
            == {k: v for k, v in check.items() if k not in volatile}), stale
    assert (tmp_path / "dal_quarterly.csv").read_text() == QUARTERLY.read_text(), stale
    for source in json.loads((FIXTURES / "sources.json").read_text()):
        if "byte_start" in source:
            raw = (ROOT / source["source_path"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == source["source_sha256"]
            assert raw[source["byte_start"]:source["byte_end"]] == (FIXTURES / source["file"]).read_bytes()
