"""Delta's guidance and results for mid-2026, each number with the quote it was read from.

Facts come from two earnings releases (EX-99.1 of the 8-Ks filed 2026-07-10 and
2026-10-09) by fixed text rules: a labelled table row inside a named section, or
a sentence matched in full. A fact is verified when its quote is an exact
substring of the normalised exhibit text at the recorded offsets and its value
parses from that quote. What a release does not state stays null and is listed
under `not_stated`; nothing is filled from memory, news or arithmetic.

Offsets refer to the project reader's normalisation (app.lab.reader.normalise)
when that module is importable, and to the local one below otherwise; every
record names which. Rules run on a single-spaced view of either text, so the
same patterns serve both, and each match is mapped back to exact offsets.

Delta prints two fuel prices. The GAAP price is aircraft fuel expense per gallon;
the adjusted price removes mark-to-market adjustments and settlements on Monroe's
hedges. Both are kept. Guidance is footnoted as non-GAAP and described as all-in
including a refinery benefit, so it is compared with the adjusted price.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
from itertools import product
import json
import math
from pathlib import Path
import re
import unicodedata

try:
    from app.lab import reader as shared_reader
except ImportError:  # Without the project reader, offsets fall back to the local normaliser.
    shared_reader = None

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "data" / "lab"
CHECK_JSON = LAB / "dal_check.json"
QUARTERLY_CSV = LAB / "dal_quarterly.csv"
JULY = {"key": "2026Q2", "accession": "0000027904-26-000029", "filed_on": "2026-07-10", "quarter_end": "June 30",
        "actuals": "actuals_jun_quarter", "guidance": "guidance_sep_quarter"}
OCTOBER = {"key": "2026Q3", "accession": "0000027904-26-000035", "filed_on": "2026-10-09",
           "quarter_end": "September 30", "actuals": "actuals_sep_quarter", "guidance": "guidance_dec_quarter"}
LOCAL = "dal-local-text-v1"  # document_text below: one space at every block or cell boundary, NFC, single-spaced.
SKIP = {"script", "style"}
BREAKS = {"address", "blockquote", "body", "br", "caption", "center", "dd", "div", "dl", "dt", "h1", "h2", "h3", "h4",
          "h5", "h6", "hr", "html", "li", "ol", "p", "pre", "table", "tbody", "td", "tfoot", "th", "thead", "title",
          "tr", "ul"}
N = r"\(?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)?"
VALUE_TOKEN = re.compile(rf"(?<![\w.,(])(?P<n>{N})(?:(?P<cents>¢|-cents?\b| cents?\b)|x\b)?(?!\w|\.\d)")
MONTH_DATE = re.compile(r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
                        r" \d{1,2}, \d{4}")
RANGE = re.compile(rf"(?P<low>\$?{N}) ?%? ?(?:-|–|—|to) ?(?P<high>\$?{N})")
PERCENT = re.compile(rf"(?P<n>{N}) ?(?:%|percent\b)")


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
        elif tag in BREAKS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in BREAKS:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def document_text(content: bytes | str) -> str:
    """Visible text of an exhibit in document order, single-spaced. Offsets in evidence index this string."""
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            content = content.decode("cp1252")
    parser = _Text()
    parser.feed(content)
    parser.close()
    return normalise("".join(parser.parts))


def exhibit_text(content: bytes, normaliser: str | None = None) -> tuple[str, str]:
    """(normalised text, label of the normalisation). The shared reader's text when it is available."""
    if normaliser != LOCAL and shared_reader is not None:
        return shared_reader.normalise(content), shared_reader.NORMALISATION
    if normaliser not in (None, LOCAL):
        raise ValueError(f"Normalisation {normaliser!r} is not available")
    return document_text(content), LOCAL


def flat_view(text: str) -> tuple[str, list[int]]:
    """A single-spaced copy of a normalised text and, for each of its characters, the offset in that text."""
    view, index = [], []
    for match in re.finditer(r"\s+|\S+", text):
        if not match.group(0)[0].isspace():
            view.append(match.group(0))
            index.extend(range(match.start(), match.end()))
        elif view:
            view.append(" ")
            index.append(match.start())
    return "".join(view), index


def document(content: bytes, normaliser: str | None = None, **fields) -> dict:
    """An exhibit ready for the rules: `text` is what offsets index, `view` is what patterns search."""
    text, label = exhibit_text(content, normaliser)
    view, index = flat_view(text)
    return {**fields, "text": text, "normalisation": label, "view": view, "index": index}


def number(token: str) -> int | float:
    """'1,604' -> 1604, '(405)' -> -405, '$2.44' -> 2.44, '13.9%' -> 13.9. Parentheses mean negative."""
    body = token.strip().replace("$", "").replace("%", "").replace(" ", "")
    negative = body.startswith("(") and body.endswith(")")
    body = body[1:-1] if negative else body
    if not re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", body):
        raise ValueError(f"Not a printed number: {token!r}")
    value = float(body.replace(",", "")) if "." in body else int(body.replace(",", ""))
    return -value if negative else value


def half_unit(token: str) -> float:
    """Half of the last printed digit: the most a rounded figure can differ from the unrounded one."""
    digits = re.sub(r"[^\d.]", "", token)
    return 0.5 * 10 ** -(len(digits.split(".")[1]) if "." in digits else 0)


def parse_range(text: str) -> tuple[float, float]:
    """'$2.00 - $2.50', '$2.00 to $2.50', '11% - 13%', '11 to 13 percent' -> (low, high)."""
    match = RANGE.search(text)
    if not match:
        raise ValueError(f"No range in {text!r}")
    low, high = number(match["low"]), number(match["high"])
    if low > high:
        raise ValueError(f"Range runs backwards in {text!r}")
    return low, high


def parse_percent(text: str) -> float:
    """'15 percent', '12.4%', '(7)%', 'Approx. 20%' -> the percentage as printed."""
    match = PERCENT.search(text)
    if not match:
        raise ValueError(f"No percentage in {text!r}")
    return number(match["n"])


def quote_values(quote: str, unit: str = "") -> list[float]:
    """Every number printed in a quote. A cents figure reads in dollars when the fact's unit is in dollars."""
    return [number(m["n"]) / (100 if m["cents"] and unit.startswith("usd") else 1) for m in VALUE_TOKEN.finditer(quote)]


def quote_dates(quote: str) -> list[str]:
    return [datetime.strptime(m.group(0), "%B %d, %Y").date().isoformat() for m in MONTH_DATE.finditer(quote)]


def verify(record: dict, text: str) -> bool:
    """The contract's rule: exact substring at the offsets, and the value parses from the quote."""
    quote, value = record.get("quote"), record.get("value")
    if not quote or quote != quote.strip() or text[record["char_start"]:record["char_end"]] != quote:
        return False
    if record["unit"] == "text":
        return value is None  # A statement: there is no number to parse.
    if record["unit"] == "date":
        return value in quote_dates(quote)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return any(math.isclose(value, v, rel_tol=0, abs_tol=1e-9) for v in quote_values(quote, record["unit"]))


def shift(period: str, quarters: int) -> str:
    index = int(period[:4]) * 4 + int(period[5]) - 1 + quarters
    return f"{index // 4}Q{index % 4 + 1}"


def fact(doc: dict, path: str, key: str, period: str, unit: str, span: tuple[int, int], printed: str | None = None,
         **extra) -> dict:
    """One evidence record, plus where it goes in the output (`_path`, `_key`) and how it was printed.

    `span` is in view coordinates; the record carries the matching offsets of the normalised text.
    """
    if unit == "text":
        value = None
    elif unit == "date":
        value = datetime.strptime(printed, "%B %d, %Y").date().isoformat()
    else:
        value = number(re.sub(r"(?:¢|-cents?| cents?|x)$", "", printed))
        value = value / 100 if re.search(r"(?:¢|cents?)$", printed) else value
    start, end = doc["index"][span[0]], doc["index"][span[1] - 1] + 1
    quote = doc["text"][start:end]
    if "text" in extra and extra["text"] not in quote:
        raise ValueError(f"{doc['key']}: {extra['text']!r} is not in its own quote")
    record = {"id": f"dal-{doc['key']}-ex991-{period}-{key}".lower().replace("_", "-"), "claim": key,
              "period": period, "value": value, "unit": unit, "quote": quote, "doc_url": doc["url"],
              "doc_sha256": doc["sha256"], "char_start": start, "char_end": end, "picked_by": "rules",
              "verified": False, "normalisation": doc["normalisation"]}
    record["verified"] = verify(record, doc["text"])
    return {**record, "_path": path, "_key": key, "_printed": printed, **{f"_{k}": v for k, v in extra.items()}}


