"""Independent raw-source, release, scalar-filter and regression audit.

No monthly-traffic/importer functions are imported. Native distribution scores are
diagnostics conditional on fitted parameters, not calibrated coverage guarantees.
"""
from collections import defaultdict
from datetime import datetime
import hashlib
from io import BytesIO, TextIOWrapper
import json
import math
from pathlib import Path
import statistics
import sys
from zipfile import ZipFile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.evidence import digest
from app.source_store import read_source

SERIES = [(c, m) for c in ("UA", "DL", "AA") for m in ("domestic", "atlantic", "pacific", "latin_america")]
CANADA = {906, 911, 916, 921, 926, 931, 934, 936, 941, 946, 951, 956, 961}


def number(period):
    return int(period[:4]) * 12 + int(period[5:]) - 1


def endpoint(code):
    if code in (3, 4) or 100 <= code < 400: return "latin_america"
    if code == 733 or 400 <= code < 700 or code in (975, 977): return "atlantic"
    if code == 5 or 700 <= code < 900: return "pacific"
    if code in CANADA or 0 < code < 100: return "domestic"
    return None


def region(origin, destination):
    a, b = endpoint(origin), endpoint(destination)
    if a == b: return a
    if a == "domestic": return b
    if b == "domestic": return a
    return None


def raw_fields(text):
    fields = text.rstrip("\r\n").split("|")
    if len(fields) in (29, 33) and fields[-1] == "": fields.pop()
    if len(fields) not in (28, 32): raise ValueError("Independent raw source layout differs")
    return fields


def raw_targets(upstream, progress=None):
    manifest = json.loads((upstream / "manifest.json").read_text())
    panel = json.loads((upstream / "panel.json").read_text())
    selected = {(r["scope"], r["period"]): r for r in panel["selected_releases"] if "2013-01" <= r["period"] <= "2023-12"}
    sources = {r["sha256"]: r for r in manifest["sources"]}
    for release in selected.values():
        filename = sources[release["source_sha256"]]["source_url"].rsplit("/", 1)[-1]
        published = datetime.strptime(filename.rsplit(".", 2)[-2], "%d%b%Y").date().isoformat()
        if published != release["published_on"]:
            raise ValueError("Independent raw archive publication date differs")
    sums, witnesses = defaultdict(lambda: [0, 0, set(), set()]), []
    count = 0
    for sha in sorted({r["source_sha256"] for r in selected.values()}):
        content, _ = read_source(sha)
        with ZipFile(BytesIO(content)) as archive:
            member = next(n for n in archive.namelist() if n.lower().endswith(".asc"))
            scope = "DD" if ".dd." in member.lower() or member.lower().startswith("dd.") else "FD"
            with archive.open(member) as binary:
                for line, text in enumerate(TextIOWrapper(binary, encoding="utf-8-sig"), 1):
                    if not text.strip(): continue
                    fields = raw_fields(text)
                    period = f"{int(fields[0]):04d}-{int(fields[1]):02d}"
                    if (scope, period) not in selected or selected[scope, period]["source_sha256"] != sha or fields[10] not in ("UA", "DL", "AA") or fields[14] != "F" or fields[17] not in ("1", "3"):
                        continue
                    market = region(int(fields[4]), int(fields[8]))
                    if (fields[10], market) not in SERIES: continue
                    legacy = len(fields) == 32
                    seats = sum(int(fields[i]) for i in (21, 22, 23)) if legacy else int(fields[21])
                    passengers = sum(int(fields[i]) for i in (24, 25, 26)) if legacy else int(fields[22])
                    distance = int(fields[13]); key = fields[10], market, period
                    sums[key][0] += passengers * distance; sums[key][1] += seats * distance
                    sums[key][2].add(sha); sums[key][3].add(fields[11]); count += 1
                    if len(witnesses) < 12:
                        witnesses.append({"sha256": sha, "member": member, "line": line, "text": text.rstrip()})
        if progress: progress({"phase": "raw_archive_complete", "sha256": sha, "segment_records": count})
    return sums, selected, {"raw_segment_records": count, "source_archives": len({r["source_sha256"] for r in selected.values()}), "raw_witnesses": witnesses}


def basis(c, m, p):
    return [float((c, m) == s) for s in SERIES] + [float(m == region and int(p[5:]) == month) for region in ("domestic", "atlantic", "pacific", "latin_america") for month in range(2, 13)]


