"""Download TSA's daily checkpoint traveler counts from tsa.gov, or rebuild offline.

TSA lists the running year at https://www.tsa.gov/travel/passenger-volumes and
each closed year at .../passenger-volumes/<year>, back to 2019. Every page is
kept as received under data/lab_raw/tsa/<sha256>.html and indexed in
data/lab/manifests/tsa.json. `--offline` rebuilds data/lab/tsa_daily.csv and
app/static/data/demand_signal.json from that store through the manifest, without
network. A day TSA does not list stays absent: nothing is filled or estimated.

Access: one request per second through curl with the project's declared
User-Agent. On 2026-10-09 tsa.gov's edge (AkamaiGHost) answered 403 "Access
Denied" to Python's httpx, with the declared agent and with a browser agent, and
200 to curl with the declared agent. So curl is used and no browser is imitated.
A refusal is recorded under failures with the status and the start of the body.

Revision rule: see REVISION_RULE below; it is copied into the manifest.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab import tsa
from app.lab.tsa import DEMAND_JSON, TSA_CSV, UAL_CSV
from app.source_store import byte_hash
from scripts.lab.fetch_market import read_raw, relative, store_raw, write_csv

BASE = "https://www.tsa.gov/travel/passenger-volumes"
FIRST_YEAR = 2019  # .../passenger-volumes/2018 answered 404 on 2026-10-09.
RAW = ROOT / "data" / "lab_raw" / "tsa"
MANIFEST = ROOT / "data" / "lab" / "manifests" / "tsa.json"
AGENT = "UAL Counterfactual Lab research"
PAUSE = 1.0  # Seconds between requests.
TERMS = ("Transportation Security Administration, U.S. Department of Homeland Security. US government data in the "
         "public domain; cite TSA as the source.")
ACCESS = ("curl with the declared User-Agent '" + AGENT + "', one request per second. On 2026-10-09 the same request "
          "from Python httpx was answered 403 Access Denied by the site's edge (AkamaiGHost), with the declared "
          "agent and with a browser agent; curl with the declared agent was answered 200. No browser is imitated.")
REVISION_RULE = (
    "1. Within one page, a date listed twice with the same number is kept once; listed twice with different numbers, "
    "the page is refused. "
    "2. When a date is listed on more than one stored page (the running-year page, a per-year page, or an earlier "
    "stored copy of either), the number from the page with the latest last_seen_at is used. "
    "3. In one run the running-year page is requested first and the per-year pages after it, so a per-year page "
    "outranks the running-year page of the same run; an exact tie in time also goes to the per-year page. "
    "4. Every date on which two stored pages disagree is listed under revisions with both numbers. No number is "
    "averaged, and a date no stored page lists is left out.")
NOT_YET = ("404 for the running year's own page. TSA lists the running year on the base URL; the per-year page is "
           "requested on every run so that it is picked up once TSA adds it.")
DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
COUNT = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")
RETRY_STATUS = {429, 500, 502, 503, 504}


class _Page(HTMLParser):
    """Collects every table as rows of cell text, the <title>, and paragraphs inside <article> outside tables."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.notes, self.title = [], [], ""
        self._table = self._row = self._cell = self._note = None
        self._article, self._in_title = 0, False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self._table is not None:
                raise ValueError("TSA page nests one table inside another")
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "article":
            self._article += 1
        elif tag == "p" and self._article and self._table is None:
            self._note = []
        elif tag == "title" and not self.title:  # The document title; later <title> elements label SVG icons.
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None
        elif tag == "article":
            self._article = max(0, self._article - 1)
        elif tag == "p" and self._note is not None:
            text = " ".join("".join(self._note).split())
            if text:
                self.notes.append(text)
            self._note = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        elif self._note is not None:
            self._note.append(data)
        elif self._in_title:
            self.title += data