# Table sections: (start pattern, end pattern). A row is looked up only between the two.
SECTIONS = {
    "gaap": (r"GAAP \$ Change % Change \(\$ in millions except per share and unit costs\)", r"Adjusted \$ Change"),
    "adjusted": (r"Adjusted \$ Change % Change \(\$ in millions except per share and unit costs\)",
                 r"About Delta Air Lines"),
    "income": (r"Consolidated Statements of Operations \(Unaudited\)", r"Passenger Revenue \(Unaudited\)"),
    "stats": (r"Statistical Summary \(Unaudited\)", r"Consolidated Statements of Cash Flows"),
    "fuel": (r"Total fuel expense, adjusted and Average fuel price per gallon, adjusted", r"(?:Six|Nine) Months Ended"),
}
# (field, section, row label, columns, unit). Columns: c current quarter, p same quarter a year earlier,
# d the dollar-change column (not read), y the percent change Delta prints.
ROWS = [
    ("rpm_m", "stats", r"Revenue passenger miles \(millions\)", "c p", "million_miles"),
    ("asm_m", "stats", r"Available seat miles \(millions\)", "c p", "million_miles"),
    ("yield_cents", "stats", r"Passenger mile yield \(cents\)", "c p y", "cents"),
    ("prasm_cents", "stats", r"Passenger revenue per available seat mile \(cents\)", "c p y", "cents"),
    ("trasm_cents", "stats", r"Total revenue per available seat mile \(cents\)", "c p y", "cents"),
    ("adj_trasm_cents", "stats", r"TRASM, adjusted - see Note A \(cents\)", "c p y", "cents"),
    ("casm_cents", "stats", r"Cost per available seat mile \(cents\)", "c p", "cents"),
    ("casm_ex_cents", "stats", r"CASM-Ex - see Note A \(cents\)", "c p y", "cents"),
    ("load_factor_pct", "stats", r"Passenger load factor", "c", "percent"),
    ("fuel_gallons_m", "stats", r"Fuel gallons consumed \(millions\)", "c p y", "million_gallons"),
    ("fuel_price_gaap_usd_gal", "stats", r"Average price per fuel gallon", "c p y", "usd_per_gallon"),
    ("fuel_price_adj_usd_gal", "stats", r"Average price per fuel gallon, adjusted - see Note A", "c p y",
     "usd_per_gallon"),
    ("passenger_revenue_musd", "income", r"Passenger", "c p d y", "usd_million"),
    ("total_revenue_musd", "income", r"Total operating revenue", "c p d y", "usd_million"),
    ("fuel_expense_gaap_musd", "income", r"Aircraft fuel and related taxes", "c p d y", "usd_million"),
    ("profit_sharing_musd", "income", r"Profit sharing", "c", "usd_million"),
    ("total_operating_expense_musd", "income", r"Total operating expense", "c", "usd_million"),
    ("operating_income_musd", "income", r"Operating Income", "c", "usd_million"),
    ("pretax_income_musd", "income", r"Income Before Income Taxes", "c", "usd_million"),
    ("income_tax_musd", "income", r"Income Tax Provision", "c", "usd_million"),
    ("net_income_musd", "income", r"Net Income", "c", "usd_million"),
    ("gaap_eps_diluted", "income", r"Diluted Earnings Per Share", "c", "usd_per_share"),
    ("diluted_shares_m", "income", r"Diluted Weighted Average Shares Outstanding", "c", "million_shares"),
    ("operating_margin_pct", "gaap", r"Operating margin", "c", "percent"),
    ("adj_operating_income_musd", "adjusted", r"Operating income", "c", "usd_million"),
    ("adj_operating_margin_pct", "adjusted", r"Operating margin", "c", "percent"),
    ("adj_pretax_income_musd", "adjusted", r"Pre-tax income", "c", "usd_million"),
    ("adj_net_income_musd", "adjusted", r"Net income", "c", "usd_million"),
    ("adj_eps_diluted", "adjusted", r"Diluted earnings per share", "c p", "usd_per_share"),
    ("adj_total_revenue_musd", "adjusted", r"Operating revenue", "c p d y", "usd_million"),
    ("non_fuel_cost_musd", "adjusted", r"Non-fuel cost1?", "c", "usd_million"),
    ("fuel_expense_adj_musd", "adjusted", r"Fuel expense", "c p d y", "usd_million"),
    ("fuel_hedge_adjustment_musd", "fuel", r"MTM adjustments and settlements on hedges", "c", "usd_million"),
]


def yoy_key(field: str) -> str:
    return re.sub(r"_(musd|cents|usd_gal|m)$", "", field) + "_yoy_pct"


def row_pattern(label: str, columns: str) -> re.Pattern:
    cell = {"c": rf"(?:\$ ?)?(?P<c>{N})(?: ?%)?", "p": rf"(?:\$ ?)?(?P<p>{N})(?: ?%)?",
            "d": rf"(?:\$ ?)?{N}", "y": rf"(?P<y>{N}) ?%"}
    return re.compile(" ".join([f"(?:{label})"] + [cell[code] for code in columns.split()]))


def section(text: str, name: str) -> tuple[int, int]:
    start = re.search(SECTIONS[name][0], text)
    end = start and re.compile(SECTIONS[name][1]).search(text, start.end())
    if not start or not end:
        raise ValueError(f"Section {name!r} not found")
    return start.end(), end.start()


def check_columns(doc: dict) -> None:
    """Refuse a release whose first numeric column is not the three months ended on its quarter end."""
    year = int(doc["key"][:4])
    title = re.search(r"Delta Air Lines Announces (\w+) Quarter (\d{4}) Financial Results", doc["view"])
    if not title or title.groups() != (doc["quarter_end"].split()[0], str(year)):
        raise ValueError(f"{doc['key']}: the exhibit title does not name this quarter")
    head = rf"Three Months Ended \w+ Months Ended {doc['quarter_end']}, {doc['quarter_end']}, "
    for name, tail in (("income", r"\(in millions, except per share data\) "), ("stats", "")):
        start, end = section(doc["view"], name)
        if not re.compile(head + tail + f"{year} {year - 1} ").match(doc["view"], start + 1, end):
            raise ValueError(f"{doc['key']}: unexpected column heading in the {name} table")


def table_facts(doc: dict) -> list[dict]:
    text, period, facts = doc["view"], doc["key"], []
    for field, name, label, columns, unit in ROWS:
        start, end = section(text, name)
        match = row_pattern(label, columns).search(text, start, end)
        if not match:
            raise ValueError(f"{period}: row {field!r} not found in the {name} table")
        facts.append(fact(doc, doc["actuals"], field, period, unit, match.span(), match["c"]))
        if "p" in columns.split():
            facts.append(fact(doc, doc["actuals"] + ".prior_year", field, shift(period, -4), unit, match.span(),
                              match["p"]))
        if "y" in columns.split():
            facts.append(fact(doc, doc["actuals"] + ".yoy_stated", yoy_key(field), period, "percent", match.span(),
                              match["y"]))
    return facts


def take(doc: dict, pattern: str, picks: list[tuple], quote: str | None = None) -> list[dict]:
    """Facts from the one place a pattern matches. picks: (group, path, key, period, unit[, extra])."""
    matches = list(re.finditer(pattern, doc["view"]))
    if len(matches) != 1:
        raise ValueError(f"{doc['key']}: expected one match, found {len(matches)}, for {pattern[:70]!r}")
    match = matches[0]
    span = match.span(quote) if quote else match.span()
    return [fact(doc, path, key, period, unit, span, match[group] if group else None, **(extra[0] if extra else {}))
            for group, path, key, period, unit, *extra in picks]


def earnings_reconciliation(doc: dict) -> str:
    year, day = doc["key"][:4], doc["quarter_end"]
    return (rf"Three Months Ended Three Months Ended {day}, {year} {day}, {year} Pre-Tax Income Net Earnings "
            rf"\(in millions, except per share data\) Income Tax Income Per Diluted Share "
            rf"GAAP \$ (?P<pt>{N}) \$ (?P<tax>{N}) \$ (?P<ni>{N}) \$ (?P<eps>{N}) Adjusted for: (?:(?!Non-GAAP).)*"
            rf"(?P<q>Non-GAAP \$ (?P<apt>{N}) \$ (?P<atax>{N}) \$ (?P<ani>{N}) \$ (?P<aeps>{N}))")


def reconciliation_facts(doc: dict) -> list[dict]:
    """Adjusted tax, refinery sales and the per-gallon hedge adjustment, from the Note A tables."""
    text, period, path = doc["view"], doc["key"], doc["actuals"]
    year, day = period[:4], doc["quarter_end"]
    revenue = (rf"\(in millions\) {day}, {year} \w+ \d+, \d{{4}} \w+ \d+, \d{{4}} Operating revenue "
               rf"\$ {N} \$ {N} \$ {N} Adjusted for: (?P<q>Third-party refinery sales (?P<sales>{N}))")
    start, end = section(text, "fuel")
    hedge = re.compile(rf"MTM adjustments and settlements on hedges {N} {N} (?P<gal>{N}) {N}").search(text, start, end)
    if not hedge:
        raise ValueError(f"{period}: fuel reconciliation row not found")
    return (take(doc, earnings_reconciliation(doc), [("atax", path, "adj_income_tax_musd", period, "usd_million")], "q")
            + take(doc, revenue, [("sales", path, "third_party_refinery_sales_musd", period, "usd_million")], "q")
            + [fact(doc, path, "fuel_hedge_adjustment_usd_gal", period, "usd_per_gallon", hedge.span(), hedge["gal"])])


def bullet_facts(doc: dict) -> list[dict]:
    """Cost bullets both releases carry: the refinery benefit inside the adjusted price, and fuel efficiency."""
    period, path = doc["key"], doc["actuals"]
    fuel = (rf"Adjusted fuel price of \${N} per gallon increased {N} percent year-over-year with a refinery benefit "
            rf"of (?P<benefit>\d+¢) per gallon(?: inclusive of a (?P<outage>\d+¢) discrete impact from a temporary "
            rf"refinery outage)?")
    picks = [("benefit", path, "refinery_benefit_usd_gal", period, "usd_per_gallon")]
    found = re.search(fuel, doc["view"])
    if found and found["outage"]:
        picks.append(("outage", path, "refinery_outage_impact_usd_gal", period, "usd_per_gallon"))
    return (take(doc, fuel, picks)
            + take(doc, r"Fuel efficiency, defined as gallons per 1,000 ASMs, was (?P<v>\d+\.\d)",
                   [("v", path, "fuel_efficiency_gal_per_1000_asm", period, "gallons_per_1000_asm")]))


