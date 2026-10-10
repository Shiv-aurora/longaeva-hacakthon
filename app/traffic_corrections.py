"""Separate, present-day source correction with original vintages preserved.

Research reconstruction only: review knowledge was unavailable at historical
issue dates. Literal cross-year cohort reuse is the sole correction trigger.
"""
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date
import hashlib
from io import BytesIO, TextIOWrapper
import json
from pathlib import Path
from zipfile import ZipFile

import duckdb

from .evidence import digest
from .monthly_traffic import observations, month_end, periods
from .operating_history import METRICS, MARKETS, archive_metadata
from .operating_vintages import verify_vintages, vintage_panel, write_immutable
from .source_store import ROOT, RAW, read_source

VERSION = "reviewed-traffic-vintages-0.1.1"
DESTINATION = ROOT / "data/reviewed_traffic_vintages"
KNOWLEDGE_LABEL = "Present-day reviewed reconstruction using hindsight source-quality knowledge; not a historical point-in-time forecast."


def scan_cohort(task):
    source, period, raw_root = task
    if period not in ("2020-01", "2021-01"): raise ValueError("Cohort scan outside reviewed temporal scope; locked quantities excluded")
    content, metadata = read_source(source["sha256"], Path(raw_root))
    if metadata["source_url"] != source["source_url"]: raise ValueError("Cohort source URL differs")
    rows = []
    with ZipFile(BytesIO(content)) as archive:
        members = [m for m in archive.infolist() if m.filename.lower().endswith(".asc")]
        if len(members) != 1 or members[0].file_size > 500_000_000: raise ValueError("Cohort requires one bounded original ASC member")
        with archive.open(members[0]) as binary:
            for line, text in enumerate(TextIOWrapper(binary, encoding="utf-8-sig"), 1):
                if not text.strip(): continue
                f = text.rstrip("\r\n").split("|")
                if len(f) in (29, 33) and f[-1] == "": f.pop()
                if len(f) not in (28, 32): raise ValueError("Cohort raw layout differs")
                p = f"{int(f[0]):04d}-{int(f[1]):02d}"
                if p != period or f[10] != "AA" or f[14] != "F" or f[17] not in ("1", "3"): continue
                rows.append({"source_line": line, "payload": f[2:]})
    return {"source_sha256": source["sha256"], "source_member": members[0].filename,
        "period": period, "scope": source["scope"], "rows": rows}


def literal_signature(cohort):
    # Comparing the actual tuples as well as saving their digest avoids using
    # quantity totals or a hash alone as the acceptance rule.
    return Counter(tuple(row["payload"]) for row in cohort["rows"])


def detect_reuse(cohorts, references, replacements, sources):
    source_index = {s["sha256"]: s for s in sources}
    patches, review = {}, []
    for cohort in cohorts:
        scope = cohort["scope"]
        reference = references[scope]
        detected = bool(cohort["rows"]) and literal_signature(cohort) == literal_signature(reference)
        replacement = replacements[scope]
        item = {"source_sha256": cohort["source_sha256"], "source_member": cohort["source_member"],
            "scope": scope, "period": cohort["period"], "cohort_rows": len(cohort["rows"]),
            "literal_payload_digest": digest(sorted([list(k), v] for k,v in literal_signature(cohort).items())),
            "status": "confirmed_prior_year_cohort_reuse" if detected else "no_exact_cohort_reuse_detected"}
        if detected:
            rejected, good = source_index[cohort["source_sha256"]], source_index[replacement["source_sha256"]]
            if cohort["period"] != "2021-01" or replacement["period"] != "2021-01" or good["published_on"] > rejected["published_on"]:
                raise ValueError("Correction requires earlier released replacement records")
            if literal_signature(replacement) == literal_signature(reference) or not replacement["rows"]:
                raise ValueError("Replacement is empty or also repeats the prior-year cohort")
            item.update({"reference_source_sha256": reference["source_sha256"],
                "replacement_source_sha256": replacement["source_sha256"], "replacement_rows": len(replacement["rows"]),
                "rejected_source_lines": [r["source_line"] for r in cohort["rows"]],
                "reference_source_lines": [r["source_line"] for r in reference["rows"]],
                "replacement_source_lines": [r["source_line"] for r in replacement["rows"]],
                "rejected_published_on": rejected["published_on"], "replacement_published_on": good["published_on"]})
            patches[cohort["source_sha256"]] = item
        review.append(item)
    return patches, review


