"""Release-aware monthly served-traffic state estimation and temporal selection.

RPM is a capacity/price-censored demand proxy. It is not a causal demand estimate.
"""
from __future__ import annotations

from collections import Counter
import calendar
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

import duckdb
import numpy as np

from .evidence import digest
from .source_store import ROOT

VERSION = "monthly-traffic-0.1.0"
ESTIMATES = ROOT / "data/monthly_traffic"
CARRIERS = ("UA", "DL", "AA")
MARKETS = ("domestic", "atlantic", "pacific", "latin_america")
SERIES = tuple((c, m) for c in CARRIERS for m in MARKETS)
HORIZONS = (1, 3, 6, 12)
FEATURES = [f"{c}:{m}" for c, m in SERIES] + [f"{m}:month{n}" for m in MARKETS for n in range(2, 13)]
BASELINES = ("released_seasonal_persistence", "fixed_initial_seasonal_mean", "expanding_regularized_log_ar")


def shift(period, amount):
    if not isinstance(period, str) or not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", period):
        raise ValueError("Invalid traffic month")
    index = int(period[:4]) * 12 + int(period[5:]) - 1 + amount
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_end(period):
    shift(period, 0)
    return f"{period}-{calendar.monthrange(int(period[:4]), int(period[5:]))[1]:02d}"


def periods(first, last):
    result = []
    while first <= last:
        result.append(first)
        first = shift(first, 1)
    return result


def check_spec(spec):
    expected = {"carriers": list(CARRIERS), "markets": list(MARKETS), "initial_first": "2013-01",
        "initial_last": "2019-12", "selection_first": "2020-01", "selection_last": "2023-12",
        "locked_first": "2024-01", "horizons_months": list(HORIZONS), "pooling_correlations": [0., .5, .9],
        "process_variance_grid": [.0001, .001, .01, .1, 1.], "measurement_variance_grid": [.0001, .001, .01, .1],
        "seasonal_ridge": 1., "autoregression_ridge": 10., "minimum_initial_months_per_series": 60,
        "baselines": list(BASELINES)}
    if any(spec.get(k) != v for k, v in expected.items()):
        raise ValueError("Traffic design differs from the declared temporal specification")


def observations(rows, selected_releases=None):
    result, excluded, seen = [], [], set()
    releases = {}
    for release in selected_releases or []:
        if not "2013-01" <= release["period"] <= "2023-12":
            continue
        key = release["scope"], release["period"]
        if key in releases or key[0] not in ("DD", "FD") or date.fromisoformat(release["published_on"]).isoformat() != release["published_on"]:
            raise ValueError("Invalid or duplicate traffic scope release")
        releases[key] = release
    for row in rows:
        period = row["period"]
        # Never even access quantities or dates for locked targets.
        if period < "2013-01" or period > "2023-12" or row["carrier"] not in CARRIERS:
            continue
        shift(period, 0)
        if row["market"] not in MARKETS:
            excluded.append({"carrier": row["carrier"], "market": row["market"], "period": period, "reason": "outside declared market crosswalk"})
            continue
        key = row["carrier"], row["market"], period
        if key in seen:
            raise ValueError("Duplicate traffic carrier-market-month")
        seen.add(key)
        published = row["published_on"]
        if date.fromisoformat(published).isoformat() != published or published < month_end(period):
            raise ValueError("Traffic release precedes completed observation month")
        if not row["both_archive_scopes_available"] or row["quality_flags"]:
            excluded.append({"carrier": key[0], "market": key[1], "period": period, "reason": "source scope/quality gate", "quality_flags": row["quality_flags"]})
            continue
        scope_releases = []
        if selected_releases is not None:
            if any((scope, period) not in releases for scope in ("DD", "FD")):
                excluded.append({"carrier": key[0], "market": key[1], "period": period, "reason": "scope release metadata missing"})
                continue
            scope_releases = [releases[scope, period] for scope in ("DD", "FD")]
        complete_published = max([published, *(r["published_on"] for r in scope_releases)])
        rpm, asm = row["rpm"], row["asm"]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in (rpm, asm)) or rpm > asm:
            raise ValueError("Traffic RPM/ASM physical identity differs")
        if rpm == 0 or asm == 0:
            excluded.append({"carrier": key[0], "market": key[1], "period": period, "reason": "nonpositive log target; not imputed"})
            continue
        result.append({"carrier": key[0], "market": key[1], "period": period, "rpm_million": rpm / 1e6,
            "asm_million": asm / 1e6, "published_on": complete_published, "contributor_published_on": published,
            "scope_releases": scope_releases, "source_sha256s": row["source_sha256s"], "carrier_entities": row["carrier_entities"]})
    return {"observations": sorted(result, key=lambda r: (r["period"], r["carrier"], r["market"])), "excluded_rows": excluded}


