"""Reader, verifier and extraction script. Fixtures are synthetic: "Example Air" is invented."""
import csv
import hashlib
import json
from pathlib import Path
import sys
import types

import pytest

from app.lab import reader
from app.lab.reader import (CLAIMS, NORMALISATION, GroqPicker, RulesPicker, find_candidates, normalise,
                            parse_all, parse_value, read_document, split_candidates)
from app.lab.verify import BOUNDS, verify, verify_raw

FIXTURES = Path(__file__).parent / "fixtures" / "lab_reader"
UPDATE = (FIXTURES / "example_air_update_2031q2.html").read_bytes()
TABLES = (FIXTURES / "example_air_update_2030q1.html").read_bytes()
EPS = ("The Company expects adjusted diluted earnings per share of $1.10-$1.90 for the third quarter of 2031 "
       "and $4.20-$6.20 for the full year 2031.")


def read_update(**kwargs):
    options = {"doc_id": "exa-2031q2-ex992", "doc_url": "https://example.test/update.htm",
               "filed_on": "2031-07-16", "period_end": "2031-06-30"}
    return read_document(UPDATE, **{**options, **kwargs})


def read_tables(**kwargs):
    options = {"doc_id": "exa-2030q1-ex992", "doc_url": "https://example.test/tables.htm",
               "filed_on": "2030-04-16", "period_end": "2030-03-31"}
    return read_document(TABLES, **{**options, **kwargs})


def fact(result, claim, period):
    found = [r for r in result["records"] if r["claim"] == claim and r["period"] == period]
    assert len(found) == 1, (claim, period, [(r["claim"], r["period"]) for r in result["records"]])
    return found[0]


def outcomes(result, claim):
    return [(o["period"], o["outcome"]) for o in result["outcomes"] if o["claim"] == claim]


def page(body: str) -> bytes:
    return f"<html><body>{body}</body></html>".encode()


# ---------------------------------------------------------------- normalisation


def test_normalisation_keeps_visible_text_only_and_is_stable():
    text = normalise(UPDATE)
    assert text == normalise(UPDATE)
    for hidden in ("$7.77", "$8.88", "$9.99", "Example Air fixture", "margin: 0"):
        assert hidden not in text
    assert "approximately $4.44 based on" in text  # the non-breaking space became one space
    assert "earnings per share of $1.10-$1.90" in text  # raised footnote marker dropped, not glued on
    assert "Adjusted Diluted Earnings Per Share\n" in text  # <sup> marker dropped
    assert "up 1.5% – 2.5%" in text
    assert "  " not in text and "\n\n\n" not in text


def test_normalisation_is_nfc_and_decodes_a_declared_or_legacy_charset():
    assert normalise(page("<p>Café fares</p>")) == "Café fares"
    assert normalise(b"<html><body><p>fares \x96 up</p></body></html>") == "fares – up"
    assert normalise(b'<html><head><meta charset="iso-8859-1"></head><body><p>caf\xe9</p></body></html>') == "café"


def test_table_rows_keep_cells_apart_and_the_tab_count_is_the_grid_column():
    text = normalise(TABLES)
    header = next(line for line in text.split("\n") if "Estimated 2Q 2030" in line)
    assert header == "\t\tEstimated 2Q 2030\t\t\t\t\t\tEstimated FY 2030"
    row = next(line for line in text.split("\n") if line.startswith("Pre-tax margin\t"))
    assert row == "Pre-tax margin\t\t\t6.0\t% —\t8.0\t%"  # both footnote markers gone: not 8.02
    assert "\n\n\t\tEstimated 2Q 2030" in text  # a table boundary is one empty line
    assert "Three Months Ended March 31," in text.split("\n")


# ---------------------------------------------------------------- candidates


def test_candidates_carry_exact_offsets_and_count_repeated_text():
    text = normalise(UPDATE)
    candidates = split_candidates(text)
    assert all(text[c.char_start:c.char_end] == c.text for c in candidates)
    assert len({c.candidate_id for c in candidates}) == len(candidates)
    repeated = [c for c in candidates if c.text == EPS]
    assert [c.occurrence_index for c in repeated] == [0, 1]
    assert repeated[0].char_start < repeated[1].char_start
    assert all(c.kind == "sentence" for c in repeated)  # the first copy sits in a bullet-table cell


def test_candidate_finder_uses_cues_per_claim_type():
    candidates = split_candidates(normalise(UPDATE))
    assert {c.text for c in find_candidates(candidates, "adj_eps_guidance")} >= {EPS}
    recovery = find_candidates(candidates, "fuel_recovery_share")
    assert len(recovery) == 2 and all("recover" in c.text for c in recovery)
    assert find_candidates(candidates, "pretax_margin_guidance") == []


def test_table_row_candidates_carry_their_header():
    candidates = split_candidates(normalise(TABLES))
    row = next(c for c in candidates if c.text.startswith("Pre-tax margin\t"))
    assert row.kind == "row" and row.context_text == "\t\tEstimated 2Q 2030\t\t\t\t\t\tEstimated FY 2030"
    actual = next(c for c in candidates if c.text.startswith("Average aircraft fuel price per gallon"))
    assert actual.context is None  # "Three Months Ended" names no guided period


# ---------------------------------------------------------------- value parser


def test_quarter_and_full_year_ranges_in_one_sentence_are_two_facts():
    parsed = parse_all(EPS, "adj_eps_guidance")
    assert {p: v.value for p, v in parsed.items()} == {"2031Q3": [1.1, 1.9], "2031FY": [4.2, 6.2]}
    assert parse_value(EPS, "adj_eps_guidance", "2031Q4") is None
    result = read_update()
    assert fact(result, "adj_eps_guidance", "2031Q3")["value"] == [1.1, 1.9]
    assert fact(result, "adj_eps_guidance", "2031FY")["value"] == [4.2, 6.2]