# The shared reader drops raised footnote markers; the local text keeps them. Both spellings are accepted.
BASIS = r"(?:1 )?Non-GAAP measures; Refer to Non-GAAP reconciliations for historical comparison figures"
APPROX = {"qualifier": "approximately"}


def fuel_rule(doc: dict, quarter: str, benefit: str) -> tuple:
    g, period = doc["guidance"], shift(doc["key"], 1)
    return (rf"Guidance for the {quarter} quarter assumes fuel at the forward curve as of "
            rf"(?P<day>{MONTH_DATE.pattern}), and includes a refinery benefit of {benefit} per gallon\. "
            r"This results in a projected all-in fuel price for the quarter of approximately "
            r"(?P<f>\$\d\.\d\d) per gallon\.",
            [("f", g, "fuel_price_assumed_usd_gal", period, "usd_per_gallon", APPROX),
             ("r", g, "refinery_benefit_usd_gal", period, "usd_per_gallon",
              APPROX if "approximately" in benefit else {}),
             ("day", g, "fuel_curve_as_of", period, "date")])


def july_rules(doc: dict) -> list[tuple]:
    g, period, year = doc["guidance"], shift(doc["key"], 1), doc["key"][:4] + "FY"
    return [
        (r"In the September quarter, we expect earnings per share to grow over prior year to "
         r"(?P<a>\$\d\.\d\d) to (?P<b>\$\d\.\d\d) on an operating margin of (?P<c>\d+) to (?P<d>\d+) percent",
         [("a", g, "eps_low", period, "usd_per_share"), ("b", g, "eps_high", period, "usd_per_share"),
          ("c", g, "operating_margin_low_pct", period, "percent"),
          ("d", g, "operating_margin_high_pct", period, "percent")]),
        (r"3Q26 Total Revenue YoY \(%\) Up Mid-Teens Operating Margin \d+% - \d+% Earnings Per Share "
         r"\$\d\.\d\d - \$\d\.\d\d",
         [(None, g, "revenue_growth_text", period, "text", {"text": "Up Mid-Teens"})]),
        (r"For the September quarter, we expect revenue to grow mid-teens over prior year on modest "
         r"capacity growth, with unit revenue growth improving sequentially\.",
         [(None, g, "capacity_text", period, "text", {"text": "modest capacity growth"}),
          (None, g, "unit_revenue_text", period, "text",
           {"text": "unit revenue growth improving sequentially"})]),
        fuel_rule(doc, "September", r"(?P<r>\d+-cents)"),
        (r"Non\u2011fuel unit cost performance is expected to improve modestly from the June quarter with "
         r"further progression in the December quarter as capacity growth begins to normalize\.",
         [(None, g, "non_fuel_unit_cost_text", period, "text",
           {"text": "expected to improve modestly from the June quarter"})]),
        (BASIS, [(None, g, "basis_text", period, "text", {"text": "Non-GAAP measures"})]),
        (r"Affirming full-year guidance for adjusted EPS of (?P<a>\$\d\.\d\d) to (?P<b>\$\d\.\d\d) and "
         r"free cash flow of (?P<c>\$\d+) to (?P<d>\$\d+) billion",
         [("a", g + ".full_year", "eps_low", year, "usd_per_share"),
          ("b", g + ".full_year", "eps_high", year, "usd_per_share"),
          ("c", g + ".full_year", "free_cash_flow_low_busd", year, "usd_billion"),
          ("d", g + ".full_year", "free_cash_flow_high_busd", year, "usd_billion")]),
        (r"Gross Leverage2? Approx\. (?P<x>\d+(?:\.\d+)?x)",
         [("x", g + ".full_year", "gross_leverage_x", year, "times_ebitdar", APPROX)]),
        (r"For the full year, we are affirming the guidance we set at the start of the year to grow "
         r"earnings by 20 percent, overcoming a multi-billion dollar fuel headwind\.",
         [(None, g + ".fuel_recovery_through_pricing", "nearest_statement_1", year, "text")]),
        (r"With continued momentum across customer segments and diverse revenue streams, we are "
         r"confident in the sustainability of yield and revenue strength\.",
         [(None, g + ".fuel_recovery_through_pricing", "nearest_statement_2", period, "text")])]


OCTOBER_TABLE = (r"4Q26 FY 2026 Total Revenue YoY \(%\) (?P<rev>Approx\. \d+%) Earnings Per Share "
                 r"(?P<fy>\$\d\.\d\d - \$\d\.\d\d) Operating Margin (?P<c>\d+)% - (?P<d>\d+)% Free Cash Flow \(\$B\) "
                 r"Approx\. (?P<fcf>\$\d+(?:\.\d+)?) Earnings Per Share (?P<eps>\$\d\.\d\d - \$\d\.\d\d) "
                 r"Gross Leverage2? Approx\. (?P<x>\d+(?:\.\d+)?x)")


def october_rules(doc: dict) -> list[tuple]:
    g, now, period, year = doc["guidance"], doc["key"], shift(doc["key"], 1), doc["key"][:4] + "FY"
    why = "management_explanation"
    return [
        (r"In the December quarter, we expect earnings of (?P<a>\$\d\.\d\d) to (?P<b>\$\d\.\d\d) per share, "
         r"based on a fuel price of approximately \$\d\.\d\d per gallon\.",
         [("a", g, "eps_low", period, "usd_per_share"), ("b", g, "eps_high", period, "usd_per_share")]),
        (OCTOBER_TABLE,
         [("c", g, "operating_margin_low_pct", period, "percent"),
          ("d", g, "operating_margin_high_pct", period, "percent"),
          (None, g, "revenue_growth_text", period, "text", {"text": "Approx. 20%"}),
          ("x", g + ".full_year", "gross_leverage_x", year, "times_ebitdar", APPROX)]),
        (r"Revenue momentum is continuing in the December quarter, with strength across all products and "
         r"geographies, supporting our outlook for total revenue growth of approximately (?P<r>\d+) "
         r"percent over last year\. With seats growing less than (?P<s>\d+) percent, including a "
         r"reduction in Main Cabin seats, our capacity positioning supports another quarter of "
         r"sequential improvement in unit revenue with progression in both Domestic and International\.",
         [("r", g, "revenue_growth_yoy_pct", period, "percent", APPROX),
          ("s", g, "seats_growth_yoy_pct", period, "percent", {"qualifier": "less than"}),
          (None, g, "capacity_text", period, "text", {"text": "seats growing less than 2 percent"}),
          (None, g, "unit_revenue_text", period, "text",
           {"text": "another quarter of sequential improvement in unit revenue"})]),
        fuel_rule(doc, "December", r"approximately (?P<r>\$\d\.\d\d)"),
        (r"With slightly higher capacity and continued operational efficiencies, we expect December "
         r"quarter non-fuel unit cost growth to improve 1 to 2 points sequentially\.",
         [(None, g, "non_fuel_unit_cost_text", period, "text",
           {"text": "non-fuel unit cost growth to improve 1 to 2 points sequentially"})]),
        (BASIS, [(None, g, "basis_text", period, "text", {"text": "Non-GAAP measures"})]),
        (r"Outlook for full-year EPS of (?P<a>\$\d\.\d\d) to (?P<b>\$\d\.\d\d) and free cash flow of "
         r"approximately (?P<c>\$\d+(?:\.\d+)?) billion",
         [("a", g + ".full_year", "eps_low", year, "usd_per_share"),
          ("b", g + ".full_year", "eps_high", year, "usd_per_share"),
          ("c", g + ".full_year", "free_cash_flow_busd", year, "usd_billion", APPROX)]),
        (r"For the full year, we expect to generate a pre-tax profit of roughly (?P<p>\$\d+(?:\.\d+)?) "
         r"billion, absorbing a (?P<f>\$\d+) billion increase in fuel costs\.",
         [("p", g + ".full_year", "pretax_profit_busd", year, "usd_billion", {"qualifier": "roughly"}),
          ("f", g + ".full_year", "fuel_cost_increase_busd", year, "usd_billion"),
          (None, why, "full_year_fuel", year, "text",
           {"speaker": "Ed Bastian, chief executive officer",
            "summary": "The full-year outlook is framed as absorbing a large increase in fuel cost."})]),
        (r"Delta delivered earnings of \$\d\.\d\d per share in the September quarter, in line with last "
         r"year while absorbing more than (?P<m>\$\d+) million of higher fuel costs compared to our "
         r"guidance in early July\.",
         [("m", why, "fuel_cost_above_july_guidance_musd", now, "usd_million",
           {"qualifier": "more than", "speaker": "Erik Snell, chief financial officer",
            "summary": "Fuel cost more than the July guidance assumed. It is the only gap to guidance the "
                       "release puts a number on."})]),
        (r"September quarter revenue grew approximately \d+ percent over prior year, on flat capacity, "
         r"driven by broad demand strength and healthy yield growth as we cover higher costs and manage "
         r"the business for margins",
         [(None, why, "revenue_and_yield", now, "text",
           {"speaker": "Joe Esposito, chief commercial officer",
            "summary": "Revenue growth on flat capacity is attributed to demand and yield, described as "
                       "covering higher costs."})]),
        (r"Adjusted total unit revenue \(TRASM\) grew \d+ percent over prior year, accelerating \d+ "
         r"points from the prior quarter\.",
         [(None, why, "unit_revenue", now, "text",
           {"speaker": "release text", "summary": "Unit revenue growth accelerated from the June quarter."})]),
        (r"September quarter non-fuel unit costs increased \d+\.\d percent over the prior year on flat "
         r"capacity, driven primarily by higher crew and revenue-related costs on capacity growth that "
         r"was several points below our original plan, including nearly one point of impact from summer "
         r"storms\.",
         [(None, why, "non_fuel_unit_cost", now, "text",
           {"speaker": "Erik Snell, chief financial officer",
            "summary": "Non-fuel unit cost rose on capacity below plan, partly from summer storms."})]),
        (r"Strength of results and outlook demonstrate Delta\u2019s structural durability in high fuel "
         r"cost environment",
         [(None, why, "headline", now, "text",
           {"speaker": "release headline",
            "summary": "The headline presents the quarter as durability under high fuel cost."})])]


