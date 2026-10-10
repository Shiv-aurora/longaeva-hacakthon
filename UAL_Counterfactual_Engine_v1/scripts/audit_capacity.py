"""Independent original-CSV capacity targets, ridge fit, recursion and score audit.

Does not import the capacity builder, fitting, forecasting or metric functions.
The original CSV is filtered to 2013–23 before reading target quantities.
"""
from collections import defaultdict
from decimal import Decimal
from io import BytesIO, StringIO
from zipfile import ZipFile
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.evidence import digest
from app.source_store import read_source

SERIES=[(c,g) for c in ('UA','DL','AA') for g in ('narrowbody','widebody')]
FIELDS=('AVL_SEAT_MILES_320','REV_PAX_MILES_140','HOURS_AIRBORNE_650','AIR_DAYS_EQUIP_810')


def quarter(p,n):
    y,q=p.split('-Q');index=int(y)*4+int(q)-1+n
    return f'{index//4:04d}-Q{index%4+1}'


def close(a,b,label):
    if not math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-5):raise ValueError(label)


def features(c,g,p,a,b):
    return [1.,a,b]+[float((c,g)==s) for s in SERIES[1:]]+[float(g==group and p[-1]==str(q)) for group in ('narrowbody','widebody') for q in (2,3,4)]


def raw_targets(manifest,observed,excluded):
    classes={r['aircraft_type']:r['fleet_group'] for r in manifest['classification']['rows']}
    content,_=read_source(manifest['source']['sha256']);groups=defaultdict(list);unknown=set()
    with ZipFile(BytesIO(content)) as z:
        members=[n for n in z.namelist() if n.lower().endswith('.csv')]
        if len(members)!=1:raise ValueError('Original T2 CSV member is ambiguous')
        reader=csv.DictReader(StringIO(z.read(members[0]).decode('utf-8-sig')))
        for r in reader:
            p=f"{int(r['YEAR']):04d}-Q{int(r['QUARTER'])}"
            if not '2013-Q1'<=p<='2023-Q4':continue
            if r['UNIQUE_CARRIER'] not in {'UA','DL','AA'} or r['SERVICE_CLASS']!='Z' or r['CARRIER_REGION'] not in {'D','A','P','L'} or r['AIRCRAFT_CONFIG'] not in {'1','3'} or r['AIRCRAFT_TYPE']=='999':continue
            group=classes.get(r['AIRCRAFT_TYPE'])
            if group is None:unknown.add((r['UNIQUE_CARRIER'],p));continue
            groups[r['UNIQUE_CARRIER'],group,p].append((reader.line_num,r))
    stored={(r['unique_carrier'],r['fleet_group'],r['period']):r for r in observed}
    withheld={(r['carrier'],r['group'],r['period']) for r in excluded}
    if set(groups)!=set(stored)|withheld or set(stored)&withheld:raise ValueError('Original group-quarter accounting differs')
    values={};rows_checked=0
    for key,items in groups.items():
        invalid=(key[0],key[2]) in unknown or len({r['AIRLINE_ID'] for _,r in items})!=1 or any(not r['AIRLINE_ID'] or not r['UNIQUE_CARRIER_ENTITY'] or r['NUM_MONTHS']!='3' for _,r in items)
        totals={}
        for field in FIELDS:
            quantities=[Decimal(r[field]) if r[field].strip() else None for _,r in items]
            totals[field]=sum(quantities,Decimal(0)) if all(v is not None and v>=0 for v in quantities) else None
        invalid=invalid or any(r['REV_PAX_MILES_140'].strip() and r['AVL_SEAT_MILES_320'].strip() and Decimal(r['REV_PAX_MILES_140'])>Decimal(r['AVL_SEAT_MILES_320']) for _,r in items)
        asm,hours,days=totals[FIELDS[0]],totals[FIELDS[2]],totals[FIELDS[3]]
        eligible=not invalid and asm is not None and hours is not None and days is not None and hours>0 and days>0 and hours/days<=24
        if eligible!=(key in stored):raise ValueError(f'Original common capacity target gate differs: {key}')
        rows_checked+=len(items)
        if not eligible:continue
        v={'capacity':float(asm/days),'utilization':float(hours/days),'productivity':float(asm/hours)}
        for component,value in v.items():close(value,stored[key]['values'][component],'Original capacity component differs')
        if sorted(line for line,_ in items)!=sorted(stored[key]['source_lines']):raise ValueError('Original source line set differs')
        if stored[key]['source_sha256s']!=[manifest['source']['sha256']]:raise ValueError('Original source hash differs')
        values[key]=v
    return values,rows_checked


