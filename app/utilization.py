"""Frozen-training quarterly utilization estimation and selection-period scores.

This same-schedule target avoids mixing passenger traffic and all-service costs.
It is an empirical utilization component, not an identified management policy.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re

import numpy as np

from .evidence import digest
from .reporting_panel import _read_snapshot, latest_snapshot, FORM41
from .source_store import ROOT, read_source

VERSION = "quarterly-utilization-0.1.0"
ESTIMATES = ROOT / "data/utilization"
SERIES = [(c, g) for c in ("UA", "DL", "AA") for g in ("narrowbody", "widebody")]
FEATURES = ["intercept", "lag_1_hpd", "lag_4_hpd"] + [f"{c}_{g}" for c, g in SERIES[1:]] + [f"{g}_Q{q}" for g in ("narrowbody", "widebody") for q in (2, 3, 4)]


def shift(period: str, amount: int) -> str:
    year, quarter = int(period[:4]), int(period[-1])
    if period != f"{year:04d}-Q{quarter}" or not 1 <= quarter <= 4:
        raise ValueError("Invalid utilization quarter")
    index = year * 4 + quarter - 1 + amount
    return f"{index // 4:04d}-Q{index % 4 + 1}"


def make_panel(rows: list[dict], classification: dict, last: str = "2023-Q4") -> dict:
    if last > "2023-Q4":
        raise ValueError("2024+ utilization targets are locked for selection")
    classes = {int(r["aircraft_type"]): r["fleet_group"] for r in classification["rows"]}
    if len(classes) != len(classification["rows"]) or any(g not in {"narrowbody", "widebody"} for g in classes.values()):
        raise ValueError("Invalid or duplicate aircraft classification")
    groups, invalid_carrier_quarters = defaultdict(list), set()
    grains = set()
    for row in rows:
        # Locked observations are excluded before accessing target values.
        if row["period"] > last or row["period"] < "2013-Q1" or row["unique_carrier"] not in {c for c, _ in SERIES}:
            continue
        shift(row["period"], 0)
        if row["schedule"] != "P52":
            raise ValueError("Utilization requires P52 observations")
        if row["reporting_region"] not in {"D", "A", "P", "L"} or row["aircraft_config"] not in {"1", "3"} or int(row["aircraft_type"]) == 999:
            continue
        code = int(row["aircraft_type"])
        key = (row["unique_carrier"], row["period"])
        if code not in classes:
            invalid_carrier_quarters.add(key)
            continue
        grain = (row["unique_carrier"], row["airline_id"], row["reporting_entity"], row["reporting_region"], row["period"], row["aircraft_type"], row["aircraft_config"], row["aircraft_group"])
        if grain in grains:
            raise ValueError("Duplicate aircraft utilization reporting grain")
        grains.add(grain)
        groups[(row["unique_carrier"], classes[code], row["period"])].append(row)
    observed, excluded = [], []
    for (carrier, group, period), source_rows in sorted(groups.items()):
        flags = []
        if (carrier, period) in invalid_carrier_quarters:
            flags.append("unmapped_aircraft_type_in_certificate_quarter")
        if len({r["airline_id"] for r in source_rows}) != 1 or any(r["airline_id"] is None for r in source_rows):
            flags.append("ambiguous_certificate_identity")
        quantities = [(r["values"]["TOTAL_AIR_HOURS"], r["values"]["AIR_DAYS_ASSIGN"]) for r in source_rows]
        if any(h is None or d is None for h, d in quantities):
            flags.append("missing_reported_hours_or_days")
        elif any(not math.isfinite(h) or not math.isfinite(d) or h < 0 or d < 0 for h, d in quantities):
            flags.append("invalid_reported_hours_or_days")
        hours = days = ratio = None
        if not flags:
            hours, days = sum(h for h, _ in quantities), sum(d for _, d in quantities)
            if days <= 0:
                flags.append("nonpositive_group_aircraft_days")
            else:
                ratio = hours / days
                if ratio > 24:
                    flags.append("group_airborne_hours_exceed_24_per_day")
        record = {"unique_carrier": carrier, "fleet_group": group, "period": period,
            "hours": hours, "aircraft_days": days, "utilization_hpd": ratio,
            "source_rows": source_rows, "quality_flags": flags}
        if flags:
            excluded.append(record)
        else:
            # Two-decimal thousand-unit reports resolve to 10 hours/days.
            # Bounds are quantization sensitivity, not statistical confidence.
            tolerance = 5 * len(source_rows)
            record["rounding_sensitivity_hpd"] = [max(0, hours - tolerance) / (days + tolerance),
                (hours + tolerance) / (days - tolerance) if days > tolerance else None]
            record["partition"] = "fit_2013_2019" if period <= "2019-Q4" else "selection_2020_2023"
            observed.append(record)
    return {"observations": observed, "excluded_group_quarters": excluded,
        "unmapped_certificate_quarters": [{"carrier": c, "period": p} for c, p in sorted(invalid_carrier_quarters)],
        "target_definition": "P52 reported airborne hours divided by aircraft days assigned, aggregated across detailed regions for passenger/combined configurations by certificate carrier and explicit NB/WB group. No FF or T100 traffic denominator; no regional attribution."}


def _history(observations: list[dict]) -> dict:
    result = {}
    for row in observations:
        key = (row["unique_carrier"], row["fleet_group"], row["period"])
        if key in result or not math.isfinite(row["utilization_hpd"]) or not 0 <= row["utilization_hpd"] <= 24:
            raise ValueError("Duplicate or invalid utilization target")
        result[key] = row["utilization_hpd"]
    return result


def _features(carrier: str, group: str, period: str, lag1: float, lag4: float) -> np.ndarray:
    if (carrier, group) not in SERIES:
        raise ValueError("Unsupported utilization series")
    return np.array([1., lag1, lag4] + [float((carrier, group) == s) for s in SERIES[1:]] +
        [float(group == g and int(period[-1]) == q) for g in ("narrowbody", "widebody") for q in (2, 3, 4)])


def fit(observations: list[dict], penalty: float) -> dict:
    if any(r["period"] > "2019-Q4" for r in observations):
        raise ValueError("Selection or locked targets cannot enter utilization fitting")
    if not math.isfinite(penalty) or penalty <= 0:
        raise ValueError("Ridge penalty must be finite and positive")
    history = _history(observations)
    design, targets, keys = [], [], []
    for (carrier, group, period), target in sorted(history.items()):
        lag1, lag4 = (history.get((carrier, group, shift(period, -lag))) for lag in (1, 4))
        if lag1 is None or lag4 is None:
            continue
        design.append(_features(carrier, group, period, lag1, lag4)); targets.append(target)
        keys.append({"carrier": carrier, "group": group, "period": period})
    counts = {f"{c}:{g}": sum(k["carrier"] == c and k["group"] == g for k in keys) for c, g in SERIES}
    if any(n < 8 for n in counts.values()):
        raise ValueError("At least eight eligible training targets per carrier/group are required")
    x, y = np.array(design), np.array(targets)
    center, scale = x.mean(axis=0), x.std(axis=0)
    center[0], scale[0] = 0., 1.
    scale[scale < 1e-12] = 1.
    z = (x - center) / scale
    shrink = np.eye(len(FEATURES)) * penalty; shrink[0, 0] = 0
    coefficients = np.linalg.solve(z.T @ z + shrink, z.T @ y)
    residual = y - z @ coefficients
    return {"version": VERSION, "ridge_penalty": penalty, "features": FEATURES,
        "feature_center": center.tolist(), "feature_scale": scale.tolist(), "coefficients": coefficients.tolist(),
        "training_target_keys": keys, "training_series_counts": counts,
        "training_target_digest": digest([{**k, "target": t} for k, t in zip(keys, targets)]),
        "training_residual_rmse_hpd": float(np.sqrt(np.mean(residual ** 2))),
        "fit_period": ["2013-Q1", "2019-Q4"], "uncertainty_status": "Point model; training residuals are not calibrated forecast intervals."}


def forecast(model: dict, history: dict, carrier: str, group: str, origin: str, horizon: int) -> list[float] | None:
    if horizon not in (1, 2, 4):
        raise ValueError("Utilization supports quarterly horizons 1, 2 and 4")
    # Copy only observations at/before origin. Later history cannot be read.
    state = {p: value for (c, g, p), value in history.items() if (c, g) == (carrier, group) and p <= origin}
    result = []
    for step in range(1, horizon + 1):
        period = shift(origin, step)
        lag1, lag4 = state.get(shift(period, -1)), state.get(shift(period, -4))
        if lag1 is None or lag4 is None:
            return None
        features = _features(carrier, group, period, lag1, lag4)
        z = (features - np.array(model["feature_center"])) / np.array(model["feature_scale"])
        value = float(np.clip(z @ np.array(model["coefficients"]), 0, 24))
        state[period] = value; result.append(value)
    return result


def _scores(predictions: list[dict], model_name: str) -> dict:
    rows = [r for r in predictions if r["model"] == model_name]
    series_horizon = defaultdict(list)
    for row in rows:
        series_horizon[(row["carrier"], row["group"], row["horizon_quarters"])].append(row)
    cells = []
    for (carrier, group, horizon), items in sorted(series_horizon.items()):
        errors = np.array([r["prediction_hpd"] - r["actual_hpd"] for r in items])
        cells.append({"carrier": carrier, "group": group, "horizon_quarters": horizon,
            "targets": len(items), "MAE_hpd": float(abs(errors).mean()),
            "RMSE_hpd": float(np.sqrt((errors ** 2).mean())), "mean_error_hpd": float(errors.mean())})
    return {"model": model_name, "series_horizon_scores": cells,
        "selection_equal_weight_MAE_hpd": float(np.mean([r["MAE_hpd"] for r in cells])),
        "prediction_count": len(rows)}


def select(observations: list[dict], spec: dict) -> dict:
    if spec["fit_last"] != "2019-Q4" or spec["selection_first"] != "2020-Q1" or spec["selection_last"] != "2023-Q4" or spec["horizons_quarters"] != [1, 2, 4]:
        raise ValueError("Utilization temporal design differs from frozen specification")
    if any(r["period"] > "2023-Q4" for r in observations):
        raise ValueError("2024+ utilization targets are locked")
    training = [r for r in observations if r["period"] <= "2019-Q4"]
    history = _history(observations)
    models = [fit(training, penalty) for penalty in spec["ridge_penalties"]]
    seasonal = defaultdict(list)
    for row in training:
        seasonal[(row["unique_carrier"], row["fleet_group"], int(row["period"][-1]))].append(row["utilization_hpd"])
    predictions, exclusions = [], []
    origin = "2019-Q4"
    while origin < "2023-Q4":
        for carrier, group in SERIES:
            for horizon in spec["horizons_quarters"]:
                target = shift(origin, horizon)
                if not "2020-Q1" <= target <= "2023-Q4":
                    continue
                actual = history.get((carrier, group, target))
                forecast_paths = [forecast(m, history, carrier, group, origin, horizon) for m in models]
                persistence = history.get((carrier, group, shift(target, -4)))
                # For horizon <=4 the seasonal reference is always known at origin.
                fixed = seasonal.get((carrier, group, int(target[-1])))
                key = {"carrier": carrier, "group": group, "origin": origin, "target": target, "horizon_quarters": horizon}
                if actual is None or any(p is None for p in forecast_paths) or persistence is None or not fixed:
                    exclusions.append({**key, "reason": "Missing target, recursive lag or common baseline input; no fallback/imputation"})
                    continue
                values = {f"ridge_{m['ridge_penalty']:g}": p[-1] for m, p in zip(models, forecast_paths)}
                values.update({"seasonal_persistence": persistence, "fixed_training_seasonal_mean": float(np.mean(fixed))})
                for name, value in values.items():
                    predictions.append({**key, "model": name, "prediction_hpd": value, "actual_hpd": actual,
                        "regime": spec["regimes"][target[:4]]})
        origin = shift(origin, 1)
    if not predictions:
        raise ValueError("No common selection-period forecasts")
    names = [f"ridge_{m['ridge_penalty']:g}" for m in models] + spec["baselines"]
    scorecard = [_scores(predictions, name) for name in names]
    if any(len(s["series_horizon_scores"]) != len(SERIES) * len(spec["horizons_quarters"]) for s in scorecard):
        raise ValueError("Selection must cover every carrier/group/horizon")
    winner = min(scorecard[:len(models)], key=lambda s: s["selection_equal_weight_MAE_hpd"])
    chosen = models[names.index(winner["model"])]
    baseline = min(scorecard[len(models):], key=lambda s: s["selection_equal_weight_MAE_hpd"])
    return {"selected_model": chosen, "selected_name": winner["model"], "scorecard": scorecard,
        "selection_predictions": predictions, "excluded_forecasts": exclusions,
        "beats_best_declared_baseline_on_selection": winner["selection_equal_weight_MAE_hpd"] < baseline["selection_equal_weight_MAE_hpd"],
        "best_declared_baseline": baseline["model"],
        "regime_scores": [{"regime": regime, "scores": [_scores([r for r in predictions if r["regime"] == regime], name) for name in names]} for regime in ("disruption", "recovery")],
        "locked_test_status": "2024+ targets excluded from estimation/selection; no locked forecast scores performed",
        "deployment_status": "Research component; live simulator integration and capacity/productivity reconciliation pending"}


def build_utilization(form41: Path, classification_path: Path, spec_path: Path, destination: Path = ESTIMATES) -> dict:
    upstream, panels = _read_snapshot(form41, "financial")
    classification = json.loads(classification_path.read_text()); spec = json.loads(spec_path.read_text())
    lookup, lookup_metadata = read_source(classification["type_lookup_sha256"])
    descriptions = {int(r["Code"]): r["Description"] for r in csv.DictReader(io.StringIO(lookup.decode("utf-8-sig")))}
    if any(descriptions.get(int(r["aircraft_type"])) != r["description"] for r in classification["rows"]):
        raise ValueError("Aircraft classification differs from the preserved official descriptions")
    for key in ("configuration_lookup_sha256", "group_lookup_sha256"):
        read_source(classification[key])
    code = Path(__file__).read_bytes()
    design = {"specification": spec, "classification": classification,
        "code_sha256": hashlib.sha256(code).hexdigest(), "numpy_version": np.__version__}
    design_id = digest(design)
    design_path = destination / "designs" / design_id
    design_path.mkdir(parents=True, exist_ok=True)
    # Persist the declared experiment before any selection target is scored.
    frozen = design_path / "design.json"
    if frozen.exists():
        declaration = json.loads(frozen.read_text())
        if declaration["design"] != design or declaration["design_id"] != design_id:
            raise ValueError("Immutable utilization design was modified")
    else:
        with frozen.open("x") as stream:
            json.dump({"design_id": design_id, "declared_at_utc": datetime.now(timezone.utc).isoformat(),
                "design": design, "status": "Local declaration persisted before selection scoring; not external preregistration"}, stream, indent=2)
    panel = make_panel(panels["P52.json"]["rows"], classification)
    result = select(panel["observations"], spec)
    artifact = {"panel": panel, "estimation": result, "classification": classification, "specification": spec}
    manifest = {"version": VERSION, "form41_snapshot_id": upstream["snapshot_id"],
        "design_id": design_id,
        "specification_digest": digest(spec), "classification_digest": digest(classification),
        "artifact_digest": digest(artifact), "code_sha256": hashlib.sha256(code).hexdigest(),
        "numpy_version": np.__version__, "type_lookup_acquired_at": lookup_metadata["retrieved_at_utc"],
        "vintage_status": "Current-download reconstruction, not historical point-in-time forecasts",
        "selected_name": result["selected_name"], "beats_baseline": result["beats_best_declared_baseline_on_selection"],
        "fit_targets": len(result["selected_model"]["training_target_keys"]),
        "selection_predictions_per_model": result["scorecard"][0]["prediction_count"],
        "excluded_group_quarters": len(panel["excluded_group_quarters"])}
    manifest["snapshot_id"] = digest(manifest)
    target = destination / manifest["snapshot_id"]; target.mkdir(parents=True, exist_ok=True)
    for name, payload in (("artifact.json", json.dumps(artifact, allow_nan=False, separators=(",", ":")).encode()),
                          ("manifest.json", json.dumps(manifest, allow_nan=False, indent=2).encode()), ("builder.py", code)):
        path = target / name
        if path.exists() and path.read_bytes() != payload:
            raise ValueError("Immutable utilization estimate was modified")
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(payload)
    pointer = destination / "latest.tmp"; pointer.write_text(json.dumps({"snapshot_id": manifest["snapshot_id"]}) + "\n"); pointer.replace(destination / "latest.json")
    return {**manifest, "path": str(target), "scorecard": result["scorecard"]}


def utilization_status(destination: Path = ESTIMATES) -> dict:
    if not (destination / "latest.json").exists():
        return {"status": "not_built", "message": "Fit utilization with scripts/fit_utilization.py"}
    identity = json.loads((destination / "latest.json").read_text())["snapshot_id"]
    if not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise ValueError("Invalid utilization snapshot ID")
    target = destination / identity
    manifest = json.loads((target / "manifest.json").read_text())
    if manifest.get("snapshot_id") != identity or digest({k: v for k, v in manifest.items() if k != "snapshot_id"}) != identity:
        raise ValueError("Utilization manifest checksum failed")
    artifact = json.loads((target / "artifact.json").read_text())
    if digest(artifact) != manifest["artifact_digest"] or hashlib.sha256((target / "builder.py").read_bytes()).hexdigest() != manifest["code_sha256"]:
        raise ValueError("Utilization artifact or preserved code checksum failed")
    frozen = json.loads((destination / "designs" / manifest["design_id"] / "design.json").read_text())
    if digest(frozen["design"]) != manifest["design_id"] or frozen["design_id"] != manifest["design_id"]:
        raise ValueError("Utilization design checksum failed")
    estimation = artifact["estimation"]
    selected_scores = next(s for s in estimation["scorecard"] if s["model"] == estimation["selected_name"])
    minimum_targets = min(c["targets"] for c in selected_scores["series_horizon_scores"])
    disruption_exclusions = sum("2020-Q1" <= r["period"] <= "2021-Q4" for r in artifact["panel"]["excluded_group_quarters"])
    return {"status": "available", **manifest, "declared_at_utc": frozen["declared_at_utc"],
        "target_definition": artifact["panel"]["target_definition"],
        "training_series_counts": estimation["selected_model"]["training_series_counts"],
        "scorecard": estimation["scorecard"], "regime_scores": estimation["regime_scores"],
        "excluded_forecast_count": len(estimation["excluded_forecasts"]),
        "minimum_selection_cell_targets": minimum_targets,
        "excluded_disruption_group_quarters": disruption_exclusions,
        "excluded_group_quarter_details": [{k: r[k] for k in ("unique_carrier", "fleet_group", "period", "quality_flags")} for r in artifact["panel"]["excluded_group_quarters"]],
        "locked_test_status": estimation["locked_test_status"], "deployment_status": estimation["deployment_status"],
        "limitations": [f"Selection-period comparison on common complete observations; missing or invalid reports exclude {disruption_exclusions} group-quarters during 2020-2021.",
            f"The sparsest series/horizon cell has {minimum_targets} resolved selection targets; the aggregate error does not establish reliability in every series/regime.",
            "Quarterly hours/day target does not identify spare-aircraft fractions, crew/hub feasibility, monthly utilization, ASM productivity or management optimization.",
            "No calibrated intervals, CRPS or locked 2024+ forecast scores yet. Simulator capacity remains assumed."]}