def guidance_rules(doc: dict) -> list[tuple]:
    """(pattern, picks) for every sentence and guidance table read from this release."""
    return july_rules(doc) if doc["key"] == JULY["key"] else october_rules(doc)


# (name, field checked, formula, input fields, function). Every input and the stated figure come from one release.
CHECKS = [
    ("fuel_expense_gaap", "fuel_expense_gaap_musd", "fuel_gallons_m * fuel_price_gaap_usd_gal",
     ("fuel_gallons_m", "fuel_price_gaap_usd_gal"), lambda g, p: g * p),
    ("fuel_expense_adj", "fuel_expense_adj_musd", "fuel_gallons_m * fuel_price_adj_usd_gal",
     ("fuel_gallons_m", "fuel_price_adj_usd_gal"), lambda g, p: g * p),
    ("fuel_expense_adj_bridge", "fuel_expense_adj_musd", "fuel_expense_gaap_musd + fuel_hedge_adjustment_musd",
     ("fuel_expense_gaap_musd", "fuel_hedge_adjustment_musd"), lambda a, b: a + b),
    ("fuel_price_adj_bridge", "fuel_price_adj_usd_gal", "fuel_price_gaap_usd_gal + fuel_hedge_adjustment_usd_gal",
     ("fuel_price_gaap_usd_gal", "fuel_hedge_adjustment_usd_gal"), lambda a, b: a + b),
    ("gaap_eps", "gaap_eps_diluted", "net_income_musd / diluted_shares_m",
     ("net_income_musd", "diluted_shares_m"), lambda a, b: a / b),
    ("adj_eps", "adj_eps_diluted", "adj_net_income_musd / diluted_shares_m",
     ("adj_net_income_musd", "diluted_shares_m"), lambda a, b: a / b),
    ("net_income", "net_income_musd", "pretax_income_musd + income_tax_musd",
     ("pretax_income_musd", "income_tax_musd"), lambda a, b: a + b),
    ("adj_net_income", "adj_net_income_musd", "adj_pretax_income_musd + adj_income_tax_musd",
     ("adj_pretax_income_musd", "adj_income_tax_musd"), lambda a, b: a + b),
    ("operating_income", "operating_income_musd", "total_revenue_musd - total_operating_expense_musd",
     ("total_revenue_musd", "total_operating_expense_musd"), lambda a, b: a - b),
    ("adj_total_revenue", "adj_total_revenue_musd", "total_revenue_musd + third_party_refinery_sales_musd",
     ("total_revenue_musd", "third_party_refinery_sales_musd"), lambda a, b: a + b),
    ("trasm", "trasm_cents", "100 * total_revenue_musd / asm_m", ("total_revenue_musd", "asm_m"),
     lambda a, b: 100 * a / b),
    ("adj_trasm", "adj_trasm_cents", "100 * adj_total_revenue_musd / asm_m", ("adj_total_revenue_musd", "asm_m"),
     lambda a, b: 100 * a / b),
    ("prasm", "prasm_cents", "100 * passenger_revenue_musd / asm_m", ("passenger_revenue_musd", "asm_m"),
     lambda a, b: 100 * a / b),
    ("yield", "yield_cents", "100 * passenger_revenue_musd / rpm_m", ("passenger_revenue_musd", "rpm_m"),
     lambda a, b: 100 * a / b),
    ("casm", "casm_cents", "100 * total_operating_expense_musd / asm_m", ("total_operating_expense_musd", "asm_m"),
     lambda a, b: 100 * a / b),
    ("casm_ex", "casm_ex_cents", "100 * non_fuel_cost_musd / asm_m", ("non_fuel_cost_musd", "asm_m"),
     lambda a, b: 100 * a / b),
    ("operating_margin", "operating_margin_pct", "100 * operating_income_musd / total_revenue_musd",
     ("operating_income_musd", "total_revenue_musd"), lambda a, b: 100 * a / b),
    ("adj_operating_margin", "adj_operating_margin_pct", "100 * adj_operating_income_musd / adj_total_revenue_musd",
     ("adj_operating_income_musd", "adj_total_revenue_musd"), lambda a, b: 100 * a / b),
    ("load_factor", "load_factor_pct", "100 * rpm_m / asm_m", ("rpm_m", "asm_m"), lambda a, b: 100 * a / b),
    ("fuel_efficiency", "fuel_efficiency_gal_per_1000_asm", "1000 * fuel_gallons_m / asm_m",
     ("fuel_gallons_m", "asm_m"), lambda a, b: 1000 * a / b),
]
YOY_CHECKS = ("adj_trasm_cents", "trasm_cents", "prasm_cents", "yield_cents", "casm_ex_cents",
              "fuel_price_gaap_usd_gal", "fuel_price_adj_usd_gal", "fuel_expense_gaap_musd", "fuel_expense_adj_musd",
              "fuel_gallons_m", "total_revenue_musd", "adj_total_revenue_musd", "passenger_revenue_musd")


def arithmetic(name: str, period: str, formula: str, stated: dict, inputs: list[dict], function) -> dict:
    """Recompute a printed figure from other printed figures.

    Tolerance is what rounding alone allows: half a unit of the stated figure's last digit, plus the furthest
    the formula moves when every input sits at an edge of its own rounding interval.
    """
    computed = function(*(f["value"] for f in inputs))
    corners = [function(*corner) for corner in product(*((f["value"] - half_unit(f["_printed"]),
                                                          f["value"] + half_unit(f["_printed"])) for f in inputs))]
    tolerance = half_unit(stated["_printed"]) + max(abs(c - computed) for c in corners)
    difference = computed - stated["value"]
    return {"name": name, "period": period, "formula": formula,
            "inputs": {("prior_year." if f["_path"].endswith(".prior_year") else "") + f["_key"]:
                       {"value": f["value"], "evidence_id": f["id"]} for f in inputs},
            "stated": stated["value"], "stated_evidence_id": stated["id"], "computed": round(computed, 6),
            "difference": round(difference, 6), "tolerance": round(tolerance, 6),
            "ok": abs(difference) <= tolerance + 1e-9}


def checks_for(doc: dict, facts: list[dict]) -> list[dict]:
    by_key = {(f["_path"], f["_key"]): f for f in facts}
    now, before, change = doc["actuals"], doc["actuals"] + ".prior_year", doc["actuals"] + ".yoy_stated"
    result = [arithmetic(name, doc["key"], formula, by_key[now, field], [by_key[now, k] for k in inputs], function)
              for name, field, formula, inputs, function in CHECKS]
    for field in YOY_CHECKS:
        if (change, yoy_key(field)) in by_key:
            result.append(arithmetic(yoy_key(field), doc["key"], f"100 * ({field} / prior_year.{field} - 1)",
                                     by_key[change, yoy_key(field)], [by_key[now, field], by_key[before, field]],
                                     lambda a, b: 100 * (a / b - 1)))
    return result