def independent_fits(history,saved):
    train={k:v for k,v in history.items() if k[2]<='2019-Q4'}
    keys=[k for k in sorted(train) if (*k[:2],quarter(k[2],-1)) in train and (*k[:2],quarter(k[2],-4)) in train]
    fits={}
    for fitted in saved:
        penalty=fitted['ridge_penalty']
        expected=[{'carrier':c,'group':g,'period':p} for c,g,p in keys]
        if fitted['training_target_keys']!=expected:raise ValueError('Training keys are not the common pre-2020 targets')
        for c,g in SERIES:
            if fitted['training_series_counts'][f'{c}:{g}']!=sum(k[:2]==(c,g) for k in keys):raise ValueError('Training series count differs')
        components={}
        for component in ('capacity','utilization','productivity'):
            x=np.array([features(c,g,p,train[c,g,quarter(p,-1)][component],train[c,g,quarter(p,-4)][component]) for c,g,p in keys])
            y=np.array([train[k][component] for k in keys])
            center=x.mean(axis=0);scale=x.std(axis=0);center[0]=0.;scale[0]=1.;scale[scale<1e-12]=1.
            z=(x-center)/scale;ridge=np.diag([0.]+[penalty]*13)
            beta=np.linalg.solve(z.T@z+ridge,z.T@y)
            m=fitted['components'][component]
            for a,b in zip(center,m['feature_center']):close(float(a),b,'Training-only centering differs')
            for a,b in zip(scale,m['feature_scale']):close(float(a),b,'Training-only scaling differs')
            # Verify fitted outputs, as coefficient representation can be sensitive
            # to float sums of original Decimal source quantities.
            if not np.allclose(z@beta,z@np.array(m['coefficients']),rtol=1e-9,atol=1e-5):raise ValueError('Independent ridge fit differs')
            close(float(np.sqrt(np.mean((y-z@beta)**2))),m['training_residual_RMSE'],'Training residual RMSE differs')
            components[component]=(center,scale,beta)
        fits[penalty]=components
    return fits,keys


def recurse(models,architecture,history,c,g,origin,horizon):
    state={p:dict(v) for (carrier,group,p),v in history.items() if (carrier,group)==(c,g) and p<=origin}
    result=[]
    for step in range(1,horizon+1):
        p=quarter(origin,step);a=state.get(quarter(p,-1));b=state.get(quarter(p,-4))
        if a is None or b is None:return None
        components=('capacity',) if architecture=='direct_capacity' else ('utilization','productivity')
        prediction={}
        for component in components:
            center,scale,beta=models[component];value=float(((np.array(features(c,g,p,a[component],b[component]))-center)/scale)@beta)
            prediction[component]=min(24.,max(0.,value)) if component=='utilization' else max(0.,value)
        if architecture!='direct_capacity':prediction['capacity']=prediction['utilization']*prediction['productivity']
        state[p]=prediction;result.append({'period':p,'capacity_asm_per_assigned_day':prediction['capacity'],
            'reported_hours_per_assigned_day':prediction.get('utilization'),'asm_per_reported_hour':prediction.get('productivity')})
    return result


