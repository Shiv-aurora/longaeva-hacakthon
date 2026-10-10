from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest

from app.evidence import digest
from app.quarterly_capacity import (
    SERIES, COMPONENTS, ARCHITECTURES, shift, make_observations, _history,
    fit_components, forecast, select, build_capacity, capacity_status,
)


def spec():
    return json.loads((Path(__file__).parents[1]/'data/t2_capacity_spec.json').read_text())


def synthetic():
    result=[]
    for i,(carrier,group) in enumerate(SERIES):
        state={}
        for t in range(44):
            period=shift('2013-Q1',t)
            if t<4:
                u=7+i*.3+t*.1; p=10000+i*2000+t*100
            else:
                a,b=state[shift(period,-1)],state[shift(period,-4)]
                u=.65*a['utilization']+.15*b['utilization']+1.2+i*.08+(period[-1]=='3')*.3
                p=.55*a['productivity']+.2*b['productivity']+2200+i*450+(period[-1]=='2')*150
            if '2020-Q2'<=period<='2021-Q1':u*=.5
            values={'capacity':u*p,'utilization':u,'productivity':p};state[period]=values
            result.append({'unique_carrier':carrier,'fleet_group':group,'period':period,'values':values})
    return result


def source_rows():
    return [{**{k:v for k,v in r.items() if k!='values'},'targets':{field:r['values'][c] for c,field in COMPONENTS.items()},
             'target_exclusions':{},'source_lines':[i+2],'source_sha256s':['preserved'],
             'P52_exposure_reconciled':False,'P52_rows_without_T2':[]} for i,r in enumerate(synthetic())]


def test_observation_loader_keeps_source_scope_and_never_reads_locked_targets():
    rows=source_rows()
    result=make_observations(rows+[{'period':'2024-Q1'}])
    assert len(result['observations'])==264
    assert result['observations'][0]['source_lines']==[2]
    assert result['observations'][0]['P52_exposure_reconciled'] is False
    with pytest.raises(ValueError,match='Duplicate'):make_observations(rows+[rows[0]])
    rows[0]['targets'][COMPONENTS['capacity']]=None
    rows[0]['target_exclusions'][COMPONENTS['capacity']]=['missing_assigned_days']
    excluded=make_observations(rows)['excluded_group_quarters'][0]
    assert excluded['target_exclusions']['capacity']==['missing_assigned_days']


@pytest.mark.parametrize('values',[
    {'capacity':1.,'utilization':25.,'productivity':.04},
    {'capacity':-1.,'utilization':1.,'productivity':1.},
    {'capacity':float('nan'),'utilization':1.,'productivity':1.},
    {'capacity':True,'utilization':1.,'productivity':1.},
    {'capacity':2.,'utilization':1.,'productivity':1.},
])
def test_invalid_components_cannot_become_capacity_training_targets(values):
    r=source_rows()[0];r['targets']={COMPONENTS[c]:v for c,v in values.items()}
    with pytest.raises(ValueError):make_observations([r])
    with pytest.raises(ValueError):_history([{**synthetic()[0],'values':values}])


def test_known_component_dynamics_recovered_and_common_train_only_standardization():
    train=[r for r in synthetic() if r['period']<='2019-Q4']
    fitted=fit_components(train,1e-8)
    assert set(fitted['training_series_counts'].values())=={24}
    assert len(fitted['training_target_keys'])==144
    assert fitted['components']['utilization']['training_residual_RMSE']<1e-6
    assert fitted['components']['productivity']['training_residual_RMSE']<.001
    h=_history(train)
    path=forecast(fitted,'utilization_times_productivity',h,'UA','narrowbody','2019-Q3',1)
    assert path[0]['capacity_asm_per_assigned_day']==pytest.approx(h['UA','narrowbody','2019-Q4']['capacity'],abs=.01)
    for component,model in fitted['components'].items():
        lag1=[h[r['carrier'],r['group'],shift(r['period'],-1)][component] for r in fitted['training_target_keys']]
        assert model['feature_center'][1]==pytest.approx(sum(lag1)/len(lag1))
        assert model['feature_scale'][1]==pytest.approx(np.std(lag1))


