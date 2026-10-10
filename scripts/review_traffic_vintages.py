"""Preserve the unresolved AA Q1 2021 source/issuer discrepancy.

This is a present-day source review, not a historical exclusion or a monthly
allocation of issuer quarterly figures. No source observations are overwritten.
"""
from decimal import Decimal
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import sys

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.evidence import digest
from app.source_store import read_source

ISSUER_SHA = "091830bdcd3b4449a36b74333825e46864e78540944fa775419895555daf4fbb"
ISSUER_URL = "https://s202.q4cdn.com/986123435/files/doc_news/2021/04/1/1Q21-Financials.pdf"
PUBLICATION_SOURCE = "https://news.aa.com/news/news-details/2021/American-Airlines-Reports-First-Quarter-2021-Financial-Results-CORP-FI-04/"


def main():
    root = ROOT / "data/traffic_vintages"
    identity = json.loads((root / "latest.json").read_text())["snapshot_id"]
    target = root / identity
    manifest = json.loads((target / "manifest.json").read_text())
    artifact = json.loads((target / "artifact.json").read_text())
    if digest(artifact) != manifest["artifact_digest"] or digest({k:v for k,v in manifest.items() if k != "snapshot_id"}) != identity:
        raise ValueError("Review input snapshot differs")
    content, metadata = read_source(ISSUER_SHA)
    if metadata["source_url"] != ISSUER_URL: raise ValueError("Unexpected issuer source")
    text = PdfReader(BytesIO(content)).pages[1].extract_text()
    reported = {}
    for key, pattern in (("rpm_million",r"Revenue passenger miles \(millions\)\s+([\d,]+)"),
                         ("asm_million",r"Available seat miles \(ASM\) \(millions\)\s+([\d,]+)")):
        matches = re.findall(pattern,text)
        if len(matches) != 1: raise ValueError("Issuer operating table extraction differs")
        reported[key] = Decimal(matches[0].replace(",",""))
    rows = [r for r in artifact["evaluation_targets"]["observations"] if r["carrier"] == "AA" and "2021-01" <= r["period"] <= "2021-03"]
    if len(rows) != 12 or {(r["period"],r["market"]) for r in rows} != {(p,m) for p in ("2021-01","2021-02","2021-03") for m in ("domestic","atlantic","pacific","latin_america")}:
        raise ValueError("AA quarter lacks complete declared source targets")
    totals = {key:sum((Decimal(str(r[key])) for r in rows),Decimal(0)) for key in reported}
    audit_root = ROOT / "data/traffic_vintage_checks"
    audits = [json.loads(p.read_text()) for p in audit_root.glob("*.json")]
    matches = [a for a in audits if a["snapshot_id"] == identity and a["artifact_digest"] == manifest["artifact_digest"]]
    if len(matches) != 1: raise ValueError("Review requires one exact original-source audit")
    audit = matches[0]
    record = {"version":"traffic-source-review-0.1.0", "reviewed_on":"2026-10-09", "status":"unresolved",
        "vintage_snapshot_id":identity,"vintage_artifact_digest":manifest["artifact_digest"],"audit_digest":digest(audit),
        "issuer_source":{**metadata,"published_on":"2021-04-22","publication_source_url":PUBLICATION_SOURCE,"page":2},
        "quarter":"2021-Q1","carrier":"AA", "units":"million passenger miles / million seat miles",
        "reported_consolidated":{k:float(v) for k,v in reported.items()},
        "certificate_dd_fd":{k:float(v) for k,v in totals.items()},
        "difference_certificate_minus_consolidated":{k:float(totals[k]-reported[k]) for k in reported},
        "source_targets":rows,"revision_events":audit["revision_events"],
        "interpretation":"Certificate DD/FD RPM exceeds the issuer's broader consolidated reported RPM. This is an unresolved source/measurement/reporting-boundary discrepancy; it is not proof of a specific erroneous row or permission to replace it.",
        "estimation_gate":"Do not treat this panel or the earlier annual-only scorecard as accepted economic validation until this discrepancy is reconciled. Preserve all quantities; do not impute monthly values from quarterly totals or use future review findings as an undisclosed historical exclusion.",
        "next_checks":["Trace AA January 2021 domestic additions by entity/route/aircraft across releases.","Compare same-scope T1/T2 and issuer mainline evidence, retaining regional and distance definitions.","Document any verified correction and a release-aware treatment before refitting every model on identical targets."],
        "review_code_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    folder = ROOT / "data/traffic_vintage_reviews";folder.mkdir(exist_ok=True)
    path = folder / (digest(record)+".json");value=json.dumps(record,indent=2,allow_nan=False)+"\n"
    if path.exists() and path.read_text()!=value:raise ValueError("Immutable source review differs")
    if not path.exists():path.write_text(value)
    print(json.dumps({"review_file":str(path),"status":record["status"],"reported_consolidated":record["reported_consolidated"],
        "certificate_dd_fd":record["certificate_dd_fd"],"difference":record["difference_certificate_minus_consolidated"]},indent=2))


if __name__ == "__main__":main()