def verify_scores(scorecard,predictions,history):
    checked=[]
    for score in scorecard:
        means=[];count=0
        for cell in score['series_horizon_scores']:
            errors=[p['prediction_asm_per_day']-history[p['carrier'],p['group'],p['target']]['capacity'] for p in predictions if p['model']==score['model'] and (p['carrier'],p['group'],p['horizon_quarters'])==(cell['carrier'],cell['group'],cell['horizon_quarters'])]
            if len(errors)!=cell['targets']:raise ValueError('Score cell count differs')
            mae=sum(abs(e) for e in errors)/len(errors);rmse=math.sqrt(sum(e*e for e in errors)/len(errors));bias=sum(errors)/len(errors)
            for a,b in ((mae,cell['MAE_asm_per_day']),(rmse,cell['RMSE_asm_per_day']),(bias,cell['mean_error_asm_per_day'])):close(a,b,'Independent score arithmetic differs')
            means.append(mae);count+=len(errors)
        if len(means)!=score['series_horizon_cells'] or count!=score['prediction_count']:raise ValueError('Model score counts differ')
        overall=sum(means)/len(means);close(overall,score['selection_equal_weight_MAE_asm_per_day'],'Equal-weight score differs')
        checked.append({'model':score['model'],'equal_weight_MAE_asm_per_day':overall,'prediction_count':count,'cells':len(means)})
    return checked


