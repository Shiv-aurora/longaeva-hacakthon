"""Independent release-history, augmented-regression and scalar-filter audit.

Uses the independent numerical primitives from the annual audit, never imports an
estimator or correction builder. Original-source checks remain separately bound
upstream evidence; this audit does not re-read every raw ZIP.
"""
from calendar import monthrange
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.evidence import digest
from scripts.audit_monthly_traffic import SERIES, basis, number, scalar_filter, interval_diagnostics

CASES = ('rolling_native_history_native_targets', 'rolling_native_history_reviewed_targets',
         'rolling_reviewed_history_reviewed_targets', 'annual_native_history_reviewed_targets')
BASELINES = ('released_seasonal_persistence', 'fixed_initial_seasonal_mean', 'expanding_regularized_log_ar')


def period(n):
    return f'{n//12:04d}-{n%12+1:02d}'


def key(row):
    return row['origin'], row['carrier'], row['market'], row['horizon_months'], row['target']


class Checks:
    def __init__(self): self.count = 0
    def check(self, value, name):
        if not value: raise ValueError('Independent vintage study audit failed: ' + name)
        self.count += 1
    def close(self, a, b, name):
        self.check(math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-7), name)


def released(rows, issue, checks):
    result = {}
    for row in rows:
        if row['period'] > issue[:7] or row['published_on'] > issue: continue
        k = row['carrier'], row['market'], row['period']
        checks.check(k not in result and k[:2] in SERIES, 'unique released observation')
        checks.check(math.isfinite(row['rpm_million']) and 0 < row['rpm_million'] <= row['asm_million'], 'physical positive released observation')
        result[k] = row
    return result


def annual_observations(panel, checks):
    releases = {(r['scope'], r['period']):r for r in panel['selected_releases'] if '2013-01' <= r['period'] <= '2023-12'}
    result = []
    for r in panel['rows']:
        # Locked quantities are not accessed.
        if not '2013-01' <= r['period'] <= '2023-12' or (r['carrier'], r['market']) not in SERIES: continue
        if not r['both_archive_scopes_available'] or r['quality_flags']: continue
        if any((s,r['period']) not in releases for s in ('DD','FD')): continue
        if r['rpm'] == 0 or r['asm'] == 0: continue
        checks.check(0 < r['rpm'] <= r['asm'], 'annual physical identity')
        scopes = [releases[s,r['period']] for s in ('DD','FD')]
        result.append({'carrier':r['carrier'], 'market':r['market'], 'period':r['period'],
            'rpm_million':r['rpm']/1e6, 'asm_million':r['asm']/1e6,
            'published_on':max(r['published_on'], *(s['published_on'] for s in scopes)),
            'contributor_published_on':r['published_on'], 'scope_releases':scopes,
            'source_sha256s':r['source_sha256s'], 'carrier_entities':r['carrier_entities']})
    return sorted(result, key=lambda r:(r['period'],r['carrier'],r['market']))


def recursive_ar(fit, known, c, m, target):
    values = {k[2]:math.log(r['rpm_million']) for k,r in known.items() if k[:2] == (c,m)}
    for t in range(number(max(values))+1, number(target)+1):
        p = period(t); previous = [period(t-lag) for lag in (1,12)]
        if any(p not in values for p in previous): return None, 'AR release-gap state is incomplete'
        vector = basis(c,m,p) + [values[p] for p in previous]
        value = sum(a*b for a,b in zip(vector,fit['autoregression']['coefficients']))
        if not math.isfinite(value) or abs(value)>40: return None, 'AR recursive forecast is numerically unsupported'
        values[p] = value
    return math.exp(values[target]), None