def reviewed_panel(normalized, sources, cutoff, patches, review_known_on):
    original = vintage_panel(normalized, sources, cutoff)
    indexed = defaultdict(list)
    for archive in normalized:
        for row in archive["rows"]: indexed[row["source_sha256"], row["period"]].append(row)
    groups, applied = {}, []
    for release in original["selected_releases"]:
        sha, period = release["source_sha256"], release["period"]
        rows = indexed[sha, period]
        patch = patches.get(sha) if period == "2021-01" else None
        if patch:
            if patch["scope"] != release["scope"] or patch["replacement_published_on"] > cutoff:
                raise ValueError("Correction replacement not available at issue date/scope")
            replacement = [r for r in indexed[patch["replacement_source_sha256"], period] if r["carrier"] == "AA"]
            if not replacement: raise ValueError("Normalized correction cohort absent")
            rows = [r for r in rows if r["carrier"] != "AA"] + replacement
            applied.append({**patch, "selected_release": release, "review_known_on": review_known_on,
                "knowledge_label": KNOWLEDGE_LABEL})
        for row in rows:
            key = row["period"], row["carrier"], row["carrier_wac"], row["market"]
            if key not in groups:
                groups[key] = {"period": key[0], "carrier": key[1], "carrier_wac": key[2], "market": key[3],
                    **dict.fromkeys(METRICS, 0), "carrier_entities": set(), "source_sha256s": set(), "published_on": row["published_on"]}
            target = groups[key]
            for metric in METRICS: target[metric] += row[metric]
            target["carrier_entities"].add(row["carrier_entity"]); target["source_sha256s"].add(row["source_sha256"])
            target["published_on"] = max(target["published_on"], row["published_on"])
    available = {(r["scope"], r["period"]) for r in original["selected_releases"]}
    result = []
    for key in sorted(groups, key=repr):
        row = groups[key]
        for field in ("carrier_entities", "source_sha256s"): row[field] = sorted(row[field])
        row["both_archive_scopes_available"] = all((scope, row["period"]) in available for scope in ("DD", "FD"))
        row["quality_flags"] = (["rpm_exceeds_asm"] if row["rpm"] > row["asm"] else []) + (["unmapped_market"] if row["market"] not in MARKETS else [])
        if row["carrier_wac"] is None: row["quality_flags"].append("missing_carrier_country_metadata")
        result.append(row)
    return {"rows": result, "selected_releases": original["selected_releases"], "corrections_applied": applied}


def reconstruct_reviewed(normalized, sources, native, patches, review_known_on, cutoff):
    versions, origins = {}, []
    for original in native["origins"]:
        issue = original["issue_on"]
        panel = reviewed_panel(normalized, sources, issue, patches, review_known_on)
        prepared = observations(panel["rows"], panel["selected_releases"])
        ids = []
        for row in prepared["observations"]:
            corrections = [p for p in panel["corrections_applied"] if row["carrier"] == "AA" and row["period"] == p["period"]]
            reviewed = {**row, "source_corrections": corrections, "review_known_on": review_known_on, "knowledge_label": KNOWLEDGE_LABEL}
            if row["published_on"] > issue or row["period"] > issue[:7]: raise ValueError("Future record in reviewed history")
            identity = digest(reviewed); versions[identity] = reviewed; ids.append(identity)
        # Keep the original scope/row availability statistics, then verify the
        # corrected panel did not change coverage or add synthetic observations.
        before = [native["observation_versions"][i] for i in original["observation_ids"]]
        keys = lambda rows: {(r["carrier"],r["market"],r["period"]) for r in rows}
        if keys(before) != keys(prepared["observations"]): raise ValueError("Correction changed retained target coverage")
        origins.append({**original, "observation_ids": ids, "excluded_rows": prepared["excluded_rows"],
            "corrections_applied": panel["corrections_applied"], "knowledge_label": KNOWLEDGE_LABEL})
    truth_panel = reviewed_panel(normalized, sources, cutoff, patches, review_known_on)
    truth = observations(truth_panel["rows"], truth_panel["selected_releases"])
    for row in truth["observations"]:
        row.update({"source_corrections": [p for p in truth_panel["corrections_applied"] if row["carrier"] == "AA" and row["period"] == p["period"]],
            "review_known_on": review_known_on, "knowledge_label": KNOWLEDGE_LABEL})
    return {"observation_versions": versions, "origins": origins, "evaluation_targets": truth,
        "evaluation_corrections": truth_panel["corrections_applied"]}


