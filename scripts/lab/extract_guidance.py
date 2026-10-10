#!/usr/bin/env python3
"""Read guidance facts out of United's investor updates and earnings releases.

Input: data/lab/ual_filings_index.csv and the documents it lists under
data/lab_raw/sec/. Output: data/lab/ual_guidance.json, the verified records only,
one per (document, claim type, period), so every revision stays its own record.
A coverage table goes to stdout. An abstention is a valid outcome and is counted;
a record that fails verification is listed with its reasons and not written.

--picker groq makes one API call per (document, claim type, period) that has a
candidate, several hundred for the full index; --pause spaces them out.
--compare N runs both pickers on N documents spread over the years, stops at
--max-requests API calls, prints where they agree and differ, and writes nothing.

Exit codes: 0 done; 1 done, but an indexed document was missing or failed its
checksum; 2 the filings index is absent or malformed; 3 the Groq picker was
asked for and is not configured or its API failed (nothing is written).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab.reader import CLAIMS, GroqPicker, RulesPicker, as_date, doc_label, read_document

INDEX = ROOT / "data" / "lab" / "ual_filings_index.csv"
RAW = ROOT / "data" / "lab_raw" / "sec"
OUT = ROOT / "data" / "lab" / "ual_guidance.json"
COLUMNS = ("accession", "filed_on", "period_end", "form", "items", "exhibit", "filename", "url", "sha256", "path")
KINDS = ("investor_update", "earnings_release")


class _Stop(Exception):
    """Index or Groq problem that ends the run: (exit code, message)."""


class _Budget(Exception):
    """The request budget of a comparison run is spent."""


def selected(row: dict) -> bool:
    """Investor updates and earnings releases. Without a kind column: EX-99 exhibits of 8-K filings."""
    if not row["filename"].lower().endswith((".htm", ".html")):
        return False
    if row.get("kind"):
        return row["kind"] in KINDS
    return row["form"].upper().startswith("8-K") and bool(re.match(r"EX-99", row["exhibit"], re.I))


def reported_period(row: dict, rows: list[dict]) -> str | None:
    """The last quarter whose earnings release was filed on or before this document's
    filing date. The index states it for a release; an update has none of its own."""
    known = [r["period_end"] for r in rows if r["period_end"] and r["filed_on"] <= row["filed_on"]]
    return row["period_end"] or (max(known) if known else None)


def document_ids(rows: list[dict]) -> dict:
    """ual-<last reported quarter>-<exhibit>; a later filing that would repeat an ID also carries its filing date."""
    ids, used = {}, set()
    for row in sorted(rows, key=lambda r: (r["filed_on"], r["accession"], r["exhibit"], r["filename"])):
        day = as_date(reported_period(row, rows)) or as_date(row["filed_on"])
        exhibit = re.sub(r"[^a-z0-9]", "", row["exhibit"].lower())
        options = [f"ual-{doc_label(day)}-{exhibit}"]
        options.append(f"{options[0]}-{row['filed_on'].replace('-', '')}")
        options.append(f"{options[1]}-{row['accession'][-6:]}")
        options.append(f"{options[2]}-{re.sub(r'[^a-z0-9]', '', row['filename'].lower())}")
        ids[(row["accession"], row["filename"])] = next(o for o in options if o not in used)
        used.add(ids[(row["accession"], row["filename"])])
    return ids


def load_index(index: Path) -> list[dict]:
    if not index.exists():
        raise _Stop(2, f"Filings index not found: {index}. Build it first (scripts/lab/fetch_ual_filings.py); "
                       "nothing was read and nothing was written.")
    with index.open(newline="") as stream:
        table = csv.DictReader(stream)
        missing = [c for c in COLUMNS if c not in (table.fieldnames or [])]
        rows = list(table)
    if missing:
        raise _Stop(2, f"Filings index {index} lacks columns: {', '.join(missing)}. Nothing was written.")
    return rows


def name_of(row: dict) -> str:
    return f"{row['filed_on']} {row['accession']} {row['exhibit']}"


def raw_bytes(row: dict, root: Path, raw_root: Path) -> tuple:
    """(bytes, None), or (None, why the document was not read). A file that fails its checksum is never read."""
    path = Path(row["path"]) if Path(row["path"]).is_absolute() else root / row["path"]
    if raw_root.resolve() not in path.resolve().parents:
        return None, f"{name_of(row)}: {row['path']} is not under {raw_root}"
    if not path.exists():
        return None, f"{name_of(row)}: raw file {row['path']} is missing"
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != row["sha256"]:
        return None, f"{name_of(row)}: raw file {row['path']} does not match the index checksum"
    return raw, None


def read_row(row: dict, raw: bytes, everything: list[dict], ids: dict, picker) -> dict:
    extra = {"accession": row["accession"], "exhibit": row["exhibit"], "form": row["form"]}
    try:
        return read_document(raw, doc_id=ids[(row["accession"], row["filename"])], doc_url=row["url"],
                             filed_on=row["filed_on"], period_end=reported_period(row, everything),
                             picker=picker, extra=extra)
    except Exception as error:
        if type(error).__name__ == "GroqNotConfigured":
            raise _Stop(3, "The Groq picker needs GROQ_API_KEY. Set it, or run with --picker rules. "
                           "Nothing was written.") from None
        if type(error).__name__ == "GroqError":
            raise _Stop(3, f"The Groq picker failed at {name_of(row)}: {error} Nothing was written.") from None
        raise


def stated(record: dict) -> tuple:
    return record["low"], record["high"], record["qualifier"], record.get("baseline_year")


def annotate_revisions(records: list[dict]) -> None:
    """Dates across documents for the same claim type and period: when this record
    was issued, when the first one was, and the next later-dated record that states
    a different value, if any. Same-day documents do not revise each other."""
    groups = {}
    for record in records:
        groups.setdefault((record["claim"], record["period"]), []).append(record)
    for group in groups.values():
        group.sort(key=lambda r: (r["doc_filed_on"] or "", r["id"]))
        for record in group:
            day = record["doc_filed_on"] or ""
            later = [o for o in group if (o["doc_filed_on"] or "") > day and stated(o) != stated(record)]
            earlier = [o for o in group if (o["doc_filed_on"] or "") < day]
            previous = [o for o in earlier if o["doc_filed_on"] == earlier[-1]["doc_filed_on"]]
            record["issued_on"] = record["doc_filed_on"]
            record["first_issued_on"] = group[0]["doc_filed_on"]
            record["revises_id"] = previous[0]["id"] if previous and all(stated(o) != stated(record) for o in previous) else None
            record["revised_later_on"] = later[0]["doc_filed_on"] if later else None
            record["revised_later_id"] = later[0]["id"] if later else None


def coverage_table(documents: list[dict]) -> str:
    claims = list(CLAIMS)
    width = max([len(d["name"]) for d in documents] + [8])
    lines = ["Coverage per document and claim type: found / abstained / failed verification",
             "  " + "  ".join(f"{n + 1}={CLAIMS[c].slug}" for n, c in enumerate(claims)),
             "document".ljust(width) + "".join(f"{n + 1:>9}" for n in range(len(claims)))]
    totals = {c: [0, 0, 0] for c in claims}
    for d in documents:
        cells = []
        for c in claims:
            counts = [sum(o["claim"] == c and o["outcome"] == kind for o in d["outcomes"])
                      for kind in ("found", "abstained", "failed")]
            totals[c] = [a + b for a, b in zip(totals[c], counts)]
            cells.append("/".join(map(str, counts)).rjust(9))
        lines.append(d["name"].ljust(width) + "".join(cells))
    lines.append("total".ljust(width) + "".join("/".join(map(str, totals[c])).rjust(9) for c in claims))
    return "\n".join(lines)


def run(index: Path = INDEX, out: Path = OUT, picker: str = "rules", model: str | None = None,
        root: Path = ROOT, raw_root: Path = RAW, pause: float = 0.0) -> int:
    try:
        everything = load_index(index)
        rows = [r for r in everything if selected(r)]
        ids = document_ids(everything)
        groq = GroqPicker(model, pause=pause) if picker == "groq" else None
        documents, records, problems = [], [], []
        for row in rows:
            raw, problem = raw_bytes(row, root, raw_root)
            if problem:
                problems.append(problem)
                continue
            result = read_row(row, raw, everything, ids, groq or RulesPicker(filed_on=row["filed_on"]))
            documents.append({"name": name_of(row), "outcomes": result["outcomes"]})
            records.extend(result["records"])
    except _Stop as stop:
        print(stop.args[1], file=sys.stderr)
        return stop.args[0]

    verified = sorted((r for r in records if r["verified"]), key=lambda r: (r["doc_filed_on"] or "", r["id"]))
    if len({r["id"] for r in verified}) != len(verified):
        raise ValueError("Duplicate evidence IDs; refusing to write")
    annotate_revisions(verified)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(verified, indent=2) + "\n")

    print(f"picker: {groq.label if groq else 'rules'}; documents read: {len(documents)} of {len(rows)} selected")
    if documents:
        print(coverage_table(documents))
    failed = [r for r in records if not r["verified"]]
    if failed:
        print("Failed verification (not written):")
        for r in failed:
            print(f"  {r['id']}: {'; '.join(r['reasons'])}")
    if problems:
        print("Not read:")
        for line in problems:
            print("  " + line)
    print(f"Wrote {len(verified)} verified records to {out}")
    return 1 if problems else 0


def spread(rows: list[dict], count: int) -> list[dict]:
    """About `count` documents at even steps through the index in date order."""
    rows = sorted(rows, key=lambda r: (r["filed_on"], r["accession"], r["exhibit"], r["filename"]))
    if count >= len(rows) or count < 2:
        return rows[:max(count, 0)] if count < 2 else rows
    return [rows[round(i * (len(rows) - 1) / (count - 1))] for i in range(count)]


def compare(index: Path = INDEX, sample: int = 30, max_requests: int = 150, model: str | None = None,
            root: Path = ROOT, raw_root: Path = RAW, pause: float = 0.0, client=None) -> int:
    """Rules picker against the Groq picker on a sample. Prints; writes nothing."""
    used = [0]
    try:
        everything = load_index(index)
        ids = document_ids(everything)
        if client is None:
            try:
                from app.lab import groq_client
            except ImportError:
                raise _Stop(3, "app.lab.groq_client is not available. Nothing was written.") from None
            client = groq_client.chat_completion

        def budgeted(messages, **options):
            if used[0] >= max_requests:
                raise _Budget()
            used[0] += 1
            return client(messages, **options)

        groq = GroqPicker(model, client=budgeted, pause=pause)
        tally, lines, done = {}, [], 0
        for row in spread([r for r in everything if selected(r)], sample):
            raw, problem = raw_bytes(row, root, raw_root)
            if problem:
                lines.append("not read: " + problem)
                continue
            by_rules = read_row(row, raw, everything, ids, RulesPicker(filed_on=row["filed_on"]))
            try:
                by_model = read_row(row, raw, everything, ids, groq)
            except _Budget:
                lines.append(f"stopped before {name_of(row)}: request budget of {max_requests} spent")
                break
            done += 1
            picks = [{(r["claim"], r["period"]): r for r in result["records"]} for result in (by_rules, by_model)]
            tried = sorted({(o["claim"], o["period"]) for o in by_model["outcomes"] if o["period"]})
            for key in tried:
                a, b = picks[0].get(key), picks[1].get(key)
                if a and b:
                    verdict = "same passage" if a["char_start"] == b["char_start"] else \
                        "different passage, same value" if stated(a) == stated(b) else "different passage and value"
                elif a:
                    verdict = "model abstained, rules picked"
                elif b:
                    verdict = "model picked, rules abstained"
                else:
                    verdict = "both abstained"
                tally[verdict] = tally.get(verdict, 0) + 1
                if verdict != "same passage":
                    shown = b or a
                    detail = f"{shown['value']} verified={shown['verified']} :: {shown['quote'][:110]!r}" if shown else ""
                    lines.append(f"{name_of(row)} {key[0]} {key[1]}: {verdict} {detail}")
    except _Stop as stop:
        print(stop.args[1], file=sys.stderr)
        return stop.args[0]
    print(f"picker under test: {groq.label}; documents compared: {done}; API requests: {used[0]} of at most {max_requests}")
    for verdict, count in sorted(tally.items(), key=lambda item: -item[1]):
        print(f"  {count:4d}  {verdict}")
    for line in lines:
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--picker", choices=("rules", "groq"), default="rules")
    parser.add_argument("--model", help="Groq model ID; default is the client's")
    parser.add_argument("--pause", type=float, default=4.0, help="seconds between Groq calls (default 4)")
    parser.add_argument("--compare", type=int, metavar="N", help="compare both pickers on N documents; writes nothing")
    parser.add_argument("--max-requests", type=int, default=150, help="API call budget for --compare (default 150)")
    parser.add_argument("--index", type=Path, default=INDEX)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    if args.compare:
        return compare(args.index, args.compare, args.max_requests, args.model, pause=args.pause)
    return run(args.index, args.out, args.picker, args.model, pause=args.pause)


if __name__ == "__main__":
    raise SystemExit(main())
