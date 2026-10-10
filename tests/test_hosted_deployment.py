"""Exercise cold-instance replay and downloads without durable server storage."""
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
from app import server


def test_hosted_export_survives_missing_previous_instance_archive(tmp_path):
    script = r'''
import base64, io, json, shutil
from fastapi.testclient import TestClient
from pypdf import PdfReader
from app.server import app
from app.model_archive import ARCHIVE_ROOT
client = TestClient(app)
assert client.get('/api/health').json()['hosted_demo']
response = client.post('/api/monthly/simulate', json={'paths': 20})
assert response.status_code == 200, response.text
run = response.json()
shutil.rmtree(ARCHIVE_ROOT)  # Simulate the next instance having no prior archive.
notes = {'title':'Hosted test', 'hypothesis':'A conditional scenario.',
    'reference':'paired_no_shock', 'checkpoint_on':'2026-10-31',
    'checkpoint':'Review published operating results.',
    'checkpoint_source':'https://ir.united.com/financial-information/quarterly-results',
    'falsification':'Revisit if the operating assumptions disagree with observations.'}
response = client.post('/api/monthly/thesis', json={'run_record':run,'notes':notes})
assert response.status_code == 200, response.text
export = response.json()
pdf = base64.b64decode(export['pdf_url'].split(',',1)[1], validate=True)
record = json.loads(base64.b64decode(export['json_url'].split(',',1)[1], validate=True))
assert len(PdfReader(io.BytesIO(pdf)).pages) == 1
assert record['source_run']['manifest']['run_id'] == run['manifest']['run_id']
assert ARCHIVE_ROOT.is_relative_to(__import__('pathlib').Path(__import__('os').environ['UAL_RUNTIME_DIR']))
'''
    env = {**os.environ, "VERCEL": "1", "UAL_RUNTIME_DIR": str(tmp_path)}
    result = subprocess.run([sys.executable, "-c", script], env=env,
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_hosted_limits_and_local_quote_boundary(monkeypatch):
    monkeypatch.setattr(server, "HOSTED_DEMO", True)
    client = TestClient(server.app)
    assert client.post('/api/quotes', json={'csv_text':'test'}).status_code == 403
    assert client.delete('/api/quotes').status_code == 403
    grid = {'base_scenario':{'paths':10000},
        'x':{'driver':'fuel_shock_pct','low':0,'high':50,'points':7},
        'y':{'driver':'delivery_delay_months','low':0,'high':12,'points':7}}
    for route in ('failure-search','target-inversion'):
        response = client.post('/api/monthly/'+route, json=grid)
        assert response.status_code == 422
        assert 'Hosted grid budget' in response.json()['detail']