def code_bundle():
    names = ("traffic_corrections.py", "operating_vintages.py", "operating_history.py", "monthly_traffic.py", "source_store.py", "evidence.py")
    return {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest() for name in names}


def build_reviewed(spec, workers=1, destination=DESTINATION, raw=RAW, progress=None):
    if (spec["carrier"], spec["period"], spec["service_class"], spec["aircraft_configurations"]) != ("AA", "2021-01", "F", ["1", "3"]):
        raise ValueError("Correction scope differs from source proof")
    date.fromisoformat(spec["review_known_on"])
    if spec["review_known_on"] <= "2023-12-31": raise ValueError("Correction knowledge must be labeled after historical selection")
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 4: raise ValueError("Use one to four source parser workers")
    root = ROOT / "data/traffic_vintages"
    upstream = root / spec["vintage_snapshot_id"]
    native = verify_vintages(upstream, root, raw)
    manifest = json.loads((upstream / "manifest.json").read_text())
    upstream_design = json.loads((root / "designs" / manifest["design_id"] / "design.json").read_text())
    trace_path = ROOT / "data/traffic_revision_traces" / spec["trace_id"] / "record.json"
    trace = json.loads(trace_path.read_text())
    audit = json.loads((ROOT / "data/traffic_revision_checks" / (spec["trace_audit_id"] + ".json")).read_text())
    if digest(trace) != spec["trace_id"] or digest(audit) != spec["trace_audit_id"] or audit["trace_id"] != spec["trace_id"] or trace["vintage_snapshot_id"] != upstream.name:
        raise ValueError("Correction source review/audit binding differs")
    domestic = [r for r in trace["quarter_t2_comparisons"]["2022-01-31"]["grains"] if r["grain"][0] == "0A050"]
    if len(domestic) != 10 or any(r["status"] != "exact_rpm_asm_departures" for r in domestic): raise ValueError("Correction lacks domestic quarterly support")
    declared_replacements = {r["scope"]:r["source_sha256"] for r in trace["quarter_selections"]["2022-01-31"]["releases"] if r["period"] == "2021-01"}
    if spec["replacement_sources"] != declared_replacements: raise ValueError("Correction replacement differs from reviewed source")
    declared_references = {s["scope"]:s["sha256"] for s in trace["sources"] if s["period_first"] == "2020-01" and s["period_last"] == "2020-12"}
    if spec["reference_sources"] != declared_references: raise ValueError("Correction reference differs from prior-year source proof")
    sources = upstream_design["spec"]["sources"]
    source_index = {s["sha256"]:s for s in sources}
    design = {"version": VERSION, "specification": spec, "upstream_artifact_digest": manifest["artifact_digest"], "code": code_bundle()}
    design_id = digest(design); destination = Path(destination)
    write_immutable(destination / "designs" / design_id / "design.json", design)
    references, replacements = {}, {}
    for scope in ("DD", "FD"):
        references[scope] = scan_cohort((source_index[spec["reference_sources"][scope]], "2020-01", str(raw)))
        replacements[scope] = scan_cohort((source_index[spec["replacement_sources"][scope]], "2021-01", str(raw)))
        if len(references[scope]["rows"]) != spec["expected_reference_rows"][scope] or len(replacements[scope]["rows"]) != spec["expected_replacement_rows"][scope]: raise ValueError("Correction declared cohort counts differ")
    candidates = [s for s in sources if s["period_first"] <= "2021-01" <= s["period_last"]]
    tasks = [(s, "2021-01", str(raw)) for s in candidates]
    if workers == 1: cohorts = [scan_cohort(task) for task in tasks]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool: cohorts = list(pool.map(scan_cohort, tasks))
    patches, review = detect_reuse(cohorts, references, replacements, sources)
    if progress: progress({"phase": "cohorts_reviewed", "archives":len(cohorts), "confirmed_reuse_archives": len(patches)})
    normalized = json.loads((upstream / "normalized.json").read_text())
    result = reconstruct_reviewed(normalized, sources, native, patches, spec["review_known_on"], upstream_design["spec"]["acquisition_cutoff"])
    artifact = {"version": VERSION, "design_id": design_id, "knowledge_label": KNOWLEDGE_LABEL,
        "native_snapshot_id": upstream.name, "cohort_review": review, "patches": patches, **result}
    output = {"version": VERSION, "design_id": design_id, "native_snapshot_id": upstream.name,
        "artifact_digest": digest(artifact), "code": design["code"], "cohorts_reviewed":len(review), "confirmed_reuse_archives":len(patches),
        "knowledge_label": KNOWLEDGE_LABEL}
    identity = digest(output); output["snapshot_id"] = identity
    target = destination / identity
    write_immutable(target / "artifact.json", artifact); write_immutable(target / "manifest.json", output)
    for name, sha in design["code"].items():
        content = (Path(__file__).parent / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != sha: raise ValueError("Correction builder changed during job")
        p = target / "code" / name; p.parent.mkdir(exist_ok=True)
        if p.exists() and p.read_bytes() != content: raise ValueError("Immutable correction source differs")
        if not p.exists(): p.write_bytes(content)
    tables = {"versions":list(artifact["observation_versions"].values()),
        "origins":[{k:v for k,v in row.items() if k != "excluded_rows"} for row in artifact["origins"]],
        "targets":artifact["evaluation_targets"]["observations"]}
    connection = duckdb.connect()
    try:
        for name, rows in tables.items():
            source = target / (name + ".json"); write_immutable(source, rows)
            parquet = target / (name + ".parquet")
            if not parquet.exists():
                escaped = str(parquet).replace("'", "''")
                connection.execute(f"COPY (SELECT * FROM read_json_auto(?, maximum_depth=-1, sample_size=-1)) TO '{escaped}' (FORMAT PARQUET)", [str(source)])
    finally: connection.close()
    # Verify content before publication; an independent raw-source audit follows.
    verify_reviewed(target, destination)
    pointer = destination / "latest.tmp"; pointer.write_text(json.dumps({"snapshot_id":identity}) + "\n"); pointer.replace(destination / "latest.json")
    affected = [o["issue_on"] for o in artifact["origins"] if o["corrections_applied"]]
    return {**output, "path":str(target), "affected_origins":affected, "evaluation_corrections":len(artifact["evaluation_corrections"])}


def verify_reviewed(target, destination=DESTINATION):
    target, destination = Path(target), Path(destination)
    manifest = json.loads((target / "manifest.json").read_text()); artifact = json.loads((target / "artifact.json").read_text())
    if digest({k:v for k,v in manifest.items() if k != "snapshot_id"}) != target.name or manifest["snapshot_id"] != target.name or digest(artifact) != manifest["artifact_digest"]: raise ValueError("Reviewed traffic content differs")
    design = json.loads((destination / "designs" / manifest["design_id"] / "design.json").read_text())
    if digest(design) != manifest["design_id"] or artifact["design_id"] != manifest["design_id"] or manifest["code"] != design["code"] or not (artifact["native_snapshot_id"] == manifest["native_snapshot_id"] == design["specification"]["vintage_snapshot_id"]): raise ValueError("Reviewed traffic design differs")
    for name, sha in manifest["code"].items():
        if hashlib.sha256((target / "code" / name).read_bytes()).hexdigest() != sha: raise ValueError("Reviewed traffic preserved code differs")
    for identity, row in artifact["observation_versions"].items():
        if digest(row) != identity: raise ValueError("Reviewed observation identity differs")
    tables = {"versions":list(artifact["observation_versions"].values()),
        "origins":[{k:v for k,v in row.items() if k != "excluded_rows"} for row in artifact["origins"]],
        "targets":artifact["evaluation_targets"]["observations"]}
    connection = duckdb.connect()
    try:
        for name, rows in tables.items():
            if digest(json.loads((target / (name + ".json")).read_text())) != digest(rows): raise ValueError("Reviewed row JSON differs")
            restored = [json.loads(r[0]) for r in connection.execute("SELECT to_json(t) FROM read_parquet(?) t", [str(target / (name + ".parquet"))]).fetchall()]
            if digest(restored) != digest(rows): raise ValueError("Reviewed Parquet differs")
    finally: connection.close()
    return artifact
