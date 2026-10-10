"""Reviewed, dated evidence. Monthly run rates are derived, never historic observations."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Literal

import duckdb

ROOT = Path(__file__).resolve().parents[1]
ANCHORS = ROOT / "data" / "monthly_anchors.json"
WAREHOUSE = ROOT / "data" / "warehouse"
UNITS = {"USD_million", "ASM_million", "RPM_million", "gallons_million", "aircraft", "USD_per_gallon", "fraction"}


def digest(value: object) -> str:
    # Browser JSON treats 1.0 and 1 as the same Number. Preserve numeric value
    # while making Python/server/browser run manifests hash identically.
    def canonical(item):
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, dict):
            return {key: canonical(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [canonical(val) for val in item]
        return item

    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class EvidenceRecord:
    record_id: str
    entity: str
    variable: str
    value: float
    units: str
    effective_start: str
    effective_end: str
    published_on: str
    source_id: str
    source_passage: str
    evidence_type: Literal["reported", "guidance", "assumption"]
    review_status: Literal["verified", "pending", "rejected"]

    def validate(self) -> None:
        if not self.record_id or not self.variable or not self.entity:
            raise ValueError("Evidence requires an ID, variable and entity")
        if self.units not in UNITS or not math.isfinite(self.value):
            raise ValueError("Evidence has unsupported units or a nonfinite value")
        start, end = date.fromisoformat(self.effective_start), date.fromisoformat(self.effective_end)
        date.fromisoformat(self.published_on)
        if end < start:
            raise ValueError("Effective period ends before it starts")
        if self.evidence_type not in {"reported", "guidance", "assumption"}:
            raise ValueError("Unsupported evidence type")
        if self.review_status not in {"verified", "pending", "rejected"}:
            raise ValueError("Unsupported review status")
        if self.evidence_type != "assumption" and (not self.source_id or not self.source_passage):
            raise ValueError("Source-backed evidence requires its original passage")


def load_snapshot(path: Path = ANCHORS) -> dict:
    snapshot = json.loads(path.read_text())
    sources = {s["source_id"]: s for s in snapshot["sources"]}
    ids = set()
    for raw in snapshot["records"]:
        record = EvidenceRecord(**raw)
        record.validate()
        if record.record_id in ids:
            raise ValueError("Duplicate evidence ID")
        ids.add(record.record_id)
        if record.source_id not in sources:
            raise ValueError("Unknown evidence source")
    return snapshot


def available_records(snapshot: dict, cutoff: str) -> list[dict]:
    date.fromisoformat(cutoff)
    return [r for r in snapshot["records"] if r["published_on"] <= cutoff and r["review_status"] == "verified"]


def evidence_manifest(snapshot: dict, cutoff: str) -> dict:
    records = available_records(snapshot, cutoff)
    source_ids = {r["source_id"] for r in records}
    return {"snapshot_id": digest(snapshot), "information_cutoff": cutoff,
            "sources": [s for s in snapshot["sources"] if s["source_id"] in source_ids],
            "record_count": len(records), "records": records,
            "vintage_status": "Curated published-period anchors, not a historical as-of panel",
            "derivation_notice": "Period totals divided into monthly run rates are assumptions, not monthly observations."}


def build_warehouse(snapshot: dict, destination: Path = WAREHOUSE) -> dict:
    """Content-addressed Parquet snapshot; queries run in DuckDB without mutable source tables."""
    snapshot_id = digest(snapshot)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / snapshot_id
    target.mkdir(exist_ok=True)
    records_file = target / "evidence.json"
    if records_file.exists() and digest(json.loads(records_file.read_text())) != digest(snapshot["records"]):
        raise ValueError("Content-addressed evidence snapshot was modified")
    if not records_file.exists():
        records_file.write_text(json.dumps(snapshot["records"], indent=2) + "\n")
    parquet = target / "evidence.parquet"
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE evidence AS SELECT * FROM read_json_auto(?)", [str(records_file)])
        if not parquet.exists():
            escaped = str(parquet).replace("'", "''")
            con.execute(f"COPY evidence TO '{escaped}' (FORMAT PARQUET)")
        count = con.execute("SELECT count(*) FROM evidence").fetchone()[0]
    finally:
        con.close()
    manifest = {"snapshot_id": snapshot_id, "records": count, "parquet_sha256": hashlib.sha256(parquet.read_bytes()).hexdigest(),
                "source_snapshot": snapshot, "created_at_utc": datetime.now(timezone.utc).isoformat()}
    manifest_path = target / "manifest.json"
    if not manifest_path.exists():
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return {"snapshot_id": snapshot_id, "records": count, "path": str(target)}


def query_as_of(parquet: Path, cutoff: str) -> list[dict]:
    date.fromisoformat(cutoff)
    con = duckdb.connect()
    try:
        result = con.execute("SELECT * FROM read_parquet(?) WHERE published_on <= ? AND review_status = 'verified' ORDER BY record_id", [str(parquet), cutoff])
        names = [column[0] for column in result.description]
        return [dict(zip(names, (value.isoformat() if isinstance(value, (date, datetime)) else value for value in row))) for row in result.fetchall()]
    finally:
        con.close()