def scalar_filter(sequence, q, r, rho):
    process = np.array([[q * (1. if i == j else rho if a[1] == b[1] else 0.) for j, b in enumerate(SERIES)] for i, a in enumerate(SERIES)])
    mean, covariance, ll = np.zeros(12), np.eye(12), 0.
    for measured in sequence:
        covariance += process
        # Sequential independent scalar observations, rather than production's
        # joint Cholesky update and Joseph covariance recursion.
        for i, value in sorted(measured.items()):
            residual, variance = value - mean[i], covariance[i, i] + r
            ll -= .5 * (math.log(2 * math.pi * variance) + residual * residual / variance)
            column = covariance[:, i].copy()
            mean += column * residual / variance
            covariance -= np.outer(column, column) / variance
    return mean, covariance, ll


def interval_diagnostics(rows, name):
    cells = []
    phi = lambda z: (1 + math.erf(z / math.sqrt(2))) / 2
    for c, m in SERIES:
        for horizon in (1, 3, 6, 12):
            for regime in ("all", "disruption", "recovery"):
                chosen = [f for f in rows if (f["carrier"], f["market"], f["horizon_months"]) == (c, m, horizon) and (regime == "all" or f["regime"] == regime)]
                if not chosen: continue
                covered, widths, scores, crps = [], [], [], []
                for f in chosen:
                    d, y = f["distributions"][name], f["actual_rpm_million"]
                    low, high = d["p10"], d["p90"]
                    covered.append(low <= y <= high); widths.append(high - low)
                    scores.append(high - low + 10 * max(0, low - y) + 10 * max(0, y - high))
                    mu, sigma = d["log_mean"], math.sqrt(d["log_variance"])
                    z = (math.log(y) - mu) / sigma
                    # Formula verified against scoringRules' original R source.
                    crps.append(y * (2 * phi(z) - 1) - 2 * math.exp(mu + sigma * sigma / 2) * (phi(z - sigma) + phi(sigma / math.sqrt(2)) - 1))
                cells.append({"carrier": c, "market": m, "horizon_months": horizon, "regime": regime,
                    "targets": len(chosen), "coverage_80": statistics.mean(covered), "width_rpm_million": statistics.mean(widths),
                    "interval_score_80_rpm_million": statistics.mean(scores), "CRPS_rpm_million": statistics.mean(crps)})
    return {"model": name, "cells": cells, "equal_cell_coverage_80": statistics.mean(x["coverage_80"] for x in cells if x["regime"] == "all"),
        "scope": "Native model-conditional intervals only. No baseline distribution comparison, calibrated interval or independent/joint coverage claim."}