def history(rows, cutoff):
    date.fromisoformat(cutoff)
    result = {}
    for row in rows:
        # Cutoff before target access also allows deliberately poisoned witnesses.
        if row["period"] > cutoff[:7] or row["published_on"] > cutoff:
            continue
        key = row["carrier"], row["market"], row["period"]
        if key in result or key[:2] not in SERIES or row["rpm_million"] <= 0 or not math.isfinite(row["rpm_million"]):
            raise ValueError("Duplicate or invalid released traffic observation")
        result[key] = row
    return result


def features(carrier, market, period):
    return np.array([float((carrier, market) == s) for s in SERIES] +
        [float(market == m and int(period[5:]) == n) for m in MARKETS for n in range(2, 13)])


def regress(known, penalty, autoregression=False):
    keys = sorted(k for k in known if not autoregression or all((*k[:2], shift(k[2], -lag)) in known for lag in (1, 12)))
    counts = Counter(k[:2] for k in keys)
    if any(counts[s] < 24 for s in SERIES):
        raise ValueError("At least 24 released training targets per series are required")
    x = np.array([np.concatenate((features(*k), [math.log(known[*k[:2], shift(k[2], -lag)]["rpm_million"]) for lag in (1, 12)]))
        if autoregression else features(*k) for k in keys])
    y = np.array([math.log(known[k]["rpm_million"]) for k in keys])
    # Unpenalized certificate-market offsets; market seasonality is shared across
    # carriers. No cross-table financial response coefficient enters this fit.
    shrink = np.eye(x.shape[1]) * penalty
    shrink[:len(SERIES), :len(SERIES)] = 0.
    beta = np.linalg.solve(x.T @ x + shrink, x.T @ y)
    return {"coefficients": beta.tolist(), "training_keys": [list(k) for k in keys],
            "training_digest": digest([{**known[k]} for k in keys]), "target_count": len(keys),
            "residual_rmse_log": float(np.sqrt(np.mean((y - x @ beta) ** 2)))}


def process_covariance(q, rho):
    if not math.isfinite(q) or q <= 0 or not math.isfinite(rho) or not 0 <= rho < 1:
        raise ValueError("Invalid traffic process covariance")
    shared = np.array([[float(a[1] == b[1]) for b in SERIES] for a in SERIES])
    return q * ((1 - rho) * np.eye(len(SERIES)) + rho * shared)


def kalman_step(mean, covariance, process, measured, measurement_variance):
    """Predict, then update just the released dimensions; Joseph covariance form."""
    if measurement_variance <= 0 or not math.isfinite(measurement_variance):
        raise ValueError("Invalid traffic measurement variance")
    prior = covariance + process
    indices = np.array(sorted(measured), dtype=int)
    if not len(indices):
        return mean.copy(), prior, 0.
    residual = np.array([measured[int(i)] for i in indices]) - mean[indices]
    innovation = prior[np.ix_(indices, indices)] + np.eye(len(indices)) * measurement_variance
    chol = np.linalg.cholesky(innovation)
    solved = np.linalg.solve(chol, residual)
    loglike = -.5 * (len(indices) * math.log(2 * math.pi) + 2 * np.log(np.diag(chol)).sum() + solved @ solved)
    gain = np.linalg.solve(innovation, prior[:, indices].T).T
    updated = mean + gain @ residual
    complement = np.eye(len(mean)); complement[:, indices] -= gain
    posterior = complement @ prior @ complement.T + measurement_variance * gain @ gain.T
    return updated, (posterior + posterior.T) / 2, float(loglike)


def measurement_sequence(known, seasonal, last):
    beta = np.asarray(seasonal["coefficients"])
    return [{i: math.log(known[c, m, period]["rpm_million"]) - features(c, m, period) @ beta
        for i, (c, m) in enumerate(SERIES) if (c, m, period) in known} for period in periods("2013-01", last)]


