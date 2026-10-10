"""Evidence ledger (evidence.json) and the facts that frame the dashboard (meta.json).

The ledger holds one entry for each management statement or reported figure that
a dashboard number rests on:
  United's guidance issued in the year of its current quarterly guidance
    (data/lab/ual_guidance.json);
  United statements quoted in data/lab/literature.json that the guidance file
    does not hold (QUOTED), and one that the literature file only paraphrases
    (PARAPHRASED);
  United's reported figures (REPORTED) for the latest reported quarter and for
    the year-ago quarter of the guided period (data/lab/ual_quarterly.csv);
  Delta's guidance for the same quarter and its result (data/lab/dal_check.json);
  the news release that dates United's next report (WEB).

`verified` follows the contract and is set only by a check made in this build
against the document's own bytes: the SHA-256 matches, the quote is the exact
text at char_start:char_end of the reader's normalised text, and the value
parses from the quote. Guidance records go through app.lab.verify unchanged. The
other classes are outside that verifier's claim types and go through
`check_quote`. A record that fails either check stops the build. `basis` says
what stands behind an entry: document_text (checked as above), paraphrase, or
web_page (read on the web, no local copy); the last two are never verified.

meta.json takes its guidance and promises from ledger entries. Its report date
and outside comparators come from WEB: pages read on the day named there.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re

from app.lab import reader, verify

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "app" / "static" / "data"
GUIDANCE, LITERATURE, DAL_CHECK = "data/lab/ual_guidance.json", "data/lab/literature.json", "data/lab/dal_check.json"
QUARTERLY, QUARTERLY_SOURCES = "data/lab/ual_quarterly.csv", "data/lab/ual_quarterly_sources.json"
INDEX = "data/lab/ual_filings_index.csv"

KEYS = ("id", "claim", "period", "value", "unit", "quote", "doc_url", "doc_sha256", "char_start", "char_end",
        "picked_by", "verified")
EXTRA_KEYS = ("basis", "source", "doc_date", "normalisation", "note")
BASES = ("document_text", "paraphrase", "web_page")
UNITS = {"fuel_recovery_share": "share"}  # The page formats `share` as a percentage; the guidance file says fraction.

# Columns of ual_quarterly.csv that the clocks, the simulator and the scoreboard read, with their units.
REPORTED = {"asm_m": "million_miles", "total_revenue_musd": "usd_million", "trasm_cents": "cents",
            "fuel_gallons_m": "million_gallons", "fuel_price_usd_gal": "usd_per_gallon",
            "fuel_expense_musd": "usd_million", "pretax_income_musd": "usd_million", "net_income_musd": "usd_million",
            "adj_eps_diluted": "usd_per_share", "diluted_shares_m": "million_shares", "profit_sharing_musd": "usd_million"}

# Statements carried from literature.json: (evidence id, claim, period, unit, literature entry, labels of the
# numbers carried, factor from the literature's unit to this one). Two labels make a [low, high] range.
QUOTED = (
    ("ual-2026q2-ex991-fuel-recovery-reported-2026q2", "fuel_recovery_share_reported", "2026Q2", "share",
     "ual-2026q2-earnings-release",
     ("Share of the second-quarter fuel expense increase recovered (stated as 'approximately half')",), 0.01),
    ("ual-2026q2-ex991-fuel-expense-increase-2026q2", "fuel_expense_increase", "2026Q2", "usd_billion",
     "ual-2026q2-earnings-release", ("Increase in fuel expense, 2026Q2 versus 2025Q2",), 1),
    ("ual-2026q2-ex991-fuel-recovery-2026q3", "fuel_recovery_share", "2026Q3", "share",
     "ual-2026q2-earnings-release", ("Expected recovery, 2026Q3, low", "Expected recovery, 2026Q3, high"), 0.01),
)
# Statements literature.json holds only as its own paraphrase: (evidence id, claim, period, entry, opening words).
PARAPHRASED = (
    ("ual-2026q2-ex992-profit-sharing-tiers-2026q3", "profit_sharing_tiers", "2026Q3", "ual-2026q2-investor-update",
     "Profit sharing is the sum of three tiers"),
)

# Read on the web on `retrieved_on`, one lookup each. The reader returns page text, not bytes, so there is no
# SHA-256 and no offset; quoted wording is as the reader returned it. Rerun the lookups to refresh these.
_MARKET = "ual-quarterly-earnings-nongaap-eps-10-20-2026-2pt84"
WEB = {
    "period": "2026Q3", "retrieved_on": "2026-10-09",
    "read_via": "page text returned by a web reader (a language model); wording not machine-checked",
    "report_date": {
        "value": "2026-10-20", "timing": "after market close", "call_date": "2026-10-21", "published_on": "2026-09-30",
        "source_url": "https://finviz.com/news/397198/united-to-hold-webcast-of-third-quarter-2026-financial-results",
        "publisher": "United Airlines news release (PR Newswire, Chicago, 30 September 2026), reprinted by Finviz",
        "quote": "The company will issue its third-quarter results and outlook after market close on Tuesday, October 20.",
    },
    "consensus_eps": {
        "value": 2.84, "as_of": "2026-10-09", "source_url": "https://tickzen.app/stocks/ual/earnings",
        "quote": "Analysts expect EPS of $2.84.",
        "as_of_note": "The page prints no date; as_of is the day it was read.",
        "provider_note": "Tickzen, which says its estimates come from Yahoo Finance. It does not name the quarter "
                         "(\"the next report\") or say whether the figure is adjusted.",
        "also_stated_by": {
            "source_url": "https://gamma-api.polymarket.com/public-search?q=United%20Airlines%20UAL%20earnings",
            "quote": "The Street consensus estimate for United Airlines’s non-GAAP EPS for the relevant quarter is $2.84",
            "note": "The threshold written into the prediction market below, which opened on 2026-10-07."},
        "note": "Aggregators do not agree on this figure. Other values seen in search snippets were not read on "
                "their pages and are not recorded.",
    },
    "market_odds_beat": {
        "value": 0.6, "as_of": "2026-10-09", "volume_usd": 689.9,
        "source_url": "https://polymarket.com/event/" + _MARKET,
        "api_url": "https://gamma-api.polymarket.com/events?slug=" + _MARKET,
        "question": "Will United Airlines (UAL) beat quarterly earnings?", "threshold_eps": 2.84,
        "price_basis": "the Yes price the market publishes; it equals the midpoint of the best bid and best ask",
        "best_bid": 0.52, "best_ask": 0.68, "last_trade": 0.43, "liquidity_usd": 100.66,
        "market_updated_at": "2026-10-09T06:22:33Z", "resolves_on": "2026-10-20",
        "note": "A thin market: about $690 traded in total, about $100 resting in the book and 16 points between "
                "bid and ask, with the last trade at 43%. Treat the price as a weak signal.",
    },
}


# ---------------------------------------------------------------- sources

def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class _Sources:
    """Reads inputs under one root and keeps the path and SHA-256 of each one read."""

    def __init__(self, root: Path):
        self.root, self.inputs, self._texts = Path(root), {}, {}

    def read(self, path: str) -> bytes:
        raw = (self.root / path).read_bytes()
        self.inputs[path] = _sha256(raw)
        return raw

    def raw(self, path: str, sha256: str) -> bytes:
        """A raw filing, which must be present and match the hash its record carries."""
        if not (self.root / path).exists():
            raise FileNotFoundError(f"{path} is not on this machine. The ledger checks every quote against the raw "
                                    "filing, so build it where data/lab_raw is present (scripts/lab/fetch_*_filings.py).")
        raw = self.read(path)
        if self.inputs[path] != sha256:
            raise ValueError(f"{path} does not match the SHA-256 recorded for it")
        return raw

    def text(self, path: str, sha256: str) -> str:
        if path not in self._texts:
            self._texts[path] = reader.normalise(self.raw(path, sha256))
        return self._texts[path]


# ---------------------------------------------------------------- the check

_PRINTED = re.compile(r"(?<![\w.,])(\()?\$?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(\))?"
                      r"(¢|-cents?\b| cents?\b|%| percent\b)?")


def printed_numbers(quote: str, unit: str = "") -> list[float]:
    """The numbers a quote prints, read for a unit.

    Parentheses mean negative. A cents figure reads in dollars for a dollar unit. For `share`
    only percentages count, as fractions, and the word "half" reads as 0.5.
    """
    values = []
    for opened, digits, closed, suffix in _PRINTED.findall(quote):
        number = float(digits.replace(",", "")) * (-1 if opened and closed else 1)
        if unit == "share":
            if suffix in ("%", " percent"):
                values.append(number / 100)
        else:
            cents = suffix in ("¢", "-cent", "-cents", " cent", " cents")
            values.append(number / 100 if cents and unit.startswith("usd") else number)
    if unit == "share" and re.search(r"\bhalf\b", quote, re.I):
        values.append(0.5)
    return values


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check_quote(record: dict, text: str) -> list[str]:
    """Why a record fails the contract's rule against a normalised document text; empty when it passes."""
    quote, start, end = record.get("quote"), record.get("char_start"), record.get("char_end")
    reasons = []
    if not (isinstance(quote, str) and quote and quote == quote.strip() and type(start) is int and type(end) is int
            and 0 <= start < end <= len(text) and text[start:end] == quote):
        return ["quote is not the exact document text at char_start:char_end"]
    value, unit = record.get("value"), record.get("unit") or ""
    if unit == "text":
        if value is not None:
            reasons.append("a text record carries no value")
        return reasons
    values, printed = value if isinstance(value, list) else [value], printed_numbers(quote, unit)
    if not values or not all(_number(v) and any(math.isclose(v, p, rel_tol=0, abs_tol=1e-9) for p in printed)
                             for v in values):
        reasons.append("value does not parse from the quote")
    return reasons