@pytest.mark.parametrize('architecture',ARCHITECTURES)
def test_recursion_ignores_every_post_origin_actual_and_reports_missing_lags(architecture):
    rows=synthetic();fitted=fit_components([r for r in rows if r['period']<='2019-Q4'],10)
    h=_history(rows)
    original=forecast(fitted,architecture,h,'UA','narrowbody','2019-Q4',4)
    poisoned={k:object() if k[2]>'2019-Q4' else v for k,v in h.items()}
    assert forecast(fitted,architecture,poisoned,'UA','narrowbody','2019-Q4',4)==original
    if architecture=='utilization_times_productivity':
        for r in original:
            assert r['capacity_asm_per_assigned_day']==r['reported_hours_per_assigned_day']*r['asm_per_reported_hour']
    del poisoned['UA','narrowbody','2019-Q1']
    assert forecast(fitted,architecture,poisoned,'UA','narrowbody','2019-Q4',4) is None


def test_component_physical_prediction_bounds():
    fitted=fit_components([r for r in synthetic() if r['period']<='2019-Q4'],10)
    for component,m in fitted['components'].items():
        m['coefficients']=[0.]*14;m['coefficients'][0]=100 if component=='utilization' else -100
    h=_history(synthetic())
    r=forecast(fitted,'utilization_times_productivity',h,'UA','narrowbody','2019-Q4',4)
    assert all(v['reported_hours_per_assigned_day']==24 and v['capacity_asm_per_assigned_day']==0 for v in r)
    assert forecast(fitted,'direct_capacity',h,'UA','narrowbody','2019-Q4',1)[0]['capacity_asm_per_assigned_day']==0


def test_common_selection_sample_independent_score_arithmetic_and_frozen_coefficients():
    rows=synthetic();result=select(rows,spec());h=_history(rows)
    assert len(result['scorecard'])==10
    assert {s['prediction_count'] for s in result['scorecard']}=={264}
    assert all(s['series_horizon_cells']==18 for s in result['scorecard'])
    for fitted in result['fitted_models']:
        assert fitted==fit_components([r for r in rows if r['period']<='2019-Q4'],fitted['ridge_penalty'])
    for s in result['scorecard']:
        means=[]
        for cell in s['series_horizon_scores']:
            subset=[p for p in result['selection_predictions'] if p['model']==s['model'] and (p['carrier'],p['group'],p['horizon_quarters'])==(cell['carrier'],cell['group'],cell['horizon_quarters'])]
            errors=[]
            for p in subset:
                actual=h[p['carrier'],p['group'],p['target']]['capacity']
                assert p['actual_asm_per_day']==actual
                if p['model']=='seasonal_capacity_persistence':
                    assert p['prediction_asm_per_day']==h[p['carrier'],p['group'],shift(p['target'],-4)]['capacity']
                if p['model']=='fixed_training_seasonal_capacity_mean':
                    expected=[v['capacity'] for (c,g,q),v in h.items() if (c,g)==(p['carrier'],p['group']) and q<='2019-Q4' and q[-1]==p['target'][-1]]
                    assert p['prediction_asm_per_day']==pytest.approx(sum(expected)/len(expected))
                errors.append(p['prediction_asm_per_day']-actual)
            mae=sum(abs(e) for e in errors)/len(errors);means.append(mae)
            assert cell['targets']==len(errors)
            assert cell['MAE_asm_per_day']==pytest.approx(mae)
            assert cell['RMSE_asm_per_day']==pytest.approx((sum(e*e for e in errors)/len(errors))**.5)
            assert cell['mean_error_asm_per_day']==pytest.approx(sum(errors)/len(errors))
        assert s['selection_equal_weight_MAE_asm_per_day']==pytest.approx(sum(means)/18)
    assert all('2020-Q1'<=p['target']<='2023-Q4' for p in result['selection_predictions'])


def test_missing_source_quarter_withholds_identical_comparisons_for_all_ten_models():
    rows=[r for r in synthetic() if (r['unique_carrier'],r['fleet_group'],r['period'])!=('UA','narrowbody','2020-Q3')]
    result=select(rows,spec())
    assert result['excluded_forecasts']
    keys={}
    for r in result['selection_predictions']:
        keys.setdefault(r['model'],set()).add((r['carrier'],r['group'],r['origin'],r['horizon_quarters']))
    assert all(v==next(iter(keys.values())) for v in keys.values())
    for r in result['excluded_forecasts']:
        assert (r['carrier'],r['group'],r['origin'],r['horizon_quarters']) not in next(iter(keys.values()))


def test_future_training_locked_selection_and_insufficient_training_rejected():
    with pytest.raises(ValueError,match='cannot enter'):fit_components(synthetic(),10)
    with pytest.raises(ValueError,match='locked'):select(synthetic()+[{'period':'2024-Q1'}],spec())
    with pytest.raises(ValueError,match='eight'):fit_components(synthetic()[:3],10)
    bad=spec();bad['horizons_quarters']=[1,4]
    with pytest.raises(ValueError,match='declared'):select(synthetic(),bad)


