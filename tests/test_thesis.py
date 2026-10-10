from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path

from pypdf import PdfReader
import pytest

from app.model_archive import capture_source, digest, run_preserved
from app.monthly import MonthlyScenario, simulate_monthly
from app.thesis import ThesisNotes, export_thesis, make_record, render_pdf, verified_export, replay_thesis, _write_immutable


@pytest.fixture(scope='module')
def saved():
    return run_preserved(capture_source('monthly'),lambda:simulate_monthly(MonthlyScenario(paths=20,stochastic=False,aircraft_unavailable_pct=10,delivery_delay_months=9)))


@pytest.fixture
def notes():
    return ThesisNotes('Test sensitivity','Hypothesis: the assumed response partly offsets the fuel shock.','frozen_policy','2026-10-31',
        'Compare reported fuel cost, yield and cash timing with the assumptions.','https://ir.united.com/',
        'Observed pricing, cash timing or capacity restrictions contradict the assumed response.')


def test_handoff_contains_scenario_distribution_constraint_reference_and_analyst_evidence(saved,notes):
    r=make_record(saved,notes)
    assert r['scenario']==saved['scenario'] and r['run_id']==saved['manifest']['run_id']
    assert r['analyst_notes']['checkpoint']==notes.checkpoint
    assert r['analyst_notes']['falsification']==notes.falsification
    assert r['constraints']['months_with_positive_median_schedule_shortfall']>0
    shortfall=saved['intervention']['monthly']['unserved_schedule_asm_million']
    peak=max(x['p50'] for x in shortfall)
    assert r['constraints']['peak_monthly_median_schedule_shortfall_asm_million']==peak
    assert r['constraints']['fleet_deficits'][0]['aircraft_below_planned_available']['narrowbody']>0
    assert r['distributions'][0]['quantiles']['p50']==pytest.approx(saved['summary']['ending_cash_musd']['p50']-saved['baseline_inputs']['cash_musd'])
    assert r['evidence']==saved['evidence'] and r['assumptions']==saved['assumptions']
    assert r['cash_floor']['below_month_end_floor_path_fraction']==saved['summary']['cash_floor_breach_path_fraction']


def test_chosen_reference_uses_saved_paired_differences_not_subtracted_medians(saved,notes):
    r=deepcopy(saved)
    r['summary']['response_contribution_to_fcf_musd']={'p10':1.,'p50':2.,'p90':3.}
    r['summary']['cumulative_fcf_impact_musd']={'p10':101.,'p50':102.,'p90':103.}
    assert make_record(r,notes)['chosen_reference']['difference_quantiles_musd']['p50']==2.
    assert make_record(r,replace(notes,reference='paired_no_shock'))['chosen_reference']['difference_quantiles_musd']['p50']==102.


@pytest.mark.parametrize('changes',[
    {'checkpoint_on':'2026-07-16'}, {'checkpoint_on':'2026-02-31'}, {'reference':'analyst_consensus'},
    {'checkpoint_source':'javascript:alert(1)'},{'checkpoint_source':'https://user:secret@example.com'},
    {'hypothesis':' '},{'hypothesis':'x'*221},{'hypothesis':'hidden\x00text'}, {'title':'unsupported emoji 💸'},
])
def test_missing_invalid_or_unsupported_notes_are_explicitly_rejected(notes,changes):
    with pytest.raises(ValueError):replace(notes,**changes).validate('2026-07-16')


def test_one_page_pdf_is_deterministic_and_escapes_analyst_markup(saved,notes):
    n=replace(notes,hypothesis='A <b>literal</b> & "quoted" hypothesis, with café as ordinary text.')
    record=make_record(saved,n);pdf=render_pdf(record)
    assert pdf==render_pdf(record)
    reader=PdfReader(BytesIO(pdf));assert len(reader.pages)==1
    text=reader.pages[0].extract_text()
    for expected in ('A <b>literal</b> & "quoted"','café','VALIDATION PENDING','Next observable checkpoint','Evidence that would overturn','Model','No automated trading'):
        assert expected in text
    assert 'USD millions' in text and 'not a calibrated probability' in text
    links=[a.get_object()['/A']['/URI'] for a in reader.pages[0].get('/Annots',[]) if '/A' in a.get_object()]
    assert notes.checkpoint_source in links