def cited_ids(node) -> list[str]:
    """Every evidence id cited under a JSON value (keys evidence_id and evidence_ids), in order, once each."""
    found = []

    def walk(item):
        if isinstance(item, dict):
            for key, value in item.items():
                if key == "evidence_id" and isinstance(value, str):
                    found.append(value)
                elif key == "evidence_ids":
                    found.extend(v for v in (value.values() if isinstance(value, dict) else value) if isinstance(v, str))
                else:
                    walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(node)
    return list(dict.fromkeys(found))


# ---------------------------------------------------------------- entries

def quarter_of(day: str) -> str:
    return f"{day[:4]}Q{(int(day[5:7]) + 2) // 3}"


def _entry(record: dict, *, basis: str, source: str, doc_date: str, note: str | None = None) -> dict:
    checked = basis == "document_text"
    return {**{key: record.get(key) for key in KEYS}, "verified": checked, "basis": basis, "source": source,
            "doc_date": doc_date, "normalisation": reader.NORMALISATION if checked else None, "note": note}


def _checked(record: dict, text: str, **fields) -> dict:
    reasons = check_quote(record, text)
    if reasons:
        raise ValueError(f"{record['id']}: {'; '.join(reasons)}")
    return _entry(record, basis="document_text", **fields)


def guidance_entries(src: _Sources, records: list[dict], index: dict) -> list[dict]:
    entries = []
    for record in records:
        checked = verify.verify_raw(record, src.raw(index[record["doc_url"]]["path"], record["doc_sha256"]))
        if not checked["verified"]:
            raise ValueError(f"{record['id']} fails app.lab.verify: {'; '.join(checked['reasons'])}")
        entries.append(_entry({**record, "unit": UNITS.get(record["claim"], record["unit"])}, basis="document_text",
                              source=GUIDANCE, doc_date=record["doc_filed_on"]))
    return entries


