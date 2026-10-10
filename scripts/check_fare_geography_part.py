"""Bind one independently audited fare quarter to independently audited endpoints."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from app.evidence import digest
from app.fare_scope import coverage
from app.source_store import read_source


def checked_report(path, code_folder, fields):
    value = json.loads(path.read_text())
    if digest(value) != path.stem:
        raise ValueError("Source audit report identity differs")
    for field in fields:
        sha = value[field]; code = code_folder / (sha + ".py")
        if hashlib.sha256(code.read_bytes()).hexdigest() != sha:
            raise ValueError("Source auditor code binding differs")
    return value


def checked_part(kind, audit):
    base = ROOT / "data" / kind
    design_id = audit["design_id"]; design = json.loads((base / "designs" / design_id / "design.json").read_text())
    if digest(design) != design_id:
        raise ValueError("Source design binding differs")
    sources = design.get("sources", design["specification"]["sources"])
    source = next(s for s in sources if s["quarter"] == audit["quarter"])
    _, meta = read_source(source["sha256"])
    if source["sha256"] != audit["source_sha256"] or source["url"] != meta["source_url"] or source["bytes"] != meta["bytes"]:
        raise ValueError("Raw source binding differs")
    folder = base / "parts" / digest({"design_id": design_id, "source_sha256": source["sha256"]})
    value = json.loads((folder / "part.json").read_text())
    if digest(value) != audit["part_digest"]:
        raise ValueError("Audited source part binding differs")
    return value, source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fare-audit", type=Path, required=True)
    parser.add_argument("--geography-audit", type=Path, required=True)
    args = parser.parse_args()
    code = Path(__file__).read_bytes(); helper = (ROOT / "app/fare_scope.py").read_bytes()
    fare_audit = checked_report(args.fare_audit, ROOT / "data/fare_history_checks/code", ("audit_code_sha256", "numerical_helper_sha256"))
    geo_audit = checked_report(args.geography_audit, ROOT / "data/fare_geography_checks/code", ("audit_code_sha256",))
    if any(fare_audit[k] != geo_audit[k] for k in ("quarter", "source_sha256")):
        raise ValueError("Fare and endpoint audits cover different original quarters")
    fare, source = checked_part("fare_history", fare_audit); geo, _ = checked_part("fare_geography", geo_audit)
    if fare["records"] != geo["records"] or fare["source_member"] != geo["source_member"]:
        raise ValueError("Original fare and endpoint populations differ")
    report = {"version": "audited-fare-endpoint-coverage-0.1.0", "quarter": fare_audit["quarter"],
        "fare_audit_id": args.fare_audit.stem, "geography_audit_id": args.geography_audit.stem,
        "source_sha256": source["sha256"], "published_on": source["published_on"],
        "inspection_code_sha256": hashlib.sha256(code).hexdigest(), "scope_code_sha256": hashlib.sha256(helper).hexdigest(),
        "knowledge_label": "Current-download reconstruction; filename publication proxy, not historically captured bytes.",
        **coverage(fare["rows"], geo["rows"])}
    if code != Path(__file__).read_bytes() or helper != (ROOT / "app/fare_scope.py").read_bytes():
        raise ValueError("Geographic inspection code changed during execution")
    identity = digest(report); base = ROOT / "data/fare_scope_checks"; base.mkdir(parents=True, exist_ok=True)
    folder = base / "code"; folder.mkdir(exist_ok=True)
    for sha, content in ((report["inspection_code_sha256"], code), (report["scope_code_sha256"], helper)):
        path = folder / (sha + ".py")
        if path.exists() and path.read_bytes() != content:
            raise ValueError("Immutable geographic inspection code differs")
        if not path.exists():
            path.write_bytes(content)
    path = base / (identity + ".json"); text = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if path.exists() and path.read_text() != text:
        raise ValueError("Immutable fare geographic report differs")
    if not path.exists():
        path.write_text(text)
    print(json.dumps({"inspection_id": identity, **report}, indent=2))


if __name__ == "__main__":
    main()