def audit(artifact, raw=None, progress=None):
    spec, result = artifact["specification"], artifact["estimation"]
    obs = {(r["carrier"], r["market"], r["period"]): r for r in artifact["panel"]["observations"]}
    checks = 0
    def check(condition, name):
        nonlocal checks
        if not condition: raise ValueError("Independent monthly traffic audit failed: " + name)
        checks += 1
    def close(a, b, name): check(math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-7), name)
    if raw is not None:
        totals, releases, report = raw
        for key, row in obs.items():
            actual = totals.pop(key, None)
            check(actual is not None, "raw target present")
            close(row["rpm_million"], actual[0] / 1e6, "original-source RPM")
            close(row["asm_million"], actual[1] / 1e6, "original-source ASM")
            check(row["source_sha256s"] == sorted(actual[2]) and row["carrier_entities"] == sorted(actual[3]), "raw source and reporting entities")
            complete = max(releases[scope, key[2]]["published_on"] for scope in ("DD", "FD"))
            check(row["published_on"] == max(row["contributor_published_on"], complete), "complete-scope availability")
        check(not totals, "no original target omitted without exclusion")
    initial = {k: v for k, v in obs.items() if k[2] <= "2019-12"}
    check(result["first_initial_release"] == max(r["published_on"] for r in initial.values()), "initial release gate")
    scales = {s: statistics.mean(abs(r["rpm_million"] - initial[*s, f"{int(k[2][:4])-1}-{k[2][5:]}"]["rpm_million"])
        for k, r in initial.items() if k[:2] == s and (*s, f"{int(k[2][:4])-1}-{k[2][5:]}") in initial) for s in SERIES}
    known_by_fit = []
    for fit in result["fits"]:
        known = {k: r for k, r in obs.items() if k[2] <= fit["origin"] and r["published_on"] <= fit["cutoff"]}
        check(fit["known_keys"] == [list(k) for k in sorted(known)], "exact released fit sample")
        known_by_fit.append(known)
        check(fit["cutoff"] >= result["first_initial_release"], "all initial inputs released before fitting")
        check([s["rho"] for s in fit["states"]] == spec["pooling_correlations"], "declared pooling families")
        for model, is_ar, penalty in ((fit["seasonal"], False, 1.), (fit["autoregression"], True, 10.)):
            keys = [tuple(k) for k in model["training_keys"]]
            expected_keys = [k for k in sorted(known) if not is_ar or all((*k[:2], f"{(number(k[2])-lag)//12:04d}-{(number(k[2])-lag)%12+1:02d}") in known for lag in (1, 12))]
            check(keys == expected_keys, "regression eligible target keys")
            check(model["target_count"] == len(keys) and model["training_digest"] == digest([known[k] for k in keys]), "complete regression target trace")
            x = np.array([basis(*k) + ([math.log(known[*k[:2], f"{(number(k[2])-lag)//12:04d}-{(number(k[2])-lag)%12+1:02d}"]["rpm_million"]) for lag in (1, 12)] if is_ar else []) for k in keys])
            y = np.array([math.log(known[k]["rpm_million"]) for k in keys])
            shrink = np.eye(x.shape[1]) * math.sqrt(penalty); shrink[:12, :12] = 0
            beta = np.linalg.lstsq(np.vstack((x, shrink)), np.concatenate((y, np.zeros(x.shape[1]))), rcond=None)[0]
            for a, b in zip(beta, model["coefficients"]): close(a, b, "augmented-design regression coefficient")
        season = fit["seasonal"]["coefficients"]
        sequence = [{i: math.log(known[c, m, p]["rpm_million"]) - sum(a * b for a, b in zip(basis(c, m, p), season))
            for i, (c, m) in enumerate(SERIES) if (c, m, p) in known}
            for p in [f"{t//12:04d}-{t%12+1:02d}" for t in range(number("2013-01"), number(fit["origin"]) + 1)]]
        for state in fit["states"]:
            check([(x["q"], x["r"]) for x in state["grid_likelihoods"]] == [(q, r) for q in spec["process_variance_grid"] for r in spec["measurement_variance_grid"]], "complete declared variance grid")
            witnesses = []
            for grid in state["grid_likelihoods"]:
                mean, cov, ll = scalar_filter(sequence, grid["q"], grid["r"], state["rho"])
                close(ll, grid["loglikelihood"], "scalar innovation likelihood")
                witnesses.append((ll, grid["q"], grid["r"], mean, cov))
            winner = max(witnesses, key=lambda x: x[0])
            check((winner[1], winner[2]) == (state["q"], state["r"]), "finite-grid likelihood selection")
            close(winner[0], state["innovation_loglikelihood"], "winning likelihood")
            for i in range(12):
                close(winner[3][i], state["mean"][i], "scalar posterior mean")
                for j in range(12): close(winner[4][i, j], state["covariance"][i][j], "scalar posterior covariance")
        if progress: progress({"phase": "fit_witness_complete", "origin": fit["origin"], "checks": checks})
    for f in result["forecasts"]:
        check(f["target"] <= "2023-12" and number(f["target"]) == number(f["origin"]) + f["horizon_months"], "locked/target horizon")
        close(f["actual_rpm_million"], obs[f["carrier"], f["market"], f["target"]]["rpm_million"], "actual target")
        close(f["initial_mase_scale"], scales[f["carrier"], f["market"]], "initial MASE scale")
        fit = result["fits"][f["fit_index"]]
        known = known_by_fit[f["fit_index"]]
        check((fit["origin"], fit["cutoff"]) == (f["origin"], f["cutoff"]), "correct issued fit")
        c, m, target = f["carrier"], f["market"], f["target"]
        persistence = max(k[2] for k in known if k[:2] == (c, m) and k[2][5:] == target[5:])
        check(persistence == f["persistence_source_period"] and known[c, m, persistence]["published_on"] == f["persistence_published_on"], "released seasonal source")
        close(f["predictions"]["released_seasonal_persistence"], known[c, m, persistence]["rpm_million"], "seasonal persistence forecast")
        close(f["predictions"]["fixed_initial_seasonal_mean"], statistics.mean(r["rpm_million"] for k, r in initial.items() if k[:2] == (c, m) and k[2][5:] == target[5:]), "fixed initial seasonal baseline")
        log_history = {k[2]: math.log(r["rpm_million"]) for k, r in known.items() if k[:2] == (c, m)}
        for t in range(number(max(log_history)) + 1, number(target) + 1):
            p = f"{t//12:04d}-{t%12+1:02d}"
            vector = basis(c, m, p) + [log_history[f"{(t-lag)//12:04d}-{(t-lag)%12+1:02d}"] for lag in (1, 12)]
            log_history[p] = sum(a * b for a, b in zip(vector, fit["autoregression"]["coefficients"]))
        close(f["predictions"]["expanding_regularized_log_ar"], math.exp(log_history[target]), "recursive release-gap AR baseline")
        for name, d in f["distributions"].items():
            state = next(s for s in fit["states"] if name == f"state_rho_{s['rho']:g}")
            i = SERIES.index((f["carrier"], f["market"]))
            mu = sum(a * b for a, b in zip(basis(f["carrier"], f["market"], f["target"]), fit["seasonal"]["coefficients"])) + state["mean"][i]
            variance = state["covariance"][i][i] + f["horizon_months"] * state["q"] + state["r"]
            close(d["log_mean"], mu, "predictive log mean"); close(d["log_variance"], variance, "release-gap predictive variance")
            for label, z in (("p10", -1.2815515655446004), ("p50", 0), ("p90", 1.2815515655446004)):
                close(d[label], math.exp(mu + z * math.sqrt(variance)), "native predictive quantile")
            close(f["predictions"][name], d["p50"], "conditional median point forecast")
    all_scorecards = [(result["forecasts"], result["scorecard"])] + [([f for f in result["forecasts"] if f["regime"] == part["regime"]], part["scores"]) for part in result["regime_scorecards"]]
    for score_rows, cards in all_scorecards:
      for score in cards:
        mase = []
        check(score["prediction_count"] == len(score_rows) and len(score["series_horizon_scores"]) == 48, "complete common score sample")
        for cell in score["series_horizon_scores"]:
            selected = [f for f in score_rows if (f["carrier"], f["market"], f["horizon_months"]) == (cell["carrier"], cell["market"], cell["horizon_months"])]
            check(len(selected) == cell["targets"], "common cell sample count")
            errors = [f["predictions"][score["model"]] - f["actual_rpm_million"] for f in selected]
            close(cell["MAE_rpm_million"], statistics.mean(map(abs, errors)), "scalar MAE")
            close(cell["RMSE_rpm_million"], math.sqrt(statistics.mean(e * e for e in errors)), "scalar RMSE")
            close(cell["mean_error_rpm_million"], statistics.mean(errors), "scalar mean error")
            close(cell["MASE"], statistics.mean(abs(e) / f["initial_mase_scale"] for e, f in zip(errors, selected)), "scalar MASE")
            mase.append(cell["MASE"])
        close(score["equal_cell_MASE"], statistics.mean(mase), "equal-cell selection score")
        close(score["equal_cell_MAE_rpm_million"], statistics.mean(c["MAE_rpm_million"] for c in score["series_horizon_scores"]), "equal-cell MAE")
    check(result["selected_name"] == min(result["scorecard"][:3], key=lambda x: x["equal_cell_MASE"])["model"], "declared candidate choice")
    return {"checks_passed": checks, "native_interval_diagnostics": [interval_diagnostics(result["forecasts"], n["model"]) for n in result["scorecard"][:3]],
        "scope": "Independent original-source totals/release gates, augmented-design coefficients, all scalar-filter variance-grid likelihoods/states and point/quantile scores. Not causal, calibrated or full-engine validation."}


