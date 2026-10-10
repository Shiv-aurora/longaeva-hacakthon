import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.evidence import digest
from app.traffic_vintage_research import vintage_research_status
from app.server import app

ROOT=Path(__file__).parents[1]
DOC='docs/reviewed_traffic.md (scripts/fit_monthly_traffic_vintages.py, scripts/audit_monthly_traffic_vintages.py)'


@pytest.fixture
def preserved_study(require_local_cache):
    # data/monthly_traffic_vintage_review.json is tracked, but the study and audit record it
    # pins are local, untracked caches. Absent from a fresh clone, where the reader tests skip.
    # A study that is present but incomplete or altered is not skipped: the reader must reject it.
    review=json.loads((ROOT/'data/monthly_traffic_vintage_review.json').read_text())
    require_local_cache('data/monthly_traffic_vintages/'+review['snapshot_id']+'/manifest.json',DOC,
        'preserved four-case traffic source-comparison study pinned by data/monthly_traffic_vintage_review.json')
    require_local_cache('data/monthly_traffic_vintage_checks/'+review['audit_id']+'.json',DOC,
        'independent statistical audit record pinned by data/monthly_traffic_vintage_review.json')


def test_verified_research_exposes_common_counts_and_failures(preserved_study):
    s=vintage_research_status()
    assert s['status']=='available' and s['checks_passed']==735982
    assert s['requested_predictions_per_model_per_case']==1800 and s['common_predictions_per_model_per_case']==1763
    assert [c['failures'] for c in s['cases']]==[37,37,37,0]
    assert all(not c['beats_best_declared_baseline'] for c in s['cases'])


def test_artifact_or_audit_corruption_blocks_reader(preserved_study,monkeypatch):
    original=Path.read_text
    def altered(path,*args,**kwargs):
        content=original(path,*args,**kwargs)
        if path.name=='artifact.json' and 'monthly_traffic_vintages' in path.parts:
            a=json.loads(content);a['common_predictions_per_model_per_case']=1800;return json.dumps(a)
        return content
    monkeypatch.setattr(Path,'read_text',altered)
    with pytest.raises(ValueError,match='artifact/manifest'):vintage_research_status()
    def corrupted_audit(path,*args,**kwargs):
        content=original(path,*args,**kwargs)
        if path.parent.name=='monthly_traffic_vintage_checks':
            a=json.loads(content);a['checks_passed']+=1;return json.dumps(a)
        return content
    monkeypatch.setattr(Path,'read_text',corrupted_audit)
    with pytest.raises(ValueError,match='audit binding'):vintage_research_status()


def test_api_downloads_match_verified_artifact_and_audit(preserved_study):
    client=TestClient(app);s=client.get('/api/traffic-vintages/status').json()
    record=client.get('/api/traffic-vintages/record');audit=client.get('/api/traffic-vintages/audit')
    assert record.status_code==audit.status_code==200
    m=json.loads((ROOT/'data/monthly_traffic_vintages'/s['snapshot_id']/'manifest.json').read_text())
    assert digest(record.json())==m['artifact_digest'] and digest(audit.json())==s['audit_id']


def test_corruption_gates_all_three_routes(monkeypatch):
    def reject():raise ValueError('altered preserved research source')
    monkeypatch.setattr('app.server.vintage_research_status',reject)
    client=TestClient(app)
    for suffix in ('status','record','audit'):
        r=client.get('/api/traffic-vintages/'+suffix)
        assert r.status_code==503 and 'failed verification' in r.json()['detail']