def audit_point(row, fit, known, initial, scales, checks):
    c,m,target = row['carrier'],row['market'],row['target']
    checks.check(target <= '2023-12' and number(target)==number(row['origin'])+row['horizon_months'], 'locked target and horizon')
    checks.check((fit['origin'],fit['cutoff'])==(row['origin'],row['cutoff']), 'correct issued fit')
    checks.check(row['regime']==('disruption' if target[:4] in ('2020','2021') else 'recovery'), 'target regime')
    checks.close(row['initial_mase_scale'],scales[c,m], 'frozen initial scale')
    source = max(k[2] for k in known if k[:2]==(c,m) and k[2][5:]==target[5:])
    checks.check(row['persistence_source_period']==source and row['persistence_published_on']==known[c,m,source]['published_on'], 'released persistence source')
    checks.close(row['predictions'][BASELINES[0]], known[c,m,source]['rpm_million'], 'seasonal persistence')
    checks.close(row['predictions'][BASELINES[1]], statistics.mean(r['rpm_million'] for k,r in initial.items() if k[:2]==(c,m) and k[2][5:]==target[5:]), 'initial seasonal mean')
    value,failure = recursive_ar(fit,known,c,m,target)
    checks.check(row['model_failures']==({BASELINES[2]:failure} if failure else {}), 'exact failure status/reason')
    if failure: checks.check(row['predictions'][BASELINES[2]] is None, 'unsupported AR retained without clipping')
    else: checks.close(row['predictions'][BASELINES[2]],value,'recursive AR prediction')
    names = [f"state_rho_{s['rho']:g}" for s in fit['states']]
    checks.check(set(row['predictions'])==set(names+list(BASELINES)) and set(row['distributions'])==set(names), 'complete model prediction families')
    for state,name in zip(fit['states'],names):
        d = row['distributions'][name];i=SERIES.index((c,m))
        mu = sum(a*b for a,b in zip(basis(c,m,target),fit['seasonal']['coefficients']))+state['mean'][i]
        variance = state['covariance'][i][i]+row['horizon_months']*state['q']+state['r']
        checks.close(d['log_mean'],mu,'forecast log mean');checks.close(d['log_variance'],variance,'forecast log variance')
        for label,z in (('p10',-1.2815515655446004),('p50',0),('p90',1.2815515655446004)):
            checks.close(d[label],math.exp(mu+z*math.sqrt(variance)), 'native quantile')
        checks.close(row['predictions'][name],d['p50'],'state point median')


def audit_scorecards(case, common, checks):
    rows = [r for r in case['forecasts'] if key(r) in common]
    names = ['state_rho_0','state_rho_0.5','state_rho_0.9',*BASELINES]
    all_cards = [(rows,case['scorecard'])]+[([r for r in rows if r['regime']==part['regime']],part['scores']) for part in case['regime_scorecards']]
    checks.check([p['regime'] for p in case['regime_scorecards']]==['disruption','recovery'],'complete regime scorecards')
    expected_cells = {(c,m,h) for c,m in SERIES for h in (1,3,6,12)}
    for chosen,cards in all_cards:
        checks.check([s['model'] for s in cards]==names,'all six score models')
        for card in cards:
            checks.check(card['prediction_count']==len(chosen), 'common score sample count')
            cells=card['series_horizon_scores']
            checks.check(len(cells)==48 and {(c['carrier'],c['market'],c['horizon_months']) for c in cells}==expected_cells,'all 48 distinct score cells')
            for cell in cells:
                sample = [r for r in chosen if (r['carrier'],r['market'],r['horizon_months'])==(cell['carrier'],cell['market'],cell['horizon_months'])]
                checks.check(bool(sample) and cell['targets']==len(sample),'exact cell count')
                errors=[r['predictions'][card['model']]-r['actual_rpm_million'] for r in sample]
                checks.close(cell['MAE_rpm_million'],statistics.mean(map(abs,errors)),'cell MAE')
                checks.close(cell['RMSE_rpm_million'],math.sqrt(statistics.mean(e*e for e in errors)),'cell RMSE')
                checks.close(cell['mean_error_rpm_million'],statistics.mean(errors),'cell bias')
                checks.close(cell['MASE'],statistics.mean(abs(e)/r['initial_mase_scale'] for e,r in zip(errors,sample)),'cell MASE')
            checks.close(card['equal_cell_MASE'],statistics.mean(c['MASE'] for c in cells),'equal-cell MASE')
            checks.close(card['equal_cell_MAE_rpm_million'],statistics.mean(c['MAE_rpm_million'] for c in cells),'equal-cell MAE')
    best = min(case['scorecard'][:3],key=lambda s:s['equal_cell_MASE'])
    checks.check(case['selected_name']==best['model'],'state candidate selection')
    checks.check(case['beats_best_declared_baseline']==(best['equal_cell_MASE']<min(s['equal_cell_MASE'] for s in case['scorecard'][3:])), 'best baseline comparison')


