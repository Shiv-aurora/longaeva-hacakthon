from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from app.monthly_traffic import BASELINES, SERIES, HORIZONS, periods
from app.monthly_traffic_vintages import initial_anchor, attach_targets, summarize, forecast_key, fit_histories


def spec():
    return json.loads((Path(__file__).parents[1]/"data/monthly_traffic_vintage_spec.json").read_text())


def initial_rows():
    return [{"carrier":c,"market":m,"period":p,"published_on":"2020-06-02",
        "rpm_million":1000+i*100+n*2+int(p[5:])*3,"asm_million":2000+i*100+n*2+int(p[5:])*3,
        "source_sha256s":["a"*64],"carrier_entities":[str(i)]}
        for i,(c,m) in enumerate(SERIES) for n,p in enumerate(periods("2013-01","2019-12"))]


def test_anchor_waits_for_complete_grid_and_freezes_before_later_revisions():
    rows=initial_rows();later=deepcopy(rows);later[0]["rpm_million"]+=99999
    anchor,fixed,scales=initial_anchor({"2020-05-31":rows,"2020-06-30":rows,"2020-07-31":later})
    assert anchor["issue_on"]=="2020-06-30" and anchor["published_on"]=="2020-06-02"
    values=[r["rpm_million"] for r in rows if r["carrier"]=="UA" and r["market"]=="domestic" and r["period"][5:]=="01"]
    assert fixed["UA","domestic",1]==sum(values)/7
    assert scales["UA","domestic"]==24
    assert all(r["rpm_million"]<10000 for r in anchor["rows"])


def test_incomplete_initial_grid_is_not_imputed():
    with pytest.raises(ValueError,match="complete declared"):
        initial_anchor({"2020-06-30":initial_rows()[:-1]})


def forecasts():
    names=[f"state_rho_{rho:g}" for rho in spec()["pooling_correlations"]]+list(BASELINES)
    return [{"origin":origin,"cutoff":origin+"-30","carrier":c,"market":m,"horizon_months":h,"target":"2021-01",
        "predictions":dict.fromkeys(names,100.),"model_failures":{},"actual_rpm_million":110.,"initial_mase_scale":10.,
        "regime":"disruption" if origin[:4]=="2020" else "recovery"} for origin in ("2020-06","2020-07","2022-06","2022-07") for c,m in SERIES for h in HORIZONS]


def test_missing_baseline_remains_visible_and_common_keys_apply_to_every_model():
    f=forecasts();f[0]["predictions"][BASELINES[2]]=None;f[0]["model_failures"]={BASELINES[2]:"numerically unsupported"}
    common={forecast_key(r) for r in f[2:]} # Another source case also lacks the second key.
    r=summarize(f,spec(),common)
    assert r["requested_predictions"]==192 and r["common_supported_predictions"]==190
    assert all(s["prediction_count"]==190 and len(s["series_horizon_scores"])==48 for s in r["scorecard"])
    assert r["forecasts"][0]["predictions"][BASELINES[2]] is None and len(r["model_failures"])==1


def test_whole_missing_comparison_cell_is_rejected():
    f=forecasts();eligible={forecast_key(r) for r in f if (r["carrier"],r["market"],r["horizon_months"])!=("UA","domestic",1)}
    with pytest.raises(ValueError,match="declared cell"):summarize(f,spec(),eligible)


def test_evaluation_targets_are_separate_and_locked_quantity_never_accessed():
    f=forecasts();targets=[{"carrier":c,"market":m,"period":"2021-01","rpm_million":123.,"published_on":"2021-08-01","source_sha256s":["source"]} for c,m in SERIES]
    targets.append({"period":"2024-01"})
    result=attach_targets({"forecasts":f},targets,spec())
    assert all(r["actual_rpm_million"]==123. for r in result["forecasts"])
    assert all(r["actual_rpm_million"]==110. for r in f)
    with pytest.raises(ValueError,match="Duplicate"):attach_targets({"forecasts":f},targets+[targets[0]],spec())


def test_per_issue_refitting_captures_revisions_and_preserves_failed_forecasts(monkeypatch):
    import app.monthly_traffic_vintages as module
    rows=initial_rows();anchor,fixed,scales=initial_anchor({"2020-06-30":rows})
    original_periods=module.periods
    monkeypatch.setattr(module,"periods",lambda a,b:["2020-10"] if (a,b)==("2020-01","2023-11") else original_periods(a,b))
    monkeypatch.setattr(module,"ar_predict",lambda *args:(_ for _ in ()).throw(ValueError("AR recursive forecast is numerically unsupported")))
    design=spec();design["process_variance_grid"]=[.01];design["measurement_variance_grid"]=[.01]
    result=fit_histories({"2020-10-31":rows},anchor,fixed,scales,design,"test")
    assert len(result["forecasts"])==48
    assert all(r["predictions"][BASELINES[2]] is None for r in result["forecasts"])
    assert all(len(r["predictions"])==6 and r["model_failures"] for r in result["forecasts"])
    changed=deepcopy(rows);changed[0]["rpm_million"]*=2
    other=fit_histories({"2020-10-31":changed},anchor,fixed,scales,design,"test")
    assert result["fits"][0]["known_digest"]!=other["fits"][0]["known_digest"]
    assert result["fits"][0]["seasonal"]["coefficients"]!=other["fits"][0]["seasonal"]["coefficients"]
