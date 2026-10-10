"""Download Delta's earnings-release 8-K exhibits from SEC EDGAR, or rebuild offline.

Every 8-K with item 2.02 filed on or after --since is listed through the filing's
index.json, typed through its index-headers page, and each EX-99.x HTML exhibit is
kept byte-exact under data/lab_raw/sec/<accession>/<filename> and indexed in
data/lab/manifests/dal_filings.json. `--offline` rebuilds data/lab/dal_check.json
and data/lab/dal_quarterly.csv from that store through the manifest, no network.

SEC asks for a declared User-Agent. It is read from SEC_USER_AGENT or --user-agent
and is never written to the manifest, the raw store or the outputs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab import delta
from app.source_store import byte_hash

CIK = 27904
RAW = ROOT / "data" / "lab_raw" / "sec"
MANIFEST = ROOT / "data" / "lab" / "manifests" / "dal_filings.json"
SUBMISSIONS = f"https://data.sec.gov/submissions/CIK{CIK:010d}.json"
ARCHIVE = f"https://www.sec.gov/Archives/edgar/data/{CIK}"
MIN_GAP = 0.3  # Seconds between requests: under SEC's ceiling and this project's 4 per second.
RETRY_STATUS = {429, 500, 502, 503, 504}
TERMS = "SEC EDGAR public filings. Fair-access policy: a declared requester identity, at most 10 requests per second."
HEADER_DOC = re.compile(r"<TYPE>([^\n<]+)\n<SEQUENCE>(\d+)\n<FILENAME>([^\n<]+)(?:\n<DESCRIPTION>([^\n<]+))?")


class Edgar:
    def __init__(self, user_agent: str):
        import httpx
        self.httpx = httpx
        self.client = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": user_agent})
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
        raise RuntimeError("unreachable")


def earnings_filings(submissions: dict, since: str, until: str | None = None) -> list[dict]:
    """8-K and 8-K/A filings carrying item 2.02 in the submissions file, newest first."""
    if int(submissions["cik"]) != CIK:
        raise ValueError("Submissions file is not Delta's")
    recent = submissions["filings"]["recent"]
    rows = [dict(zip(recent, values)) for values in zip(*recent.values())]
    picked = [{"accession": r["accessionNumber"], "filed_on": r["filingDate"], "report_date": r["reportDate"],
               "form": r["form"], "items": r["items"], "primary_document": r["primaryDocument"]}
              for r in rows
              if r["form"] in ("8-K", "8-K/A") and "2.02" in r["items"].split(",")
              and r["filingDate"] >= since and (until is None or r["filingDate"] <= until)]
    if rows[-1]["filingDate"] > since:
        raise ValueError("--since is older than the recent page of the submissions file")
    return picked


def header_documents(page: bytes) -> list[dict]:
    """Document type, sequence, filename and description from a filing's index-headers page."""
    text = page.decode("utf-8", "replace").replace("&lt;", "<").replace("&gt;", ">").replace("\r", "")
    return [{"type": t.strip(), "sequence": int(s), "filename": f.strip(), "description": (d or "").strip()}
            for t, s, f, d in HEADER_DOC.findall(text)]


def exhibits(index: dict, documents: list[dict]) -> list[dict]:
    """EX-99.x HTML exhibits that the directory listing and the header page both name."""
    listed = {item["name"] for item in index["directory"]["item"]}
    picked = [d for d in documents if re.fullmatch(r"EX-99(\.\d+)?", d["type"])
              and d["filename"].lower().endswith((".htm", ".html"))]
    missing = [d["filename"] for d in picked if d["filename"] not in listed]
    if missing:
        raise ValueError(f"Exhibit named in the header but absent from index.json: {missing}")
    return picked