def filter_state(known, seasonal, q, r, rho, last, sequence=None):
    q_matrix = process_covariance(q, rho)
    mean, covariance, likelihood = np.zeros(len(SERIES)), np.eye(len(SERIES)), 0.
    for measured in measurement_sequence(known, seasonal, last) if sequence is None else sequence:
        mean, covariance, contribution = kalman_step(mean, covariance, q_matrix, measured, r)
        likelihood += contribution
    return mean, covariance, likelihood


def fit_state(known, seasonal, rho, spec, last, sequence=None):
    choices = []
    sequence = measurement_sequence(known, seasonal, last) if sequence is None else sequence
    for q in spec["process_variance_grid"]:
        for r in spec["measurement_variance_grid"]:
            mean, covariance, ll = filter_state(known, seasonal, q, r, rho, last, sequence)
            choices.append((ll, q, r, mean, covariance))
    # Strict > preserves declared grid order on exact ties.
    best = choices[0]
    for choice in choices[1:]:
        if choice[0] > best[0]:
            best = choice
    ll, q, r, mean, covariance = best
    return {"rho": rho, "q": q, "r": r, "innovation_loglikelihood": ll,
            "grid_likelihoods": [{"q": x[1], "r": x[2], "loglikelihood": x[0]} for x in choices],
            "mean": mean.tolist(), "covariance": covariance.tolist(), "filtered_through": last,
            "variance_grid_boundary": q in (spec["process_variance_grid"][0], spec["process_variance_grid"][-1]) or r in (spec["measurement_variance_grid"][0], spec["measurement_variance_grid"][-1])}


def prediction(state, seasonal, carrier, market, target, horizon):
    i = SERIES.index((carrier, market))
    mu = float(features(carrier, market, target) @ np.asarray(seasonal["coefficients"]) + state["mean"][i])
    variance = state["covariance"][i][i] + horizon * state["q"] + state["r"]
    if not math.isfinite(mu) or not math.isfinite(variance) or variance <= 0:
        raise ValueError("Invalid traffic predictive distribution")
    sigma = math.sqrt(variance)
    z90 = 1.2815515655446004
    return {"log_mean": mu, "log_variance": variance, "p10": math.exp(mu - z90 * sigma),
            "p50": math.exp(mu), "p90": math.exp(mu + z90 * sigma)}


def ar_predict(model, known, carrier, market, target):
    state = {p: math.log(row["rpm_million"]) for (c, m, p), row in known.items() if (c, m) == (carrier, market)}
    latest = max(state)
    beta = np.asarray(model["coefficients"])
    for period in periods(shift(latest, 1), target):
        required = [shift(period, -lag) for lag in (1, 12)]
        if any(p not in state for p in required):
            raise ValueError("AR release-gap state is incomplete")
        state[period] = float(np.concatenate((features(carrier, market, period), [state[p] for p in required])) @ beta)
        if not math.isfinite(state[period]) or abs(state[period]) > 40:
            raise ValueError("AR recursive forecast is numerically unsupported")
    return math.exp(state[target])


def score(rows, name):
    cells = []
    for carrier, market in SERIES:
        for horizon in HORIZONS:
            values = [r for r in rows if (r["carrier"], r["market"], r["horizon_months"]) == (carrier, market, horizon)]
            if not values:
                raise ValueError("No common traffic selection targets in a declared cell")
            errors = np.array([r["predictions"][name] - r["actual_rpm_million"] for r in values])
            scales = np.array([r["initial_mase_scale"] for r in values])
            cells.append({"carrier": carrier, "market": market, "horizon_months": horizon, "targets": len(values),
                "MAE_rpm_million": float(np.abs(errors).mean()), "RMSE_rpm_million": float(np.sqrt((errors ** 2).mean())),
                "mean_error_rpm_million": float(errors.mean()), "MASE": float((np.abs(errors) / scales).mean())})
    return {"model": name, "prediction_count": len(rows), "equal_cell_MASE": float(np.mean([c["MASE"] for c in cells])),
            "equal_cell_MAE_rpm_million": float(np.mean([c["MAE_rpm_million"] for c in cells])), "series_horizon_scores": cells}