def test_deterministic_ties_use_declared_architecture_then_penalty_order():
    rows=synthetic()
    for r in rows:r['values']={'capacity':0.,'utilization':0.,'productivity':0.}
    result=select(rows,spec())
    assert result['selected_name']=='direct_capacity_ridge_0.1'
    assert not result['beats_best_declared_baseline_on_selection']


def test_immutable_offline_rebuild_and_cache_verification(tmp_path):
    upstream=tmp_path/'source';upstream.mkdir()
    panel={'group_quarters':source_rows()}
    m={'panel_digest':digest(panel),'available_on':'2026-10-09'};m['snapshot_id']=digest(m)
    (upstream/'manifest.json').write_text(json.dumps(m));(upstream/'panel.json').write_text(json.dumps(panel))
    design=spec();design['source_snapshot_id']=m['snapshot_id'];specpath=tmp_path/'spec.json';specpath.write_text(json.dumps(design))
    destination=tmp_path/'fit'
    assert capacity_status(destination)['status']=='not_built'
    first=build_capacity(upstream,specpath,destination)
    assert build_capacity(upstream,specpath,destination)['snapshot_id']==first['snapshot_id']
    status=capacity_status(destination)
    assert status['fit_targets']==144 and status['common_predictions_per_model']==264
    assert status['minimum_selection_cell_targets']==13
    folder=Path(first['path']);original=(folder/'artifact.json').read_bytes()
    (folder/'artifact.json').write_text('{}')
    with pytest.raises(ValueError,match='checksum'):capacity_status(destination)
    with pytest.raises(ValueError,match='Immutable'):build_capacity(upstream,specpath,destination)
    (folder/'artifact.json').write_bytes(original)
    original=(folder/'builder.py').read_bytes();(folder/'builder.py').write_bytes(b'changed')
    with pytest.raises(ValueError,match='checksum'):capacity_status(destination)
    (folder/'builder.py').write_bytes(original)
    frozen=destination/'designs'/first['design_id']/'design.json';d=json.loads(frozen.read_text());d['design']['numpy_version']='changed';frozen.write_text(json.dumps(d))
    with pytest.raises(ValueError,match='design checksum'):capacity_status(destination)


def test_cached_api_returns_unbuilt_and_fails_closed_on_corruption(monkeypatch):
    from fastapi.testclient import TestClient
    import app.server as server
    client=TestClient(server.app)
    monkeypatch.setattr(server,'capacity_status',lambda:{'status':'not_built','message':'Not fitted'})
    assert client.get('/api/capacity/status').json()['status']=='not_built'
    def corrupt():raise ValueError('Checksum failure')
    monkeypatch.setattr(server,'capacity_status',corrupt)
    response=client.get('/api/capacity/status')
    assert response.status_code==503 and 'failed verification' in response.json()['detail']


def test_independent_audit_detects_altered_fitted_coefficients():
    from scripts.audit_capacity import independent_fits
    rows=synthetic();h=_history(rows)
    fitted=fit_components([r for r in rows if r['period']<='2019-Q4'],10)
    fits,keys=independent_fits(h,[fitted])
    assert len(keys)==144 and set(fits)=={10}
    changed=deepcopy(fitted);changed['components']['productivity']['coefficients'][0]+=1000
    with pytest.raises(ValueError,match='ridge fit differs'):independent_fits(h,[changed])


def test_independent_audit_detects_wrong_score_and_uses_reported_actual():
    from scripts.audit_capacity import verify_scores
    h={('UA','narrowbody','2020-Q1'):{'capacity':100.},('UA','narrowbody','2020-Q2'):{'capacity':200.}}
    predictions=[{'model':'example','carrier':'UA','group':'narrowbody','horizon_quarters':1,'target':q,'prediction_asm_per_day':p,'actual_asm_per_day':-999} for q,p in [('2020-Q1',120.),('2020-Q2',160.)]]
    cell={'carrier':'UA','group':'narrowbody','horizon_quarters':1,'targets':2,'MAE_asm_per_day':30.,'RMSE_asm_per_day':1000**.5,'mean_error_asm_per_day':-10.}
    score={'model':'example','series_horizon_scores':[cell],'series_horizon_cells':1,'prediction_count':2,'selection_equal_weight_MAE_asm_per_day':30.}
    assert verify_scores([score],predictions,h)[0]['equal_weight_MAE_asm_per_day']==30.
    cell['MAE_asm_per_day']=29.
    with pytest.raises(ValueError,match='arithmetic differs'):verify_scores([score],predictions,h)
