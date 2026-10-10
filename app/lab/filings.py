"""United's 8-K exhibits: what kind each is, the earnings-release tables, and the quarterly loader.

A quarterly value is taken only from a table row whose label matches a stated
pattern, and only from the column headed by the three months ended on the
release's own quarter end. A cell the release does not print stays blank:
nothing is summed, differenced or back-solved to fill it. A dash the release
prints for nil is kept as 0, with the dash recorded as the printed text.
"""
from __future__ import annotations

import calendar
import csv
from datetime import date
from decimal import Decimal
from functools import lru_cache
from html.parser import HTMLParser
import math
from pathlib import Path
import re

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "data" / "lab"
INDEX_CSV = LAB / "ual_filings_index.csv"
QUARTERLY_CSV = LAB / "ual_quarterly.csv"
SOURCES_JSON = LAB / "ual_quarterly_sources.json"
INDEX_COLUMNS = ("accession", "filed_on", "period_end", "form", "items", "exhibit", "filename", "url", "sha256",
                 "path", "kind")
VALUE_COLUMNS = ("asm_m", "rpm_m", "total_revenue_musd", "passenger_revenue_musd", "trasm_cents", "prasm_cents",
                 "casm_cents", "casm_ex_cents", "fuel_gallons_m", "fuel_price_usd_gal", "fuel_expense_musd",
                 "pretax_income_musd", "adj_pretax_income_musd", "net_income_musd", "gaap_eps_diluted",
                 "adj_eps_diluted", "diluted_shares_m", "profit_sharing_musd")
QUARTERLY_COLUMNS = ("period_end", "filed_on", "accession") + VALUE_COLUMNS + ("source_note",)
FIRST_QUARTER = "2013-03-31"
SKIP = {"script", "style", "title", "head"}
BREAKS = {"p", "div", "br", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "li", "hr", "center"}


MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
PERIOD = re.compile(r"\b(three|six|nine|twelve)\s+months\s+ended\b|\byears?\s+ended\b|\bquarters?\s+ended\b", re.I)
QUARTER = re.compile(r"\bthree\s+months\s+ended\b", re.I)
YEAR = re.compile(r"(19|20)\d\d")
YEAR_IN = re.compile(r"\b(19|20)\d\d\b")
DAY = re.compile(r"\b(" + "|".join(MONTHS) + r")\s*,?\s*(\d{1,2})\b", re.I)
STATEMENT_TITLE = re.compile(r"STATEMENTS\s+OF\s+CONSOLIDATED\s+OPERATIONS")
ITEM_START = re.compile(r"Item\s+(2\.02|7\.01|8\.01)\.?\s+(Results of Operations|Regulation FD|Other Events)", re.I)
ITEM_STOP = re.compile(r"Item\s+9\.01|SIGNATURE|Cautionary Statement|Forward-Looking Statements|Safe Harbor"
                       r"|The information (in|contained in|furnished)|Pursuant to General Instruction B\.2"
                       r"|shall not be deemed", re.I)
GUIDANCE_PHRASE = re.compile(
    r"\b(now|currently|continues? to|does not currently) expects?\b|\bexpects to (report|generate)\b"
    r"|\bproviding (guidance|an update regarding its financial outlook)\b|\bupdated [\w\s-]{0,40}guidance\b"
    r"|\bquarter \d{4} guidance\b|\bCompany Outlook\b", re.I)