def test_a_period_named_without_its_year_needs_the_filing_date():
    span = "For the third quarter, the Company assumes an average fuel price per gallon of approximately $4.44."
    assert parse_all(span, "fuel_assumed_usd_gal") == {}
    parsed = parse_value(span, "fuel_assumed_usd_gal", "2031Q3", filed_on="2031-07-16")
    assert (parsed.value, parsed.qualifier, parsed.period_basis) == (4.44, "approximately", "inferred_year")
    january = "The Company expects adjusted diluted EPS of $1.00 to $2.00 for the full year."
    assert parse_all(january, "adj_eps_guidance", filed_on="2031-01-20") == {}  # this year or last: not told


def test_two_ranges_that_cannot_be_told_apart_are_refused():
    trap = "The Company expects TRASM to be up 1% to 3% and up 4% to 6% in the third quarter and fourth quarter."
    assert parse_all(trap, "trasm_guidance", filed_on="2031-07-16") == {}
    assert parse_all("The Company expects adjusted diluted EPS of $1.10-$1.90 and $4.20-$6.20.", "adj_eps_guidance") == {}
    same = "The Company expects third-quarter 2031 adjusted diluted EPS of $1.10-$1.90 or $1.50-$2.30 excluding fuel."
    assert parse_value(same, "adj_eps_guidance", "2031Q3") is None
    assert outcomes(read_update(), "trasm_guidance") == [(None, "abstained")]


def test_respectively_pairs_periods_and_values_in_order():
    span = ("The Company expects third-quarter and full-year 2031 adjusted diluted EPS of $1.10-$1.90 and "
            "$4.20-$6.20, respectively.")
    assert {p: v.value for p, v in parse_all(span, "adj_eps_guidance").items()} == {"2031Q3": [1.1, 1.9], "2031FY": [4.2, 6.2]}


def test_a_prior_period_actual_next_to_guidance_is_not_guidance():
    span = ("Capacity was up 5.5% in the second quarter of 2031 and is expected to be up 3% to 4% in the third "
            "quarter of 2031 compared to the third quarter of 2030.")
    assert {p: v.value for p, v in parse_all(span, "capacity_growth").items()} == {"2031Q3": [0.03, 0.04]}
    for actual in ("Second-quarter adjusted diluted earnings per share of $2.22 was above the guidance range.",
                   "As expected, adjusted diluted earnings per share was $2.22 in the second quarter of 2031.",
                   "Adjusted diluted earnings per share was $2.22 in the second quarter of 2031, compared to $1.75 "
                   "in the second quarter of 2030."):
        assert parse_all(actual, "adj_eps_guidance", filed_on="2031-07-16") == {}
    result = read_update()
    assert {r["period"] for r in result["records"] if r["claim"] == "adj_eps_guidance"} == {"2031Q3", "2031FY"}
    assert all(r["period"] != "2031Q2" for r in result["records"])


@pytest.mark.parametrize("span, claim, period, low, high, qualifier", [
    ("The Company expects adjusted diluted EPS of $1.10 to $1.90 for the third quarter of 2031.", "adj_eps_guidance", "2031Q3", 1.1, 1.9, None),
    ("The Company expects adjusted diluted EPS of $1.10 – $1.90 for 3Q31.", "adj_eps_guidance", "2031Q3", 1.1, 1.9, None),
    ("The Company expects adjusted diluted EPS of between $4 and $6 for full-year 2031.", "adj_eps_guidance", "2031FY", 4.0, 6.0, None),
    ("The Company expects adjusted diluted EPS of 85 cents to 95 cents for Q3 2031.", "adj_eps_guidance", "2031Q3", 0.85, 0.95, None),
    ("The Company expects adjusted diluted earnings (loss) per share of ($0.85) to ($0.35) for the first quarter of 2032.", "adj_eps_guidance", "2032Q1", -0.85, -0.35, None),
    ("The Company now expects an adjusted diluted loss per share of between $0.60 and $1.00 in the first quarter 2032.", "adj_eps_guidance", "2032Q1", -1.0, -0.6, None),
    ("The Company expects a fuel price of about $2.17 per gallon for the fourth quarter of 2031.", "fuel_assumed_usd_gal", "2031Q4", 2.17, 2.17, "approximately"),
    ("The Company expects to recover 60% to 70% of the fuel price increase in the third quarter of 2031.", "fuel_recovery_share", "2031Q3", 0.6, 0.7, None),
    ("For the full year of 2031, the Company expects capital expenditures of approximately $6.5 billion.", "capex_guidance", "2031FY", 6500.0, 6500.0, "approximately"),
    ("The Company expects 2031 capital expenditures of $6,250 million to $6,750 million.", "capex_guidance", "2031FY", 6250.0, 6750.0, None),
    ("The Company expects capital expenditures of at least $6.5B in fiscal year 2031.", "capex_guidance", "2031FY", 6500.0, 6500.0, "more_than"),
    ("The Company expects to accrue between $35 million and $60 million of profit sharing in the third quarter of 2031.", "profit_sharing_accrual", "2031Q3", 35.0, 60.0, None),
    ("The Company expects third-quarter 2031 capacity to be down 1% to up 1% year-over-year.", "capacity_growth", "2031Q3", -0.01, 0.01, None),
    ("The Company expects capacity in the third and fourth quarters of 2031 to be flat to up approximately 2%.", "capacity_growth", "2031Q4", 0.0, 0.02, "approximately"),
    ("The Company expects third-quarter 2031 CASM-ex to decrease 2.5 percent to 3.5 percent.", "casm_ex_guidance", "2031Q3", -0.035, -0.025, None),
    ("The Company expects second-quarter 2031 pre-tax margin to be between 6 and 8 percent.", "pretax_margin_guidance", "2031Q2", 0.06, 0.08, None),
    ("Higher fuel prices are a headwind of $345 million in the third quarter of 2031.", "fuel_cost_impact", "2031Q3", 345.0, 345.0, None),
])
def test_value_forms(span, claim, period, low, high, qualifier):
    parsed = parse_value(span, claim, period)
    assert parsed is not None, parse_all(span, claim)
    assert (parsed.low, parsed.high, parsed.qualifier, parsed.unit) == (low, high, qualifier, CLAIMS[claim].unit)
    assert span[parsed.start:parsed.end].strip("()")[0] in "$0123456789"


