"""Independent-oracle tamper and numerical-support checks, not estimator mirrors."""
from copy import deepcopy
import json

import pytest

from app.evidence import digest
from scripts.audit_monthly_traffic_vintages import Checks, recursive_ar, audit_point, audit_scorecards, key


@pytest.fixture(scope='module')
def study(require_local_cache):
    # Preserved research fixture; no estimation or network fetch in these tests.
    # The study is a local, untracked cache: absent from a fresh clone, where these tests skip.
    p=require_local_cache('data/monthly_traffic_vintages/31226d81e2e4cc0163b965034b6e9e6127ca61cc04ab0a9873a56adbebf8d164/artifact.json',
        'docs/reviewed_traffic.md (scripts/fit_monthly_traffic_vintages.py)','preserved four-case traffic source-comparison study')
    return json.loads(p.read_text())


@pytest.fixture(scope='module')
def native_source(study,require_local_cache):
    # Upstream native issue histories named by the study; also local and untracked.
    p=require_local_cache('data/traffic_vintages/'+study['specification']['source_snapshot_id']+'/artifact.json',
        'docs/traffic_vintages.md (scripts/build_traffic_vintages.py)','native traffic issue-history snapshot used by the preserved study')
    return json.loads(p.read_text())


def point_context(study,source):
    r=study['history_fits']['native']['forecasts'][0]
    fit=study['history_fits']['native']['fits'][r['fit_index']]
    origin=next(o for o in source['origins'] if o['issue_on']==r['cutoff'])
    known={(v['carrier'],v['market'],v['period']):v for v in (source['observation_versions'][i] for i in origin['observation_ids'])}
    initial={(v['carrier'],v['market'],v['period']):v for v in study['initial_anchor']['rows']}
    scales={(v['carrier'],v['market']):v['scale'] for v in study['initial_anchor']['mase_scales']}
    return r,fit,known,initial,scales


def test_rehashed_prediction_tamper_fails_independent_equation(study,native_source):
    r,*context=point_context(study,native_source)
    audit_point(r,*context,Checks())
    forged=deepcopy(r);forged['predictions']['state_rho_0']*=1.1
    assert digest(forged)!=digest(r) # Attacker can update a checksum; numerical witness must still reject.
    with pytest.raises(ValueError,match='state point median'):audit_point(forged,*context,Checks())


def test_future_target_cannot_be_accepted_by_point_oracle(study,native_source):
    r,*context=point_context(study,native_source);forged=deepcopy(r);forged['target']='2024-01'
    with pytest.raises(ValueError,match='locked target'):audit_point(forged,*context,Checks())


def test_rehashed_target_or_score_tamper_fails_scalar_scores(study):
    case=next(iter(study['cases'].values()));common={tuple(k) for k in study['common_forecast_keys']}
    audit_scorecards(case,common,Checks())
    forged=deepcopy(case);forged['forecasts'][0]['actual_rpm_million']*=1.1
    with pytest.raises(ValueError,match='cell MAE'):audit_scorecards(forged,common,Checks())
    forged=deepcopy(case);forged['scorecard'][0]['series_horizon_scores'][0]['MASE']+=1
    with pytest.raises(ValueError,match='cell MASE'):audit_scorecards(forged,common,Checks())


def test_different_common_sample_or_duplicate_cell_is_rejected(study):
    case=next(iter(study['cases'].values()));common={tuple(k) for k in study['common_forecast_keys']}
    common.remove(key(case['forecasts'][0]))
    with pytest.raises(ValueError,match='sample count'):audit_scorecards(case,common,Checks())
    common={tuple(k) for k in study['common_forecast_keys']};forged=deepcopy(case)
    forged['scorecard'][0]['series_horizon_scores'][1]=forged['scorecard'][0]['series_horizon_scores'][0]
    with pytest.raises(ValueError,match='distinct score cells'):audit_scorecards(forged,common,Checks())


def test_absolute_log_bound_is_preserved_without_clipping():
    known={('UA','domestic','2019-02'):{'rpm_million':1.},('UA','domestic','2020-01'):{'rpm_million':1.}}
    fit={'autoregression':{'coefficients':[40.]+[0.]*57}}
    value,failure=recursive_ar(fit,known,'UA','domestic','2020-02')
    assert failure is None and value>1e17
    fit['autoregression']['coefficients'][0]=40.0001
    assert recursive_ar(fit,known,'UA','domestic','2020-02')==(None,'AR recursive forecast is numerically unsupported')
    del known['UA','domestic','2019-02']
    assert recursive_ar(fit,known,'UA','domestic','2020-02')==(None,'AR release-gap state is incomplete')
