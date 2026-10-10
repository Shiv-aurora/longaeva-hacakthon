"""Verified read-only access to the separate traffic source-ablation research."""
import hashlib
import json
from pathlib import Path
import re

from .evidence import digest
from .source_store import ROOT


def read_review(root=ROOT):
    root=Path(root)
    def load(p): return json.loads(p.read_text())
    def identity(v):
        if not isinstance(v,str) or re.fullmatch('[0-9a-f]{64}',v) is None: raise ValueError('Invalid research identity')
        return v
    review=load(root/'data/monthly_traffic_vintage_review.json')
    study_id=identity(review['snapshot_id']);audit_id=identity(review['audit_id'])
    p=root/'data/monthly_traffic_vintages'/study_id
    m=load(p/'manifest.json');a=load(p/'artifact.json')
    if digest({k:v for k,v in m.items() if k!='snapshot_id'})!=study_id or m['snapshot_id']!=study_id or digest(a)!=m['artifact_digest']:
        raise ValueError('Research artifact/manifest checksum differs')
    d=load(p.parent/'designs'/m['design_id']/'design.json')
    if digest(d)!=m['design_id'] or a['design_id']!=m['design_id'] or d['specification']!=a['specification'] or d['inputs']!=a['inputs'] or d['code']!=m['code']:
        raise ValueError('Research design/input/code binding differs')
    for name,sha in m['code'].items():
        if Path(name).name!=name or hashlib.sha256((p/'code'/name).read_bytes()).hexdigest()!=sha:
            raise ValueError('Preserved research source checksum differs')
    audit=load(root/'data/monthly_traffic_vintage_checks'/(audit_id+'.json'))
    if digest(audit)!=audit_id or audit['snapshot_id']!=study_id: raise ValueError('Independent statistical audit binding differs')
    for field in ('audit_code_sha256','numerical_helpers_sha256'):
        sha=identity(audit[field]);code=root/'data/monthly_traffic_vintage_checks/code'/(sha+'.py')
        if hashlib.sha256(code.read_bytes()).hexdigest()!=sha:raise ValueError('Independent audit source checksum differs')
    spec=a['specification']
    for folder,idfield,field,inputfield in (
        ('traffic_vintages','source_snapshot_id','artifact','native_artifact_digest'),
        ('reviewed_traffic_vintages','reviewed_snapshot_id','artifact','reviewed_artifact_digest'),
        ('operating_history','annual_snapshot_id','panel','annual_panel_digest')):
        upstream=root/'data'/folder/identity(spec[idfield]);um=load(upstream/'manifest.json');u=load(upstream/(field+'.json'))
        if digest({k:v for k,v in um.items() if k!='snapshot_id'})!=upstream.name or um['snapshot_id']!=upstream.name or digest(u)!=um[field+'_digest'] or digest(u)!=a['inputs'][inputfield]:
            raise ValueError('Upstream research input checksum differs')
    expected_audits=[review['native_source_audit_id'],spec['reviewed_audit_id']]
    if audit['upstream_source_audits']!=expected_audits:raise ValueError('Original-source audit list differs')
    for folder,aid,sid in (('traffic_vintage_checks',expected_audits[0],spec['source_snapshot_id']),('reviewed_traffic_checks',expected_audits[1],spec['reviewed_snapshot_id'])):
        report=load(root/'data'/folder/(identity(aid)+'.json'))
        if digest(report)!=aid or report['snapshot_id']!=sid:raise ValueError('Original-source audit binding differs')
    if review['status']!='numerically_verified_research' or review['review_known_on']!= '2026-10-09' or audit['requested_predictions_per_model_per_case']!=a['requested_predictions_per_model_per_case'] or audit['common_predictions_per_model_per_case']!=a['common_predictions_per_model_per_case']:
        raise ValueError('Research review/count binding differs')
    return p,a,audit,review


def vintage_research_status(root=ROOT):
    if not (Path(root)/'data/monthly_traffic_vintage_review.json').exists():
        return {'status':'not_built','message':'Reviewed traffic source comparison is not available.'}
    p,a,audit,review=read_review(root)
    return {'status':'available','snapshot_id':p.name,'audit_id':review['audit_id'],'checks_passed':audit['checks_passed'],
        'label':review['knowledge_label'],'scope':audit['scope'],
        'requested_predictions_per_model_per_case':a['requested_predictions_per_model_per_case'],
        'common_predictions_per_model_per_case':a['common_predictions_per_model_per_case'],
        'cases':[{'name':name,'selected_name':c['selected_name'],'beats_best_declared_baseline':c['beats_best_declared_baseline'],
            'failures':len(c['model_failures']),'scorecard':c['scorecard'],
            'selected_interval_coverage_80':next(d['equal_cell_coverage_80'] for d in audit['native_interval_diagnostics'][name] if d['model']==c['selected_name'])}
            for name,c in a['cases'].items()]}