def main():
    root = ROOT / "data/monthly_traffic"
    identity = json.loads((root / "latest.json").read_text())["snapshot_id"]
    target = root / identity
    manifest = json.loads((target / "manifest.json").read_text()); artifact = json.loads((target / "artifact.json").read_text())
    if digest(artifact) != manifest["artifact_digest"]: raise ValueError("Traffic artifact checksum failed")
    progress = lambda event: print(json.dumps(event), file=sys.stderr, flush=True)
    raw = raw_targets(ROOT / "data/operating_history" / manifest["upstream_snapshot_id"], progress)
    result = {"snapshot_id": identity, "artifact_digest": manifest["artifact_digest"], **raw[2], **audit(artifact, raw, progress),
              "audit_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    folder = ROOT / "data/monthly_traffic_checks"; folder.mkdir(exist_ok=True)
    path = folder / (digest(result) + ".json")
    content = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if path.exists() and path.read_text() != content: raise ValueError("Immutable traffic audit differs")
    if not path.exists(): path.write_text(content)
    print(json.dumps({"audit_file": str(path), "snapshot_id": identity, "checks_passed": result["checks_passed"],
        "raw_segment_records": result["raw_segment_records"], "source_archives": result["source_archives"],
        "native_coverage": [{"model": d["model"], "coverage_80": d["equal_cell_coverage_80"]} for d in result["native_interval_diagnostics"]]}, indent=2))


if __name__ == "__main__": main()