def parse_page(content: bytes, year: int | None = None) -> tuple[list[tuple[date, int]], dict]:
    """One TSA passenger-volumes page -> ascending (date, travelers), and what the page says about itself.

    The page must hold exactly one table headed Date, Numbers, with two cells per
    row: a M/D/YYYY date and a whole number. Any other layout is refused rather
    than guessed at. `year` is the year of a per-year page; a date outside it is
    refused. A date listed twice with one number is kept once; listed twice with
    two numbers, the page is refused.
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("TSA page is not UTF-8") from exc
    page = _Page()
    page.feed(text)
    page.close()
    tables = [t for t in page.tables if t and [cell.lower() for cell in t[0]] == ["date", "numbers"]]
    if len(tables) != 1:
        raise ValueError(f"TSA page has {len(tables)} tables headed Date, Numbers; expected exactly one")
    rows, listed = {}, []
    for cells in tables[0][1:]:
        if len(cells) != 2 or not DATE.fullmatch(cells[0]) or not COUNT.fullmatch(cells[1]):
            raise ValueError(f"TSA table row is not a date and a whole number: {cells!r}")
        month, day, listed_year = (int(part) for part in DATE.fullmatch(cells[0]).groups())
        try:
            when = date(listed_year, month, day)
        except ValueError as exc:
            raise ValueError(f"TSA table has an impossible date: {cells[0]!r}") from exc
        count = int(cells[1].replace(",", ""))
        if count <= 0:
            raise ValueError(f"TSA table has a non-positive count on {when}")
        if year is not None and when.year != year:
            raise ValueError(f"The {year} page lists {when}")
        if rows.setdefault(when, count) != count:
            raise ValueError(f"TSA page lists {when} twice with different numbers")
        listed.append(when)
    if not rows:
        raise ValueError("TSA table has no data rows")
    order = ("ascending" if listed == sorted(listed) else
             "descending" if listed == sorted(listed, reverse=True) else "mixed")
    ordered = sorted(rows.items())
    return ordered, {"rows": len(ordered), "first": ordered[0][0].isoformat(), "last": ordered[-1][0].isoformat(),
                     "listed_order": order, "page_title": " ".join(page.title.split()), "page_notes": page.notes}


def curl_get(url: str, timeout: int = 60) -> tuple[int, bytes]:
    """One HTTPS GET with the declared User-Agent. Returns (HTTP status, body); a refusal is returned, not raised."""
    curl = shutil.which("curl")
    if not curl:
        raise ValueError("Install curl with normal certificate trust to retrieve the TSA pages")
    with tempfile.TemporaryDirectory(prefix="ual-tsa-") as temporary:
        target = Path(temporary) / "body"
        try:
            done = subprocess.run([curl, "--silent", "--show-error", "--location", "--proto", "=https",
                                   "--proto-redir", "=https", "--max-time", str(timeout), "--user-agent", AGENT,
                                   "--output", str(target), "--write-out", "%{http_code}", url],
                                  capture_output=True, timeout=timeout + 10)
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"curl timed out after {timeout + 10} seconds") from exc
        if done.returncode != 0:
            raise ValueError(f"curl exit {done.returncode}: {done.stderr.decode(errors='replace')[:300]}")
        return int(done.stdout.decode().strip()), target.read_bytes() if target.exists() else b""


def page_sources(today: date) -> list[dict]:
    """The running-year page first, then one page per year from the current year back to 2019."""
    return ([{"source": "tsa:current", "url": BASE, "year": None}]
            + [{"source": f"tsa:{year}", "url": f"{BASE}/{year}", "year": year}
               for year in range(today.year, FIRST_YEAR - 1, -1)])


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_with_retry(get, url: str, pause: float, sleep) -> tuple[int, bytes]:
    """Retry a transport error or a 429/5xx twice, waiting longer each time. A 403 or 404 is not retried."""
    for attempt in range(3):
        try:
            status, content = get(url)
        except ValueError:
            if attempt == 2:
                raise
        else:
            if status not in RETRY_STATUS or attempt == 2:
                return status, content
        sleep(2 * pause * (attempt + 1))
    raise AssertionError("unreachable")


def download(previous: dict | None = None, get=curl_get, raw: Path = RAW, pause: float = PAUSE,
             today: date | None = None, sleep=time.sleep) -> dict:
    """Fetch every page, one request per `pause` seconds.

    Entries only accumulate: a page whose bytes are already stored keeps its entry
    and gets a new last_seen_at; new bytes add an entry. A page that is refused or
    does not parse is listed under failures with what was returned, and the
    entries already stored for it stay.
    """
    entries = [dict(entry) for entry in (previous or {}).get("entries", [])]
    failures, not_published = [], []
    today = today or datetime.now(timezone.utc).date()
    for number, page in enumerate(page_sources(today)):
        if number:
            sleep(pause)
        stamp, stored, returned = _utc_now(), None, {}
        try:
            status, content = _get_with_retry(get, page["url"], pause, sleep)
            returned = {"http_status": status, "returned_bytes": len(content),
                        "returned_sha256": byte_hash(content) if content else None,
                        "returned_excerpt": " ".join(content.decode("utf-8", "replace").split())[:400]}
            if status == 404 and page["year"] == today.year:
                not_published.append({"source": page["source"], "url": page["url"], "attempted_at": stamp,
                                      "http_status": status, "note": NOT_YET})
                continue
            if status != 200:
                raise ValueError(f"HTTP {status}")
            stored = relative(store_raw(content, "html", raw))
            rows, info = parse_page(content, page["year"])
            if rows[-1][0] > today:
                raise ValueError(f"Page lists {rows[-1][0]}, which is after the retrieval date {today}")
            sha = byte_hash(content)
            same = next((e for e in entries if e["source"] == page["source"] and e["sha256"] == sha), None)
            if same:
                same["last_seen_at"] = stamp
            else:
                entries.append({**page, "terms_note": TERMS, "retrieved_at": stamp, "last_seen_at": stamp,
                                "http_status": status, "sha256": sha, "bytes": len(content), "path": stored,
                                "rows": info["rows"], "first": info["first"], "last": info["last"],
                                "listed_order": info["listed_order"], "page_title": info["page_title"],
                                "page_notes": info["page_notes"]})
        except Exception as exc:
            failures.append({"source": page["source"], "url": page["url"], "attempted_at": stamp,
                             "error": f"{type(exc).__name__}: {exc}"[:400], **returned, "raw_kept_at": stored,
                             "kept_previous_entry": any(e["source"] == page["source"] for e in entries)})
    return {"area": "tsa", "builder": "scripts/lab/fetch_tsa.py", "raw_dir": relative(raw),
            "raw_note": "Each raw file is the response body as received, named by its SHA-256. An entry is one "
                        "distinct body of one page: retrieved_at is when those bytes were first received, "
                        "last_seen_at the latest run that received the same bytes again.",
            "access_note": ACCESS, "revision_rule": REVISION_RULE,
            "entries": entries, "not_published": not_published, "failures": failures}


def merge_pages(pages: list[tuple[dict, list[tuple[date, int]]]]) -> tuple[list[tuple[date, int]], list[dict], dict]:
    """Apply REVISION_RULE to (manifest entry, parsed rows) pairs.

    Returns the ascending rows, every date on which two pages disagree, and the
    sha256 of the page each date was taken from.
    """
    ranked = sorted(pages, key=lambda p: (datetime.fromisoformat(p[0]["last_seen_at"]), p[0].get("year") is not None))
    seen: dict[date, list[tuple[int, dict]]] = {}
    for entry, rows in ranked:
        for when, count in rows:
            seen.setdefault(when, []).append((count, entry))

    def tag(entry: dict) -> dict:
        return {"source": entry["source"], "sha256": entry["sha256"], "last_seen_at": entry["last_seen_at"]}

    merged, revisions, origin = [], [], {}
    for when in sorted(seen):
        count, entry = seen[when][-1]
        merged.append((when, count))
        origin[when] = entry["sha256"]
        others = [(c, e) for c, e in seen[when][:-1] if c != count]
        if others:
            revisions.append({"date": when.isoformat(), "travelers": count, "taken_from": tag(entry),
                              "not_used": [{"travelers": c, **tag(e)} for c, e in others]})
    return merged, revisions, origin


def build(manifest: dict, csv_path: Path = TSA_CSV, json_path: Path = DEMAND_JSON, ual_csv: Path = UAL_CSV,
          now: datetime | None = None) -> dict:
    """Write tsa_daily.csv and demand_signal.json from the stored pages. No network."""
    parsed = [(entry, *parse_page(read_raw(entry), entry.get("year"))) for entry in manifest.get("entries", [])]
    if not parsed:
        return {"outputs": [], "coverage": None, "revisions": [],
                "not_built": [{"path": relative(Path(p)), "missing": ["no TSA page is stored"]}
                              for p in (csv_path, json_path)]}
    rows, revisions, origin = merge_pages([(entry, page_rows) for entry, page_rows, _ in parsed])
    csv_output = write_csv(Path(csv_path), ("date", "travelers"), [(d.isoformat(), str(v)) for d, v in rows])
    used = [(entry, info) for entry, _, info in parsed if entry["sha256"] in set(origin.values())]
    notes = list(dict.fromkeys(note for _, info in used for note in info["page_notes"]))
    payload = tsa.demand_signal(
        csv_path, ual_csv, now=now, page_notes=notes,
        raw_inputs=[{"path": e["path"], "sha256": e["sha256"], "url": e["url"], "retrieved_at": e["retrieved_at"]}
                    for e, _ in used])
    tsa.write_demand_signal(payload, json_path)
    return {"outputs": [csv_output,
                        {"path": relative(Path(json_path)), "as_of": payload["as_of"],
                         "versus_united": "computed" if payload["versus_united"] else payload["versus_united_reason"],
                         "note": "Carries generated_at, so its hash changes on every rebuild and is not recorded."}],
            "coverage": payload["coverage"], "revisions": revisions, "not_built": []}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true",
                        help="Rebuild the CSV and demand_signal.json from the raw store through the manifest; no network")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args()
    previous = json.loads(args.manifest.read_text()) if args.manifest.exists() else None
    if args.offline:
        if previous is None:
            raise SystemExit(f"No manifest at {args.manifest}; run once with network first")
        manifest = previous
    else:
        manifest = download(previous)
    result = build(manifest)
    if not args.offline:
        manifest.update(result)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    print(json.dumps({**result, "not_published": manifest.get("not_published", []),
                      "failures": manifest.get("failures", [])}, indent=2))
    if manifest.get("failures") or result["not_built"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