def select(rows, spec, progress=None):
    check_spec(spec)
    initial = [r for r in rows if r["period"] <= "2019-12"]
    counts = Counter((r["carrier"], r["market"]) for r in initial)
    if any(counts[s] < spec["minimum_initial_months_per_series"] for s in SERIES):
        raise ValueError("Initial traffic sample is insufficient in a declared series")
    first_release = max(r["published_on"] for r in initial)
    initial_known = history(initial, first_release)
    fixed, scales = {}, {}
    for c, m in SERIES:
        for month in range(1, 13):
            values = [r["rpm_million"] for r in initial if (r["carrier"], r["market"]) == (c, m) and int(r["period"][5:]) == month]
            if not values:
                raise ValueError("Initial seasonal baseline month missing")
            fixed[c, m, month] = float(np.mean(values))
        differences = [abs(r["rpm_million"] - initial_known[c, m, shift(r["period"], -12)]["rpm_million"])
            for r in initial if (r["carrier"], r["market"]) == (c, m) and (c, m, shift(r["period"], -12)) in initial_known]
        scales[c, m] = float(np.mean(differences))
        if not math.isfinite(scales[c, m]) or scales[c, m] <= 0:
            raise ValueError("Initial MASE scale is undefined")
    target_map = {(r["carrier"], r["market"], r["period"]): r for r in rows}
    names = [f"state_rho_{rho:g}" for rho in spec["pooling_correlations"]]
    forecasts, excluded, fits = [], [], []
    for origin in periods("2020-01", "2023-11"):
        cutoff = month_end(origin)
        if cutoff < first_release:
            excluded.append({"origin": origin, "reason": "initial sample not yet fully released", "available_on": first_release})
            continue
        known = history(rows, cutoff)
        seasonal = regress(known, spec["seasonal_ridge"])
        ar = regress(known, spec["autoregression_ridge"], True)
        sequence = measurement_sequence(known, seasonal, origin)
        states = [fit_state(known, seasonal, rho, spec, origin, sequence) for rho in spec["pooling_correlations"]]
        fit_index = len(fits)
        fits.append({"origin": origin, "cutoff": cutoff, "known_keys": [list(k) for k in sorted(known)],
            "max_known_publication": max(r["published_on"] for r in known.values()),
            "latest_period_by_series": {f"{c}:{m}": max(k[2] for k in known if k[:2] == (c, m)) for c, m in SERIES},
            "seasonal": seasonal, "autoregression": ar, "states": states})
        for c, m in SERIES:
            for h in HORIZONS:
                target = shift(origin, h)
                if target > "2023-12":
                    continue
                actual = target_map.get((c, m, target))
                if actual is None:
                    excluded.append({"origin": origin, "carrier": c, "market": m, "horizon_months": h, "target": target, "reason": "target source excluded or missing"})
                    continue
                seasonal_periods = [p for carrier, market, p in known if (carrier, market) == (c, m) and p[5:] == target[5:]]
                if not seasonal_periods:
                    excluded.append({"origin": origin, "carrier": c, "market": m, "horizon_months": h, "target": target, "reason": "released seasonal baseline missing"})
                    continue
                try:
                    regularized = ar_predict(ar, known, c, m, target)
                except ValueError as error:
                    excluded.append({"origin": origin, "carrier": c, "market": m, "horizon_months": h, "target": target, "reason": str(error)})
                    continue
                distributions = {name: prediction(state, seasonal, c, m, target, h) for name, state in zip(names, states)}
                persistence = max(seasonal_periods)
                forecasts.append({"origin": origin, "cutoff": cutoff, "target": target, "carrier": c, "market": m, "horizon_months": h,
                    "actual_rpm_million": actual["rpm_million"], "actual_published_on": actual["published_on"],
                    "actual_source_sha256s": actual["source_sha256s"], "fit_index": fit_index,
                    "initial_mase_scale": scales[c, m], "persistence_source_period": persistence,
                    "persistence_published_on": known[c, m, persistence]["published_on"],
                    "predictions": {**{n: d["p50"] for n, d in distributions.items()},
                        BASELINES[0]: known[c, m, persistence]["rpm_million"], BASELINES[1]: fixed[c, m, int(target[5:])], BASELINES[2]: regularized},
                    "distributions": distributions, "regime": "disruption" if target[:4] in ("2020", "2021") else "recovery"})
        if progress is not None:
            progress({"phase": "origin_complete", "origin": origin, "released_training_targets": len(known), "common_forecasts": len(forecasts)})
    scores = [score(forecasts, n) for n in (*names, *BASELINES)]
    selected = min(scores[:len(names)], key=lambda s: s["equal_cell_MASE"])
    return {"first_initial_release": first_release, "initial_series_counts": {f"{c}:{m}": counts[c, m] for c, m in SERIES},
        "initial_mase_scales": {f"{c}:{m}": scales[c, m] for c, m in SERIES}, "fits": fits, "forecasts": forecasts,
        "excluded_forecasts": excluded, "scorecard": scores, "selected_name": selected["model"],
        "beats_best_declared_baseline": selected["equal_cell_MASE"] < min(s["equal_cell_MASE"] for s in scores[len(names):]),
        "regime_scorecards": [{"regime": regime, "scores": [score([r for r in forecasts if r["regime"] == regime], n) for n in (*names, *BASELINES)]} for regime in ("disruption", "recovery")],
        "locked_status": "2024+ quantities excluded before observation access; no locked forecast scoring",
        "deployment_status": "Served-traffic demand-proxy component; fare/yield, unconstrained demand, causal policy, calibration and monthly simulator integration pending"}