def test_fuel_cost_is_read_in_dollars_and_per_share_and_is_not_an_eps_guide():
    span = ("Since the beginning of July, fuel for the Company has increased $345 million or $0.77 in adjusted "
            "diluted earnings per share for the third quarter alone.")
    assert parse_value(span, "fuel_cost_impact", "2031Q3", filed_on="2031-07-16").value == 345.0
    assert parse_value(span, "fuel_cost_impact_eps", "2031Q3", filed_on="2031-07-16").value == 0.77
    assert parse_all(span, "adj_eps_guidance", filed_on="2031-07-16") == {}
    assert parse_all(span, "fuel_assumed_usd_gal", filed_on="2031-07-16") == {}


@pytest.mark.parametrize("span, claim", [
    ("The Company expects full-year 2031 capital expenditures to be approximately $1 billion lower than 2030.", "capex_guidance"),
    ("The Company expects adjusted diluted EPS for full-year 2031 above its previous guidance of $4.20 to $6.20.", "adj_eps_guidance"),
    ("The Company raised its full-year 2031 adjusted diluted EPS guidance from $4.20 to $6.20.", "adj_eps_guidance"),
    ("For 2031, the Company will pay 10% of adjusted earnings as profit sharing up to a 6.9% pre-tax margin and 14% above it.", "pretax_margin_guidance"),
    ("Share-based compensation expense for the profit sharing calculation is estimated to be $14 million for full-year 2031.", "profit_sharing_accrual"),
    ("The Company expects to recover its costs and TRASM of 100% of plan in the third quarter of 2031.", "trasm_guidance"),
    ("The Company's guidance for the third quarter of 2031 includes TRASM of up 1% in the month of July 2031.", "trasm_guidance"),
    ("The Company expects third-quarter 2031 fuel price excluding hedges of $1.80 per gallon.", "fuel_assumed_usd_gal"),
    ("The Company expects an adjusted diluted loss per share of ($0.85) to ($0.35) for the third quarter of 2031.", "adj_eps_guidance"),
    ("In the first quarter of 2032, the Company expects $580 million of gross capital expenditures and $350 million of net capital expenditures.", "capex_guidance"),
])
def test_statements_that_are_not_the_claim_yield_nothing(span, claim):
    assert parse_all(span, claim, filed_on="2031-07-16") == {}


def test_comparison_year_is_recorded_for_changes_not_against_the_prior_year():
    span = "The Company expects fourth quarter 2031 capacity to be down approximately 23% versus fourth quarter 2029."
    parsed = parse_value(span, "capacity_growth", "2031Q4")
    assert (parsed.value, parsed.baseline_year) == (-0.23, 2029)
    result = read_document(page(f"<p>{span}</p>"), doc_id="x", doc_url="u", filed_on="2031-10-19")
    record = fact(result, "capacity_growth", "2031Q4")
    assert record["verified"] and record["baseline_year"] == 2029
    assert "change is stated against 2029, not the prior year" in record["flags"]


def test_growth_to_exceed_a_reported_figure_is_a_lower_bound_for_each_named_quarter():
    span = ("The Company expects each of the third and fourth quarters of 2031 total revenue per available seat "
            "mile (“TRASM”) growth to exceed the second quarter 2031, which was up 7.3% year-over-year.")
    parsed = parse_all(span, "trasm_guidance")
    assert {p: (v.value, v.qualifier) for p, v in parsed.items()} == {"2031Q3": (0.073, "more_than"), "2031Q4": (0.073, "more_than")}
    assert parse_all("Second quarter 2031 TRASM was up 7.3% year-over-year, ahead of guidance.", "trasm_guidance") == {}


# ---------------------------------------------------------------- tables


def test_table_row_guidance_takes_its_period_from_the_header_column():
    result = read_tables()
    fuel = fact(result, "fuel_assumed_usd_gal", "2030Q2")
    assert fuel["value"] == [1.95, 2.05] and fuel["unit"] == "usd_per_gallon" and fuel["verified"]
    assert fuel["quote"].startswith("Fuel Price Including Operating Cash-Settled Hedges (Price per Gallon)\t")
    assert fuel["context_quote"] == "\t\tEstimated 2Q 2030\t\t\t\t\t\tEstimated FY 2030"
    assert fuel["period_basis"] == "table_header"
    assert fact(result, "capacity_growth", "2030Q2")["value"] == [0.01, 0.02]
    assert fact(result, "capacity_growth", "2030FY")["value"] == [-0.005, 0.005]
    assert fact(result, "capex_guidance", "2030Q2")["value"] == [510.0, 530.0]  # label says ($M)
    assert fact(result, "capex_guidance", "2030FY")["value"] == [2100.0, 2300.0]
    only_full_year = fact(result, "profit_sharing_accrual", "2030FY")  # one value, under the second header
    assert only_full_year["value"] == [95.0, 125.0]
    assert outcomes(result, "profit_sharing_accrual") == [("2030FY", "found")]


def test_year_over_year_row_is_read_with_the_row_above_that_names_the_measure():
    result = read_tables()
    casm = fact(result, "casm_ex_guidance", "2030Q2")
    assert casm["value"] == [-0.01, 0.0] and "\n" in casm["quote"] and casm["verified"]
    assert fact(result, "casm_ex_guidance", "2030FY")["value"] == [0.005, 0.015]
    # The PRASM change two rows up is not capacity, although its label says "/ASM".
    assert {r["period"] for r in result["records"] if r["claim"] == "capacity_growth"} == {"2030Q2", "2030FY"}