def literature_entries(src: _Sources, literature: dict, index: dict) -> list[dict]:
    by_id, entries = {entry["id"]: entry for entry in literature["entries"]}, []
    for item_id, claim, period, unit, entry_id, labels, factor in QUOTED:
        entry = by_id[entry_id]
        numbers = [n for label in labels for n in entry["numbers"] if n["label"] == label]
        if entry.get("verified") is not True or len(numbers) != len(labels) or len({n["quote"] for n in numbers}) != 1:
            raise ValueError(f"{item_id}: {entry_id} does not hold one verified quote for {labels}")
        document, quote = index[entry["url"]], numbers[0]["quote"]
        text = src.text(document["path"], document["sha256"])
        start = text.find(quote)
        if start < 0:
            raise ValueError(f"{item_id}: the quote in {LITERATURE} is not in {document['path']}")
        values = [round(n["value"] * factor, 6) for n in numbers]
        record = {"id": item_id, "claim": claim, "period": period, "value": values[0] if len(values) == 1 else values,
                  "unit": unit, "quote": quote, "doc_url": document["url"], "doc_sha256": document["sha256"],
                  "char_start": start, "char_end": start + len(quote), "picked_by": "literature"}
        entries.append(_checked(record, text, source=LITERATURE, doc_date=document["filed_on"]))
    for item_id, claim, period, entry_id, opening in PARAPHRASED:
        entry = by_id[entry_id]
        match = re.search(re.escape(opening) + r".*?\.(?!\d)", entry["notes"])
        if not match:
            raise ValueError(f"{item_id}: {entry_id} no longer holds the paraphrase starting {opening!r}")
        record = {"id": item_id, "claim": claim, "period": period, "value": None, "unit": "text",
                  "doc_url": entry["url"], "picked_by": "literature"}
        entries.append(_entry(record, basis="paraphrase", source=LITERATURE, doc_date=index[entry["url"]]["filed_on"],
                              note=f"A paraphrase written in {LITERATURE} (entry {entry_id}), not United's words and "
                                   f"not checked against the document: {match.group(0)}"))
    return entries