def repeats(doc: dict, facts: list[dict]) -> list[dict]:
    """A figure the release prints in a second place must match the first."""
    by_key = {(f["_path"], f["_key"]): f for f in facts}
    text, a, g = doc["view"], doc["actuals"], doc["guidance"]
    low, high = (lambda t: parse_range(t)[0]), (lambda t: parse_range(t)[1])
    eps = re.search(earnings_reconciliation(doc), text)
    start, end = section(text, "adjusted")
    highlight = {k: row_pattern(label, "c").search(text, start, end)
                 for k, label in (("price", "Average fuel price per gallon"), ("trasm", r"TRASM \(cents\)"),
                                  ("casm_ex", r"Non-fuel unit cost \(CASM-Ex\) \(cents\)"))}
    start, end = section(text, "fuel")
    fuel = re.compile(rf"Total fuel expense, adjusted \$ (?P<e>{N}) \$ {N} {N} % \$ (?P<p>{N})").search(text, start,
                                                                                                        end)
    bullet = re.search(rf"Adjusted fuel price of (?P<p>\${N}) per gallon increased (?P<y>{N} percent)", text)
    trasm = re.search(rf"Adjusted total unit revenue \(TRASM\) grew (?P<y>{N} percent) over prior year", text)
    pairs = [(a, "pretax_income_musd", eps, "pt", number), (a, "income_tax_musd", eps, "tax", number),
             (a, "net_income_musd", eps, "ni", number), (a, "gaap_eps_diluted", eps, "eps", number),
             (a, "adj_pretax_income_musd", eps, "apt", number), (a, "adj_net_income_musd", eps, "ani", number),
             (a, "adj_eps_diluted", eps, "aeps", number),
             (a, "fuel_price_adj_usd_gal", highlight["price"], "c", number),
             (a, "adj_trasm_cents", highlight["trasm"], "c", number),
             (a, "casm_ex_cents", highlight["casm_ex"], "c", number), (a, "fuel_expense_adj_musd", fuel, "e", number),
             (a, "fuel_price_adj_usd_gal", fuel, "p", number), (a, "fuel_price_adj_usd_gal", bullet, "p", number),
             (a + ".yoy_stated", "fuel_price_adj_yoy_pct", bullet, "y", parse_percent),
             (a + ".yoy_stated", "adj_trasm_yoy_pct", trasm, "y", parse_percent)]
    if doc["key"] == JULY["key"]:
        table = re.search(r"3Q26 Total Revenue YoY \(%\) Up Mid-Teens Operating Margin (?P<m>\d+% - \d+%) Earnings "
                          r"Per Share (?P<eps>\$\d\.\d\d - \$\d\.\d\d)", text)
        year = re.search(r"FY 2026 Earnings Per Share (?P<eps>\$\d\.\d\d - \$\d\.\d\d) Free Cash Flow \(\$B\) "
                         r"(?P<fcf>\$\d+ - \$\d+)", text)
        pairs += [(g, "eps_low", table, "eps", low), (g, "eps_high", table, "eps", high),
                  (g, "operating_margin_low_pct", table, "m", low), (g, "operating_margin_high_pct", table, "m", high),
                  (g + ".full_year", "eps_low", year, "eps", low), (g + ".full_year", "eps_high", year, "eps", high),
                  (g + ".full_year", "free_cash_flow_low_busd", year, "fcf", low),
                  (g + ".full_year", "free_cash_flow_high_busd", year, "fcf", high)]
    else:
        table = re.search(OCTOBER_TABLE, text)
        cfo = re.search(r"based on a fuel price of approximately (?P<f>\$\d\.\d\d) per gallon", text)
        pairs += [(g, "eps_low", table, "eps", low), (g, "eps_high", table, "eps", high),
                  (g, "revenue_growth_yoy_pct", table, "rev", parse_percent),
                  (g + ".full_year", "eps_low", table, "fy", low), (g + ".full_year", "eps_high", table, "fy", high),
                  (g + ".full_year", "free_cash_flow_busd", table, "fcf", number),
                  (g, "fuel_price_assumed_usd_gal", cfo, "f", number)]
    result = []
    for path, key, match, group, read in pairs:
        if not match:
            raise ValueError(f"{doc['key']}: second printing of {key} not found")
        first, again = by_key[path, key], read(match[group])
        result.append({"field": f"{path}.{key}", "evidence_id": first["id"], "value": first["value"],
                       "printed_again": match[group], "read_as": again,
                       "context": text[max(match.start(), match.start(group) - 60):match.end(group)],
                       "agree": math.isclose(first["value"], again, abs_tol=1e-9)})
    return result


# Wording whose absence backs a `not_stated` entry. Counted in each release, so the claim can be rechecked.
ABSENT = [
    ("fuel_recovery_through_pricing", r"(?i)recover|recaptur|pass(?:ed|es|ing)? (?:\w+ )?through|surcharge|"
                                      r"\bfares?\b|\bpricing\b|\boffset"),
    ("income_tax_rate", r"(?i)tax rate (?:of|was|is|at) |(?:%|percent) (?:effective )?tax rate"),
    ("guidance_missed_wording", r"(?i)\bmiss(?:ed)?\b|below (?:our )?guidance|shortfall|fell short"),
]
NOT_STATED = {
    "guidance_sep_quarter": [
        "revenue_growth_yoy_pct: printed only as 'Up Mid-Teens' / 'mid-teens'; no number",
        "capacity_growth_yoy_pct: printed only as 'modest capacity growth'; no number",
        "fuel_recovery_through_pricing: no share, amount or timetable for recovering fuel cost through fares; "
        "the words recover, recapture, pricing, fare, surcharge and offset do not occur in the release",
        "non_fuel_unit_cost_growth_pct: printed only as 'improve modestly from the June quarter'",
        "income_tax_rate, diluted_shares: not guided",
        "fuel_price_basis: the release does not use the word 'adjusted' for the assumed price; it calls it "
        "'all-in', says it includes the refinery benefit, and footnotes the guidance table as non-GAAP",
    ],
    "guidance_dec_quarter": [
        "capacity_growth_yoy_pct: only 'seats growing less than 2 percent'; no available-seat-mile figure",
        "non_fuel_unit_cost_growth_pct: only 'improve 1 to 2 points sequentially'",
        "fuel_recovery_through_pricing: no share, amount or timetable; the nearest wording is 'healthy yield "
        "growth as we cover higher costs' (see management_explanation)",
        "income_tax_rate, diluted_shares: not guided",
    ],
    "actuals": [
        "income_tax_rate_pct: neither release prints a tax rate; the tax provision and pre-tax income are printed "
        "and their ratio is given under derived",
        "asm_m year-over-year percent: printed rounded to a whole percent (a dash in the September quarter), so "
        "only the two levels are recorded",
    ],
    "management_explanation": [
        "The October release never says guidance was missed and gives no per-share effect of the fuel overrun.",
        "It does not split the gap to guidance between fuel, revenue and non-fuel cost.",
    ],
}


def load(manifest: dict, spec: dict) -> dict:
    """The EX-99.1 of one release, checked against its manifest entry, with its normalised text."""
    entries = [e for e in manifest["entries"] if e.get("kind") == "exhibit" and e["accession"] == spec["accession"]]
    if len(entries) != 1 or entries[0]["exhibit"] != "EX-99.1" or entries[0]["filed_on"] != spec["filed_on"]:
        raise ValueError(f"Manifest does not hold exactly one EX-99.1 for {spec['accession']}")
    entry = entries[0]
    if entry["form"] != "8-K" or "2.02" not in entry["items"].split(","):
        raise ValueError(f"{spec['accession']} is not an item 2.02 8-K")
    content = (ROOT / entry["path"]).read_bytes()
    if hashlib.sha256(content).hexdigest() != entry["sha256"] or len(content) != entry["bytes"]:
        raise ValueError(f"Raw exhibit for {spec['accession']} failed its checksum")
    return document(content, **spec, url=entry["url"], sha256=entry["sha256"], path=entry["path"], raw=content)


def extract(doc: dict) -> list[dict]:
    check_columns(doc)
    facts = (table_facts(doc) + reconciliation_facts(doc) + bullet_facts(doc)
             + [f for pattern, picks in guidance_rules(doc) for f in take(doc, pattern, picks)])
    ids = [f["id"] for f in facts]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{doc['key']}: two facts share an evidence id")
    unverified = [f["id"] for f in facts if not f["verified"]]
    if unverified:
        raise ValueError(f"{doc['key']}: quotes failed verification: {unverified}")
    return facts


def place(sections: dict, item: dict) -> None:
    node = sections
    for part in item["_path"].split("."):
        node = node.setdefault(part, {})
    node[item["_key"]] = item.get("_text", item["quote"]) if item["unit"] == "text" else item["value"]
    node.setdefault("evidence_ids", {})[item["_key"]] = item["id"]
    if "_qualifier" in item:
        node.setdefault("qualifiers", {})[item["_key"]] = item["_qualifier"]


def ordered(node: dict) -> dict:
    """Plain fields first, then nested blocks, at every level."""
    plain = {k: v for k, v in node.items() if not isinstance(v, (dict, list))}
    nested = {k: ordered(v) if isinstance(v, dict) and k != "evidence_ids" else v for k, v in node.items()
              if isinstance(v, (dict, list))}
    return {**plain, **nested}