def test_pre_tax_margin_guide_instead_of_eps():
    result = read_tables()
    margin = fact(result, "pretax_margin_guidance", "2030Q2")
    assert margin["value"] == [0.06, 0.08] and margin["unit"] == "fraction" and margin["verified"]
    assert outcomes(result, "adj_eps_guidance") == [(None, "abstained")]


def test_tables_of_actuals_and_restated_periods_yield_nothing():
    for result in (read_tables(), read_tables(period_end=None)):
        periods = {r["period"] for r in result["records"]}
        assert periods == {"2030Q2", "2030FY"}  # nothing for 1Q 2030 or 1Q 2029
        assert all(r["value"] not in (2.31, 2.02, 0.044) for r in result["records"])


def test_two_scenario_columns_for_one_period_are_refused():
    html = page("<p>Outlook</p><table><tr><td></td><td>4Q 2031 (Assumes early reopening)</td><td>4Q 2031 (Assumes late reopening)</td></tr>"
                "<tr><td>Adjusted diluted earnings per share</td><td>~$1.80</td><td>~$1.50</td></tr></table>")
    result = read_document(html, doc_id="x", doc_url="u", filed_on="2031-10-17")
    assert result["records"] == [] and outcomes(result, "adj_eps_guidance") == [(None, "abstained")]


def test_a_dash_before_a_table_value_whose_low_end_is_a_word_is_refused():
    html = page("<p>Outlook</p><table><tr><td></td><td>Estimated 1Q 2032</td></tr>"
                "<tr><td>CASM-ex Year-Over-Year Change Higher/(Lower)</td><td>Better</td><td>-</td><td>0.0</td><td>%</td></tr>"
                "<tr><td>Capacity Year-Over-Year Change</td><td>Flat</td><td>-</td><td>2.0</td><td>%</td></tr></table>")
    result = read_document(html, doc_id="x", doc_url="u", filed_on="2032-01-15")
    assert outcomes(result, "casm_ex_guidance") == [(None, "abstained")]
    assert fact(result, "capacity_growth", "2032Q1")["value"] == [0.0, 0.02]


def test_caption_supplies_the_forward_wording_when_the_header_has_none():
    html = page("<p>Third-Quarter 2031 Outlook</p><table><tr><td></td><td>3Q 2031</td><td>FY 2031</td></tr>"
                "<tr><td>Adjusted diluted earnings per share</td><td>$1.10 - $1.90</td><td>$4.20 - $6.20</td></tr></table>")
    result = read_document(html, doc_id="x", doc_url="u", filed_on="2031-07-16")
    record = fact(result, "adj_eps_guidance", "2031Q3")
    assert record["verified"] and record["value"] == [1.1, 1.9]
    assert record["context_quote"] == "Third-Quarter 2031 Outlook\n\n\t3Q 2031\tFY 2031"
    bare = page("<table><tr><td></td><td>3Q 2031</td></tr><tr><td>Adjusted diluted earnings per share</td><td>$1.10 - $1.90</td></tr></table>")
    assert read_document(bare, doc_id="x", doc_url="u", filed_on="2031-07-16")["records"] == []


# ---------------------------------------------------------------- pickers


def test_rules_picker_takes_the_first_of_identical_sentences():
    result = read_update()
    record = fact(result, "adj_eps_guidance", "2031Q3")
    text = normalise(UPDATE)
    assert record["occurrence_index"] == 0 and record["char_start"] == text.find(EPS)
    assert record["picked_by"] == "rules" and record["verified"]


def test_rules_picker_never_takes_a_negated_statement():
    result = read_update()
    recovery = fact(result, "fuel_recovery_share", "2031Q3")
    assert recovery["value"] == [0.6, 0.7] and "does not" not in recovery["quote"]
    assert fact(result, "fuel_recovery_share", "2031Q4")["value"] == 0.95
    assert outcomes(result, "profit_sharing_accrual") == [("2031FY", "abstained")]  # only "no longer expects"


def test_rules_picker_abstains_when_passages_disagree_and_applies_a_stated_preference():
    html = page("<p>The Company expects adjusted diluted EPS of $1.10 to $1.90 for the third quarter of 2031.</p>"
                "<p>The Company expects adjusted diluted EPS of $1.30 to $2.10 for the third quarter of 2031.</p>")
    result = read_document(html, doc_id="x", doc_url="u", filed_on="2031-07-16")
    assert result["records"] == [] and outcomes(result, "adj_eps_guidance") == [("2031Q3", "abstained")]
    candidates = split_candidates(normalise(TABLES))
    rows = [c for c in find_candidates(candidates, "fuel_assumed_usd_gal") if "Cash-Settled" in c.text]
    assert len(rows) == 2  # operating hedges, all hedges; the pre-hedge row is excluded by rule
    chosen = RulesPicker("2030-04-16").pick("fuel_assumed_usd_gal", "2030Q2", rows)
    assert next(c for c in rows if c.candidate_id == chosen).text.startswith("Fuel Price Including Operating")


def groq_reply(content, model="fake-model"):
    return {"model": model, "choices": [{"message": {"role": "assistant", "content": content}}]}


def groq_candidates():
    candidates = split_candidates(normalise(UPDATE))
    return find_candidates(candidates, "fuel_recovery_share")