def _table_row(text: str, label: str, printed: str) -> tuple[int, int] | None:
    """The first table line whose label cell is `label` and whose first figure is `printed`."""
    position = 0
    for line in text.split("\n"):
        cells = [cell for cell in line.split("\t") if cell.strip() not in ("", "$")]
        if len(cells) > 1 and cells[0] == label and cells[1] == printed:
            return position, position + len(line)
        position += len(line) + 1
    return None


def reported_entries(src: _Sources, rows: list[dict], cells: dict, index: dict, periods: set[str]) -> list[dict]:
    documents, entries = {(row["accession"], row["filename"]): row for row in index.values()}, []
    for row in rows:
        period = quarter_of(row["period_end"])
        for field, unit in REPORTED.items() if period in periods else ():
            cell = cells[row["period_end"]][field]
            document = documents[cell["accession"], cell["filename"]]
            text = src.text(document["path"], document["sha256"])
            span = _table_row(text, cell["label"], cell["printed"])
            if span is None or float(cell["value"]) != float(row[field]):
                raise ValueError(f"{period} {field}: no table row prints {cell['printed']!r} under {cell['label']!r}")
            exhibit = re.sub(r"[^a-z0-9]", "", document["exhibit"].lower())
            record = {"id": f"ual-{period.lower()}-{exhibit}-{field.replace('_', '-')}-{period.lower()}",
                      "claim": field, "period": period, "value": float(row[field]) if "." in row[field] else int(row[field]),
                      "unit": unit, "quote": text[span[0]:span[1]], "doc_url": document["url"],
                      "doc_sha256": document["sha256"], "char_start": span[0], "char_end": span[1], "picked_by": "rules"}
            entries.append(_checked(record, text, source=QUARTERLY, doc_date=document["filed_on"]))
    return entries


def delta_entries(src: _Sources, check: dict) -> list[dict]:
    """Delta's guidance for the September quarter and its result: every record the file's comparison cites."""
    documents, records, entries = {d["url"]: d for d in check["documents"]}, {r["id"]: r for r in check["evidence"]}, []
    for item_id in cited_ids(check["guidance_vs_actual_sep_quarter"]):
        record, document = records[item_id], documents[records[item_id]["doc_url"]]
        if record.get("normalisation") != reader.NORMALISATION:
            raise ValueError(f"{item_id}: offsets are not under the reader's normalisation")
        entries.append(_checked({**record, "claim": "dal_" + record["claim"]},
                                src.text(document["path"], record["doc_sha256"]),
                                source=DAL_CHECK, doc_date=document["filed_on"]))
    return entries


def web_entries(period: str) -> list[dict]:
    if period != WEB["period"]:
        raise ValueError(f"The web lookups in WEB were made for {WEB['period']}; the guided quarter is now {period}. "
                         "Rerun them and update WEB.")
    page = WEB["report_date"]
    record = {"id": f"ual-{period.lower()}-news-release-report-date", "claim": "report_date", "period": period,
              "value": page["value"], "unit": "date", "quote": page["quote"], "doc_url": page["source_url"],
              "picked_by": "web_lookup"}
    return [_entry(record, basis="web_page", source="web", doc_date=page["published_on"],
                   note=f"{page['publisher']}. Read on {WEB['retrieved_on']}: {WEB['read_via']}. No local copy, so no "
                        "hash or offsets, and not verified against document text.")]