def _read_upstream(path):
    manifest = json.loads((path / "manifest.json").read_text())
    panel = json.loads((path / "panel.json").read_text())
    if digest({k: v for k, v in manifest.items() if k != "snapshot_id"}) != manifest["snapshot_id"] or digest(panel) != manifest["panel_digest"]:
        raise ValueError("Traffic upstream manifest/panel checksum failed")
    return manifest, panel


def _immutable(path, content):
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError("Immutable traffic artifact differs")
    else:
        with path.open("xb") as stream:
            stream.write(content)


def _verify_parquet(path, rows):
    connection = duckdb.connect()
    try:
        restored = [json.loads(row[0]) for row in connection.execute("SELECT to_json(row) FROM read_parquet(?)", [str(path)]).fetchall()]
    finally:
        connection.close()
    if restored != rows:
        raise ValueError("Traffic Parquet readback differs from its JSON rows")


def build_traffic(upstream, spec_path, destination=ESTIMATES, progress=None):
    spec = json.loads(Path(spec_path).read_text()); check_spec(spec)
    manifest, panel = _read_upstream(Path(upstream))
    if manifest["snapshot_id"] != spec["source_snapshot_id"]:
        raise ValueError("Traffic upstream differs from declared source snapshot")
    code = Path(__file__).read_bytes()
    design = {"specification": spec, "builder_code_sha256": hashlib.sha256(code).hexdigest(),
              "numpy_version": np.__version__, "duckdb_version": duckdb.__version__}
    design_id = digest(design)
    root = Path(destination); declared = root / "designs" / design_id
    declared.mkdir(parents=True, exist_ok=True)
    design_path = declared / "design.json"
    if design_path.exists():
        original = json.loads(design_path.read_text())
        if original["design"] != design or original["design_id"] != design_id:
            raise ValueError("Immutable traffic design differs")
    else:
        _immutable(design_path, (json.dumps({"design_id": design_id, "design": design,
            "declared_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "Local declaration saved before fitting; not external preregistration"}, indent=2) + "\n").encode())
    if "selected_releases" not in panel:
        raise ValueError("Traffic source lacks complete scope-release metadata")
    prepared = observations(panel["rows"], panel["selected_releases"])
    fitted = select(prepared["observations"], spec, progress)
    artifact = {"version": VERSION, "panel": prepared, "estimation": fitted, "specification": spec}
    output = {"version": VERSION, "upstream_snapshot_id": manifest["snapshot_id"], "design_id": design_id,
        "artifact_digest": digest(artifact), "builder_code_sha256": hashlib.sha256(code).hexdigest(),
        "numpy_version": np.__version__, "duckdb_version": duckdb.__version__, "vintage_status": spec["vintage"],
        "selected_name": fitted["selected_name"], "common_predictions_per_model": len(fitted["forecasts"]),
        "first_initial_release": fitted["first_initial_release"]}
    output["snapshot_id"] = digest(output)
    target = root / output["snapshot_id"]; target.mkdir(parents=True, exist_ok=True)
    for name, data in (("artifact.json", artifact), ("manifest.json", output)):
        _immutable(target / name, (json.dumps(data, separators=(",", ":"), allow_nan=False) + "\n").encode())
    _immutable(target / "builder.py", code)
    for name, rows in (("observations", prepared["observations"]), ("forecasts", fitted["forecasts"])):
        rows_path = target / (name + ".json")
        _immutable(rows_path, (json.dumps({"rows": rows}, separators=(",", ":"), allow_nan=False) + "\n").encode())
        parquet = target / (name + ".parquet")
        if not parquet.exists():
            connection = duckdb.connect()
            try:
                escaped = str(parquet).replace("'", "''")
                connection.execute(f"COPY (SELECT unnest(rows) AS row FROM read_json_auto(?)) TO '{escaped}' (FORMAT PARQUET)", [str(rows_path)])
            finally:
                connection.close()
        _verify_parquet(parquet, rows)
    if Path(__file__).read_bytes() != code:
        raise ValueError("Traffic source changed during fitting")
    pointer = root / "latest.tmp"; pointer.write_text(json.dumps({"snapshot_id": output["snapshot_id"]}) + "\n")
    pointer.replace(root / "latest.json")
    return {**output, "path": str(target), "scorecard": fitted["scorecard"]}


def traffic_status(destination=ESTIMATES):
    root = Path(destination)
    if not (root / "latest.json").exists():
        return {"status": "not_built", "message": "Fit release-aware monthly traffic with scripts/fit_monthly_traffic.py"}
    identity = json.loads((root / "latest.json").read_text())["snapshot_id"]
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise ValueError("Invalid traffic snapshot identifier")
    target = root / identity
    manifest = json.loads((target / "manifest.json").read_text())
    artifact = json.loads((target / "artifact.json").read_text())
    declaration = json.loads((root / "designs" / manifest["design_id"] / "design.json").read_text())
    if digest({k: v for k, v in manifest.items() if k != "snapshot_id"}) != identity or manifest.get("snapshot_id") != identity or digest(artifact) != manifest["artifact_digest"]:
        raise ValueError("Traffic artifact/manifest checksum failed")
    if hashlib.sha256((target / "builder.py").read_bytes()).hexdigest() != manifest["builder_code_sha256"] or declaration["design_id"] != manifest["design_id"] or digest(declaration["design"]) != manifest["design_id"]:
        raise ValueError("Traffic preserved source/design checksum failed")
    for name, rows in (("observations", artifact["panel"]["observations"]), ("forecasts", artifact["estimation"]["forecasts"])):
        if json.loads((target / (name + ".json")).read_text()) != {"rows": rows}:
            raise ValueError("Traffic row JSON checksum failed")
        _verify_parquet(target / (name + ".parquet"), rows)
    result = artifact["estimation"]
    lags = [int(f["origin"][:4]) * 12 + int(f["origin"][5:]) - int(p[:4]) * 12 - int(p[5:])
        for f in result["fits"] for p in f["latest_period_by_series"].values()]
    return {"status": "available", **manifest, "declared_at_utc": declaration["declared_at_utc"],
        "scope": artifact["specification"]["scope"], "target_definition": artifact["specification"]["target"],
        "scorecard": result["scorecard"], "regime_scorecards": result["regime_scorecards"],
        "initial_series_counts": result["initial_series_counts"], "excluded_forecasts": len(result["excluded_forecasts"]),
        "information_lag_months": {"minimum": min(lags), "maximum": max(lags)},
        "beats_best_declared_baseline": result["beats_best_declared_baseline"],
        "locked_status": result["locked_status"], "deployment_status": result["deployment_status"],
        "variance_boundary_fits": sum(s["variance_grid_boundary"] for f in result["fits"] for s in f["states"]),
        "limitations": ["Served scheduled-passenger certificate traffic is a demand proxy; no fare/yield, unconstrained demand, regional consolidation or causal elasticity.",
            "Actual selected-release dates are respected. Long annual-backfill gaps and unavailable intermediate vintages prevent point-in-time performance claims.",
            "Native lognormal intervals condition on fitted parameters and are uncalibrated; simulator integration and parameter uncertainty remain pending.",
            "Selection-period overlapping forecasts; 2024+ targets remain locked."]}