def test_groq_picker_returns_a_listed_id_and_records_the_model():
    candidates = groq_candidates()
    calls = []

    def client(messages, **options):
        calls.append((messages, options))
        return groq_reply(json.dumps({"candidate_id": candidates[1].candidate_id}))

    picker = GroqPicker(client=client)
    assert picker.pick("fuel_recovery_share", "2031Q3", candidates) == candidates[1].candidate_id
    assert picker.label == "groq:fake-model"
    messages, options = calls[0]
    assert options == {"response_format": {"type": "json_object"}, "model": None, "temperature": 0, "max_tokens": 1024}
    assert all(f"[{c.candidate_id}]" in messages[1]["content"] for c in candidates)
    assert "2031Q3" in messages[1]["content"] and "fuel_recovery_share" in messages[1]["content"]


@pytest.mark.parametrize("content", [
    '{"candidate_id": "none"}',
    "s0001",
    "I think it is the second one.",
    '{"candidate_id": "s9999"}',
    '{"candidate_id": 3}',
    '{"candidate_id": null}',
    '["s0001"]',
    '{"candidate_id": "REPLACE", "value": 0.99}',
    '{"quote": "The Company expects to recover 99%"}',
    "",
])
def test_groq_picker_abstains_on_none_garbage_and_out_of_range(content):
    candidates = groq_candidates()
    content = content.replace("REPLACE", candidates[0].candidate_id)
    picker = GroqPicker(model="m", client=lambda messages, **options: groq_reply(content))
    assert picker.pick("fuel_recovery_share", "2031Q3", candidates) is None


def test_groq_picker_abstains_on_a_malformed_response_and_makes_no_call_without_candidates():
    for reply in ({}, {"choices": []}, {"choices": [{"message": {}}]}, None):
        assert GroqPicker(client=lambda messages, **options: reply).pick("fuel_recovery_share", "2031Q3", groq_candidates()) is None

    def never(messages, **options):
        raise AssertionError("no call expected")

    assert GroqPicker(client=never).pick("fuel_recovery_share", "2031Q3", []) is None


def test_groq_picker_imports_the_client_lazily(monkeypatch):
    def chat_completion(messages, *, tools=None, tool_choice=None, response_format=None, model=None,
                        temperature=0, max_tokens=None):
        listing = messages[1]["content"].split("Passages:\n", 1)[1]
        first = listing.split("]", 1)[0].lstrip("[")
        return groq_reply(json.dumps({"candidate_id": first}), model="fake/llm-1")

    monkeypatch.setitem(sys.modules, "app.lab.groq_client", types.SimpleNamespace(chat_completion=chat_completion))
    result = read_update(picker=GroqPicker())
    record = fact(result, "adj_eps_guidance", "2031Q3")
    assert record["picked_by"] == "groq:fake/llm-1" and record["verified"] and record["value"] == [1.1, 1.9]


def test_a_model_that_picks_the_negated_passage_cannot_get_it_verified():
    def client(messages, **options):
        listing = messages[1]["content"].split("Passages:\n", 1)[1]
        negated = [line for line in listing.split("\n") if "does not expect" in line]
        chosen = negated[0] if negated else listing.split("\n")[0]
        return groq_reply(json.dumps({"candidate_id": chosen.split("]", 1)[0].lstrip("[")}))

    result = read_update(picker=GroqPicker(client=client), claims=["fuel_recovery_share"])
    record = fact(result, "fuel_recovery_share", "2031Q3")
    assert record["value"] == 1.0 and not record["verified"]
    assert "statement is negated or withdrawn in the quote" in record["reasons"]
    assert ("2031Q3", "failed") in outcomes(result, "fuel_recovery_share")


# ---------------------------------------------------------------- verifier


def verified_fuel():
    result = read_update()
    return fact(result, "fuel_assumed_usd_gal", "2031Q3"), normalise(UPDATE), result["doc_sha256"]


def test_reader_records_have_the_evidence_shape_and_verify():
    record, text, sha = verified_fuel()
    assert record["id"] == "exa-2031q2-ex992-fuel-assumption-2031q3"
    for key in ("id", "claim", "period", "value", "unit", "quote", "doc_url", "doc_sha256", "char_start", "char_end",
                "picked_by", "verified"):
        assert key in record
    assert (record["value"], record["unit"], record["qualifier"]) == (4.44, "usd_per_gallon", "approximately")
    assert text[record["char_start"]:record["char_end"]] == record["quote"]
    assert record["doc_sha256"] == hashlib.sha256(UPDATE).hexdigest() == sha
    assert record["normalisation"] == NORMALISATION and record["verified"] and record["reasons"] == []
    assert verify(record, text, doc_sha256=sha)["verified"]
    assert verify_raw(record, UPDATE)["verified"]
    unchecked = verify(record, text)
    assert unchecked["verified"] and "document hash not checked" in unchecked["flags"]
    assert all(r["verified"] for r in read_update()["records"])
    assert set(BOUNDS) == set(CLAIMS)


def test_verifier_rejects_a_quote_altered_by_one_character():
    record, text, sha = verified_fuel()
    altered = dict(record, quote=record["quote"].replace("$4.44", "$4.45"))
    assert altered["quote"] != record["quote"] and len(altered["quote"]) == len(record["quote"])
    checked = verify(altered, text, doc_sha256=sha)
    assert not checked["verified"]
    assert "quote is not the exact document text at char_start:char_end" in checked["reasons"]


def test_verifier_rejects_a_wrong_offset():
    record, text, sha = verified_fuel()
    for shift in (1, -1):
        moved = dict(record, char_start=record["char_start"] + shift, char_end=record["char_end"] + shift)
        assert not verify(moved, text, doc_sha256=sha)["verified"]
    assert not verify(dict(record, char_end=len(text) + 5), text, doc_sha256=sha)["verified"]
    assert not verify(dict(record, char_start=True), text, doc_sha256=sha)["verified"]