# ---------------------------------------------------------------- meta

def _current_guidance(records: list[dict]) -> tuple[dict, dict]:
    """The latest quarterly EPS guidance and the fuel price assumed with it."""
    quarterly = [r for r in records if r["claim"] == "adj_eps_guidance" and re.fullmatch(r"\d{4}Q[1-4]", r["period"])]
    eps = max(quarterly, key=lambda r: (r["issued_on"], r["period"]))
    fuel = [r for r in records if r["claim"] == "fuel_assumed_usd_gal" and r["period"] == eps["period"]
            and r["issued_on"] == eps["issued_on"]]
    if len(fuel) != 1 or fuel[0]["low"] != fuel[0]["high"]:
        raise ValueError(f"Expected one fuel price assumed with {eps['id']}")
    return eps, fuel[0]


def build_meta(records: list[dict], entries: list[dict]) -> dict:
    eps, fuel = _current_guidance(records)
    year = eps["issued_on"][:4]
    promised = sorted(sorted((r for r in records if r["claim"] == "fuel_recovery_share" and r["issued_on"][:4] == year),
                             key=lambda r: r["period"]), key=lambda r: r["issued_on"], reverse=True)
    promises = []
    for record in promised:
        restated = [e["id"] for e in entries if e["claim"] == "fuel_recovery_share" and e["source"] == LITERATURE
                    and e["period"] == record["period"] and e["doc_date"] == record["issued_on"]]
        promise = {"made_on": record["issued_on"], "period": record["period"], "recovery_low": record["low"],
                   "recovery_high": record["high"], "evidence_ids": [record["id"]] + restated, "outcome": None,
                   "superseded_on": record["revised_later_on"]}
        for entry in entries:
            if entry["claim"] == "fuel_recovery_share_reported" and entry["period"] == record["period"]:
                promise["management_reported"] = {"value": entry["value"], "stated_on": entry["doc_date"],
                                                  "approximate": "approximately" in entry["quote"],
                                                  "evidence_ids": [entry["id"]]}
        promises.append(promise)
    page = WEB["report_date"]
    stamp = {"retrieved_on": WEB["retrieved_on"], "read_via": WEB["read_via"]}
    return {"company": "UAL", "report_date": page["value"],
            "report_date_source": {**{k: page[k] for k in ("timing", "call_date", "published_on", "source_url")}, **stamp,
                                   "evidence_ids": [e["id"] for e in entries if e["claim"] == "report_date"]},
            "guidance": {"made_on": eps["issued_on"], "period": eps["period"], "eps_low": eps["low"],
                         "eps_high": eps["high"], "fuel_assumed_usd_gal": fuel["value"],
                         "evidence_ids": [eps["id"], fuel["id"]]},
            "promises": promises,
            "comparators": {name: {**WEB[name], **stamp} for name in ("consensus_eps", "market_odds_beat")}}


# ---------------------------------------------------------------- build

