"""Verifier for reader records.

A record is verified only if every check below passes. The checks establish
provenance (the quote is in the document, at the stated place, and the value is
what the quote says). They do not establish that the statement is true.

Checks, each with its own reason on failure:
  normalisation label matches the reader's;
  quote is the exact text at char_start:char_end and occurrence_index agrees;
  a table-row quote covers whole lines and its header context is the one the
    finder derives for that row;
  claim type is known and the unit is that claim type's unit;
  the value, range, qualifier and comparison year re-parse from the quote for the stated period;
  the value lies inside the plausibility bounds below;
  the target period ends on or after the document's period (or filing date);
  the document hash matches when the caller supplies one;
  picked_by is "rules" or "groq:<model>";
  the statement is not negated or withdrawn ("does not expect", "no longer").
Hedge words ("if", "could", "may") do not fail a record; they are listed in flags.
"""
from __future__ import annotations

import math
import re

from app.lab.reader import (CLAIMS, FORWARD, NORMALISATION, as_date, normalise, occurrence_index, parse_value,
                            period_end_date, row_context, sha256_hex)

# Plausibility bounds per claim type, in the claim type's unit. Wide on purpose:
# they catch a misread scale or a stray number, not an unusual quarter.
BOUNDS = {
    "adj_eps_guidance": (-30.0, 30.0),        # usd_per_share
    "fuel_assumed_usd_gal": (0.5, 8.0),       # usd_per_gallon
    "fuel_recovery_share": (0.0, 1.5),        # fraction of the fuel price increase
    "capacity_growth": (-1.0, 2.0),           # fraction, year over year
    "trasm_guidance": (-0.6, 0.8),            # fraction, year over year
    "casm_ex_guidance": (-0.5, 1.0),          # fraction, year over year
    "capex_guidance": (100.0, 20000.0),       # usd_million
    "profit_sharing_accrual": (0.0, 3000.0),  # usd_million
    "fuel_cost_impact": (1.0, 10000.0),       # usd_million
    "fuel_cost_impact_eps": (0.01, 20.0),     # usd_per_share
    "pretax_margin_guidance": (-1.0, 0.5),    # fraction of revenue
}
_SHA = re.compile(r"[0-9a-f]{64}")
_PICKED = re.compile(r"rules|groq:\S+")


def _integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _same(a, b) -> bool:
    return _number(a) and _number(b) and abs(a - b) <= 1e-9


def _whole_lines(text: str, start: int, end: int) -> bool:
    return (start == 0 or text[start - 1] == "\n") and (end == len(text) or text[end] == "\n")


def verify(record: dict, normalised_text: str, *, doc_sha256: str | None = None) -> dict:
    """Return a copy of the record with verified, reasons and flags set."""
    text, out, reasons, flags = normalised_text, dict(record), [], []
    if record.get("normalisation") != NORMALISATION:
        reasons.append("offsets were not produced under the reader's normalisation")

    quote, start, end = record.get("quote"), record.get("char_start"), record.get("char_end")
    located = (isinstance(quote, str) and quote != "" and _integer(start) and _integer(end)
               and 0 <= start < end <= len(text) and text[start:end] == quote)
    if not located:
        reasons.append("quote is not the exact document text at char_start:char_end")
    elif occurrence_index(text, quote, start) != record.get("occurrence_index"):
        reasons.append("occurrence_index does not match the position of the quote")

    context = record.get("context_quote")
    is_row = isinstance(quote, str) and "\t" in quote
    if located and (is_row or context is not None):
        if not _whole_lines(text, start, end):
            reasons.append("table-row quote does not cover whole lines")
        elif context is not None:
            span = (record.get("context_char_start"), record.get("context_char_end"))
            if not (_integer(span[0]) and _integer(span[1]) and text[span[0]:span[1]] == context):
                reasons.append("context_quote is not the exact document text at its offsets")
            elif row_context(text, start) != span:
                reasons.append("context_quote is not the header of this table row")

    claim = CLAIMS.get(record.get("claim"))
    if claim is None:
        reasons.append("unknown claim type")
    elif record.get("unit") != claim.unit:
        reasons.append(f"unit must be {claim.unit} for {claim.name}")

    period, target = record.get("period"), None
    try:
        target = period_end_date(period)
    except (TypeError, ValueError):
        reasons.append("period is not a label such as 2026Q3 or 2026FY")
    try:
        filed_on, period_end = as_date(record.get("doc_filed_on")), as_date(record.get("doc_period_end"))
    except ValueError:
        filed_on = period_end = None
        reasons.append("document dates are not ISO dates")
    reference = period_end or filed_on
    if reference is None:
        reasons.append("document period is unknown")
    elif target is not None and target < reference:
        reasons.append("target period ends before the document's period")
    elif target is not None and target == period_end:
        flags.append("target period is the one the document reports, so this restates earlier guidance")

    low, high, value = record.get("low"), record.get("high"), record.get("value")
    parsed = None
    if isinstance(quote, str) and claim is not None and target is not None:
        parsed = parse_value(quote, claim.name, period, context=context, filed_on=filed_on)
    if parsed is None:
        reasons.append("no value re-parses from the quote for this claim type and period")
    else:
        stated = value if isinstance(value, list) else [value, value]
        if not (_same(low, parsed.low) and _same(high, parsed.high) and len(stated) == 2
                and _same(stated[0], parsed.low) and _same(stated[1], parsed.high)
                and isinstance(value, list) == (parsed.low != parsed.high)):
            reasons.append("value does not match the value parsed from the quote")
        if record.get("qualifier") != parsed.qualifier:
            reasons.append("qualifier does not match the quote")
        if record.get("period_basis") != parsed.period_basis:
            reasons.append("period_basis does not match how the period is stated in the quote")
        if record.get("baseline_year") != parsed.baseline_year:
            reasons.append("baseline_year does not match the comparison year named in the quote")
        elif parsed.baseline_year is not None and target is not None and parsed.baseline_year != target.year - 1:
            flags.append(f"change is stated against {parsed.baseline_year}, not the prior year")
        if parsed.used_context and context is None:
            reasons.append("the row needs its table header to parse and none is recorded")
        if parsed.negated:
            reasons.append("statement is negated or withdrawn in the quote")
        flags.extend(f"hedged: {word}" for word in parsed.hedges)
        if claim.forward and not FORWARD.search(quote + " " + (context or "")):
            flags.append("no forward-looking wording in the quote")
    if claim is not None and _number(low) and _number(high):
        floor, ceiling = BOUNDS[claim.name]
        if not (floor <= low <= high <= ceiling):
            reasons.append(f"value outside plausibility bounds {floor} to {ceiling} {claim.unit}")

    sha = record.get("doc_sha256")
    if not (isinstance(sha, str) and _SHA.fullmatch(sha)):
        reasons.append("doc_sha256 is not a SHA-256 hex digest")
    elif doc_sha256 is not None and sha != doc_sha256:
        reasons.append("doc_sha256 does not match the document")
    elif doc_sha256 is None:
        flags.append("document hash not checked")
    if not (isinstance(record.get("picked_by"), str) and _PICKED.fullmatch(record["picked_by"])):
        reasons.append("picked_by must be rules or groq:<model>")

    out.update(verified=not reasons, reasons=reasons, flags=flags)
    return out


def verify_raw(record: dict, raw: bytes) -> dict:
    """Verify against the raw exhibit bytes: normalises them and checks their hash."""
    return verify(record, normalise(raw), doc_sha256=sha256_hex(raw))