def test_verifier_rejects_a_value_that_is_not_in_the_quote():
    record, text, sha = verified_fuel()
    for change in ({"value": 4.45, "low": 4.45, "high": 4.45}, {"value": [4.0, 4.44]}, {"qualifier": None},
                   {"period": "2031Q4"}, {"claim": "adj_eps_guidance", "unit": "usd_per_share"}):
        checked = verify(dict(record, **change), text, doc_sha256=sha)
        assert not checked["verified"], change
    wrong = verify(dict(record, value=4.45, low=4.45, high=4.45), text, doc_sha256=sha)
    assert wrong["reasons"] == ["value does not match the value parsed from the quote"]


def test_verifier_rejects_an_out_of_bounds_value():
    html = page("<p>For the third quarter, the Company assumes an average fuel price per gallon of approximately $44.40.</p>")
    result = read_document(html, doc_id="x", doc_url="u", filed_on="2031-07-16")
    record = fact(result, "fuel_assumed_usd_gal", "2031Q3")
    assert record["value"] == 44.4 and not record["verified"]
    assert record["reasons"] == ["value outside plausibility bounds 0.5 to 8.0 usd_per_gallon"]
    assert outcomes(result, "fuel_assumed_usd_gal") == [("2031Q3", "failed")]


def test_verifier_checks_occurrence_period_hash_unit_and_picker():
    record, text, sha = verified_fuel()
    eps = fact(read_update(), "adj_eps_guidance", "2031Q3")
    second = text.find(EPS, eps["char_start"] + 1)
    moved = dict(eps, char_start=second, char_end=second + len(EPS))
    assert verify(moved, text, doc_sha256=sha)["reasons"] == ["occurrence_index does not match the position of the quote"]
    assert verify(dict(moved, occurrence_index=1), text, doc_sha256=sha)["verified"]

    cases = {
        "target period ends before the document's period": dict(record, doc_period_end="2031-12-31"),
        "doc_sha256 does not match the document": dict(record, doc_sha256="0" * 64),
        "doc_sha256 is not a SHA-256 hex digest": dict(record, doc_sha256="abc"),
        "unit must be usd_per_gallon for fuel_assumed_usd_gal": dict(record, unit="usd_per_share"),
        "unknown claim type": dict(record, claim="made_up"),
        "picked_by must be rules or groq:<model>": dict(record, picked_by="human"),
        "offsets were not produced under the reader's normalisation": dict(record, normalisation="other"),
        "document period is unknown": dict(record, doc_period_end=None, doc_filed_on=None),
    }
    for reason, bad in cases.items():
        checked = verify(bad, text, doc_sha256=sha)
        assert not checked["verified"] and reason in checked["reasons"], reason
    restated = verify(dict(record, doc_period_end="2031-09-30"), text, doc_sha256=sha)
    assert restated["verified"] and any("restates" in flag for flag in restated["flags"])
    assert not verify_raw(record, UPDATE + b" ")["verified"]


def test_verifier_flags_hedged_statements_and_fails_negated_ones():
    result = read_update()
    hedged = fact(result, "capacity_growth", "2031Q4")
    assert hedged["value"] == [-0.02, -0.01] and hedged["verified"] and "hedged: if" in hedged["flags"]
    text = normalise(UPDATE)
    quote = "The Company does not expect to recover 100% of the fuel price increase in the third quarter of 2031."
    start = text.find(quote)
    assert start > 0
    record = dict(fact(result, "fuel_recovery_share", "2031Q3"), quote=quote, char_start=start,
                  char_end=start + len(quote), value=1.0, low=1.0, high=1.0, period_basis="explicit")
    checked = verify(record, text, doc_sha256=result["doc_sha256"])
    assert checked["reasons"] == ["statement is negated or withdrawn in the quote"]


def test_verifier_binds_a_table_row_to_its_own_header():
    result = read_tables()
    text = normalise(TABLES)
    record = fact(result, "pretax_margin_guidance", "2030Q2")
    assert verify(record, text, doc_sha256=result["doc_sha256"])["verified"]
    other = "\tThree Months Ended March 31,"
    assert other not in text
    lines = text.split("\n")
    wrong_header = next(line for line in lines if line.startswith("\t1Q 2030"))
    at = text.find(wrong_header)
    swapped = dict(record, context_quote=wrong_header, context_char_start=at, context_char_end=at + len(wrong_header))
    assert not verify(swapped, text, doc_sha256=result["doc_sha256"])["verified"]
    without = {k: v for k, v in record.items() if not k.startswith("context_")}
    assert "no value re-parses from the quote for this claim type and period" in verify(without, text)["reasons"]
    partial = dict(record, quote=record["quote"][4:], char_start=record["char_start"] + 4)
    assert "table-row quote does not cover whole lines" in verify(partial, text)["reasons"]


# ---------------------------------------------------------------- script


def build_tree(root: Path, *, kind_column=True, break_checksum=False) -> Path:
    raw = root / "data" / "lab_raw" / "sec"
    files = [("0000000001-30-000010", "2030-04-16", "", "EX-99.2", "tables.htm", TABLES, "investor_update"),
             ("0000000001-31-000020", "2031-07-16", "2031-06-30", "EX-99.1", "release.htm", UPDATE, "earnings_release"),
             ("0000000001-31-000020", "2031-07-16", "", "EX-99.2", "update.htm", UPDATE, "investor_update"),
             ("0000000001-31-000021", "2031-07-17", "", "EX-99.1", "slides.htm", b"<p>never read</p>", "other")]
    rows = []
    for accession, filed, period, exhibit, name, content, kind in files:
        path = raw / accession / name
        if kind != "other":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        if break_checksum and name == "tables.htm":
            sha = "f" * 64
        row = {"accession": accession, "filed_on": filed, "period_end": period, "form": "8-K", "items": "2.02,7.01,9.01",
               "exhibit": exhibit, "filename": name, "url": f"https://example.test/{accession}/{name}", "sha256": sha,
               "path": path.relative_to(root).as_posix()}
        if kind_column:
            row["kind"] = kind
        rows.append(row)
    index = root / "data" / "lab" / "ual_filings_index.csv"
    index.parent.mkdir(parents=True)
    with index.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return index