def keep(content: bytes, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def entry_ok(entry: dict | None) -> bool:
    if not entry:
        return False
    path = ROOT / entry["path"]
    return path.exists() and byte_hash(path.read_bytes()) == entry["sha256"]


def download(edgar: Edgar, since: str, until: str | None, previous: dict | None, refresh: bool = False) -> dict:
    """Fetch what the manifest does not already hold intact. A failed filing is listed, not dropped silently."""
    kept = {e["url"]: e for e in (previous or {}).get("entries", [])}
    entries, failures = [], []

    def fetch(url: str, target: Path, **fields) -> tuple[dict, bytes]:
        if not refresh and entry_ok(kept.get(url)):
            entry = {**kept[url], **fields}
            return entry, (ROOT / entry["path"]).read_bytes()
        content, stamp = edgar.get(url)
        keep(content, target)
        return {**fields, "url": url, "retrieved_at": stamp, "sha256": byte_hash(content), "bytes": len(content),
                "path": target.relative_to(ROOT).as_posix()}, content

    content, stamp = edgar.get(SUBMISSIONS)  # Always refreshed: it is the list of what exists.
    target = RAW / "submissions" / f"CIK{CIK:010d}.json"
    keep(content, target)
    entries.append({"kind": "submissions", "url": SUBMISSIONS, "retrieved_at": stamp, "sha256": byte_hash(content),
                    "bytes": len(content), "path": target.relative_to(ROOT).as_posix()})
    filings = earnings_filings(json.loads(content), since, until)
    for filing in filings:
        folder = f"{ARCHIVE}/{filing['accession'].replace('-', '')}"
        base = {k: filing[k] for k in ("accession", "filed_on", "report_date", "form", "items")}
        try:
            index_entry, index = fetch(f"{folder}/index.json", RAW / filing["accession"] / "index.json",
                                       kind="index", **base)
            name = f"{filing['accession']}-index-headers.html"
            header_entry, header = fetch(f"{folder}/{name}", RAW / filing["accession"] / name, kind="headers", **base)
            found = exhibits(json.loads(index), header_documents(header))
            if not found:
                raise ValueError("No EX-99 HTML exhibit in this filing")
            fetched = [fetch(f"{folder}/{doc['filename']}", RAW / filing["accession"] / doc["filename"],
                             kind="exhibit", **base, exhibit=doc["type"], filename=doc["filename"],
                             description=doc["description"])[0] for doc in found]
            entries += [index_entry, header_entry, *fetched]
        except Exception as exc:
            failures.append({"accession": filing["accession"], "filed_on": filing["filed_on"],
                             "error": f"{type(exc).__name__}: {exc}"[:400]})
            entries += [e for e in kept.values() if e.get("accession") == filing["accession"] and entry_ok(e)]
    done = {e["url"] for e in entries}
    entries += [e for url, e in kept.items() if url not in done and entry_ok(e)]  # Outside this run's window.
    entries.sort(key=lambda e: (e.get("filed_on", "9999"), e.get("accession", ""), e["kind"], e["url"]), reverse=True)
    return {"area": "dal_filings", "cik": f"{CIK:010d}", "raw_dir": RAW.relative_to(ROOT).as_posix(),
            "raw_note": "Each raw file is the response body as received (after HTTP content decoding).",
            "terms_note": TERMS, "entries": entries, "failures": failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true",
                        help="Rebuild the tidy outputs from the raw store through the manifest; no network")
    parser.add_argument("--since", default="2018-01-01", help="Earliest filing date to download")
    parser.add_argument("--until", help="Latest filing date to download")
    parser.add_argument("--refresh", action="store_true", help="Download again even where the raw file is intact")
    parser.add_argument("--user-agent", default=os.environ.get("SEC_USER_AGENT"),
                        help="Declared User-Agent for SEC; defaults to the SEC_USER_AGENT environment variable")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args()
    previous = json.loads(args.manifest.read_text()) if args.manifest.exists() else None
    if args.offline:
        if previous is None:
            raise SystemExit(f"No manifest at {args.manifest}; run once with network first")
        manifest = previous
    else:
        if not (args.user_agent or "").strip():
            raise SystemExit("SEC requires a declared User-Agent: set SEC_USER_AGENT or pass --user-agent. "
                             "It is used for the requests only and is not stored.")
        manifest = download(Edgar(args.user_agent.strip()), args.since, args.until, previous, args.refresh)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    result = delta.build(manifest)
    print(json.dumps({**result, "failures": manifest.get("failures", [])}, indent=2))
    if manifest.get("failures") or result.get("not_built"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