SHARE = r"(earnings|loss|income|earnings \(loss\)|income \(loss\))"
# Row labels, most specific first, matched whole against norm(label). "statement" rows come from the income
# statement only; the others from any table, never from a mainline-only or regional-only section.
FIELDS = {
    "asm_m": {"labels": (r"available seat miles( \(asms\))? \(millions\)", r"asms \(millions\)")},
    "rpm_m": {"labels": (r"revenue passenger miles( \(rpms\))? \(millions\)", r"rpms \(millions\)")},
    "total_revenue_musd": {"statement": True, "labels": (r"total operating revenues?",)},
    "passenger_revenue_musd": {"statement": True,
                               "labels": (r"total passenger revenue", r"passenger revenue", r"passenger")},
    "trasm_cents": {"labels": (r"total revenue per available seat miles?( \(trasm\))? \(cents\)", r"trasm \(cents\)")},
    "prasm_cents": {"labels": (r"passenger revenue per available seat mile( \(prasm\))? \(cents\)",
                               r"prasm \(cents\)")},
    "casm_cents": {"labels": (r"cost per available seat mile( \(casm\))?( \(gaap\))?( \(cents\))?",
                              r"cost per asm \(casm\)( \(cents\))?", r"casm( \(gaap\))?( \(cents\))?")},
    "casm_ex_cents": {"labels": (r"casm-ex( \(non-gaap\))?( \(cents\))?",
                                 r"casm,? excluding special charges( \(credits\))?, third-party business expenses, "
                                 r"fuel,? and profit sharing( \(non-gaap\))?")},
    "fuel_gallons_m": {"labels": (r"fuel gallons consumed( \(millions\))?", r"consolidated (fuel )?consumption \(gallons\)")},
    "fuel_price_usd_gal": {"unit": "usd",
                           "labels": (r"(consolidated )?average aircraft fuel price per gallon( \(cents\))?",)},
    "fuel_expense_musd": {"statement": True, "labels": (r"aircraft fuel",)},
    "pretax_income_musd": {"statement": True, "labels": (
        r"(income|loss|income \(loss\)) before income tax(es| expense| benefit| expense \(benefit\))",)},
    "adj_pretax_income_musd": {"labels": (
        r"adjusted pre-?tax (income|loss|income \(loss\))( \(non-gaap\))?",
        r"(pre-?tax income|income( \(loss\))? before income taxes),? (and )?excluding special (items|charges).*")},
    "net_income_musd": {"statement": True, "labels": (r"net (income|loss|income \(loss\))",)},
    "gaap_eps_diluted": {"statement": True, "labels": (SHARE + r" per share, (basic and )?diluted",
                                                       r"diluted " + SHARE + r" per share")},
    "adj_eps_diluted": {"labels": (r"adjusted diluted " + SHARE + r" per share( \(non-gaap\))?",
                                   r"diluted " + SHARE + r" per share,? excluding special (items|charges).*")},
    "diluted_shares_m": {"statement": True, "labels": (r"weighted average shares, (basic and )?diluted",
                                                       r"diluted weighted[- ]average shares( outstanding)?")},
    # Dollars, not the per-seat-mile line of the CASM table: only below an operating expenses row.
    "profit_sharing_musd": {"labels": (r"(less: )?profit sharing( programs)?(, including taxes)?",),
                            "after": r"operating expenses?( \(gaap\))?",
                            "barrier": r"casm|cost per a(vailable seat mile|sm)"},
}


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def month_end(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"


class _Document(HTMLParser):
    """Paragraphs and tables in document order; a table is rows of (text, first column, column span)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.text, self.skip, self.tables = [], [], 0, []

    def flush(self):
        text = clean("".join(self.text))
        self.text = []
        if text:
            self.blocks.append(("text", text))

    def close_cell(self, table):
        if table["cell"] is not None and table["row"] is not None:
            start = sum(span for _, _, span in table["row"])
            table["row"].append((clean("".join(table["cell"][0])), start, table["cell"][1]))
        table["cell"] = None

    def close_row(self, table):
        self.close_cell(table)
        if table["row"] is not None:
            table["rows"].append(table["row"])
        table["row"] = None

    def handle_starttag(self, tag, attrs):
        table = self.tables[-1] if self.tables else None
        if tag in SKIP:
            self.skip += 1
        elif tag == "table":
            self.flush()
            self.tables.append({"rows": [], "row": None, "cell": None})
        elif tag == "tr" and table:
            self.close_row(table)
            table["row"] = []
        elif tag in ("td", "th") and table:
            self.close_cell(table)
            if table["row"] is None:
                table["row"] = []
            span = dict(attrs).get("colspan") or "1"
            table["cell"] = ([], int(span) if span.isdigit() and int(span) > 0 else 1)
        elif tag in BREAKS:
            if table and table["cell"] is not None:
                table["cell"][0].append(" ")
            elif not table:
                self.flush()

    def handle_endtag(self, tag):
        table = self.tables[-1] if self.tables else None
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in ("td", "th") and table:
            self.close_cell(table)
        elif tag == "tr" and table:
            self.close_row(table)
        elif tag == "table" and table:
            self.close_row(table)
            self.tables.pop()
            if any(text for row in table["rows"] for text, _, _ in row):
                self.blocks.append(("table", table["rows"]))
        elif tag in BREAKS and not table:
            self.flush()

    def handle_data(self, data):
        if self.skip:
            return
        if not self.tables:
            self.text.append(data)
        elif self.tables[-1]["cell"] is not None:
            self.tables[-1]["cell"][0].append(data)


def blocks(content: bytes | str) -> list[tuple]:
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
    start = re.search(r"<html\b", text, re.I)  # EDGAR's SGML wrapper sits before the document proper.
    parser = _Document()
    parser.feed(text[start.start():] if start else text)
    parser.close()
    while parser.tables:  # A cut-off document still yields the rows it has.
        parser.handle_endtag("table")
    parser.flush()
    return parser.blocks


def document_text(content: bytes | str) -> str:
    """Visible text in document order, table cells included, single-spaced."""
    parts = []
    for kind, body in blocks(content):
        parts.append(body if kind == "text" else " ".join(text for row in body for text, _, _ in row if text))
    return clean(" ".join(parts))


def title_text(content: bytes | str, length: int = 300) -> str:
    """The opening text of an exhibit after its 'Exhibit 99.x' banner: the title the kind rules read."""
    return re.sub(r"^\s*Exhibit\s+99(\.\d+)?\s*", "", document_text(content), flags=re.I)[:length]


def classify_exhibit(content: bytes | str, items: str) -> str:
    """earnings_release | investor_update | other, by title text.

    investor_update: the exhibit's title text (its first 300 characters) says "Investor Update".
    earnings_release: otherwise, the filing reports under item 2.02 and the exhibit carries the
    statement title "STATEMENTS OF CONSOLIDATED OPERATIONS". Headlines alone do not work: from
    2019 many do not name the results. Anything else is other.
    """
    text = document_text(content)
    if re.search(r"investor\s+update", title_text(text), re.I):
        return "investor_update"
    if "2.02" in items.split(",") and STATEMENT_TITLE.search(text):
        return "earnings_release"
    return "other"


def item_text(content: bytes | str) -> str:
    """What an 8-K says under items 2.02, 7.01 and 8.01, cut before the furnished and forward-looking boilerplate."""
    text, parts = document_text(content), []
    for start in ITEM_START.finditer(text):
        rest = text[start.end():]
        stop = ITEM_STOP.search(rest)
        parts.append(rest[:stop.start()] if stop else rest)
    return " ".join(parts)


def classify_primary(content: bytes | str) -> str:
    """investor_update when the 8-K's own item text states guidance in one of United's set phrases, else other."""
    return "investor_update" if GUIDANCE_PHRASE.search(item_text(content)) else "other"


def number(text: str) -> Decimal | None:
    """A table cell as a number: brackets are negative, a lone dash is nil, text is None."""
    body = re.sub(r"[$%,\s]", "", text)
    if body in ("—", "–", "-"):
        return Decimal(0)
    match = re.fullmatch(r"(\()?(-)?(\d+\.?\d*|\.\d+)\)?", body)
    if not match:
        return None
    return -Decimal(match.group(3)) if match.group(1) or match.group(2) else Decimal(match.group(3))


def norm(label: str) -> str:
    """A row label for matching: lower case, no quotes, no footnote marks."""
    text = re.sub("[“”\"]", "", label.lower()).replace("’", "'")
    text = re.sub(r"\s*\([a-z]\)", "", text)
    text = re.sub(r"(?<=[a-z)])\d$", "", text.strip())
    return clean(text.rstrip(":"))


def is_heading(cells: list[tuple]) -> bool:
    rest = [text for text, col, _ in cells if col > 0] or [text for text, _, _ in cells]
    if any(number(text) is not None and not YEAR.fullmatch(text) for text in rest):
        return False
    return any(PERIOD.search(text) or YEAR.fullmatch(text) or DAY.search(text) for text in rest)


def table_lines(rows: list[list[tuple]]) -> list[dict]:
    """Each labelled row with numbers: its label, numeric cells, the heading rows above it and its scope.

    Scope is the last label-only row naming mainline, regional or consolidated operations.
    """
    heading, fresh, scope, lines = [], True, None, []
    for row in rows:
        cells = [cell for cell in row if cell[0]]
        if not cells:
            continue
        if is_heading(cells):
            if not fresh:
                heading, fresh = [], True
            heading.append(cells)
            continue
        if cells[0][1] != 0 or number(cells[0][0]) is not None:
            continue
        fresh = False
        values = [(number(text), col, text) for text, col, _ in cells[1:] if number(text) is not None]
        if values:
            lines.append({"label": cells[0][0], "values": values, "heading": heading, "scope": scope})
        else:
            low = cells[0][0].lower()
            scope = next((word for word in ("consolidated", "mainline", "regional") if word in low), scope)
    return lines


def quarter_columns(heading: list[list[tuple]]) -> list[dict]:
    """Every 'Three Months Ended' column group of a heading block, reduced to its first (latest) year.

    Years are paired with period headings by order, since a year row is sometimes shifted a column
    against the period row; where the counts do not divide, by the columns each period heading spans.
    """
    result = []
    for depth, row in enumerate(heading):
        periods = [cell for cell in row if PERIOD.search(cell[0]) and cell[1] > 0]
        below = [cell for later in heading[depth + 1:] for cell in later]
        years = [cell for cell in below if YEAR.fullmatch(cell[0])]
        for place, (text, col, span) in enumerate(periods):
            if not QUARTER.search(text):
                continue
            low, high, header = col, col + span, text
            day = DAY.search(text)
            if not day:
                dated = next((cell[0] for cell in below if low <= cell[1] < high and DAY.search(cell[0])), None)
                if not dated:
                    continue
                day, header = DAY.search(dated), f"{header} {dated}"
            inline = YEAR_IN.search(text[day.end():]) if DAY.search(text) else None
            each = len(years) // len(periods) if years and len(years) % len(periods) == 0 else 0
            mine = years[place * each:(place + 1) * each]
            if not descending(mine):
                mine = [cell for cell in years if low <= cell[1] < high]
            if inline:
                year = int(inline.group())
            elif descending(mine):
                year, header = int(mine[0][0]), f"{header} {mine[0][0]}"
                if len(mine) > 1:
                    aligned = low <= mine[0][1] < mine[1][1] < high
                    high = mine[1][1] if aligned else low + span / len(mine)
            else:
                continue
            month = MONTHS[day.group(1).lower()]
            end = f"{year:04d}-{month:02d}-{int(day.group(2)):02d}"
            if month % 3 == 0 and end == month_end(year, month):
                result.append({"low": low, "high": high, "column": clean(header), "period_end": end})
    return result


def descending(years: list[tuple]) -> bool:
    values = [int(cell[0]) for cell in years]
    return bool(values) and all(a > b for a, b in zip(values, values[1:]))


def quarter_value(line: dict, period: str) -> tuple | None:
    """(value, printed text, column header) from the row's column for the three months ended on `period`."""
    for column in quarter_columns(line["heading"]):
        if column["period_end"] != period:
            continue
        for value, col, text in line["values"]:
            if column["low"] <= col < column["high"]:
                return value, text, column["column"]
        return None
    return None


def release_tables(content: bytes | str) -> list[list[dict]]:
    return [table_lines(body) for kind, body in blocks(content) if kind == "table"]


def statement_index(tables: list[list[dict]]) -> int | None:
    """The income statement: the first table with both a total operating revenue and an aircraft fuel row."""
    for index, lines in enumerate(tables):
        labels = {norm(line["label"]) for line in lines}
        if "aircraft fuel" in labels and labels & {"total operating revenue", "total operating revenues"}:
            return index
    return None


def statement_period(tables: list[list[dict]], index: int | None) -> str | None:
    columns = quarter_columns(tables[index][0]["heading"]) if index is not None else []
    return columns[0]["period_end"] if columns else None


def release_period(content: bytes | str) -> str | None:
    """Quarter end of an earnings release: the first three-months column of its income statement."""
    tables = release_tables(content)
    return statement_period(tables, statement_index(tables))


def parse_release(content: bytes | str) -> dict:
    """Quarter values of one earnings release: {period_end, cells: {field: {value, label, column, printed}}}.

    For each field the row is the best label match (consolidated section first, then pattern order,
    then document order) that has a number in the column for the release's own quarter.
    """
    tables = release_tables(content)
    index = statement_index(tables)
    period = statement_period(tables, index)
    cells = {}
    if period is None:
        return {"period_end": None, "cells": cells}
    for field, spec in FIELDS.items():
        best = None
        for t, lines in enumerate(tables):
            if spec.get("statement") and t != index:
                continue
            anchored = False
            for n, line in enumerate(lines):
                label = norm(line["label"])
                if "after" in spec:
                    if re.fullmatch(spec["after"], label):
                        anchored = True
                    elif re.search(spec["barrier"], label):
                        anchored = False
                    if not anchored:
                        continue
                if not spec.get("statement") and line["scope"] in ("mainline", "regional"):
                    continue
                rank = next((i for i, pattern in enumerate(spec["labels"]) if re.fullmatch(pattern, label)), None)
                found = quarter_value(line, period) if rank is not None else None
                if found:
                    key = (line["scope"] != "consolidated", rank, t, n)
                    if best is None or key < best[0]:
                        best = (key, line, found)
        if best:
            _, line, (value, printed, column) = best
            cell = {"label": line["label"], "column": column, "printed": printed}
            if spec.get("unit") == "usd" and "(cents)" in norm(line["label"]):
                value, cell["note"] = value / 100, "printed in cents, divided by 100"
            cells[field] = {"value": format(value, "f"), **cell}
    return {"period_end": period, "cells": cells}


def quarterly_row(parsed: dict, entry: dict) -> dict:
    """The CSV row of one parsed release; source_note names the exhibit and anything unusual about a cell."""
    cells = parsed["cells"]
    notes = [f"{field} {cell['note']}" for field, cell in cells.items() if "note" in cell]
    notes += [f"{field} printed as a dash" for field, cell in cells.items() if not re.search(r"\d", cell["printed"])]
    notes += [f"{field} blank: no such row for the quarter" for field in VALUE_COLUMNS if field not in cells]
    return {"period_end": parsed["period_end"], "filed_on": entry["filed_on"], "accession": entry["accession"],
            **{field: cells.get(field, {}).get("value", "") for field in VALUE_COLUMNS},
            "source_note": "; ".join([f"{entry['exhibit']} {entry['filename']}"] + notes)}


def expected_quarters(first: str, last: str) -> list[str]:
    """Quarter ends from `first` to `last`, both quarter-end dates."""
    result, year, month = [], int(first[:4]), int(first[5:7])
    while (year, month) <= (int(last[:4]), int(last[5:7])):
        result.append(month_end(year, month))
        year, month = (year + 1, 3) if month == 12 else (year, month + 3)
    return result


def coverage(rows: list[dict]) -> dict:
    return {field: sum(row[field] not in ("", None) for row in rows) for field in VALUE_COLUMNS}


def arithmetic_checks(rows: list[dict]) -> list[dict]:
    """Reported cells against the same quantity implied by other cells, wherever a row has every input."""
    def have(row, *fields):
        return all(row[f] not in ("", None) for f in fields)
    result = []
    for row in rows:
        if have(row, "fuel_expense_musd", "fuel_gallons_m", "fuel_price_usd_gal"):
            implied = float(row["fuel_gallons_m"]) * float(row["fuel_price_usd_gal"])
            result.append({"period_end": row["period_end"], "check": "fuel_expense = gallons x price",
                           "reported": float(row["fuel_expense_musd"]), "implied": round(implied, 2)})
        for unit, revenue in (("trasm", "total_revenue_musd"), ("prasm", "passenger_revenue_musd")):
            if have(row, f"{unit}_cents", revenue, "asm_m"):
                implied = float(row[revenue]) / float(row["asm_m"]) * 100
                result.append({"period_end": row["period_end"], "check": f"{unit} = {revenue[:-5]} / asm",
                               "reported": float(row[f"{unit}_cents"]), "implied": round(implied, 4)})
    for item in result:
        item["difference"] = round(item["implied"] / item["reported"] - 1, 5) if item["reported"] else math.inf
    return result


def arithmetic_report(rows: list[dict], tolerance: float = 0.02) -> dict:
    report = {}
    for item in arithmetic_checks(rows):
        entry = report.setdefault(item["check"], {"compared": 0, "tolerance": tolerance, "outside": [], "worst": []})
        entry["compared"] += 1
        entry["worst"].append(item)
        if abs(item["difference"]) > tolerance:
            entry["outside"].append(item)
    for entry in report.values():
        entry["worst"] = [{"period_end": i["period_end"], "difference": i["difference"]}
                          for i in sorted(entry["worst"], key=lambda i: -abs(i["difference"]))[:5]]
    return report


@lru_cache(maxsize=4)
def _cached(path: str, stamp: int) -> tuple[dict, ...]:
    with open(path, newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != QUARTERLY_COLUMNS:
            raise ValueError("Unexpected ual_quarterly.csv header")
        rows = tuple(reader)
    ends = [row["period_end"] for row in rows]
    if ends != sorted(set(ends)) or any(int(e[5:7]) % 3 or e != month_end(int(e[:4]), int(e[5:7])) for e in ends):
        raise ValueError("ual_quarterly.csv quarters are not unique, ordered quarter ends")
    return rows


def load_ual_quarterly(path: Path = QUARTERLY_CSV) -> list[dict]:
    """One dict per fiscal quarter, oldest first. Numbers are floats; a cell the release left out is None."""
    result = []
    for row in _cached(str(path), Path(path).stat().st_mtime_ns):
        end = date.fromisoformat(row["period_end"])
        result.append({"period_end": end, "quarter": f"{end.year}Q{end.month // 3}",
                       "filed_on": date.fromisoformat(row["filed_on"]), "accession": row["accession"],
                       **{field: float(row[field]) if row[field] != "" else None for field in VALUE_COLUMNS},
                       "source_note": row["source_note"]})
    return result


def quarterly_series(field: str, path: Path = QUARTERLY_CSV) -> tuple[list[date], np.ndarray]:
    """Quarter ends and one column as an array, NaN where the release did not report it."""
    if field not in VALUE_COLUMNS:
        raise ValueError(f"Unknown quarterly column {field}")
    rows = load_ual_quarterly(path)
    return [r["period_end"] for r in rows], np.array([np.nan if r[field] is None else r[field] for r in rows], dtype=float)