def compare(by_key: dict, guidance: str, actuals: str) -> dict:
    """Guided against reported for one quarter: differences of printed figures only, no model."""
    def v(path, key):
        return by_key[path, key]["value"]

    def ids(*keys):
        return [by_key[k]["id"] for k in keys]

    def position(value, low, high):
        return "below_range" if value < low else "above_range" if value > high else "inside_range"
    low, high, eps = v(guidance, "eps_low"), v(guidance, "eps_high"), v(actuals, "adj_eps_diluted")
    margin_low, margin_high = v(guidance, "operating_margin_low_pct"), v(guidance, "operating_margin_high_pct")
    margin = v(actuals, "adj_operating_margin_pct")
    assumed, paid = v(guidance, "fuel_price_assumed_usd_gal"), v(actuals, "fuel_price_adj_usd_gal")
    benefit_assumed, benefit = v(guidance, "refinery_benefit_usd_gal"), v(actuals, "refinery_benefit_usd_gal")
    gallons, shares = v(actuals, "fuel_gallons_m"), v(actuals, "diluted_shares_m")
    tax_rate = -v(actuals, "adj_income_tax_musd") / v(actuals, "adj_pretax_income_musd")
    extra = (paid - assumed) * gallons
    stated = by_key["management_explanation", "fuel_cost_above_july_guidance_musd"]
    before, after = JULY["actuals"] + ".yoy_stated", actuals + ".yoy_stated"
    return {
        "note": "Differences of figures printed in the two releases. Arithmetic only: not a model or a forecast.",
        "eps": {"guided_low": low, "guided_high": high, "guided_mid": round((low + high) / 2, 4), "actual_adj": eps,
                "actual_minus_low": round(eps - low, 4), "actual_minus_mid": round(eps - (low + high) / 2, 4),
                "position": position(eps, low, high),
                "evidence_ids": ids((guidance, "eps_low"), (guidance, "eps_high"), (actuals, "adj_eps_diluted"))},
        "operating_margin_pct": {
            "guided_low": margin_low, "guided_high": margin_high, "actual_adj": margin,
            "actual_minus_low": round(margin - margin_low, 4), "position": position(margin, margin_low, margin_high),
            "evidence_ids": ids((guidance, "operating_margin_low_pct"), (guidance, "operating_margin_high_pct"),
                                (actuals, "adj_operating_margin_pct"))},
        "fuel_price_usd_gal": {
            "assumed_all_in": assumed, "actual_adj": paid, "actual_gaap": v(actuals, "fuel_price_gaap_usd_gal"),
            "actual_adj_minus_assumed": round(paid - assumed, 4),
            "actual_adj_over_assumed_pct": round(100 * (paid / assumed - 1), 2),
            "refinery_benefit_assumed": benefit_assumed, "refinery_benefit_actual": benefit,
            "before_refinery_benefit": {
                "formula": "all-in price + refinery benefit", "assumed": round(assumed + benefit_assumed, 4),
                "actual_adj": round(paid + benefit, 4),
                "actual_minus_assumed": round(paid + benefit - assumed - benefit_assumed, 4)},
            "basis_note": "The assumed price is all-in including the refinery benefit, so it is set against the "
                          "adjusted price. The GAAP price is shown for reference only.",
            "evidence_ids": ids((guidance, "fuel_price_assumed_usd_gal"), (actuals, "fuel_price_adj_usd_gal"),
                                (actuals, "fuel_price_gaap_usd_gal"), (guidance, "refinery_benefit_usd_gal"),
                                (actuals, "refinery_benefit_usd_gal"))},
        "fuel_cost_above_guidance": {
            "formula": "(fuel_price_adj_usd_gal - fuel_price_assumed_usd_gal) * fuel_gallons_m",
            "computed_musd": round(extra, 1), "delta_states_more_than_musd": stated["value"],
            "consistent_with_statement": extra > stated["value"],
            "per_diluted_share_pretax": round(extra / shares, 3),
            "per_diluted_share_after_tax": round(extra * (1 - tax_rate) / shares, 3),
            "tax_rate_used": round(tax_rate, 4),
            "tax_rate_note": "adjusted income tax over adjusted pre-tax income of the same quarter; the release "
                             "prints no tax rate",
            "evidence_ids": ids((actuals, "fuel_price_adj_usd_gal"), (guidance, "fuel_price_assumed_usd_gal"),
                                (actuals, "fuel_gallons_m"), (actuals, "diluted_shares_m"),
                                (actuals, "adj_income_tax_musd"), (actuals, "adj_pretax_income_musd"))
            + [stated["id"]]},
        "revenue": {"guided_text": by_key[guidance, "revenue_growth_text"]["_text"],
                    "actual_adj_revenue_yoy_stated_pct": v(after, "adj_total_revenue_yoy_pct"),
                    "evidence_ids": ids((guidance, "revenue_growth_text"), (after, "adj_total_revenue_yoy_pct"))},
        "unit_revenue": {"guided_text": by_key[guidance, "unit_revenue_text"]["_text"],
                         "adj_trasm_yoy_stated_pct_jun_quarter": v(before, "adj_trasm_yoy_pct"),
                         "adj_trasm_yoy_stated_pct_sep_quarter": v(after, "adj_trasm_yoy_pct"),
                         "evidence_ids": ids((guidance, "unit_revenue_text"), (before, "adj_trasm_yoy_pct"),
                                             (after, "adj_trasm_yoy_pct"))},
    }


def derived(by_key: dict, path: str) -> dict:
    def v(key):
        return by_key[path, key]["value"]
    prior = by_key[path + ".prior_year", "asm_m"]
    return {"gaap_effective_tax_rate_pct": round(-100 * v("income_tax_musd") / v("pretax_income_musd"), 2),
            "adj_effective_tax_rate_pct": round(-100 * v("adj_income_tax_musd") / v("adj_pretax_income_musd"), 2),
            "asm_yoy_pct": round(100 * (v("asm_m") / prior["value"] - 1), 2),
            "evidence_ids": [by_key[path, k]["id"] for k in ("income_tax_musd", "pretax_income_musd",
                                                             "adj_income_tax_musd", "adj_pretax_income_musd", "asm_m")]
            + [prior["id"]]}


SHARED_CLAIMS = {"adj_eps_guidance": ("eps_low", "eps_high"),
                 "fuel_assumed_usd_gal": ("fuel_price_assumed_usd_gal", "fuel_price_assumed_usd_gal"),
                 "fuel_cost_impact": ("fuel_cost_above_july_guidance_musd", "fuel_cost_above_july_guidance_musd")}


def shared_reading(doc: dict, facts: list[dict]) -> dict:
    """The project reader's own records for one release, each set against the same fact from the rules here."""
    end = datetime.strptime(f"{doc['quarter_end']}, {doc['key'][:4]}", "%B %d, %Y").date().isoformat()
    try:
        result = shared_reader.read_document(doc["raw"], doc_id=f"dal-{doc['key'].lower()}-ex991", doc_url=doc["url"],
                                             filed_on=doc["filed_on"], period_end=end)
    except Exception as exc:  # The cross-check is extra; a fault in it must not stop the facts being written.
        return {"release": doc["key"], "error": f"{type(exc).__name__}: {exc}"[:300]}
    mine = {(f["claim"], f["period"]): f for f in facts}
    found = []
    for record in result["records"]:
        pair = [mine.get((key, record["period"])) for key in SHARED_CLAIMS.get(record["claim"], ())]
        found.append({"id": record["id"], "claim": record["claim"], "period": record["period"], "low": record["low"],
                      "high": record["high"], "verified_by_shared_verifier": record["verified"],
                      "reasons": record["reasons"], "quote": record["quote"],
                      "same_fact_here": [f["id"] for f in pair if f],
                      "agree": [f["value"] for f in pair] == [record["low"], record["high"]] if all(pair) and pair
                      else None})
    return {"release": doc["key"], "normalisation": result["normalisation"], "candidates": result["candidates"],
            "found": found,
            "abstained": [{"claim": o["claim"], "period": o["period"], "reason": o["reason"]}
                          for o in result["outcomes"] if o["outcome"] != "found"]}


def build_check(docs: list[dict], generated_at: str | None = None) -> dict:
    """The dal_check.json payload from the two loaded releases. Reads and writes no file."""
    july, october = docs
    extracted = {doc["key"]: extract(doc) for doc in docs}
    facts = [f for doc in docs for f in extracted[doc["key"]]]
    by_key = {(f["_path"], f["_key"]): f for f in facts}
    sections: dict = {}
    for item in facts:
        if item["_path"] != "management_explanation":
            place(sections, item)
    for doc in docs:
        sections[doc["actuals"]] = {
            "period": doc["key"], "reported_on": doc["filed_on"], "accession": doc["accession"],
            **sections[doc["actuals"]], "income_tax_rate_pct": None, "not_stated": NOT_STATED["actuals"]}
        guidance = sections[doc["guidance"]]
        guidance["fuel_recovery_through_pricing"] = {
            "stated": False,
            "note": "No sentence gives a share, an amount or a date for recovering fuel cost through fares.",
            **guidance.get("fuel_recovery_through_pricing", {})}
        sections[doc["guidance"]] = {
            "period": shift(doc["key"], 1), "made_on": doc["filed_on"], "accession": doc["accession"],
            "revenue_growth_yoy_pct": None, "capacity_growth_yoy_pct": None,
            "fuel_price_basis": "adjusted (all-in, including the refinery benefit)", **guidance,
            "not_stated": NOT_STATED[doc["guidance"]]}
    explanation = [{"topic": f["_key"], "speaker": f["_speaker"], "summary": f["_summary"], "quote": f["quote"],
                    "value": f["value"], "unit": f["unit"], "qualifier": f.get("_qualifier"), "evidence_id": f["id"]}
                   for f in facts if f["_path"] == "management_explanation"]
    checks = [c for doc in docs for c in checks_for(doc, extracted[doc["key"]])]
    twice = [r for doc in docs for r in repeats(doc, extracted[doc["key"]])]
    shared = None if shared_reader is None else {
        "note": "What app.lab.reader.read_document and app.lab.verify return for these exhibits on their own. Their "
                "claim types and cues were written for United's guidance wording and do not cover reported "
                "results, so most of Delta's facts are outside them; a record they do find is set against the "
                "same fact above.",
        "releases": [shared_reading(doc, extracted[doc["key"]]) for doc in docs]}
    absent = [{"release": doc["key"], "claim": claim, "pattern": pattern,
               "matches": len(re.findall(pattern, doc["view"]))} for doc in docs for claim, pattern in ABSENT]
    return {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": max(doc["filed_on"] for doc in docs),
        "inputs": [{"path": doc["path"], "sha256": doc["sha256"]} for doc in docs],
        "company": "DAL", "cik": "0000027904",
        "documents": [{"release": doc["key"], "accession": doc["accession"], "filed_on": doc["filed_on"],
                       "form": "8-K", "exhibit": "EX-99.1", "url": doc["url"], "sha256": doc["sha256"],
                       "path": doc["path"], "normalisation": doc["normalisation"],
                       "normalised_chars": len(doc["text"])} for doc in docs],
        "normalisation_note": "char_start and char_end index the exhibit text under the normalisation named on each "
                              "record: app.lab.reader.normalise when the label is the reader's, otherwise "
                              "app.lab.delta.document_text. `verified` is the contract's rule applied here (exact "
                              "substring at the offsets, value parses from the quote).",
        "shared_reader": shared,
        "shape_note": "In the four fact sections every number sits beside `evidence_ids[<field>]`, the id of a "
                      "verified record under `evidence`; a text field holds words taken from its quote; "
                      "`qualifiers` keeps Delta's hedge words (approximately, less than). Blocks named derived, "
                      "guidance_vs_actual and arithmetic_checks are computed from those facts.",
        "fuel_price_note": "fuel_price_gaap_usd_gal is aircraft fuel expense per gallon as reported. "
                           "fuel_price_adj_usd_gal removes mark-to-market adjustments and settlements on Monroe "
                           "hedges (Note A) and is the price Delta describes together with its refinery benefit. "
                           "Guidance assumes an all-in price including the refinery benefit and is footnoted as "
                           "non-GAAP, so it corresponds to the adjusted price.",
        **{name: ordered(sections[name]) for name in ("guidance_sep_quarter", "actuals_jun_quarter",
                                                       "actuals_sep_quarter", "guidance_dec_quarter")},
        "management_explanation": explanation,
        "management_explanation_not_stated": NOT_STATED["management_explanation"],
        "guidance_vs_actual_sep_quarter": compare(by_key, july["guidance"], october["actuals"]),
        "derived": {"note": "Ratios of printed figures; Delta does not print them.",
                    **{doc["actuals"]: derived(by_key, doc["actuals"]) for doc in docs}},
        "arithmetic_checks": {"tolerance_rule": "half a unit of the stated figure's last printed digit, plus the "
                                                "spread of the formula over the rounding intervals of its inputs",
                              "passed": sum(c["ok"] for c in checks), "total": len(checks), "checks": checks},
        "printed_twice": {"agree": sum(r["agree"] for r in twice), "total": len(twice), "items": twice},
        "absence_checks": absent,
        "evidence": [{k: v for k, v in f.items() if not k.startswith("_")} for f in facts],
    }