def audit(target, progress=None):
    checks=Checks();check,close=checks.check,checks.close;target=Path(target)
    def load(p): return json.loads(p.read_text())
    def snapshot(p,field='artifact'):
        m=load(p/'manifest.json');a=load(p/(field+'.json'))
        check(digest({k:v for k,v in m.items() if k!='snapshot_id'})==p.name==m['snapshot_id'],'immutable manifest identity')
        check(digest(a)==m[field+'_digest'],'artifact bytes binding')
        return m,a
    manifest,a=snapshot(target);spec=a['specification']
    design=load(target.parent/'designs'/a['design_id']/'design.json')
    check(digest(design)==a['design_id']==manifest['design_id'],'study design identity')
    check(design['specification']==spec and design['inputs']==a['inputs'] and design['code']==manifest['code'],'design/input/code binding')
    check(design['version']==a['version']==manifest['version'] and design['numpy_version']==manifest['numpy_version'],'design version/environment binding')
    for name,sha in manifest['code'].items():check(hashlib.sha256((target/'code'/name).read_bytes()).hexdigest()==sha,'preserved estimator source')
    check(spec['cases']==list(CASES) and set(a['cases'])==set(CASES),'four declared source cases')
    # These values are the existing pre-fit design, not adjusted after scoring.
    for k,v in {'initial_first':'2013-01','initial_last':'2019-12','selection_first':'2020-01','selection_last':'2023-12','locked_first':'2024-01',
        'horizons_months':[1,3,6,12],'pooling_correlations':[0.,.5,.9],'process_variance_grid':[.0001,.001,.01,.1,1.],
        'measurement_variance_grid':[.0001,.001,.01,.1],'seasonal_ridge':1.,'autoregression_ridge':10.}.items():check(spec[k]==v,'frozen temporal/numerical design '+k)
    failure_path=ROOT/spec['amendment_basis'];failure=load(failure_path)
    check(digest(failure)==failure_path.stem,'preserved failed-design amendment witness')
    nm,native=snapshot(ROOT/'data/traffic_vintages'/spec['source_snapshot_id'])
    rm,reviewed=snapshot(ROOT/'data/reviewed_traffic_vintages'/spec['reviewed_snapshot_id'])
    am,annual=snapshot(ROOT/'data/operating_history'/spec['annual_snapshot_id'],'panel')
    check(a['inputs']=={'native_artifact_digest':digest(native),'reviewed_artifact_digest':digest(reviewed),'annual_panel_digest':digest(annual)},'exact upstream inputs')
    source_audit_path=ROOT/'data/reviewed_traffic_checks'/(spec['reviewed_audit_id']+'.json');source_audit=load(source_audit_path)
    check(digest(source_audit)==spec['reviewed_audit_id'] and source_audit['snapshot_id']==rm['snapshot_id'],'reviewed original-source audit binding')
    native_audit_id='067486ee870fd57c0d29543a34f14986455cd6827f659c5503728c79c5037598'
    native_audit=load(ROOT/'data/traffic_vintage_checks'/(native_audit_id+'.json'))
    check(digest(native_audit)==native_audit_id and native_audit['snapshot_id']==nm['snapshot_id'] and native_audit['artifact_digest']==digest(native),'native original-source audit binding')
    histories={}
    for label,panel in (('native',native),('reviewed',reviewed)):
        histories[label]={}
        for o in panel['origins']:
            check(o['issue_on'] not in histories[label],'unique source issue')
            for identity in o['observation_ids']:check(digest(panel['observation_versions'][identity])==identity,'source observation identity')
            rows=[panel['observation_versions'][i] for i in o['observation_ids']]
            known=released(rows,o['issue_on'],checks)
            check(len(known)==len(rows),'origin stores only available observations')
            histories[label][o['issue_on']]=known
    check(set(histories['native'])==set(histories['reviewed']) and len(histories['native'])==48,'identical source issue coverage')
    annual_rows=annual_observations(annual,checks)
    histories['annual']={issue:released(annual_rows,issue,checks) for issue in histories['native']}
    expected_initial={(c,m,period(t)) for c,m in SERIES for t in range(number('2013-01'),number('2019-12')+1)}
    first=next(issue for issue in sorted(histories['native']) if {k for k in histories['native'][issue] if k[2]<='2019-12'}==expected_initial)
    initial={k:r for k,r in histories['native'][first].items() if k[2]<='2019-12'}
    anchor=a['initial_anchor'];check(anchor['issue_on']==first and anchor['rows']==list(initial.values()),'first complete initial anchor rows')
    check(anchor['published_on']==max(r['published_on'] for r in initial.values()),'initial release timestamp')
    scales={(c,m):statistics.mean(abs(r['rpm_million']-initial[c,m,period(number(k[2])-12)]['rpm_million']) for k,r in initial.items() if k[:2]==(c,m) and (c,m,period(number(k[2])-12)) in initial) for c,m in SERIES}
    check(len(anchor['mase_scales'])==12 and len(anchor['fixed_seasonal_means'])==144,'complete frozen baseline records')
    for s in anchor['mase_scales']:close(s['scale'],scales[s['carrier'],s['market']],'initial scale witness')
    for r in anchor['fixed_seasonal_means']:close(r['rpm_million'],statistics.mean(v['rpm_million'] for k,v in initial.items() if k[:2]==(r['carrier'],r['market']) and int(k[2][5:])==r['month']),'initial seasonal witness')
    requested=[(period(t),c,m,h,period(t+h)) for t in range(number(first[:7]),number('2023-11')+1) for c,m in SERIES for h in (1,3,6,12) if t+h<=number('2023-12')]
    check(set(a['history_fits'])=={'native','reviewed','annual'},'all three fitted source histories')
    for label,result in a['history_fits'].items():
        check(len(result['fits'])==42 and [key(r) for r in result['forecasts']]==requested,'complete requested forecasts/fits')
        check([r['origin'] for r in result['excluded_origins']]==[period(t) for t in range(number('2020-01'),number(first[:7]))],'exact withheld initial origins')
        for fit in result['fits']:
            origin=fit['origin'];issue=date(int(origin[:4]),int(origin[5:]),monthrange(int(origin[:4]),int(origin[5:]))[1]).isoformat()
            check(fit['cutoff']==issue and issue>=first,'month-end issue gate')
            known=histories[label][issue]
            check(fit['known_keys']==[list(k) for k in sorted(known)] and fit['known_digest']==digest([known[k] for k in sorted(known)]),'exact issue-specific revision sample')
            check(fit['max_known_publication']==max(r['published_on'] for r in known.values()),'newest publication gate')
            check(fit['latest_period_by_series']=={f'{c}:{m}':max(k[2] for k in known if k[:2]==(c,m)) for c,m in SERIES},'latest series observations')
            for model,is_ar,penalty in ((fit['seasonal'],False,1.),(fit['autoregression'],True,10.)):
                keys=sorted(k for k in known if not is_ar or all((*k[:2],period(number(k[2])-lag)) in known for lag in (1,12)))
                check(model['training_keys']==[list(k) for k in keys] and model['target_count']==len(keys) and model['training_digest']==digest([known[k] for k in keys]),'regression sample trace')
                x=np.array([basis(*k)+([math.log(known[*k[:2],period(number(k[2])-lag)]['rpm_million']) for lag in (1,12)] if is_ar else []) for k in keys]);y=np.array([math.log(known[k]['rpm_million']) for k in keys])
                shrink=np.eye(x.shape[1])*math.sqrt(penalty);shrink[:12,:12]=0
                beta=np.linalg.lstsq(np.vstack((x,shrink)),np.concatenate((y,np.zeros(x.shape[1]))),rcond=None)[0]
                for observed,expected in zip(model['coefficients'],beta):close(observed,expected,'augmented-design regression coefficient')
                check(len(model['coefficients'])==len(beta),'coefficient vector length')
                close(model['residual_rmse_log'],math.sqrt(statistics.mean(float(v)**2 for v in y-x@beta)),'regression residual RMSE')
            season=fit['seasonal']['coefficients']
            sequence=[{i:math.log(known[c,m,period(t)]['rpm_million'])-sum(a*b for a,b in zip(basis(c,m,period(t)),season)) for i,(c,m) in enumerate(SERIES) if (c,m,period(t)) in known} for t in range(number('2013-01'),number(origin)+1)]
            check([s['rho'] for s in fit['states']]==spec['pooling_correlations'],'state families')
            for state in fit['states']:
                check([(g['q'],g['r']) for g in state['grid_likelihoods']]==[(q,r) for q in spec['process_variance_grid'] for r in spec['measurement_variance_grid']],'complete finite grid')
                witnesses=[]
                for grid in state['grid_likelihoods']:
                    mean,cov,ll=scalar_filter(sequence,grid['q'],grid['r'],state['rho']);close(ll,grid['loglikelihood'],'scalar innovation likelihood');witnesses.append((ll,grid['q'],grid['r'],mean,cov))
                winner=max(witnesses,key=lambda w:w[0]);check((state['q'],state['r'])==winner[1:3],'selected finite-grid variances');close(state['innovation_loglikelihood'],winner[0],'selected likelihood')
                check(state['filtered_through']==origin,'filter end month')
                check(state['variance_grid_boundary']==(state['q'] in (spec['process_variance_grid'][0],spec['process_variance_grid'][-1]) or state['r'] in (spec['measurement_variance_grid'][0],spec['measurement_variance_grid'][-1])),'variance boundary flag')
                for i in range(12):
                    close(state['mean'][i],winner[3][i],'scalar posterior mean')
                    for j in range(12):close(state['covariance'][i][j],winner[4][i,j],'scalar posterior covariance')
            if progress:progress({'phase':'fit_verified','history':label,'origin':origin,'checks':checks.count})
        for r in result['forecasts']:
            fit=result['fits'][r['fit_index']];audit_point(r,fit,histories[label][r['cutoff']],initial,scales,checks)
    common=set(requested);assignments={CASES[0]:('native',native),CASES[1]:('native',reviewed),CASES[2]:('reviewed',reviewed),CASES[3]:('annual',reviewed)}
    for name,(label,panel) in assignments.items():
        case=a['cases'][name];check(case['history']==label and [key(r) for r in case['forecasts']]==requested,'case history/requested keys')
        targets={(r['carrier'],r['market'],r['period']):r for r in panel['evaluation_targets']['observations']}
        for f,r in zip(a['history_fits'][label]['forecasts'],case['forecasts']):
            check({k:v for k,v in r.items() if not k.startswith('actual_')}==f,'identical issued forecasts across target ablation')
            actual=targets[r['carrier'],r['market'],r['target']]
            check({k:r[k] for k in ('actual_rpm_million','actual_published_on','actual_source_sha256s','actual_corrections')}=={'actual_rpm_million':actual['rpm_million'],'actual_published_on':actual['published_on'],'actual_source_sha256s':actual['source_sha256s'],'actual_corrections':actual.get('source_corrections',[])},'exact evaluation target and provenance')
        failures=[{**{k:r[k] for k in ('origin','carrier','market','horizon_months','target')},'failures':r['model_failures']} for r in case['forecasts'] if r['model_failures']]
        check(case['model_failures']==failures,'complete failed forecast report')
        common&={key(r) for r in case['forecasts'] if all(v is not None and math.isfinite(v) and v>0 for v in r['predictions'].values())}
    check(a['common_forecast_keys']==[list(k) for k in sorted(common)] and a['common_predictions_per_model_per_case']==len(common) and a['requested_predictions_per_model_per_case']==len(requested),'global supported intersection')
    diagnostics={}
    for name,case in a['cases'].items():
        check(case['requested_predictions']==len(requested) and case['common_supported_predictions']==len(common),'case requested/retained counts')
        audit_scorecards(case,common,checks)
        eligible=[r for r in case['forecasts'] if key(r) in common]
        diagnostics[name]=[interval_diagnostics(eligible,s['model']) for s in case['scorecard'][:3]]
    return {'checks_passed':checks.count,'fits_verified':126,'requested_predictions_per_model_per_case':len(requested),'common_predictions_per_model_per_case':len(common),
        'upstream_source_audits':[native_audit_id,spec['reviewed_audit_id']], 'native_interval_diagnostics':diagnostics,
        'scope':'Independent issue-version/input/design bindings, augmented least-squares coefficients, every scalar-filter grid likelihood/posterior, forecasts/failures, target ablations and common point scores. Raw-source audits separately bound, not rerun here. Hindsight reconstruction and common-supported subset; not causal demand/pricing, calibrated forecasts, full-engine or investment validation.'}


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot',type=Path);args=parser.parse_args()
    root=ROOT/'data/monthly_traffic_vintages';target=args.snapshot or root/json.loads((root/'latest.json').read_text())['snapshot_id']
    result={'snapshot_id':target.name,'audit_code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'numerical_helpers_sha256':hashlib.sha256((ROOT/'scripts/audit_monthly_traffic.py').read_bytes()).hexdigest(),
        **audit(target,lambda event:print(json.dumps(event),file=sys.stderr,flush=True))}
    p=ROOT/'data/monthly_traffic_vintage_checks'/(digest(result)+'.json');p.parent.mkdir(exist_ok=True)
    content=json.dumps(result,indent=2,allow_nan=False)+'\n'
    if p.exists() and p.read_text()!=content:raise ValueError('Immutable statistical audit differs')
    if not p.exists():p.write_text(content)
    print(json.dumps({'audit_file':str(p), **{k:v for k,v in result.items() if k!='native_interval_diagnostics'}},indent=2))


if __name__=='__main__':main()
