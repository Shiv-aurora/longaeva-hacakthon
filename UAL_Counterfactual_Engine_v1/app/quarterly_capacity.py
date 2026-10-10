"""Frozen-training T2 covered-aircraft capacity models and temporal selection.

These quarterly point models do not identify fleet availability or management policy.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np

from .evidence import digest
from .source_store import ROOT

VERSION = "quarterly-capacity-0.1.0"
ESTIMATES = ROOT / "data/quarterly_capacity"
SERIES = [(c, g) for c in ("UA", "DL", "AA") for g in ("narrowbody", "widebody")]
COMPONENTS = {"capacity": "capacity_asm_per_assigned_day", "utilization": "reported_hours_per_assigned_day", "productivity": "asm_per_reported_hour"}
ARCHITECTURES = ("direct_capacity", "utilization_times_productivity")
FEATURES = ["intercept", "target_lag_1", "target_lag_4"] + [f"{c}_{g}" for c, g in SERIES[1:]] + [f"{g}_Q{q}" for g in ("narrowbody", "widebody") for q in (2, 3, 4)]


def shift(period, amount):
    if not isinstance(period, str) or not re.fullmatch(r"\d{4}-Q[1-4]", period): raise ValueError("Invalid capacity quarter")
    index = int(period[:4])*4 + int(period[-1])-1 + amount
    return f"{index//4:04d}-Q{index%4+1}"


def check_spec(spec):
    expected = {"carriers": ["UA", "DL", "AA"], "groups": ["narrowbody", "widebody"],
        "fit_first": "2013-Q1", "fit_last": "2019-Q4", "selection_first": "2020-Q1", "selection_last": "2023-Q4",
        "locked_first": "2024-Q1", "horizons_quarters": [1, 2, 4], "architectures": list(ARCHITECTURES),
        "ridge_penalties": [.1, 1., 10., 100.], "component_targets": COMPONENTS,
        "baselines": ["seasonal_capacity_persistence", "fixed_training_seasonal_capacity_mean"],
        "regimes": {"2020": "disruption", "2021": "disruption", "2022": "recovery", "2023": "recovery"}}
    if any(spec.get(k) != v for k, v in expected.items()): raise ValueError("Capacity design differs from the declared temporal specification")


def make_observations(rows):
    observed, exclusions, seen = [], [], set()
    for row in rows:
        # Input accounting may include 2024+, but model selection never accesses
        # those target fields. Even a locked row without quantities is ignored.
        period = row["period"]
        if period < "2013-Q1" or period > "2023-Q4": continue
        shift(period, 0)
        carrier, group = row["unique_carrier"], row["fleet_group"]
        if (carrier, group) not in SERIES: continue
        key = carrier, group, period
        if key in seen: raise ValueError("Duplicate capacity group-quarter")
        seen.add(key)
        values = {c: row["targets"][field] for c, field in COMPONENTS.items()}
        reasons = {c: row["target_exclusions"].get(field, []) for c, field in COMPONENTS.items()}
        if any(v is None for v in values.values()):
            exclusions.append({"carrier": carrier, "group": group, "period": period, "target_exclusions": reasons})
            continue
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values.values()) or values["utilization"] > 24:
            raise ValueError("Invalid eligible capacity component")
        if not math.isclose(values["capacity"], values["utilization"] * values["productivity"], rel_tol=1e-12, abs_tol=1e-8):
            raise ValueError("Capacity decomposition identity differs")
        observed.append({"unique_carrier": carrier, "fleet_group": group, "period": period, "values": values,
            "source_lines": row["source_lines"], "source_sha256s": row["source_sha256s"],
            "P52_exposure_reconciled": row["P52_exposure_reconciled"], "P52_rows_without_T2": row["P52_rows_without_T2"]})
    return {"observations": observed, "excluded_group_quarters": exclusions,
            "scope": "Same-table T2 covered-aircraft quarterly ratios; P52 exposure discrepancies retained. No owned-fleet or monthly target inference."}


def _history(observations):
    result = {}
    for r in observations:
        key = r["unique_carrier"], r["fleet_group"], r["period"]
        shift(key[2], 0)
        if key[:2] not in SERIES or key in result: raise ValueError("Duplicate or unsupported capacity series")
        values = r["values"]
        if set(values) != set(COMPONENTS) or any(isinstance(v, bool) or not math.isfinite(v) or v < 0 for v in values.values()) or values["utilization"] > 24:
            raise ValueError("Invalid capacity history")
        if not math.isclose(values["capacity"], values["utilization"]*values["productivity"], rel_tol=1e-12, abs_tol=1e-8): raise ValueError("Capacity history decomposition identity differs")
        result[key] = values
    return result


def _features(carrier, group, period, lag1, lag4):
    if (carrier, group) not in SERIES: raise ValueError("Unsupported capacity series")
    return np.array([1., lag1, lag4] + [float((carrier, group)==s) for s in SERIES[1:]] +
        [float(group==g and int(period[-1])==q) for g in ("narrowbody", "widebody") for q in (2, 3, 4)])


def fit_components(observations, penalty):
    if any(not "2013-Q1" <= r["period"] <= "2019-Q4" for r in observations): raise ValueError("Selection/locked targets cannot enter capacity fitting")
    if isinstance(penalty, bool) or not math.isfinite(penalty) or penalty <= 0: raise ValueError("Capacity ridge penalty must be positive and finite")
    history = _history(observations)
    keys = [k for k in sorted(history) if (*k[:2], shift(k[2], -1)) in history and (*k[:2], shift(k[2], -4)) in history]
    counts = {f"{c}:{g}": sum(k[:2]==(c,g) for k in keys) for c,g in SERIES}
    if any(n < 8 for n in counts.values()): raise ValueError("At least eight common training targets per carrier/group are required")
    models = {}
    for component in COMPONENTS:
        x = np.array([_features(c,g,p,history[c,g,shift(p,-1)][component],history[c,g,shift(p,-4)][component]) for c,g,p in keys])
        y = np.array([history[k][component] for k in keys])
        center, scale = x.mean(axis=0), x.std(axis=0)
        center[0], scale[0] = 0., 1.; scale[scale<1e-12] = 1.
        z = (x-center)/scale
        ridge = np.eye(len(FEATURES))*penalty; ridge[0,0] = 0.
        beta = np.linalg.solve(z.T@z + ridge, z.T@y)
        models[component] = {"component": component, "features": FEATURES, "feature_center": center.tolist(), "feature_scale": scale.tolist(),
            "coefficients": beta.tolist(), "training_residual_RMSE": float(np.sqrt(np.mean((y-z@beta)**2)))}
    return {"version": VERSION, "ridge_penalty": penalty, "components": models,
        "training_target_keys": [{"carrier":c,"group":g,"period":p} for c,g,p in keys],
        "training_target_digest": digest([{"carrier":c,"group":g,"period":p,"values":history[c,g,p]} for c,g,p in keys]),
        "training_series_counts": counts, "fit_period": ["2013-Q1","2019-Q4"],
        "uncertainty_status": "Point regressions; training residuals are not calibrated intervals"}


def forecast(fitted, architecture, history, carrier, group, origin, horizon):
    if architecture not in ARCHITECTURES or horizon not in (1,2,4): raise ValueError("Unsupported capacity architecture or quarterly horizon")
    if (carrier,group) not in SERIES: raise ValueError("Unsupported capacity series")
    shift(origin,0)
    state = {p: dict(v) for (c,g,p),v in history.items() if (c,g)==(carrier,group) and p<=origin}
    required = ["capacity"] if architecture=="direct_capacity" else ["utilization","productivity"]
    result = []
    for step in range(1,horizon+1):
        period = shift(origin,step)
        a,b = state.get(shift(period,-1)), state.get(shift(period,-4))
        if a is None or b is None: return None
        prediction = {}
        for component in required:
            m = fitted["components"][component]
            z = (_features(carrier,group,period,a[component],b[component])-np.array(m["feature_center"]))/np.array(m["feature_scale"])
            v = float(z@np.array(m["coefficients"]))
            if not math.isfinite(v): raise ValueError("Nonfinite capacity prediction")
            prediction[component] = float(np.clip(v,0,24)) if component=="utilization" else max(0.,v)
        if architecture=="utilization_times_productivity": prediction["capacity"] = prediction["utilization"]*prediction["productivity"]
        state[period] = prediction
        result.append({"period":period, "capacity_asm_per_assigned_day":prediction["capacity"],
            "reported_hours_per_assigned_day":prediction.get("utilization"), "asm_per_reported_hour":prediction.get("productivity")})
    return result


def scores(rows, name):
    cells = defaultdict(list)
    for r in rows:
        if r["model"]==name: cells[r["carrier"],r["group"],r["horizon_quarters"]].append(r)
    output = []
    for (c,g,h),items in sorted(cells.items()):
        errors = np.array([r["prediction_asm_per_day"]-r["actual_asm_per_day"] for r in items])
        output.append({"carrier":c,"group":g,"horizon_quarters":h,"targets":len(items),
            "MAE_asm_per_day":float(abs(errors).mean()),"RMSE_asm_per_day":float(np.sqrt(np.mean(errors**2))),"mean_error_asm_per_day":float(errors.mean())})
    return {"model":name,"series_horizon_scores":output,"selection_equal_weight_MAE_asm_per_day":float(np.mean([r["MAE_asm_per_day"] for r in output])) if output else None,
        "prediction_count":sum(r["targets"] for r in output),"series_horizon_cells":len(output)}


def select(observations, spec):
    check_spec(spec)
    if any(r["period"]>"2023-Q4" for r in observations): raise ValueError("2024+ capacity targets are locked")
    history = _history(observations)
    training = [r for r in observations if r["period"]<="2019-Q4"]
    fits = [fit_components(training,p) for p in spec["ridge_penalties"]]
    candidates = [(a,f,f"{a}_ridge_{f['ridge_penalty']:g}") for a in ARCHITECTURES for f in fits]
    seasonal = defaultdict(list)
    for r in training: seasonal[r["unique_carrier"],r["fleet_group"],int(r["period"][-1])].append(r["values"]["capacity"])
    predictions, exclusions = [], []
    origin = "2019-Q4"
    while origin<"2023-Q4":
        for c,g in SERIES:
            for h in spec["horizons_quarters"]:
                target = shift(origin,h)
                if not "2020-Q1"<=target<="2023-Q4": continue
                key={"carrier":c,"group":g,"origin":origin,"target":target,"horizon_quarters":h}
                actual = history.get((c,g,target)); reference = history.get((c,g,shift(target,-4)))
                mean_values = seasonal.get((c,g,int(target[-1])))
                paths = [forecast(f,a,history,c,g,origin,h) for a,f,_ in candidates]
                reasons=[]
                if actual is None: reasons.append("missing_common_source_eligible_actual")
                if reference is None: reasons.append("missing_seasonal_reference_at_origin")
                if not mean_values: reasons.append("missing_fixed_training_seasonal_mean")
                if any(p is None for p in paths): reasons.append("missing_recursive_lag_state_at_origin")
                if reasons: exclusions.append({**key,"reasons":reasons}); continue
                for (a,f,name),path in zip(candidates,paths):
                    predictions.append({**key,"model":name,"architecture":a,"ridge_penalty":f["ridge_penalty"],
                        "prediction_asm_per_day":path[-1]["capacity_asm_per_assigned_day"],"actual_asm_per_day":actual["capacity"],
                        "forecast_path":path,"regime":spec["regimes"][target[:4]]})
                for name,value in ((spec["baselines"][0],reference["capacity"]),(spec["baselines"][1],float(np.mean(mean_values)))):
                    predictions.append({**key,"model":name,"architecture":None,"ridge_penalty":None,
                        "prediction_asm_per_day":value,"actual_asm_per_day":actual["capacity"],"forecast_path":None,"regime":spec["regimes"][target[:4]]})
        origin = shift(origin,1)
    names=[n for _,_,n in candidates]+spec["baselines"]
    scorecard=[scores(predictions,n) for n in names]
    if any(s["series_horizon_cells"]!=18 for s in scorecard): raise ValueError("Common capacity comparisons must cover all carrier/group/horizon cells")
    winner=min(scorecard[:len(candidates)],key=lambda s:s["selection_equal_weight_MAE_asm_per_day"])
    a,f,_=next(c for c in candidates if c[2]==winner["model"])
    best_other={a:min([s for s in scorecard[:len(candidates)] if s["model"].startswith(a+"_")],key=lambda s:s["selection_equal_weight_MAE_asm_per_day"])["model"] for a in ARCHITECTURES}
    baseline=min(scorecard[len(candidates):],key=lambda s:s["selection_equal_weight_MAE_asm_per_day"])
    return {"selected_name":winner["model"],"selected_architecture":a,"selected_model":f,"fitted_models":fits,
        "scorecard":scorecard,"best_by_architecture":best_other,"selection_predictions":predictions,"excluded_forecasts":exclusions,
        "best_declared_baseline":baseline["model"],"beats_best_declared_baseline_on_selection":winner["selection_equal_weight_MAE_asm_per_day"]<baseline["selection_equal_weight_MAE_asm_per_day"],
        "regime_scores":[{"regime":regime,"scores":[scores([r for r in predictions if r["regime"]==regime],n) for n in names]} for regime in ("disruption","recovery")],
        "locked_test_status":"2024+ targets excluded before model target access; no locked forecast scores performed",
        "deployment_status":"Covered-aircraft quarterly point component; monthly simulator integration, exposure treatment, fitted policies and calibrated uncertainty pending"}


def _read_upstream(path):
    m=json.loads((path/"manifest.json").read_text())
    if digest({k:v for k,v in m.items() if k!="snapshot_id"})!=m["snapshot_id"]: raise ValueError("T2 source manifest checksum failed")
    p=json.loads((path/"panel.json").read_text())
    if digest(p)!=m["panel_digest"]: raise ValueError("T2 source panel checksum failed")
    return m,p


def build_capacity(t2, spec_path, destination=ESTIMATES):
    spec=json.loads(Path(spec_path).read_text());check_spec(spec)
    m,p=_read_upstream(Path(t2))
    if m["snapshot_id"]!=spec["source_snapshot_id"]: raise ValueError("Capacity source differs from the declared snapshot")
    code=Path(__file__).read_bytes()
    design={"specification":spec,"builder_code_sha256":hashlib.sha256(code).hexdigest(),"numpy_version":np.__version__}
    design_id=digest(design); design_path=Path(destination)/"designs"/design_id;design_path.mkdir(parents=True,exist_ok=True)
    frozen=design_path/"design.json"
    if frozen.exists():
        d=json.loads(frozen.read_text())
        if d["design"]!=design or d["design_id"]!=design_id: raise ValueError("Immutable capacity design differs")
    else:
        with frozen.open("x") as stream:json.dump({"design_id":design_id,"declared_at_utc":datetime.now(timezone.utc).isoformat(),"design":design,
            "status":"Local declaration persisted before fitting/selection; not external preregistration"},stream,indent=2)
    panel=make_observations(p["group_quarters"])
    result=select(panel["observations"],spec)
    artifact={"panel":panel,"estimation":result,"specification":spec}
    manifest={"version":VERSION,"t2_snapshot_id":m["snapshot_id"],"available_on":m["available_on"],
        "design_id":design_id,"specification_digest":digest(spec),"artifact_digest":digest(artifact),"builder_code_sha256":hashlib.sha256(code).hexdigest(),
        "numpy_version":np.__version__,"selected_name":result["selected_name"],"selected_architecture":result["selected_architecture"],
        "fit_targets":len(result["selected_model"]["training_target_keys"]),"common_predictions_per_model":result["scorecard"][0]["prediction_count"],
        "excluded_group_quarters":len(panel["excluded_group_quarters"]),"vintage_status":spec["vintage"]}
    manifest["snapshot_id"]=digest(manifest)
    target=Path(destination)/manifest["snapshot_id"];target.mkdir(parents=True,exist_ok=True)
    for name,payload in (("artifact.json",json.dumps(artifact,separators=(",",":"),allow_nan=False).encode()),
        ("manifest.json",json.dumps(manifest,indent=2,allow_nan=False).encode()),("builder.py",code)):
        path=target/name
        if path.exists() and path.read_bytes()!=payload:raise ValueError("Immutable capacity estimate differs")
        if not path.exists():
            with path.open("xb") as stream:stream.write(payload)
    pointer=Path(destination)/"latest.tmp";pointer.write_text(json.dumps({"snapshot_id":manifest["snapshot_id"]})+"\n");pointer.replace(Path(destination)/"latest.json")
    return {**manifest,"path":str(target),"scorecard":result["scorecard"]}


def capacity_status(destination=ESTIMATES):
    if not (Path(destination)/"latest.json").exists():return {"status":"not_built","message":"Fit the quarterly capacity study with scripts/fit_capacity.py"}
    identity=json.loads((Path(destination)/"latest.json").read_text())["snapshot_id"]
    if not isinstance(identity,str) or not re.fullmatch(r"[a-f0-9]{64}",identity):raise ValueError("Invalid capacity snapshot ID")
    folder=Path(destination)/identity;m=json.loads((folder/"manifest.json").read_text());a=json.loads((folder/"artifact.json").read_text())
    if m.get("snapshot_id")!=identity or digest({k:v for k,v in m.items() if k!="snapshot_id"})!=identity:raise ValueError("Capacity manifest checksum failed")
    if digest(a)!=m["artifact_digest"] or hashlib.sha256((folder/"builder.py").read_bytes()).hexdigest()!=m["builder_code_sha256"]:raise ValueError("Capacity artifact/builder checksum failed")
    d=json.loads((Path(destination)/"designs"/m["design_id"]/"design.json").read_text())
    if d["design_id"]!=m["design_id"] or digest(d["design"])!=m["design_id"]:raise ValueError("Capacity frozen design checksum failed")
    result=a["estimation"]
    selected=next(s for s in result["scorecard"] if s["model"]==result["selected_name"])
    return {"status":"available",**m,"declared_at_utc":d["declared_at_utc"],"target_definition":a["specification"]["primary_target"],
        "scope":a["specification"]["scope"],"scorecard":result["scorecard"],"best_by_architecture":result["best_by_architecture"],
        "regime_scores":result["regime_scores"],"training_series_counts":result["selected_model"]["training_series_counts"],
        "excluded_forecasts":len(result["excluded_forecasts"]),"minimum_selection_cell_targets":min(c["targets"] for c in selected["series_horizon_scores"]),
        "excluded_disruption_group_quarters":sum("2020-Q1"<=r["period"]<="2021-Q4" for r in a["panel"]["excluded_group_quarters"]),
        "beats_best_declared_baseline_on_selection":result["beats_best_declared_baseline_on_selection"],
        "locked_test_status":result["locked_test_status"],"deployment_status":result["deployment_status"],
        "limitations":["T2-covered aircraft ratios exclude unresolved quantities; they are not whole owned-fleet or marketed regional capacity.",
            "Selection-period point scores on overlapping quarterly horizons; no locked-test scores, calibrated intervals, causal policy or investment validation.",
            "Monthly capacity, spare fractions, crew/hub ceilings and simulator integration remain pending."]}