def audit(path):
    manifest=json.loads((path/'manifest.json').read_text());artifact=json.loads((path/'artifact.json').read_text())
    identity=manifest['snapshot_id']
    if digest({k:v for k,v in manifest.items() if k!='snapshot_id'})!=identity or digest(artifact)!=manifest['artifact_digest']:raise ValueError('Estimate checksum failed')
    if hashlib.sha256((path/'builder.py').read_bytes()).hexdigest()!=manifest['builder_code_sha256']:raise ValueError('Builder checksum failed')
    declared=json.loads((path.parent/'designs'/manifest['design_id']/'design.json').read_text())
    if digest(declared['design'])!=manifest['design_id'] or declared['design']['specification']!=artifact['specification'] or declared['design']['builder_code_sha256']!=manifest['builder_code_sha256']:raise ValueError('Frozen design differs')
    source=ROOT/'data/t2_history'/manifest['t2_snapshot_id'];upstream=json.loads((source/'manifest.json').read_text())
    if digest({k:v for k,v in upstream.items() if k!='snapshot_id'})!=manifest['t2_snapshot_id']:raise ValueError('Upstream manifest checksum failed')
    history,nrows=raw_targets(upstream,artifact['panel']['observations'],artifact['panel']['excluded_group_quarters'])
    result=artifact['estimation'];fits,keys=independent_fits(history,result['fitted_models'])
    architectures=('direct_capacity','utilization_times_productivity');penalties=(.1,1.,10.,100.)
    names=[f'{a}_ridge_{p:g}' for a in architectures for p in penalties]+['seasonal_capacity_persistence','fixed_training_seasonal_capacity_mean']
    saved={}
    for p in result['selection_predictions']:
        key=(p['carrier'],p['group'],p['origin'],p['horizon_quarters'],p['model'])
        if key in saved:raise ValueError('Duplicate prediction')
        saved[key]=p
    exclusions={}
    for p in result['excluded_forecasts']:
        k=(p['carrier'],p['group'],p['origin'],p['horizon_quarters'])
        if k in exclusions:raise ValueError('Duplicate exclusion')
        exclusions[k]=p
    checked=set();withheld=set();origin='2019-Q4'
    while origin<'2023-Q4':
        for c,g in SERIES:
            for h in (1,2,4):
                target=quarter(origin,h)
                if target>'2023-Q4':continue
                actual=history.get((c,g,target));reference=history.get((c,g,quarter(target,-4)))
                season=[v['capacity'] for (cc,gg,p),v in history.items() if (cc,gg)==(c,g) and p<='2019-Q4' and p[-1]==target[-1]]
                paths={f'{a}_ridge_{penalty:g}':recurse(fits[penalty],a,history,c,g,origin,h) for a in architectures for penalty in penalties}
                reasons=[]
                if actual is None:reasons.append('missing_common_source_eligible_actual')
                if reference is None:reasons.append('missing_seasonal_reference_at_origin')
                if not season:reasons.append('missing_fixed_training_seasonal_mean')
                if any(v is None for v in paths.values()):reasons.append('missing_recursive_lag_state_at_origin')
                k=(c,g,origin,h)
                if reasons:
                    if k not in exclusions or exclusions[k]['reasons']!=reasons or exclusions[k]['target']!=target:raise ValueError('Independent exclusion accounting differs')
                    withheld.add(k);continue
                for name in names:
                    p=saved.get((*k,name))
                    if p is None or p['target']!=target:raise ValueError('Common forecast comparison missing')
                    close(p['actual_asm_per_day'],actual['capacity'],'Forecast target differs')
                    regime='disruption' if target[:4] in ('2020','2021') else 'recovery'
                    if p['regime']!=regime:raise ValueError('Forecast regime differs')
                    if name in paths:
                        expected=paths[name]
                        if len(p['forecast_path'])!=h:raise ValueError('Recursive path length differs')
                        for independent,stored in zip(expected,p['forecast_path']):
                            if independent['period']!=stored['period']:raise ValueError('Recursive quarter differs')
                            for field in ('capacity_asm_per_assigned_day','reported_hours_per_assigned_day','asm_per_reported_hour'):
                                a,b=independent[field],stored[field]
                                if (a is None)!=(b is None):raise ValueError('Recursive architecture fields differ')
                                if a is not None:close(a,b,'Independent recursive prediction differs')
                        prediction=expected[-1]['capacity_asm_per_assigned_day']
                    else:
                        if p['forecast_path'] is not None:raise ValueError('Baseline unexpectedly has a model path')
                        prediction=reference['capacity'] if name==names[-2] else sum(season)/len(season)
                    close(prediction,p['prediction_asm_per_day'],'Independent endpoint prediction differs');checked.add((*k,name))
        origin=quarter(origin,1)
    if checked!=set(saved) or withheld!=set(exclusions):raise ValueError('Extra model comparisons or exclusions')
    scorecard=verify_scores(result['scorecard'],list(saved.values()),history)
    regimes=[{'regime':r['regime'],'scores':verify_scores(r['scores'],[p for p in saved.values() if p['regime']==r['regime']],history)} for r in result['regime_scores']]
    selected=min(scorecard[:8],key=lambda r:r['equal_weight_MAE_asm_per_day'])
    if result['selected_name']!=selected['model']:raise ValueError('Declared model selection order differs')
    return {'version':'quarterly-capacity-independent-audit-0.1.0','snapshot_id':identity,'t2_source_sha256':upstream['source']['sha256'],
        'original_source_rows_checked':nrows,'eligible_common_component_group_quarters':len(history),'excluded_group_quarters':len(artifact['panel']['excluded_group_quarters']),
        'training_targets_checked':len(keys),'frozen_ridge_component_fits_checked':12,'recursive_or_baseline_forecasts_checked':len(checked),
        'excluded_common_forecasts_checked':len(withheld),'scores':scorecard,'regime_scores':regimes,
        'audit_code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'scope':'Independent original-CSV common eligibility, targets, training-only ridge fits, post-origin isolation, recursive paths, two baselines, exclusions, MAE/RMSE/bias and selection order. No 2024+ forecast scores, interval or whole-engine validation.'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot',type=Path);args=parser.parse_args()
    path=args.snapshot or ROOT/'data/quarterly_capacity'/json.loads((ROOT/'data/quarterly_capacity/latest.json').read_text())['snapshot_id']
    report=audit(path);destination=ROOT/'data/quarterly_capacity_checks';destination.mkdir(exist_ok=True)
    output=destination/(report['snapshot_id']+'.json')
    if output.exists() and json.loads(output.read_text())!=report:raise ValueError('Immutable capacity audit differs')
    if not output.exists():output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('scores','regime_scores')},indent=2))


if __name__=='__main__':main()