def test_maximum_length_notes_remain_one_page(saved,notes):
    long=replace(notes,title='W'*70,hypothesis='W'*220,checkpoint='W'*180,falsification='W'*220,checkpoint_source='https://example.com/'+'w'*160)
    long.validate('2026-07-16')
    assert len(PdfReader(BytesIO(render_pdf(make_record(saved,long)))).pages)==1


def test_export_immutable_repeat_offline_replay_and_tamper_checks(saved,notes,tmp_path):
    first=export_thesis(saved,notes,tmp_path)
    browser=json.loads(json.dumps(saved),parse_float=lambda x:int(float(x)) if float(x).is_integer() else float(x))
    assert export_thesis(browser,notes,tmp_path)['artifact_id']==first['artifact_id']
    assert replay_thesis(first['artifact_id'],tmp_path)['status']=='numerical derivation and one-page PDF replay verified'
    folder,manifest,companion=verified_export(first['artifact_id'],tmp_path)
    assert companion['source_run']['manifest']['run_id']==saved['manifest']['run_id']
    assert (folder/'font-license.txt').stat().st_size>0
    original=(folder/'thesis.pdf').read_bytes();(folder/'thesis.pdf').write_bytes(b'changed')
    with pytest.raises(ValueError,match='checksum'):verified_export(first['artifact_id'],tmp_path)
    with pytest.raises(ValueError,match='Immutable'):export_thesis(saved,notes,tmp_path)
    (folder/'thesis.pdf').write_bytes(original)
    assert verified_export(first['artifact_id'],tmp_path)[1]==manifest


def test_altered_run_even_with_rehashed_receipt_cannot_be_exported(saved,notes,tmp_path):
    modified=deepcopy(saved)
    modified['summary']['ending_cash_musd']['p50']+=100
    from app.model_archive import deterministic
    modified['replay_code']['result_sha256']=digest(deterministic(modified))
    with pytest.raises(ValueError):export_thesis(modified,notes,tmp_path)
    assert not list(tmp_path.iterdir())


def test_atomic_file_write_preserves_first_content(tmp_path):
    path=tmp_path/'a.json';_write_immutable(path,b'first');_write_immutable(path,b'first')
    with pytest.raises(ValueError,match='Immutable'):_write_immutable(path,b'second')
    assert path.read_bytes()==b'first'
    assert list(tmp_path.iterdir())==[path]


def test_api_export_downloads_and_corruption_gate(saved,notes,tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    import app.server as server
    monkeypatch.setattr(server,'export_thesis',lambda r,n:export_thesis(r,n,tmp_path))
    monkeypatch.setattr(server,'verified_export',lambda i:verified_export(i,tmp_path))
    client=TestClient(server.app)
    response=client.post('/api/monthly/thesis',json={'run_record':saved,'notes':notes.__dict__})
    assert response.status_code==200
    result=response.json();pdf=client.get(result['pdf_url']);assert pdf.status_code==200
    assert pdf.headers['content-type']=='application/pdf'
    assert len(PdfReader(BytesIO(pdf.content)).pages)==1
    j=client.get(result['json_url']);assert j.status_code==200
    assert j.json()['source_run']['manifest']['run_id']==saved['manifest']['run_id']
    assert client.get('/api/theses/'+'a'*64+'/pdf').status_code==404
    assert client.get('/api/theses/'+result['artifact_id']+'/builder.py').status_code==404
    (tmp_path/result['artifact_id']/'thesis.json').write_text('{}')
    assert client.get(result['pdf_url']).status_code==503
    bad={**notes.__dict__,'model_claim':'validated'}
    assert client.post('/api/monthly/thesis',json={'run_record':saved,'notes':bad}).status_code==422
    assert client.post('/api/monthly/thesis',json={'run_record':saved,'notes':{**notes.__dict__,'checkpoint_on':'2026-01-01'}}).status_code==422