def build(generated_at: str | None = None, root: Path = ROOT) -> tuple[dict, dict]:
    """(evidence.json, meta.json) from the committed inputs and the raw filings they point at. No network."""
    src = _Sources(root)
    records = json.loads(src.read(GUIDANCE))
    index = {row["url"]: row for row in csv.DictReader(io.StringIO(src.read(INDEX).decode("utf-8")))}
    rows = list(csv.DictReader(io.StringIO(src.read(QUARTERLY).decode("utf-8"))))
    eps, _ = _current_guidance(records)
    year_ago = f"{int(eps['period'][:4]) - 1}{eps['period'][4:]}"
    entries = (guidance_entries(src, [r for r in records if r["issued_on"][:4] == eps["issued_on"][:4]], index)
               + literature_entries(src, json.loads(src.read(LITERATURE)), index)
               + reported_entries(src, rows, json.loads(src.read(QUARTERLY_SOURCES)), index,
                                  {quarter_of(rows[-1]["period_end"]), year_ago})
               + delta_entries(src, json.loads(src.read(DAL_CHECK)))
               + web_entries(eps["period"]))
    stamp = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inputs = [{"path": path, "sha256": sha} for path, sha in src.inputs.items()]
    ledger = {"generated_at": stamp, "as_of": max(e["doc_date"] for e in entries), "inputs": inputs,
              "note": "verified is true only for an entry checked in this build against the document's bytes: the "
                      "SHA-256 matches, the quote is the exact text at char_start:char_end of the normalised text, and "
                      "the value parses from the quote. basis names what stands behind an entry: document_text, "
                      "paraphrase or web_page.",
              "counts": {"items": len(entries), "verified": sum(e["verified"] for e in entries),
                         **{basis: sum(e["basis"] == basis for e in entries) for basis in BASES}},
              "items": entries}
    meta = {"generated_at": stamp, "as_of": max(WEB["retrieved_on"], eps["issued_on"]), "inputs": inputs,
            **build_meta(records, entries)}
    validate(ledger, meta)
    return ledger, meta


def validate(ledger: dict, meta: dict) -> None:
    """Raise ValueError unless the two files are well formed and every id meta cites is in the ledger."""
    problems, items = [], ledger.get("items") or []
    ids = [item.get("id") for item in items]
    if len(set(ids)) != len(ids):
        problems.append("two entries share an id")
    for item in items:
        name = item.get("id")
        if not (isinstance(name, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", name)):
            problems.append(f"{name!r} is not a readable id")
        if set(item) != set(KEYS + EXTRA_KEYS) or item["basis"] not in BASES:
            problems.append(f"{name}: wrong keys or basis")
        elif item["verified"] is not (item["basis"] == "document_text"):
            problems.append(f"{name}: verified must be true exactly when the basis is document_text")
        elif item["verified"] and not (isinstance(item["quote"], str) and item["quote"]
                                       and re.fullmatch(r"[0-9a-f]{64}", item["doc_sha256"] or "")
                                       and type(item["char_start"]) is int and type(item["char_end"]) is int
                                       and item["char_end"] - item["char_start"] == len(item["quote"])
                                       and str(item["doc_url"]).startswith("https://")):
            problems.append(f"{name}: a verified entry needs its quote, document hash, offsets and URL")
        elif not item["verified"] and not item["note"]:
            problems.append(f"{name}: an unverified entry needs a note saying why")
    by_id = {item.get("id"): item for item in items}
    cited = cited_ids(meta)
    problems.extend(f"meta cites {name}, which is not in the ledger" for name in cited if name not in by_id)
    guidance = meta.get("guidance") or {}
    if not problems:
        values = [by_id[name]["value"] for name in guidance.get("evidence_ids", [])]
        if values != [[guidance.get("eps_low"), guidance.get("eps_high")], guidance.get("fuel_assumed_usd_gal")]:
            problems.append("guidance does not match the entries it cites")
        if not all(promise["evidence_ids"] and promise["outcome"] is None for promise in meta.get("promises", [])):
            problems.append("each promise needs evidence and a null outcome")
    for name, comparator in (meta.get("comparators") or {}).items():
        if comparator.get("value") is None and not comparator.get("reason"):
            problems.append(f"{name}: a null comparator needs a reason")
        elif comparator.get("value") is not None and not (str(comparator.get("source_url", "")).startswith("https://")
                                                          and comparator.get("as_of")):
            problems.append(f"{name}: a comparator needs the page it was read on and its date")
    for payload in (ledger, meta):
        if "fixture" in payload or not all(payload.get(key) for key in ("generated_at", "as_of", "inputs")):
            problems.append("each file needs generated_at, as_of and inputs, and is not a fixture")
        if any(Path(entry["path"]).is_absolute() for entry in payload.get("inputs") or []):
            problems.append("inputs must be relative paths")
    if problems:
        raise ValueError("; ".join(problems))


def write(ledger: dict, meta: dict, out_dir: Path = OUT_DIR) -> None:
    validate(ledger, meta)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in (("evidence.json", ledger), ("meta.json", meta)):
        (out_dir / name).write_text(json.dumps(payload, indent=1, allow_nan=False) + "\n")
