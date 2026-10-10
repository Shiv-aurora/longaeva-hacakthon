"""Fixed-model source ablations on exact rolling issue histories.

Corrections are hindsight research inputs. The financial simulator is unchanged.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .evidence import digest
from .monthly_traffic import (SERIES, HORIZONS, BASELINES, check_spec, periods, shift, month_end,
    history, observations, regress, measurement_sequence, fit_state, prediction, ar_predict, score)
from .operating_vintages import verify_vintages, origin_history, write_immutable
from .traffic_corrections import verify_reviewed
from .source_store import ROOT

VERSION = "monthly-traffic-vintage-study-0.1.1"
DESTINATION = ROOT / "data/monthly_traffic_vintages"
CASES = ("rolling_native_history_native_targets", "rolling_native_history_reviewed_targets",
    "rolling_reviewed_history_reviewed_targets", "annual_native_history_reviewed_targets")


def initial_anchor(histories):
    expected = {(c,m,p) for c,m in SERIES for p in periods("2013-01","2019-12")}
    for issue in sorted(histories):
        initial = [r for r in histories[issue] if r["period"] <= "2019-12"]
        known = history(initial,issue)
        if set(known) != expected: continue
        fixed, scales = {}, {}
        for c,m in SERIES:
            for month in range(1,13):
                fixed[c,m,month] = float(np.mean([r["rpm_million"] for r in initial if (r["carrier"],r["market"])==(c,m) and int(r["period"][5:])==month]))
            differences = [abs(r["rpm_million"]-known[c,m,shift(r["period"],-12)]["rpm_million"])
                for r in initial if (r["carrier"],r["market"])==(c,m) and (c,m,shift(r["period"],-12)) in known]
            scales[c,m] = float(np.mean(differences))
            if not math.isfinite(scales[c,m]) or scales[c,m] <= 0: raise ValueError("Initial scale is undefined")
        return {"issue_on":issue,"published_on":max(r["published_on"] for r in initial),"rows":initial,
            "fixed_seasonal_means":[{"carrier":c,"market":m,"month":n,"rpm_million":v} for (c,m,n),v in fixed.items()],
            "mase_scales":[{"carrier":c,"market":m,"scale":v} for (c,m),v in scales.items()]},fixed,scales
    raise ValueError("No issue has the complete declared initial grid available")


def fit_histories(histories, anchor, fixed, scales, spec, label, progress=None):
    names = [f"state_rho_{rho:g}" for rho in spec["pooling_correlations"]]
    forecasts, fits, excluded = [], [], []
    for origin in periods("2020-01","2023-11"):
        issue = month_end(origin)
        if issue < anchor["issue_on"]:
            excluded.append({"origin":origin,"reason":"initial grid not fully released","available_on":anchor["published_on"]});continue
        if issue not in histories: raise ValueError("Declared issue history is absent")
        known = history(histories[issue],issue)
        if any(r["period"] > origin for r in known.values()): raise ValueError("Future record entered an issue history")
        seasonal=regress(known,spec["seasonal_ridge"]);ar=regress(known,spec["autoregression_ridge"],True)
        sequence=measurement_sequence(known,seasonal,origin)
        states=[fit_state(known,seasonal,rho,spec,origin,sequence) for rho in spec["pooling_correlations"]]
        fit_index=len(fits)
        fits.append({"origin":origin,"cutoff":issue,"known_keys":[list(k) for k in sorted(known)],
            "known_digest":digest([known[k] for k in sorted(known)]),"max_known_publication":max(r["published_on"] for r in known.values()),
            "latest_period_by_series":{f"{c}:{m}":max(k[2] for k in known if k[:2]==(c,m)) for c,m in SERIES},
            "seasonal":seasonal,"autoregression":ar,"states":states})
        for c,m in SERIES:
            for h in HORIZONS:
                target=shift(origin,h)
                if target > "2023-12":continue
                eligible=[p for carrier,market,p in known if (carrier,market)==(c,m) and p[5:]==target[5:]]
                if not eligible: raise ValueError("Declared common sample lacks a seasonal baseline")
                persistence=max(eligible)
                distributions={name:prediction(state,seasonal,c,m,target,h) for name,state in zip(names,states)}
                failures={}
                try: regularized=ar_predict(ar,known,c,m,target)
                except ValueError as error:
                    regularized=None
                    failures[BASELINES[2]]=str(error)
                forecasts.append({"origin":origin,"cutoff":issue,"target":target,"carrier":c,"market":m,"horizon_months":h,
                    "fit_index":fit_index,"initial_mase_scale":scales[c,m],"persistence_source_period":persistence,
                    "persistence_published_on":known[c,m,persistence]["published_on"],
                    "predictions":{**{n:d["p50"] for n,d in distributions.items()},BASELINES[0]:known[c,m,persistence]["rpm_million"],
                        BASELINES[1]:fixed[c,m,int(target[5:])],BASELINES[2]:regularized},"model_failures":failures,
                    "distributions":distributions,"regime":"disruption" if target[:4] in ("2020","2021") else "recovery"})
        if progress:progress({"phase":"origin_fitted","history":label,"origin":origin,"known_targets":len(known),"forecasts":len(forecasts)})
    return {"fits":fits,"forecasts":forecasts,"excluded_origins":excluded}


def forecast_key(row):
    return row["origin"],row["carrier"],row["market"],row["horizon_months"],row["target"]


def supported(row):
    return all(v is not None and math.isfinite(v) and v > 0 for v in row["predictions"].values())


def summarize(forecasts,spec,common_keys=None):
    eligible=[r for r in forecasts if supported(r) and (common_keys is None or forecast_key(r) in common_keys)]
    names=[f"state_rho_{rho:g}" for rho in spec["pooling_correlations"]]
    scores=[score(eligible,n) for n in (*names,*BASELINES)]
    if any(len(s["series_horizon_scores"])!=48 for s in scores):raise ValueError("Supported comparison omits a declared cell")
    selected_score=min(scores[:len(names)],key=lambda s:s["equal_cell_MASE"])
    return {"forecasts":forecasts,"scorecard":scores,"selected_name":selected_score["model"],
        "requested_predictions":len(forecasts),"common_supported_predictions":len(eligible),
        "model_failures":[{**{k:r[k] for k in ("origin","carrier","market","horizon_months","target")},"failures":r["model_failures"]} for r in forecasts if r["model_failures"]],
        "beats_best_declared_baseline":selected_score["equal_cell_MASE"] < min(s["equal_cell_MASE"] for s in scores[len(names):]),
        "regime_scorecards":[{"regime":regime,"scores":[score([r for r in eligible if r["regime"]==regime],n) for n in (*names,*BASELINES)]} for regime in ("disruption","recovery")],
        "scope":"Common-supported selection subset with failed forecasts retained separately; conditional uncalibrated intervals; no pricing, financial integration, whole-domain robustness or locked test."}


def attach_targets(fitted, targets, spec):
    selected={}
    for row in targets:
        # Do not touch quantities/source metadata of locked or out-of-scope rows.
        if not "2013-01" <= row["period"] <= "2023-12" or (row["carrier"],row["market"]) not in SERIES:continue
        key=row["carrier"],row["market"],row["period"]
        if key in selected:raise ValueError("Duplicate evaluation target")
        selected[key]=row
    forecasts=[]
    for forecast in fitted["forecasts"]:
        row=selected.get((forecast["carrier"],forecast["market"],forecast["target"]))
        if row is None:raise ValueError("Declared common evaluation target is missing")
        forecasts.append({**forecast,"actual_rpm_million":row["rpm_million"],"actual_published_on":row["published_on"],
            "actual_source_sha256s":row["source_sha256s"],"actual_corrections":row.get("source_corrections",[])})
    return {"forecasts":forecasts}


def build_study(spec, destination=DESTINATION, progress=None):
    check_spec(spec)
    if tuple(spec["cases"]) != CASES:raise ValueError("Source ablation cases differ from declaration")
    native_root=ROOT/"data/traffic_vintages";npth=native_root/spec["source_snapshot_id"]
    native=verify_vintages(npth,native_root)
    reviewed_root=ROOT/"data/reviewed_traffic_vintages";rpth=reviewed_root/spec["reviewed_snapshot_id"]
    reviewed=verify_reviewed(rpth,reviewed_root)
    audit=json.loads((ROOT/"data/reviewed_traffic_checks"/(spec["reviewed_audit_id"]+".json")).read_text())
    if digest(audit)!=spec["reviewed_audit_id"] or audit["snapshot_id"]!=rpth.name or reviewed["native_snapshot_id"]!=npth.name:raise ValueError("Reviewed source audit/input binding differs")
    annual_path=ROOT/"data/operating_history"/spec["annual_snapshot_id"]
    am=json.loads((annual_path/"manifest.json").read_text());annual=json.loads((annual_path/"panel.json").read_text())
    if digest({k:v for k,v in am.items() if k!="snapshot_id"})!=annual_path.name or digest(annual)!=am["panel_digest"]:raise ValueError("Annual comparison input checksum differs")
    annual_rows=observations(annual["rows"],annual["selected_releases"])["observations"]
    inputs={"native_artifact_digest":digest(native),"reviewed_artifact_digest":digest(reviewed),"annual_panel_digest":digest(annual)}
    code_names=("monthly_traffic_vintages.py","monthly_traffic.py","traffic_corrections.py","operating_vintages.py","operating_history.py","source_store.py","evidence.py")
    code={name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest() for name in code_names}
    design={"version":VERSION,"specification":spec,"inputs":inputs,"code":code,"numpy_version":np.__version__}
    design_id=digest(design);destination=Path(destination)
    declared=destination/"designs"/design_id/"design.json"
    write_immutable(declared,design)
    stamp=declared.parent/"declaration_time.json"
    if not stamp.exists():write_immutable(stamp,{"declared_at_utc":datetime.now(timezone.utc).isoformat(),"scope":"Local declaration before fitting, not external preregistration"})
    histories={"native":{o["issue_on"]:origin_history(native,o["issue_on"]) for o in native["origins"]},
        "reviewed":{o["issue_on"]:[reviewed["observation_versions"][i] for i in o["observation_ids"]] for o in reviewed["origins"]}}
    histories["annual"]={issue:list(history(annual_rows,issue).values()) for issue in histories["native"]}
    anchor,fixed,scales=initial_anchor(histories["native"])
    fitted={label:fit_histories(rows,anchor,fixed,scales,spec,label,progress) for label,rows in histories.items()}
    assignments={CASES[0]:("native",native["evaluation_targets"]["observations"]),CASES[1]:("native",reviewed["evaluation_targets"]["observations"]),
        CASES[2]:("reviewed",reviewed["evaluation_targets"]["observations"]),CASES[3]:("annual",reviewed["evaluation_targets"]["observations"])}
    cases={name:{"history":label,**attach_targets(fitted[label],targets,spec)} for name,(label,targets) in assignments.items()}
    requested=[forecast_key(r) for r in cases[CASES[0]]["forecasts"]]
    if any([forecast_key(r) for r in c["forecasts"]]!=requested for c in cases.values()):raise ValueError("Source cases lack identical comparison keys")
    common=set(requested)
    for case in cases.values():common&={forecast_key(r) for r in case["forecasts"] if supported(r)}
    cases={name:{"history":case["history"],**summarize(case["forecasts"],spec,common)} for name,case in cases.items()}
    artifact={"version":VERSION,"design_id":design_id,"specification":spec,"inputs":inputs,"initial_anchor":anchor,
        "history_fits":fitted,"cases":cases,"common_forecast_keys":[list(k) for k in sorted(common)],
        "requested_predictions_per_model_per_case":len(requested),"common_predictions_per_model_per_case":len(common)}
    manifest={"version":VERSION,"design_id":design_id,"artifact_digest":digest(artifact),"code":code,
        "common_predictions_per_model_per_case":len(common),"numpy_version":np.__version__,"scope":spec["research_gate"]}
    identity=digest(manifest);manifest["snapshot_id"]=identity;target=destination/identity
    write_immutable(target/"artifact.json",artifact);write_immutable(target/"manifest.json",manifest)
    for name,sha in code.items():
        content=(Path(__file__).parent/name).read_bytes()
        if hashlib.sha256(content).hexdigest()!=sha:raise ValueError("Traffic study source changed during fit")
        p=target/"code"/name;p.parent.mkdir(exist_ok=True)
        if p.exists() and p.read_bytes()!=content:raise ValueError("Immutable study source differs")
        if not p.exists():p.write_bytes(content)
    if digest(json.loads((target/"artifact.json").read_text()))!=manifest["artifact_digest"]:raise ValueError("Study readback differs")
    # This separate research pointer never replaces the annual scorecard.
    pointer=destination/"latest.tmp";pointer.write_text(json.dumps({"snapshot_id":identity})+"\n");pointer.replace(destination/"latest.json")
    return {**manifest,"path":str(target),"initial_anchor_issue":anchor["issue_on"],
        "cases":{name:{"selected_name":c["selected_name"],"beats_best_declared_baseline":c["beats_best_declared_baseline"],"scorecard":c["scorecard"]} for name,c in cases.items()}}
