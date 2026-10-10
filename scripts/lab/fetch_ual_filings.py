"""Download United's earnings-release and investor-update 8-Ks, then build the quarterly table.

Every 8-K or 8-K/A filed on or after --since with item 2.02, 7.01 or 8.01 is
listed through its index.json and typed through its EDGAR index page. Its
primary document and each EX-99.x HTML exhibit are kept byte-exact under
data/lab_raw/sec/<accession>/<filename> and listed in
data/lab/manifests/ual_filings.json. The index keeps every 2.02 filing, and a
7.01/8.01 filing only when it carries an investor update by the stated rules in
app/lab/filings.py: an exhibit titled so, or guidance in the 8-K's own text.
`--offline` rebuilds the index, the quarterly table, its cell sources and the
checks from that store, with no network.

SEC asks for a declared User-Agent. It is read from SEC_USER_AGENT or --user-agent
and is never written to the manifest, the raw store or the outputs.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab import filings
from app.source_store import byte_hash

CIK = 100517
RAW = ROOT / "data" / "lab_raw" / "sec"
MANIFEST = ROOT / "data" / "lab" / "manifests" / "ual_filings.json"
ANCHOR = ROOT / "data" / "ual_q2_2026.json"
DATA_API = "https://data.sec.gov/submissions/"
ARCHIVE = f"https://www.sec.gov/Archives/edgar/data/{CIK}"
MIN_GAP = 0.3  # Seconds between requests: under this project's 4 per second.
RETRY_STATUS = {429, 500, 502, 503, 504}
TERMS = "SEC EDGAR public filings. Fair-access policy: declared User-Agent, at most 10 requests per second."
ITEMS = {"2.02", "7.01", "8.01"}
INDEX_ROW = re.compile(r'<tr[^>]*>\s*<td[^>]*>\s*(\d+)\s*</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>\s*'
                       r'<a href="([^"]+)"[^>]*>[^<]*</a>.*?</td>\s*<td[^>]*>(.*?)</td>', re.S)


class Edgar:
    """Paced GETs: one request per MIN_GAP, redirects followed, 429 and 5xx retried with doubling waits."""

    def __init__(self, user_agent: str, transport=None):
        import httpx
        self.httpx = httpx
        self.client = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": user_agent},
                                   transport=transport)
        self.last = 0.0

    def get(self, url: str) -> tuple[bytes, str]:
        for attempt in range(6):
            time.sleep(max(0.0, self.last + MIN_GAP - time.monotonic()))
            self.last = time.monotonic()
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                response = self.client.get(url)
            except self.httpx.TransportError:
                if attempt == 5:
                    raise
                time.sleep(2 ** attempt)
                continue
            if response.status_code in RETRY_STATUS and attempt < 5:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            if not response.content:
                raise ValueError(f"Empty response from {url}")
            return response.content, stamp
        raise ValueError(f"No response from {url}")


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def read_raw(entry: dict) -> bytes:
    content = (ROOT / entry["path"]).read_bytes()
    if byte_hash(content) != entry["sha256"] or len(content) != entry["bytes"]:
        raise ValueError(f"Raw file {entry['path']} failed its checksum")
    return content


class Store:
    """The raw store behind the manifest: a URL already held with a matching checksum is not fetched again."""

    def __init__(self, previous: dict | None, edgar: Edgar | None, refresh: bool):
        self.kept = {e["url"]: e for e in (previous or {}).get("entries", [])}
        self.edgar, self.refresh, self.entries, self.fetched = edgar, refresh, [], 0

    def get(self, url: str, target: Path, always: bool = False, **meta) -> bytes:
        held = self.kept.get(url)
        if held and not (self.refresh or always) and (ROOT / held["path"]).exists():
            content = read_raw(held)
            self.entries.append({**meta, **{k: held[k] for k in ("url", "retrieved_at", "sha256", "bytes", "path")}})
            return content
        content, stamp = self.edgar.get(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self.fetched += 1
        self.entries.append({**meta, "url": url, "retrieved_at": stamp, "sha256": byte_hash(content),
                             "bytes": len(content), "path": relative(target)})
        return content


def filing_rows(blob: dict) -> list[dict]:
    columns = blob["filings"]["recent"] if "filings" in blob else blob
    return [{k: columns[k][i] for k in ("accessionNumber", "filingDate", "reportDate", "form", "items")}
            for i in range(len(columns["accessionNumber"]))]


def candidates(pages: list[dict], since: str) -> list[dict]:
    rows = {r["accessionNumber"]: r for page in pages for r in filing_rows(page)}
    picked = [r for r in rows.values() if r["form"] in ("8-K", "8-K/A") and r["filingDate"] >= since
              and ITEMS & set(r["items"].split(","))]
    return sorted(picked, key=lambda r: (r["filingDate"], r["accessionNumber"]))


def filing_documents(index_page: bytes, listing: bytes, form: str) -> list[dict]:
    """The primary document and the EX-99.x HTML exhibits of one filing: typed by the EDGAR index page."""
    present = {item["name"] for item in json.loads(listing)["directory"]["item"]}
    documents = []
    for _, description, href, kind in INDEX_ROW.findall(index_page.decode("utf-8", "replace")):
        kind, name = re.sub(r"<[^>]+>|\s+", "", kind).upper(), href.rsplit("/", 1)[-1]
        if (kind == form or re.fullmatch(r"EX-99(\.\d+)?", kind)) and name.lower().endswith((".htm", ".html")):
            if name not in present or not re.fullmatch(r"[\w.-]+", name):
                raise ValueError(f"{name!r} is on the index page but is not a plain file name in index.json")
            documents.append({"kind": "primary" if kind == form else "exhibit", "exhibit": kind, "filename": name,
                              "description": re.sub(r"<[^>]+>|\s+", " ", description).strip()})
    if sum(d["kind"] == "primary" for d in documents) != 1:
        raise ValueError("Filing index does not show exactly one primary HTML document")
    return documents


def write_manifest(store: Store, since: str, failures: list[dict]) -> dict:
    manifest = {"area": "ual_filings", "cik": f"{CIK:010d}", "since": since, "raw_dir": relative(RAW),
                "raw_note": "Each raw file is the response body as received (after HTTP content decoding).",
                "terms_note": TERMS, "entries": store.entries, "failures": failures}
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def download(previous: dict | None, user_agent: str, since: str, refresh: bool) -> dict:
    """Fetch what the store does not hold. The manifest is saved as it grows, so a stopped run resumes."""
    store = Store(previous, Edgar(user_agent), refresh)
    first = json.loads(store.get(f"{DATA_API}CIK{CIK:010d}.json", RAW / "submissions" / f"CIK{CIK:010d}.json",
                                 always=True, kind="submissions"))
    if int(first["cik"]) != CIK:
        raise ValueError("Submissions file is not United's")
    pages = [first] + [json.loads(store.get(DATA_API + f["name"], RAW / "submissions" / f["name"], kind="submissions"))
                       for f in first["filings"].get("files", []) if f["filingTo"] >= since]
    failures = []
    for n, row in enumerate(candidates(pages, since), 1):
        accession = row["accessionNumber"]
        folder, base = RAW / accession, f"{ARCHIVE}/{accession.replace('-', '')}"
        meta = {"accession": accession, "filed_on": row["filingDate"], "report_date": row["reportDate"],
                "form": row["form"], "items": row["items"]}
        try:
            listing = store.get(f"{base}/index.json", folder / "index.json", kind="index", **meta)
            page = store.get(f"{base}/{accession}-index.html", folder / f"{accession}-index.html",
                             kind="filing_index", **meta)
            for document in filing_documents(page, listing, row["form"]):
                store.get(f"{base}/{document['filename']}", folder / document["filename"], **meta, **document)
        except Exception as exc:  # One filing failing must not lose the others; it is listed, never skipped silently.
            failures.append({"accession": accession, "error": f"{type(exc).__name__}: {exc}"[:300]})
        if n % 25 == 0:
            write_manifest(store, since, failures)
            print(f"  {n} filings listed, {store.fetched} requests", file=sys.stderr)
    return write_manifest(store, since, failures)


def build_index(manifest: dict) -> tuple[list[dict], dict]:
    """Index rows for the selected filings, and each document's bytes keyed by (accession, filename).

    Selected: every item 2.02 filing, and any 7.01/8.01 filing with an investor update, whether that is
    an EX-99 exhibit or guidance stated in the 8-K's own text. A selected filing lists all its EX-99
    HTML exhibits; its primary document is listed (exhibit "8-K") only when it states guidance itself
    or the filing has no such exhibit.
    """
    by_filing, content = {}, {}
    for entry in manifest["entries"]:
        if entry["kind"] not in ("exhibit", "primary"):
            continue
        body = read_raw(entry)
        content[(entry["accession"], entry["filename"])] = body
        kind = (filings.classify_exhibit(body, entry["items"]) if entry["kind"] == "exhibit"
                else filings.classify_primary(body))
        by_filing.setdefault(entry["accession"], []).append({**entry, "document": entry["kind"], "kind": kind})
    rows = []
    for accession, documents in by_filing.items():
        exhibits = [d for d in documents if d["document"] == "exhibit"]
        if "2.02" not in documents[0]["items"].split(",") and not any(d["kind"] == "investor_update" for d in documents):
            continue
        for d in documents:
            if d["document"] == "primary" and d["kind"] != "investor_update" and exhibits:
                continue
            body = content[(accession, d["filename"])]
            period = filings.release_period(body) if d["kind"] == "earnings_release" else None
            rows.append({"accession": accession, "filed_on": d["filed_on"], "period_end": period or "",
                         "form": d["form"], "items": d["items"], "exhibit": d["exhibit"], "filename": d["filename"],
                         "url": d["url"], "sha256": d["sha256"], "path": d["path"], "kind": d["kind"]})
    rows.sort(key=lambda r: (r["filed_on"], r["accession"], r["exhibit"] != r["form"], r["exhibit"], r["filename"]))
    return rows, content


def write_csv(path: Path, columns: tuple, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build_quarterly(index: list[dict], content: dict) -> tuple[list[dict], dict, list[str]]:
    """One row per fiscal quarter from the release filed first for it; anything set aside is reported."""
    rows, sources, notes = {}, {}, []
    for entry in index:
        if entry["kind"] != "earnings_release":
            continue
        parsed = filings.parse_release(content[(entry["accession"], entry["filename"])])
        period, name = parsed["period_end"], f"{entry['accession']} {entry['filename']}"
        if not period:
            notes.append(f"{name}: earnings release without a parsed period")
        elif period < filings.FIRST_QUARTER:
            notes.append(f"{name}: reports {period}, before the table starts")
        elif period in rows:
            notes.append(f"{name}: second release for {period}, not used")
        else:
            rows[period] = filings.quarterly_row(parsed, entry)
            sources[period] = {field: {"accession": entry["accession"], "filename": entry["filename"], **cell}
                               for field, cell in parsed["cells"].items()}
    return [rows[p] for p in sorted(rows)], {p: sources[p] for p in sorted(sources)}, notes


def xbrl_store() -> Path | None:
    history = ROOT / "data" / "history"
    latest = history / "latest.json"
    if latest.exists():
        store = history / json.loads(latest.read_text())["snapshot_id"] / "facts.parquet"
        if store.exists():
            return store
    stores = sorted(history.glob("*/facts.parquet"), key=lambda path: path.stat().st_mtime)
    return stores[-1] if stores else None


def xbrl_check(rows: list[dict], tolerance: float = 0.005) -> dict | None:
    """Release values against the local SEC XBRL store: as first filed, and as last restated.

    A quarter is the three-month fact, or the year-to-date fact less the previous year-to-date fact
    of the same vintage. Script-time only: the store is not in Git.
    """
    store = xbrl_store()
    if store is None:
        return None
    import duckdb
    facts = duckdb.connect().execute(
        "select variable, start, \"end\", value_musd from read_parquet(?) where entity = 'UAL' and start is not null "
        "and variable in ('operating_revenue', 'fuel_expense', 'net_income') order by published_on, fact_id",
        [str(store)]).fetchall()
    first, last = {}, {}
    for variable, start, end, value in facts:
        first.setdefault((variable, str(start), str(end)), value)
        last[(variable, str(start), str(end))] = value

    def quarter(book: dict, variable: str, end: str) -> float | None:
        year, month = int(end[:4]), int(end[5:7])
        direct = book.get((variable, f"{year}-{month - 2:02d}-01", end))
        if direct is not None or month == 3:
            return direct
        before = filings.month_end(year, month - 3)
        ytd, earlier = book.get((variable, f"{year}-01-01", end)), book.get((variable, f"{year}-01-01", before))
        return ytd - earlier if ytd is not None and earlier is not None else None

    result = {"store": relative(store), "tolerance": tolerance}
    pairs = (("total_revenue_musd", "operating_revenue"), ("fuel_expense_musd", "fuel_expense"),
             ("net_income_musd", "net_income"))
    for vintage, book in (("first_filed", first), ("last_restated", last)):
        compared, exact, missing, differences = {f: 0 for f, _ in pairs}, 0, [], []
        for row in rows:
            for field, variable in pairs:
                if row[field] == "":
                    continue
                value = quarter(book, variable, row["period_end"])
                if value is None:
                    missing.append(f"{row['period_end']} {variable}")
                    continue
                compared[field] += 1
                exact += float(row[field]) == round(value, 6)
                gap = float(row[field]) / value - 1 if value else float("inf")
                if abs(gap) > tolerance:
                    differences.append({"period_end": row["period_end"], "field": field, "release": float(row[field]),
                                        "xbrl": round(value, 3), "difference": round(gap, 4)})
        result[vintage] = {"compared": compared, "equal": exact, "not_in_store": missing,
                           "differences": sorted(differences, key=lambda d: -abs(d["difference"]))}
    return result


def anchor_check(rows: list[dict]) -> dict:
    anchor = json.loads(ANCHOR.read_text())
    row = next((r for r in rows if r["period_end"] == anchor["period_end"]), None)
    pairs = {"asm_m": "asm_million", "fuel_gallons_m": "fuel_gallons_million",
             "fuel_price_usd_gal": "average_paid_fuel_usd_per_gallon", "trasm_cents": "trasm_cents_per_asm",
             "prasm_cents": "prasm_cents_per_asm", "casm_cents": "casm_cents_per_asm"}
    result = {field: {"release": row and row[field], "anchor": anchor[key],
                      "match": bool(row) and row[field] != "" and float(row[field]) == float(anchor[key])}
              for field, key in pairs.items()}
    return {"period_end": anchor["period_end"], "all_match": all(v["match"] for v in result.values()), "fields": result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="Rebuild from the raw store and manifest, no network")
    parser.add_argument("--refresh", action="store_true", help="Fetch again even where the raw file is already held")
    parser.add_argument("--since", default="2013-01-01")
    parser.add_argument("--user-agent", default=os.environ.get("SEC_USER_AGENT"),
                        help="Declared SEC User-Agent with a contact address (default: $SEC_USER_AGENT)")
    args = parser.parse_args()
    previous = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else None
    if args.offline:
        if previous is None:
            raise SystemExit("No manifest to rebuild from; run once without --offline")
        manifest = previous
    else:
        if not args.user_agent or "\n" in args.user_agent or "\r" in args.user_agent:
            raise SystemExit("SEC requires a declared User-Agent: set SEC_USER_AGENT or pass --user-agent")
        manifest = download(previous, args.user_agent, args.since, args.refresh)
    index, content = build_index(manifest)
    write_csv(filings.INDEX_CSV, filings.INDEX_COLUMNS, index)
    rows, sources, notes = build_quarterly(index, content)
    write_csv(filings.QUARTERLY_CSV, filings.QUARTERLY_COLUMNS, rows)
    filings.SOURCES_JSON.write_text(json.dumps(sources, indent=1) + "\n")
    by_kind, updates = {}, {}
    for row in index:
        year = row["filed_on"][:4]
        by_kind.setdefault(row["kind"], {}).setdefault(year, 0)
        by_kind[row["kind"]][year] += 1
        if row["kind"] == "investor_update":
            where = "8-K text" if row["exhibit"] == row["form"] else "exhibit"
            updates.setdefault(year, {"exhibit": 0, "8-K text": 0})[where] += 1
    kinds = [e["kind"] for e in manifest["entries"]]
    expected = filings.expected_quarters(filings.FIRST_QUARTER, rows[-1]["period_end"]) if rows else []
    blank = {field: [r["period_end"] for r in rows if r[field] == ""] for field in filings.VALUE_COLUMNS}
    print(json.dumps({
        "raw_files": len(kinds), "filings_listed": kinds.count("index"), "exhibits_downloaded": kinds.count("exhibit"),
        "primary_documents_downloaded": kinds.count("primary"), "failures": manifest.get("failures", []),
        "filings_selected": len({r["accession"] for r in index}), "documents_indexed": len(index),
        "filed_from": index[0]["filed_on"], "filed_to": index[-1]["filed_on"],
        "documents_by_kind_and_year": by_kind, "investor_updates_by_year": updates,
        "quarters": len(rows), "missing_quarters": sorted(set(expected) - {r["period_end"] for r in rows}),
        "coverage": filings.coverage(rows), "blank_cells": {f: p for f, p in blank.items() if p}, "notes": notes,
        "check_a_arithmetic": filings.arithmetic_report(rows),
        "check_b_anchor": anchor_check(rows),
        "check_c_xbrl": xbrl_check(rows),
    }, indent=2))


if __name__ == "__main__":
    main()