def test_script_exits_2_with_a_clear_message_when_the_index_is_absent(tmp_path, capsys):
    from scripts.lab.extract_guidance import main

    out = tmp_path / "ual_guidance.json"
    assert main(["--index", str(tmp_path / "ual_filings_index.csv"), "--out", str(out)]) == 2
    message = capsys.readouterr().err
    assert "Filings index not found" in message and "nothing was written" in message
    assert not out.exists()


def test_script_exits_2_when_the_index_lacks_columns(tmp_path, capsys):
    from scripts.lab.extract_guidance import main

    index = tmp_path / "ual_filings_index.csv"
    index.write_text("accession,filed_on\n0000000001-31-000020,2031-07-16\n")
    assert main(["--index", str(index), "--out", str(tmp_path / "out.json")]) == 2
    assert "lacks columns" in capsys.readouterr().err and not (tmp_path / "out.json").exists()


def test_script_writes_verified_records_and_prints_coverage(tmp_path, capsys):
    from scripts.lab.extract_guidance import run

    index, out = build_tree(tmp_path), tmp_path / "data" / "lab" / "ual_guidance.json"
    assert run(index, out, root=tmp_path, raw_root=tmp_path / "data" / "lab_raw" / "sec") == 0
    records = json.loads(out.read_text())
    assert isinstance(records, list) and records and all(r["verified"] and r["picked_by"] == "rules" for r in records)
    assert len({r["id"] for r in records}) == len(records)
    by_id = {r["id"]: r for r in records}
    update = by_id["ual-2031q2-ex992-fuel-recovery-2031q3"]
    assert (update["value"], update["doc_period_end"], update["accession"]) == ([0.6, 0.7], "2031-06-30", "0000000001-31-000020")
    assert by_id["ual-2031q2-ex991-adj-eps-2031q3"]["exhibit"] == "EX-99.1"
    assert by_id["ual-2030q1-ex992-pretax-margin-2030q2"]["doc_period_end"] is None
    printed = capsys.readouterr().out
    assert "documents read: 3 of 3 selected" in printed
    assert "found / abstained / failed verification" in printed
    row = next(line for line in printed.split("\n") if line.startswith("2031-07-16 0000000001-31-000020 EX-99.2"))
    assert row.split()[3:] == ["2/0/0", "1/0/0", "2/0/0", "2/0/0", "0/1/0", "1/0/0", "1/0/0", "0/1/0", "1/0/0", "1/0/0", "0/1/0"]
    assert f"Wrote {len(records)} verified records" in printed and "Not read" not in printed


def test_script_reports_a_checksum_mismatch_and_exits_1(tmp_path, capsys):
    from scripts.lab.extract_guidance import run

    index, out = build_tree(tmp_path, break_checksum=True), tmp_path / "out.json"
    assert run(index, out, root=tmp_path, raw_root=tmp_path / "data" / "lab_raw" / "sec") == 1
    printed = capsys.readouterr().out
    assert "does not match the index checksum" in printed and "documents read: 2 of 3 selected" in printed
    assert all(r["doc_filed_on"] == "2031-07-16" for r in json.loads(out.read_text()))


def test_script_selects_ex99_exhibits_when_the_index_has_no_kind_and_keeps_ids_unique(tmp_path):
    from scripts.lab.extract_guidance import document_ids, run, selected

    index, out = build_tree(tmp_path, kind_column=False), tmp_path / "out.json"
    with index.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [selected(r) for r in rows] == [True, True, True, True]
    assert not selected(dict(rows[0], form="10-Q")) and not selected(dict(rows[0], filename="x.pdf"))
    assert run(index, out, root=tmp_path, raw_root=tmp_path / "data" / "lab_raw" / "sec") == 1  # slides.htm is absent
    later = dict(rows[2], accession="0000000001-31-000030", filed_on="2031-09-05", filename="midquarter.htm")
    ids = document_ids(rows + [later])
    assert ids[(rows[2]["accession"], "update.htm")] == "ual-2031q2-ex992"
    assert ids[(later["accession"], "midquarter.htm")] == "ual-2031q2-ex992-20310905"


def test_script_exits_3_when_the_groq_picker_is_not_configured(tmp_path, capsys, monkeypatch):
    from scripts.lab.extract_guidance import run

    class GroqNotConfigured(RuntimeError):
        pass

    def chat_completion(messages, **options):
        raise GroqNotConfigured("GROQ_API_KEY is not set")

    monkeypatch.setitem(sys.modules, "app.lab.groq_client",
                        types.SimpleNamespace(chat_completion=chat_completion, GroqNotConfigured=GroqNotConfigured))
    index, out = build_tree(tmp_path), tmp_path / "out.json"
    assert run(index, out, picker="groq", root=tmp_path, raw_root=tmp_path / "data" / "lab_raw" / "sec") == 3
    assert "GROQ_API_KEY" in capsys.readouterr().err and not out.exists()

    class GroqError(RuntimeError):
        pass

    def failing(messages, **options):
        raise GroqError("Groq rate limit reached (HTTP 429).")

    monkeypatch.setitem(sys.modules, "app.lab.groq_client", types.SimpleNamespace(chat_completion=failing))
    assert run(index, out, picker="groq", root=tmp_path, raw_root=tmp_path / "data" / "lab_raw" / "sec") == 3
    assert "Groq picker failed" in capsys.readouterr().err and not out.exists()