# ---- Quarterly history (data/lab/dal_quarterly.csv): the contract's ual_quarterly.csv columns, then Delta's
# adjusted series. fuel_price_usd_gal, fuel_expense_musd, total_revenue_musd and trasm_cents are the GAAP figures.
CONTRACT_COLUMNS = ("period_end", "filed_on", "accession", "asm_m", "rpm_m", "total_revenue_musd",
                    "passenger_revenue_musd", "trasm_cents", "prasm_cents", "casm_cents", "casm_ex_cents",
                    "fuel_gallons_m", "fuel_price_usd_gal", "fuel_expense_musd", "pretax_income_musd",
                    "adj_pretax_income_musd", "net_income_musd", "gaap_eps_diluted", "adj_eps_diluted",
                    "diluted_shares_m", "profit_sharing_musd", "source_note")
DELTA_COLUMNS = ("adj_fuel_price_usd_gal", "adj_fuel_expense_musd", "adj_total_revenue_musd", "adj_trasm_cents")
QUARTERLY_COLUMNS = CONTRACT_COLUMNS + DELTA_COLUMNS
V = r"\(? ?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?: ?\))?"  # Older releases print a negative as "(344 )".
QUARTER_ENDS = {"March 31": "03-31", "June 30": "06-30", "September 30": "09-30", "December 31": "12-31"}
LONGER = r"(?:(?:Six|Nine|Twelve) Months Ended |Year Ended )?"
PERIOD_HEAD = re.compile(rf" Three Months Ended {LONGER}(?P<day>{'|'.join(QUARTER_ENDS)}),(?: {LONGER}(?P=day),)? "
                         r"(?P<year>\d{4}) (?P<base>\d{4}) Change ")
HISTORY_SECTIONS = {
    "income": (r"Consolidated Statements of Operations \(Unaudited\)",
               r"(?:Passenger Revenue|Other Revenue|Total Revenue|Statistical Summary) \(Unaudited\)"),
    "stats": (r"Statistical Summary \(Unaudited\)", r"Consolidated Statements of Cash Flows|Mainline:"),
    "all": (r"^", r"$"),
}
# (column, section, row labels in order of preference). The first number after the label is the quarter's.
HISTORY_ROWS = [
    ("asm_m", "stats", [r"Available seat miles \(millions\)"]),
    ("rpm_m", "stats", [r"Revenue passenger miles \(millions\)"]),
    ("total_revenue_musd", "income", [r"Total operating revenue"]),
    ("passenger_revenue_musd", "income", [r"Operating Revenue: Passenger", r"Total passenger revenue"]),
    ("trasm_cents", "stats", [r"Total revenue per available seat mile \(cents\)"]),
    ("prasm_cents", "stats", [r"Passenger revenue per available seat mile \(cents\)"]),
    ("casm_cents", "stats", [r"Cost per available seat mile \(cents\)",
                             r"Operating cost per available seat mile \(cents\)",
                             r"Total cost per available seat mile \(cents\)"]),
    ("casm_ex_cents", "stats", [r"CASM-Ex ?- see Note [AB] \(cents\)"]),
    ("fuel_gallons_m", "stats", [r"Fuel gallons consumed \(millions\)"]),
    ("fuel_price_usd_gal", "stats", [r"Average price per fuel gallon"]),
    ("fuel_expense_musd", "income", [r"Aircraft fuel and related taxes"]),
    ("pretax_income_musd", "income", [r"(?:Income/\(Loss\)|\(Loss\)/Income|Income|Loss) Before Income Taxes"]),
    ("net_income_musd", "income", [r"Net (?:Income/\(Loss\)|\(Loss\)/Income|Income|Loss)"]),
    ("gaap_eps_diluted", "income",
     [r"Diluted (?:Earnings/\(Loss\)|\(Loss\)/Earnings|Income/\(Loss\)|Earnings|Loss) Per Share"]),
    ("diluted_shares_m", "income", [r"Diluted Weighted Average Shares Outstanding"]),
    ("profit_sharing_musd", "income", [r"Profit sharing"]),
    ("adj_fuel_price_usd_gal", "stats", [r"Average price per fuel gallon, adjusted ?- see Note [AB]"]),
    ("adj_trasm_cents", "stats", [r"TRASM, adjusted ?- see Note [AB] \(cents\)"]),
    ("adj_fuel_expense_musd", "all", [r"Total fuel expense, adjusted"]),
    ("adj_total_revenue_musd", "all", [r"(?:Total o|O)perating revenue, adjusted"]),
]
# (name, stated column, input columns, function): the same rounding-only tolerance as the checks above.
HISTORY_CHECKS = [
    ("fuel_expense", "fuel_expense_musd", ("fuel_gallons_m", "fuel_price_usd_gal"), lambda g, p: g * p),
    ("adj_fuel_expense", "adj_fuel_expense_musd", ("fuel_gallons_m", "adj_fuel_price_usd_gal"), lambda g, p: g * p),
    ("gaap_eps", "gaap_eps_diluted", ("net_income_musd", "diluted_shares_m"), lambda a, b: a / b),
    ("trasm", "trasm_cents", ("total_revenue_musd", "asm_m"), lambda a, b: 100 * a / b),
    ("prasm", "prasm_cents", ("passenger_revenue_musd", "asm_m"), lambda a, b: 100 * a / b),
    ("adj_trasm", "adj_trasm_cents", ("adj_total_revenue_musd", "asm_m"), lambda a, b: 100 * a / b),
]


def printed(token: str) -> str:
    """A printed cell as a plain decimal string with its printed precision: '(344 )' -> '-344', '$ 3.80' -> '3.80'."""
    value = number(token)
    digits = re.sub(r"[^\d.]", "", token)
    return ("-" if value < 0 else "") + digits


def history_row(view: str) -> tuple[dict, list[str]]:
    """Columns one release prints for its own quarter, as printed, and notes on what it does not print."""
    spans = {}
    for name, (first, last) in HISTORY_SECTIONS.items():
        start = re.search(first, view)
        end = start and re.compile(last).search(view, start.end())
        if not start or not end:
            raise ValueError(f"The {name} table was not found")
        spans[name] = (start.end(), end.start())
    head = PERIOD_HEAD.match(view, spans["stats"][0])
    if not head:
        raise ValueError("The statistics table does not open with a three-month column heading")
    row, notes = {"period_end": f"{head['year']}-{QUARTER_ENDS[head['day']]}"}, []
    if int(head["base"]) != int(head["year"]) - 1:
        notes.append(f"release compares with {head['base']}")
    for column, name, labels in HISTORY_ROWS:
        for position, label in enumerate(labels):
            match = re.compile(rf"(?:{label}) (?:\$ ?)?(?P<c>{V})(?![\d,.])").search(view, *spans[name])
            if match:
                row[column] = printed(match["c"])
                if position:
                    label = match.group(0)[:match.start("c") - match.start()].strip(" $")
                    notes.append(f"{column} from the row '{label}'")
                break
        else:
            dash = any(re.compile(rf"(?:{label}) (?:\$ ?)?[—–-] ").search(view, *spans[name]) for label in labels)
            notes.append(f"{column} {'printed as a dash' if dash else 'not printed'}")
    day, year = head["day"], head["year"]
    # Note A's earnings table for this quarter: pre-tax, tax, net income, per share; or net income and per share.
    earnings = re.search(rf"Three Months Ended (?:Three Months Ended )?{day}, {year} (?:Three Months Ended )?{day}, "
                         rf"{year} (?:(?!Three Months Ended|Non-GAAP).)*Non-GAAP(?P<cells>(?: \$ {V}){{2,4}})", view)
    cells = re.findall(V, earnings["cells"]) if earnings else []
    if len(cells) in (2, 4):
        row["adj_eps_diluted"] = printed(cells[-1])
    else:
        notes.append("adj_eps_diluted not printed")
    if len(cells) == 4:
        row["adj_pretax_income_musd"] = printed(cells[0])
    else:
        notes.append("adj_pretax_income_musd not printed" if len(cells) != 2 else
                     "adj_pretax_income_musd not in the adjusted earnings table")
    if re.search(r"(?i)Updated definition excludes aircraft fuel and related taxes, third-party refinery sales, "
                 r"MRO expense, and profit sharing", view):
        notes.append("casm_ex_cents under the definition updated in 2026 (also excludes MRO expense and profit "
                     "sharing); not comparable with earlier rows")
    total = re.search(rf"Total fuel expense \$ (?P<c>{V})(?![\d,.])", view)
    if total and row.get("fuel_expense_musd") and printed(total["c"]) != row["fuel_expense_musd"]:
        notes.append(f"fuel_expense_musd is 'Total fuel expense' from Note A; the income statement line "
                     f"'Aircraft fuel and related taxes' prints {row['fuel_expense_musd']}")
        row["fuel_expense_musd"] = printed(total["c"])
    return row, notes


