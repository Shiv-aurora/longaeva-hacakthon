"""Current-download rolling-release reconstruction, selected anew at each issue date.

Filename release dates are availability proxies, not independently preserved
historical publication timestamps. No 2024+ observation is exposed here.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from collections import defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import re

import duckdb

from .evidence import digest
from .monthly_traffic import CARRIERS, observations, periods, month_end
from .operating_history import archive_metadata, normalize_archive, wac_lookup, WAC_URL, METRICS, MARKETS
from .source_store import RAW, ROOT, read_source

VERSION = "traffic-vintages-0.1.0"
DESTINATION = ROOT / "data/traffic_vintages"
VINTAGE_STATUS = ("Rolling-release reconstruction from current official downloads. "
    "Archive filename release dates gate each issue-date selection; original historical "
    "bytes and exact publication timestamps are not independently recovered.")


def write_immutable(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if digest(json.loads(path.read_text())) != digest(value):
            raise ValueError("Immutable vintage record differs")
    else:
        with path.open("x") as stream:
            json.dump(value, stream, separators=(",", ":"), allow_nan=False)
            stream.write("\n")


def code_bundle():
    names = ("operating_vintages.py", "operating_history.py", "monthly_traffic.py", "source_store.py", "evidence.py")
    return {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest() for name in names}


def normalize_source(task):
    source, wac_sha, raw, cache, code = task
    content, metadata = read_source(source["sha256"], Path(raw))
    if metadata["source_url"] != source["source_url"]:
        raise ValueError("Vintage archive URL differs from preserved source")
    wac_content, wac_metadata = read_source(wac_sha, Path(raw))
    if wac_metadata["source_url"] != WAC_URL:
        raise ValueError("Unexpected vintage WAC lookup URL")
    key = digest({"source": source["sha256"], "wac": wac_sha, "code": code})
    path = Path(cache) / (key + ".json")
    if path.exists():
        record = json.loads(path.read_text())
        if record["key"] != key or record["normalized_digest"] != digest(record["normalized"]):
            raise ValueError("Vintage normalization cache differs")
        return record["normalized"]
    value = normalize_archive(content, source, wac_lookup(wac_content))
    # Original parser checks every record. Only the declared certificate panel
    # is retained; archive-wide quality flags are not attributed to a carrier.
    value["rows"] = [r for r in value["rows"] if r["carrier"] in CARRIERS and "2013-01" <= r["period"] <= "2023-12"]
    write_immutable(path, {"key": key, "normalized_digest": digest(value), "normalized": value})
    return value


def reconstruct(normalized, sources, issue_dates, acquisition_cutoff):
    date.fromisoformat(acquisition_cutoff)
    if len(set(issue_dates)) != len(issue_dates) or not issue_dates:
        raise ValueError("Vintage issue dates must be nonempty and unique")
    versions, origins = {}, []
    for issue in sorted(issue_dates):
        if date.fromisoformat(issue).isoformat() != issue or not "2020-01" <= issue[:7] <= "2023-12" or issue > acquisition_cutoff:
            raise ValueError("Vintage issue outside the declared selection period")
        # Never build once at acquisition cutoff and then filter dates. Later
        # releases can replace earlier numbers or remove contributions entirely.
        panel = vintage_panel(normalized, sources, issue)
        prepared = observations(panel["rows"], panel["selected_releases"])
        ids = []
        for row in prepared["observations"]:
            if row["published_on"] > issue or row["period"] > issue[:7]:
                raise ValueError("Future observation entered vintage history")
            identity = digest(row)
            versions[identity] = row
            ids.append(identity)
        complete = [r["period"] for r in prepared["observations"]]
        newest = max(complete) if complete else None
        lag = (int(issue[:4]) - int(newest[:4])) * 12 + int(issue[5:7]) - int(newest[5:7]) if newest else None
        counts = {c: sum(r["carrier"] == c for r in prepared["observations"]) for c in CARRIERS}
        origins.append({"issue_on": issue, "observation_ids": ids,
            "excluded_rows": prepared["excluded_rows"], "selected_releases": panel["selected_releases"],
            "newest_complete_period": newest, "information_lag_months": lag, "observations_by_carrier": counts})
    # Truth uses the declared acquisition cutoff, separately from any history
    # permitted to a forecaster. Locked observations are discarded before access.
    truth_panel = vintage_panel(normalized, sources, acquisition_cutoff)
    truth = observations(truth_panel["rows"], truth_panel["selected_releases"])
    return {"observation_versions": versions, "origins": origins, "evaluation_targets": truth}


def vintage_panel(normalized, sources, cutoff):
    """Select each scope/month; accept same-release aliases only if equal.

    Equality covers the retained UA/DL/AA certificate/entity/country/market
    aggregate grain and every normalized metric, not all carriers in the ZIP.
    """
    grouped = defaultdict(list)
    archive_rows = defaultdict(list)
    for n in normalized:
        for row in n["rows"]:
            archive_rows[row["source_sha256"], row["period"]].append(row)
    for s in sources:
        m = archive_metadata(s["source_url"])
        if m["published_on"] > cutoff: continue
        for p in periods(max(m["period_first"], "2013-01"), min(m["period_last"], "2023-12")):
            grouped[m["scope"], p].append((m["published_on"], m["release_number"], s["sha256"]))
    releases = {}
    for key, candidates in grouped.items():
        rank = max((c[0], c[1]) for c in candidates)
        shas = sorted(c[2] for c in candidates if (c[0], c[1]) == rank)
        if len(shas) > 1:
            signatures = {digest([{k:v for k,v in row.items() if k not in ("source_sha256", "source_member", "published_on")}
                for row in archive_rows[sha, key[1]]]) for sha in shas}
            if len(signatures) != 1:
                raise ValueError("Conflicting same-release certificate aggregates")
        release = {"scope": key[0], "period": key[1], "source_sha256": shas[0], "published_on": rank[0],
            "equivalent_source_sha256s": shas}
        releases[key] = release
    aggregates = {}
    for (_, period), release in releases.items():
        for row in archive_rows[release["source_sha256"], period]:
            key = row["period"], row["carrier"], row["carrier_wac"], row["market"]
            if key not in aggregates:
                aggregates[key] = {"period": key[0], "carrier": key[1], "carrier_wac": key[2], "market": key[3],
                    **{metric:0 for metric in METRICS}, "carrier_entities":set(), "source_sha256s":set(), "published_on":row["published_on"]}
            a = aggregates[key]
            for metric in METRICS: a[metric] += row[metric]
            a["carrier_entities"].add(row["carrier_entity"]); a["source_sha256s"].add(row["source_sha256"])
            a["published_on"] = max(a["published_on"], row["published_on"])
    rows = []
    for key in sorted(aggregates, key=repr):
        row = aggregates[key]
        row["carrier_entities"] = sorted(row["carrier_entities"]); row["source_sha256s"] = sorted(row["source_sha256s"])
        row["both_archive_scopes_available"] = all((scope, row["period"]) in releases for scope in ("DD", "FD"))
        row["quality_flags"] = (["rpm_exceeds_asm"] if row["rpm"] > row["asm"] else []) + (["unmapped_market"] if row["market"] not in MARKETS else [])
        if row["carrier_wac"] is None: row["quality_flags"].append("missing_carrier_country_metadata")
        rows.append(row)
    return {"rows":rows, "selected_releases":[releases[k] for k in sorted(releases)]}


def origin_history(artifact, issue):
    matches = [r for r in artifact["origins"] if r["issue_on"] == issue]
    if len(matches) != 1:
        raise ValueError("Requested issue is absent from vintage design")
    rows = [artifact["observation_versions"][key] for key in matches[0]["observation_ids"]]
    keys = [(r["carrier"], r["market"], r["period"]) for r in rows]
    if len(keys) != len(set(keys)) or any(r["published_on"] > issue or r["period"] > issue[:7] for r in rows):
        raise ValueError("Invalid or future vintage history")
    return rows


def build_vintages(spec, destination=DESTINATION, raw=RAW, workers=1):
    if (spec.get("origin_first"), spec.get("origin_last"), spec.get("target_first"), spec.get("target_last"), spec.get("carriers")) != (
            "2020-01", "2023-12", "2013-01", "2023-12", list(CARRIERS)):
        raise ValueError("Vintage design differs from proposal temporal/carrier boundaries")
    cutoff = spec["acquisition_cutoff"]
    date.fromisoformat(cutoff)
    sources = sorted(spec["sources"], key=lambda s: s["source_url"])
    if not sources or len({s["sha256"] for s in sources}) != len(sources):
        raise ValueError("Vintage sources must be nonempty and unique")
    for s in sources:
        m = archive_metadata(s["source_url"])
        if m["published_on"] > cutoff or m["period_last"] > "2023-12":
            raise ValueError("Future or locked archive outside vintage design")
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 4:
        raise ValueError("Use one to four parser workers")
    code = code_bundle()
    design = {"version": VERSION, "spec": spec, "code": code, "duckdb_version": duckdb.__version__}
    design_id = digest(design)
    destination = Path(destination)
    write_immutable(destination / "designs" / design_id / "design.json", design)
    tasks = [(s, spec["wac_source"]["sha256"], str(raw), str(destination / "normalization_cache"), code) for s in sources]
    if workers == 1:
        normalized = [normalize_source(t) for t in tasks]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            normalized = list(pool.map(normalize_source, tasks))
    issues = [month_end(p) for p in periods(spec["origin_first"], spec["origin_last"])]
    artifact = {"version": VERSION, "design_id": design_id, "vintage_status": VINTAGE_STATUS,
        "boundary": spec["boundary"], "source_summaries": [{k: v for k, v in n.items() if k != "rows"} for n in normalized],
        **reconstruct(normalized, sources, issues, cutoff)}
    manifest = {"version": VERSION, "design_id": design_id, "artifact_digest": digest(artifact),
        "normalized_digest": digest(normalized),
        "source_count": len(sources), "source_bytes": sum(s["bytes"] for s in sources), "code": code,
        "duckdb_version": duckdb.__version__, "scope": "Three certificates; 2013–23 targets, 2020–23 issue dates; DD/FD only"}
    identity = digest(manifest)
    manifest["snapshot_id"] = identity
    target = destination / identity
    write_immutable(target / "artifact.json", artifact)
    write_immutable(target / "manifest.json", manifest)
    write_immutable(target / "normalized.json", normalized)
    for name in code:
        payload = (Path(__file__).parent / name).read_bytes()
        if hashlib.sha256(payload).hexdigest() != code[name]:
            raise ValueError("Vintage builder source changed during job")
        p = target / "code" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.read_bytes() != payload:
            raise ValueError("Immutable vintage source differs")
        if not p.exists():
            p.write_bytes(payload)
    return _publish(destination, target, artifact, manifest, raw)


def _publish(destination, target, artifact, manifest, raw):
    # Publication verifies all persisted JSON and native Parquet tables before
    # replacing the latest pointer. Normalization digest is checked by the reader.
    saved_manifest = json.loads((target / "manifest.json").read_text())
    if saved_manifest != manifest:
        raise ValueError("Vintage manifest differs before publication")
    tables = {"versions": list(artifact["observation_versions"].values()),
        "origins": [{k: v for k, v in r.items() if k != "excluded_rows"} for r in artifact["origins"]]}
    connection = duckdb.connect()
    try:
        for name, rows in tables.items():
            source = target / (name + ".json")
            write_immutable(source, rows)
            parquet = target / (name + ".parquet")
            if not parquet.exists():
                escaped = str(parquet).replace("'", "''")
                connection.execute(f"COPY (SELECT * FROM read_json_auto(?, maximum_depth=-1, sample_size=-1)) TO '{escaped}' (FORMAT PARQUET)", [str(source)])
            # Read through JSON to normalize DuckDB's inferred date types.
            restored = [json.loads(r[0]) for r in connection.execute("SELECT to_json(t) FROM read_parquet(?) t", [str(parquet)]).fetchall()]
            if digest(restored) != digest(rows):
                raise ValueError("Vintage Parquet differs from JSON")
    finally:
        connection.close()
    verify_vintages(target, destination, raw)
    pointer = destination / "latest.tmp"
    pointer.write_text(json.dumps({"snapshot_id": target.name}) + "\n")
    pointer.replace(destination / "latest.json")
    lags = [r["information_lag_months"] for r in artifact["origins"] if r["information_lag_months"] is not None]
    return {**saved_manifest, "path": str(target), "origins": len(artifact["origins"]),
        "observation_versions": len(artifact["observation_versions"]), "information_lag_months": {"min": min(lags), "max": max(lags)}}


def verify_vintages(target, destination=DESTINATION, raw=RAW):
    target, destination = Path(target), Path(destination)
    if not re.fullmatch(r"[a-f0-9]{64}", target.name):
        raise ValueError("Invalid vintage snapshot identity")
    manifest = json.loads((target / "manifest.json").read_text())
    artifact = json.loads((target / "artifact.json").read_text())
    if digest({k: v for k, v in manifest.items() if k != "snapshot_id"}) != target.name or manifest["snapshot_id"] != target.name or digest(artifact) != manifest["artifact_digest"]:
        raise ValueError("Vintage manifest/artifact checksum differs")
    design = json.loads((destination / "designs" / manifest["design_id"] / "design.json").read_text())
    if digest(design) != manifest["design_id"] or design["code"] != manifest["code"] or artifact["design_id"] != manifest["design_id"]:
        raise ValueError("Vintage design checksum differs")
    normalized = json.loads((target / "normalized.json").read_text())
    if digest(normalized) != manifest["normalized_digest"]:
        raise ValueError("Vintage normalized checksum differs")
    for source in [*design["spec"]["sources"], design["spec"]["wac_source"], *design["spec"].get("catalogs", [])]:
        _, metadata = read_source(source["sha256"], Path(raw))
        if metadata["source_url"] != source["source_url"]:
            raise ValueError("Preserved vintage source URL differs")
    for name, sha in manifest["code"].items():
        if hashlib.sha256((target / "code" / name).read_bytes()).hexdigest() != sha:
            raise ValueError("Preserved vintage builder checksum differs")
    for key, row in artifact["observation_versions"].items():
        if digest(row) != key:
            raise ValueError("Vintage observation identity differs")
    for origin in artifact["origins"]:
        origin_history(artifact, origin["issue_on"])
    expected = {"versions": list(artifact["observation_versions"].values()),
        "origins": [{k: v for k, v in r.items() if k != "excluded_rows"} for r in artifact["origins"]]}
    connection = duckdb.connect()
    try:
        for name, rows in expected.items():
            if digest(json.loads((target / (name + ".json")).read_text())) != digest(rows):
                raise ValueError("Vintage row JSON differs")
            restored = [json.loads(r[0]) for r in connection.execute("SELECT to_json(t) FROM read_parquet(?) t", [str(target / (name + ".parquet"))]).fetchall()]
            if digest(restored) != digest(rows):
                raise ValueError("Vintage Parquet differs from JSON")
    finally:
        connection.close()
    return artifact