def test_script_runs_the_groq_picker_end_to_end_with_a_stub_client(tmp_path, capsys, monkeypatch):
    from scripts.lab.extract_guidance import run

    def chat_completion(messages, **options):
        first = messages[1]["content"].split("Passages:\n", 1)[1].split("]", 1)[0].lstrip("[")
        return groq_reply(json.dumps({"candidate_id": first}), model="stub-1")

    monkeypatch.setitem(sys.modules, "app.lab.groq_client", types.SimpleNamespace(chat_completion=chat_completion))
    index, out = build_tree(tmp_path), tmp_path / "out.json"
    assert run(index, out, picker="groq", root=tmp_path, raw_root=tmp_path / "data" / "lab_raw" / "sec") in (0, 1)
    records = json.loads(out.read_text())
    assert records and all(r["picked_by"] == "groq:stub-1" and r["verified"] for r in records)
    assert "picker: groq:stub-1" in capsys.readouterr().out


def test_revisions_are_kept_as_records_and_linked_by_date():
    from scripts.lab.extract_guidance import annotate_revisions

    def record(name, day, low, high):
        return {"id": name, "claim": "adj_eps_guidance", "period": "2031Q3", "doc_filed_on": day, "low": low, "high": high,
                "qualifier": None, "baseline_year": None}

    records = [record("release", "2031-07-16", 1.1, 1.9), record("update", "2031-07-16", 1.1, 1.9),
               record("midquarter", "2031-09-05", 1.3, 1.7), record("reaffirmed", "2031-09-20", 1.3, 1.7),
               dict(record("other-period", "2031-09-05", 9.0, 9.0), period="2031FY")]
    annotate_revisions(records)
    by_id = {r["id"]: r for r in records}
    assert len(records) == 5  # nothing is merged or overwritten
    assert (by_id["update"]["issued_on"], by_id["update"]["first_issued_on"]) == ("2031-07-16", "2031-07-16")
    assert (by_id["update"]["revised_later_on"], by_id["update"]["revised_later_id"]) == ("2031-09-05", "midquarter")
    assert by_id["release"]["revised_later_id"] == "midquarter" and by_id["release"]["revises_id"] is None
    assert by_id["midquarter"]["revises_id"] == "release" and by_id["midquarter"]["revised_later_on"] is None
    assert by_id["reaffirmed"]["revises_id"] is None and by_id["reaffirmed"]["first_issued_on"] == "2031-07-16"
    assert by_id["other-period"]["revised_later_on"] is None and by_id["other-period"]["revises_id"] is None


def test_compare_runs_both_pickers_within_a_request_budget_and_writes_nothing(tmp_path, capsys):
    from scripts.lab.extract_guidance import compare, spread

    index = build_tree(tmp_path)
    before = sorted(p.name for p in (tmp_path / "data" / "lab").iterdir())
    calls = []

    def client(messages, **options):
        calls.append(messages)
        listing = messages[1]["content"].split("Passages:\n", 1)[1].split("\n")
        if "fuel_recovery_share" in messages[1]["content"]:
            return groq_reply('{"candidate_id": "none"}', model="stub-2")
        return groq_reply(json.dumps({"candidate_id": listing[0].split("]", 1)[0].lstrip("[")}), model="stub-2")

    raw_root = tmp_path / "data" / "lab_raw" / "sec"
    assert compare(index, sample=3, max_requests=100, root=tmp_path, raw_root=raw_root, client=client) == 0
    printed = capsys.readouterr().out
    assert "picker under test: groq:stub-2; documents compared: 3" in printed and f"API requests: {len(calls)} of" in printed
    assert "same passage" in printed and "model abstained, rules picked" in printed
    assert "model picked, rules abstained" in printed  # the negated profit-sharing sentence; it fails verification
    assert "verified=False" in printed
    assert sorted(p.name for p in (tmp_path / "data" / "lab").iterdir()) == before

    calls.clear()
    assert compare(index, sample=3, max_requests=4, root=tmp_path, raw_root=raw_root, client=client) == 0
    assert len(calls) == 4 and "request budget of 4 spent" in capsys.readouterr().out
    rows = [{"filed_on": f"20{n:02d}-01-01", "accession": "a", "exhibit": "e", "filename": "f"} for n in range(10, 30)]
    assert [r["filed_on"][:4] for r in spread(rows, 5)] == ["2010", "2015", "2020", "2024", "2029"]
    assert spread(rows, 50) == rows


def test_unit_cost_rows_that_are_mainline_or_still_include_fuel_are_not_casm_ex():
    def row(label, value):
        return f"<tr><td>{label}</td><td>9.10 - 9.20</td><td>{value}</td></tr>"

    html = page("<p>Outlook</p><table><tr><td></td><td>Estimated 2Q 2030</td><td>Year-Over-Year % Change Higher/(Lower)</td></tr>"
                + row("Mainline Unit Cost Excluding Profit Sharing, Fuel &amp; Third-Party Business Expenses", "0.9% - 1.9%")
                + row("Consolidated Unit Cost Excluding Profit Sharing &amp; Third-Party Business Expenses", "(0.3%) - 1.0%")
                + row("Consolidated Unit Cost Excluding Profit Sharing, Fuel &amp; Third-Party Business Expenses", "1.25% - 2.25%")
                + "</table>")
    result = read_document(html, doc_id="x", doc_url="u", filed_on="2030-04-16")
    record = fact(result, "casm_ex_guidance", "2030Q2")
    assert record["value"] == [0.0125, 0.0225] and record["verified"]
    assert [r["claim"] for r in result["records"]] == ["casm_ex_guidance"]


def test_the_model_is_shown_the_table_header_of_a_row():
    seen = []

    def client(messages, **options):
        seen.append(messages[1]["content"])
        return groq_reply('{"candidate_id": "none"}')

    read_tables(picker=GroqPicker(client=client), claims=["pretax_margin_guidance"])
    assert "Pre-tax margin | 6.0 | % — | 8.0 | %  (table header: Estimated 2Q 2030 | Estimated FY 2030)" in seen[0]