def history_checks(row: dict) -> list[dict]:
    result = []
    for name, stated, inputs, function in HISTORY_CHECKS:
        if all(row.get(column) for column in (stated, *inputs)):
            values = [float(row[c]) for c in inputs]
            computed = function(*values)
            corners = [function(*corner) for corner in product(*((v - half_unit(row[c]), v + half_unit(row[c]))
                                                                 for v, c in zip(values, inputs)))]
            tolerance = half_unit(row[stated]) + max(abs(c - computed) for c in corners)
            difference = computed - float(row[stated])
            result.append({"name": name, "period_end": row["period_end"], "stated": float(row[stated]),
                           "computed": round(computed, 4), "difference": round(difference, 4),
                           "tolerance": round(tolerance, 4), "ok": abs(difference) <= tolerance + 1e-9})
    return result


def quarterly(manifest: dict) -> dict:
    """One row per quarter from every earnings release in the manifest, with internal checks. No network."""
    read, failures = [], []
    for entry in manifest["entries"]:
        if entry.get("kind") != "exhibit" or entry["exhibit"] != "EX-99.1":
            continue
        try:
            content = (ROOT / entry["path"]).read_bytes()
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError("raw exhibit failed its checksum")
            view = flat_view(exhibit_text(content)[0])[0]
            if not re.search(r"Delta Air Lines Announces \w+ Quarter", view):
                raise ValueError("not an earnings release")
            row, notes = history_row(view)
            if not row["period_end"] < entry["filed_on"] <= f"{int(row['period_end'][:4]) + 1}{row['period_end'][4:]}":
                raise ValueError(f"period {row['period_end']} does not fit a filing dated {entry['filed_on']}")
            read.append(({**row, "filed_on": entry["filed_on"], "accession": entry["accession"]}, notes, entry))
        except (ValueError, OSError) as exc:
            failures.append({"accession": entry["accession"], "filed_on": entry["filed_on"],
                             "error": f"{type(exc).__name__}: {exc}"[:300]})
    rows, checks = [], []
    for period_end in sorted({row["period_end"] for row, _, _ in read}):
        # An amended filing replaces the original for the same quarter.
        same = sorted((item for item in read if item[0]["period_end"] == period_end),
                      key=lambda item: (item[2]["filed_on"], item[2]["form"] == "8-K/A", item[2]["accession"]))
        row, notes, entry = same[-1]
        for other, _, old in same[:-1]:
            differ = [c for c in QUARTERLY_COLUMNS[3:] if c != "source_note" and other.get(c) != row.get(c)]
            notes.append(f"{entry['form']} replaces {old['form']} {old['accession']}; "
                         + (f"differs in {', '.join(differ)}" if differ else "same figures"))
        tested = history_checks(row)
        checks += tested
        off = [f"{c['name']} off by {c['difference']} (tolerance {c['tolerance']})" for c in tested if not c["ok"]]
        notes.append(f"checks {sum(c['ok'] for c in tested)}/{len(tested)}" + (": " + "; ".join(off) if off else ""))
        rows.append({**row, "source_note": "; ".join([f"{entry['exhibit']} {entry['filename']}"] + notes)})
    return {"rows": rows, "checks": checks, "failures": failures}


def write_quarterly(rows: list[dict], path: Path = QUARTERLY_CSV) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=QUARTERLY_COLUMNS, restval="", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def load_quarterly(path: Path = QUARTERLY_CSV) -> list[dict]:
    """Rows of dal_quarterly.csv with numbers as floats and blanks as None."""
    with path.open(newline="") as stream:
        table = csv.DictReader(stream)
        if tuple(table.fieldnames or ()) != QUARTERLY_COLUMNS:
            raise ValueError("Unexpected dal_quarterly.csv header")
        text = ("period_end", "filed_on", "accession", "source_note")
        return [{k: v if k in text else float(v) if v else None for k, v in row.items()} for row in table]


# dal_check prior-year field -> dal_quarterly column, where the two names differ.
FIRST_REPORTED = {"fuel_price_gaap_usd_gal": "fuel_price_usd_gal", "fuel_price_adj_usd_gal": "adj_fuel_price_usd_gal",
                  "fuel_expense_gaap_musd": "fuel_expense_musd", "fuel_expense_adj_musd": "adj_fuel_expense_musd"}


def restatements(check: dict, rows: list[dict]) -> dict:
    """Prior-year figures the 2026 releases print, set against the same quarter as first reported a year earlier."""
    by_period = {row["period_end"]: row for row in rows}
    compared, changed = 0, []
    for doc in (JULY, OCTOBER):
        period = shift(doc["key"], -4)
        first = by_period.get(f"{period[:4]}-{QUARTER_ENDS[doc['quarter_end']]}")
        if not first:
            continue
        section = check[doc["actuals"]]["prior_year"]
        for field, value in section.items():
            column = FIRST_REPORTED.get(field, field)
            if field == "evidence_ids" or not first.get(column):
                continue
            compared += 1
            if not math.isclose(value, float(first[column]), abs_tol=1e-9):
                changed.append({"period": period, "field": field, "printed_in_2026_release": value,
                                "first_reported": float(first[column]), "evidence_id": section["evidence_ids"][field],
                                "first_reported_accession": first["accession"]})
    return {"note": "Year-over-year changes Delta prints in 2026 use the prior-year figures of the 2026 release. "
                    "Where those differ from the figure first reported (data/lab/dal_quarterly.csv) the base was "
                    "restated: Note A of the 2026 releases says prior-period hedge adjustments were re-presented, "
                    "and the highlights table footnotes an updated non-fuel cost definition.",
            "compared": compared, "changed": changed}


def build(manifest: dict, check_json: Path = CHECK_JSON, quarterly_csv: Path = QUARTERLY_CSV) -> dict:
    """Write dal_check.json and dal_quarterly.csv from the raw store through the manifest. No network."""
    def relative(path):
        return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
    history = quarterly(manifest)
    result = build_check([load(manifest, JULY), load(manifest, OCTOBER)])
    evidence = result.pop("evidence")
    result["prior_year_as_first_reported"] = restatements(result, history["rows"])
    result["evidence"] = evidence
    used = {item["first_reported_accession"] for item in result["prior_year_as_first_reported"]["changed"]}
    result["inputs"] += [{"path": e["path"], "sha256": e["sha256"]} for e in manifest["entries"]
                         if e.get("kind") == "exhibit" and e["exhibit"] == "EX-99.1" and e["accession"] in used]
    check_json.parent.mkdir(parents=True, exist_ok=True)
    check_json.write_text(json.dumps(result, indent=2, allow_nan=False, ensure_ascii=False) + "\n")
    write_quarterly(history["rows"], quarterly_csv)
    periods = [row["period_end"] for row in history["rows"]]
    return {"outputs": [{"path": relative(check_json), "evidence": len(result["evidence"]),
                         "verified": sum(e["verified"] for e in result["evidence"]),
                         "normalisation": sorted({e["normalisation"] for e in result["evidence"]}),
                         "arithmetic_checks": f"{result['arithmetic_checks']['passed']}/"
                                              f"{result['arithmetic_checks']['total']}",
                         "printed_twice": f"{result['printed_twice']['agree']}/{result['printed_twice']['total']}",
                         "prior_year_restated": len(result["prior_year_as_first_reported"]["changed"])},
                        {"path": relative(quarterly_csv), "rows": len(periods), "first": min(periods),
                         "last": max(periods), "checks": f"{sum(c['ok'] for c in history['checks'])}/"
                                                         f"{len(history['checks'])}"}],
            "arithmetic_failures": [f"{c['period']}:{c['name']}" for c in result["arithmetic_checks"]["checks"]
                                    if not c["ok"]],
            "printed_twice_disagreements": [r["field"] for r in result["printed_twice"]["items"] if not r["agree"]],
            "unexpected_wording": [a for a in result["absence_checks"] if a["matches"]],
            "quarterly_checks_off": [c for c in history["checks"] if not c["ok"]],
            "not_built": history["failures"]}
