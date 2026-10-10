from fastapi.testclient import TestClient
from app.server import app
client=TestClient(app)

def test_bootstrap_and_health():
    r=client.get('/api/health');assert r.status_code==200
    r=client.get('/api/bootstrap');assert r.status_code==200
    data=r.json()
    assert data['fuel_last_price']==4.342
    assert data['real_world_status'].startswith('EIA historical')
    assert 'not investable' in data['signal_gate']

def test_simulate_endpoint_and_validation():
    r=client.post('/api/simulate',json={'paths':20,'horizon_days':30,'fuel_shock_pct':0})
    assert r.status_code==200
    assert r.json()['summary']['cumulative_operating_income_impact_musd']['p50']==0
    bad=client.post('/api/simulate',json={'paths':10000})
    assert bad.status_code==422

def test_website_and_template():
    assert client.get('/').status_code==200
    assert client.get('/').content == client.get('/monthly').content
    assert b'monthly.js' in client.get('/').content
    assert client.get('/fuel-pilot').status_code==200
    assert b'Price shocks.' in client.get('/fuel-pilot').content
    assert client.get('/static/quotes_template.csv').status_code==200
    assert 'fares' not in client.get('/api/bootstrap').json().get('quotes',{}).get('status','').lower()

def test_upload_rejects_incomplete_quote_csv():
    r=client.post('/api/quotes',json={'csv_text':'bad,data\nhello,world'})
    assert r.status_code==422
