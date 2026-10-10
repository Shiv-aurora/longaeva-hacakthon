from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path

import numpy as np
from pypdf import PdfReader
import pytest
from fastapi.testclient import TestClient

from app.evidence import load_snapshot
from app.failure_search import FailureSearch, SearchAxis, search_failures
from app.fleet import GROUPS, PURCHASE_FLOWS, PURCHASE_STOCKS, capacity_paths, fleet_plan
from app.model_archive import capture_source, deterministic, replay_saved, run_preserved
from app.monthly import MonthlyScenario, _rollout, _world, baseline, month_sequence, shock_weights, simulate_monthly
from app.server import app
from app.target_inversion import TargetInversion, invert_target
from app.thesis import ThesisNotes, make_record, render_pdf


def plan():
    return json.loads((Path(__file__).resolve().parents[1] / 'data/purchase_plan_example.json').read_text())


def scenario(**kw):
    return MonthlyScenario(**dict(paths=20, stochastic=False, fuel_shock_pct=0, purchase_plan=plan(),
                                  delivery_delay_months=9, delivery_aircraft_count=30,
                                  delivery_group='narrowbody', **kw))


def fleet(s):
    b = baseline(load_snapshot(), s.information_cutoff, s.cash_plan, s.purchase_plan)
    return capacity_paths(b, s, b['fleet_plan'], month_sequence(s.information_cutoff, s.horizon_months), shock_weights(s))


def test_exact_30_delay_splits_batch_preserves_inventories_and_unallocated_entries():
    s = scenario(); f = fleet(s)
    assert [(e['month'],e['aircraft']) for e in f['intervention']['effective_queue'] if 'purchase_id' in e] == [
        ('2027-06',20),('2026-12',10),('2027-09',10)]
    assert sum(e['aircraft'] for e in f['intervention']['effective_queue'] if 'original_month' in e)==30
    assert f['intervention']['monthly'][1]['entries']['narrowbody']==7
    assert f['intervention']['monthly'][4]['entries']['narrowbody']==20  # 10 MAX + 10 A321
    assert f['intervention']['purchase_mapping']['allocated_aircraft']==40
    assert sum(e['aircraft'] for e in f['intervention']['purchase_mapping']['unallocated_mainline_entries'])==29
    for track in (f['control'],f['intervention']):
        for r in track['monthly']:
            for g in GROUPS:assert r['closing_inventory'][g]==r['opening_inventory'][g]+r['entries'][g]-r['exits'][g]
    assert f['intervention']['monthly'][10]['opening_inventory']['narrowbody']==894
    assert f['intervention']['monthly'][11]['opening_inventory']['narrowbody']==914  # June settlement enters July service
    long = fleet(replace(s,horizon_months=24))
    assert long['control']['monthly'][-1]['closing_inventory']==long['intervention']['monthly'][-1]['closing_inventory']
    assert not long['intervention']['purchase_cash']['pending_settlements']


def test_deposits_stay_paid_settlement_borrowing_and_interest_follow_the_same_queue():
    f=fleet(scenario());c=f['control']['purchase_cash']['monthly'];d=f['intervention']['purchase_cash']['monthly']
    assert c[0]['purchase_deposit_cash_musd']==d[0]['purchase_deposit_cash_musd']==80
    assert c[0]['allocated_purchase_deposits_musd']==d[0]['allocated_purchase_deposits_musd']==240
    assert c[1]['purchase_settlement_cash_musd']==680 and d[1]['purchase_settlement_cash_musd']==0
    assert c[1]['purchase_asset_additions_musd']==800
    assert c[1]['purchase_borrowing_musd']==510 and c[1]['purchase_financing_fee_musd']==pytest.approx(5.1)
    assert c[1]['purchase_interest_musd']==0 and c[2]['purchase_interest_musd']==pytest.approx(2.55)
    assert d[4]['purchase_settlement_cash_musd']==340 and d[4]['purchase_borrowing_musd']==255
    assert d[4]['purchase_interest_musd']==0 and d[5]['purchase_interest_musd']==pytest.approx(1.275)
    assert d[4]['allocated_purchase_deposits_musd']==180
    assert d[10]['purchase_settlement_cash_musd']==680
    assert d[10]['allocated_purchase_deposits_musd']==60
    assert d[10]['purchase_debt_principal_musd']==765
    assert d[11]['purchase_interest_musd']==pytest.approx(3.825)
    assert d[11]['cumulative_purchase_asset_additions_musd']==1200
    assert [(e['month'],e['aircraft']) for e in f['intervention']['purchase_cash']['pending_settlements']]==[('2027-09',10)]


def test_long_horizon_purchase_cash_plus_opening_deposits_equals_acquired_cost():
    f=fleet(scenario(horizon_months=24))
    for name in ('control','intervention'):
        cash=f[name]['purchase_cash'];rows=cash['monthly']
        assert sum(r['purchase_deposit_cash_musd']+r['purchase_settlement_cash_musd'] for r in rows)+cash['opening_allocated_deposits_musd']==1600
        assert rows[-1]['allocated_purchase_deposits_musd']==0
        assert rows[-1]['cumulative_purchase_asset_additions_musd']==1600
        assert rows[-1]['purchase_debt_principal_musd']==1020


def test_replacement_capex_and_financing_are_not_double_counted_in_fcf_or_income():
    # Spare capacity and a fixed schedule isolate cash identities from operating changes.
    s=scenario(schedule_growth_fraction=0,fleet_spare_fraction=.2)
    r=simulate_monthly(s);m=r['intervention']['monthly'];c=r['control']['monthly']
    assert m['cash_capex_musd'][0]['p50']==180  # residual 100 + new deposits 80, no historical 447.7 added
    assert m['cash_capex_musd'][1]['p50']==100
    assert c['cash_capex_musd'][1]['p50']==780
    assert r['incremental']['monthly']['operating_income_musd'][1]['p50']==0
    assert r['incremental']['monthly']['free_cash_flow_musd'][1]['p50']==680
    assert r['incremental']['monthly']['cash_musd'][1]['p50']==pytest.approx(175.1)
    assert m['cash_musd'][0]['p50']==pytest.approx(10166+m['free_cash_flow_musd'][0]['p50'])  # opening deposits not paid again
    assert m['debt_principal_musd'][-1]['p50']==23081+765
    assert r['summary']['ending_cash_musd']['p50']-10166==pytest.approx(
        r['summary']['cumulative_fcf_musd']['p50']+765-7.65,abs=.0001)


def test_all_paths_cash_fcf_and_subset_rollforwards_reconcile_and_replay(tmp_path):
    s=replace(scenario(),stochastic=True,fuel_shock_pct=25)
    b=baseline(load_snapshot(),s.information_cutoff,s.cash_plan,s.purchase_plan)
    r=_rollout(b,s,*_world(s,4,.2),4,True,s.response_mode)
    np.testing.assert_allclose(r['cash_musd'],10166+r['net_cash_change_musd'].cumsum(axis=1))
    np.testing.assert_allclose(r['free_cash_flow_musd'],r['operating_cash_flow_musd']-r['cash_capex_musd'])
    np.testing.assert_allclose(r['cash_capex_musd'],r['residual_cash_capex_musd']+r['purchase_deposit_cash_musd']+r['purchase_settlement_cash_musd'])
    np.testing.assert_allclose(r['purchase_debt_principal_musd'],r['purchase_borrowing_musd'].cumsum(axis=1))
    np.testing.assert_allclose(r['allocated_purchase_deposits_musd'],160+r['purchase_deposit_cash_musd'].cumsum(axis=1)-(r['purchase_asset_additions_musd']-r['purchase_settlement_cash_musd']).cumsum(axis=1))
    out=run_preserved(capture_source('monthly'),lambda:simulate_monthly(s));p=tmp_path/'run.json';p.write_text(json.dumps(out))
    assert deterministic(replay_saved(p,'monthly'))==deterministic(out)
    q=out['intervention']['quarterly'][1]
    assert q['purchase_settlement_cash_musd']['p50']==340
    assert q['ending_allocated_purchase_deposits_musd']['p50']==180
    changed=deepcopy(out);changed['fleet']['intervention']['purchase_cash']['monthly'][1]['allocated_purchase_deposits_musd']+=1
    p.write_text(json.dumps(changed))
    with pytest.raises(ValueError):replay_saved(p,'monthly')


def test_zero_delay_placebo_same_worlds_and_same_funding_for_both_policies():
    r=simulate_monthly(replace(scenario(),delivery_delay_months=0,stochastic=True))
    assert r['intervention']==r['control']
    assert r['fleet']['intervention']==r['fleet']['control']
    a=simulate_monthly(scenario());f=simulate_monthly(replace(scenario(),response_mode='frozen'))
    for key in (*PURCHASE_FLOWS,*PURCHASE_STOCKS):assert a['intervention']['monthly'][key]==f['intervention']['monthly'][key]


@pytest.mark.parametrize('patch',[
    {'delivery_aircraft_count':41},{'delivery_aircraft_count':True},{'delivery_aircraft_count':0},
    {'delivery_aircraft_count':30.5},{'delivery_group':'widebody'},{'duration_months':1},
])
def test_invalid_delay_selection_cannot_silently_delay_fewer_aircraft(patch):
    with pytest.raises(ValueError):simulate_monthly(replace(scenario(),**patch))


@pytest.mark.parametrize('mutation',[
    lambda p:p.update(basis=''),lambda p:p.update(residual_monthly_capex_musd=float('nan')),
    lambda p:p.update(residual_monthly_capex_musd=True),lambda p:p.update(automatic_borrow=True),
    lambda p:p['purchases'][0].update(fleet_record_id='unknown'),
    lambda p:p['purchases'][0].update(aircraft=21),lambda p:p['purchases'][0].update(aircraft=True),
    lambda p:p['purchases'][0].update(unit_price_musd=0),lambda p:p['purchases'][0].update(debt_fraction=1.1),
    lambda p:p['purchases'][0].update(opening_deposits_musd=800),
    lambda p:p['purchases'][0]['deposits'][0].update(month='2026-07'),
    lambda p:p['purchases'][0]['deposits'][0].update(month='2026-10'),
    lambda p:p['purchases'][0].update(purchase_id=p['purchases'][1]['purchase_id']),
    lambda p:p['purchases'].append(deepcopy(p['purchases'][0])|{'purchase_id':'excess'}),
])
def test_bad_purchase_assumptions_reject_invalid_stocks_mapping_and_dates(mutation):
    p=plan();mutation(p)
    with pytest.raises(ValueError):simulate_monthly(replace(scenario(),purchase_plan=p))


def test_late_cutoff_and_generic_amortization_cannot_rebase_historical_purchase_cash():
    with pytest.raises(ValueError,match='historical cash'):simulate_monthly(replace(scenario(),information_cutoff='2026-10-09'))
    cp={'basis':'Illustrative','events':[{'month':'2026-08','kind':'repay','amount_musd':1,'scope':'both'}]}
    with pytest.raises(ValueError,match='amortization'):simulate_monthly(replace(scenario(),cash_plan=cp))


def test_cash_events_remain_incremental_to_the_replaced_capex_and_borrowing_schedule():
    s=scenario();p=simulate_monthly(s)
    cp={'basis':'Illustrative extra capital spending and revolver funding','events':[
        {'month':'2026-08','kind':'additional_capex','amount_musd':20,'scope':'intervention'},
        {'month':'2026-08','kind':'revolver_draw','amount_musd':100,'scope':'intervention'}]}
    r=simulate_monthly(replace(s,cash_plan=cp));m=r['intervention']['monthly']
    assert m['cash_capex_musd'][0]['p50']==200
    assert m['debt_principal_musd'][-1]['p50']==23081+765+100
    assert r['summary']['cumulative_fcf_musd']['p50']-p['summary']['cumulative_fcf_musd']['p50']==pytest.approx(-20)
    assert r['summary']['ending_cash_musd']['p50']-p['summary']['ending_cash_musd']['p50']==pytest.approx(80)


def test_both_search_workflows_and_api_keep_purchase_funding_and_count():
    base=scenario();axes={'x':SearchAxis('delivery_delay_months',0,9,2),'y':SearchAxis('fuel_shock_pct',0,25,2)}
    a=search_failures(base,FailureSearch(**axes));b=invert_target(base,TargetInversion(**axes))
    for result in (a,b):
        assert result['manifest']['base_scenario']['purchase_plan']==base.purchase_plan
        assert result['manifest']['base_scenario']['delivery_aircraft_count']==30
    client=TestClient(app)
    from dataclasses import asdict
    response=client.post('/api/monthly/simulate',json=asdict(base))
    assert response.status_code==200
    assert response.json()['fleet']['intervention']['purchase_cash']['pending_settlements'][0]['aircraft']==10
    assert client.post('/api/monthly/simulate',json=asdict(replace(base,delivery_aircraft_count=41))).status_code==422


def test_thesis_reports_purchase_funding_separately_from_fcf_and_shows_delay_cash_boundary():
    r=simulate_monthly(scenario());notes=ThesisNotes('Illustrative delivery delay','Test the timing sensitivity','paired_no_shock',
        '2026-10-31','Check actual deliveries','https://ir.united.com/','Loan funding is unavailable')
    record=make_record(r,notes)
    assert record['cash_funding']['purchase_batch_count']==2
    assert record['cash_funding']['financing_cash_flow_musd']==pytest.approx(757.35)
    assert record['distributions'][0]['quantiles']==r['summary']['cumulative_fcf_musd']
    pdf=PdfReader(BytesIO(render_pdf(record)));assert len(pdf.pages)==1
    text=pdf.pages[0].extract_text()
    assert '30 aircraft' in text and 'Mapped purchase settlement' in text
    assert 'Entry delays do not defer' not in text
    assert 'Cumulative FCF equals ending minus opening' not in text
    longest=replace(notes,title='W'*70,hypothesis='W'*220,checkpoint='W'*180,
        falsification='W'*220,checkpoint_source='https://example.com/'+'w'*160)
    assert len(PdfReader(BytesIO(render_pdf(make_record(r,longest)))).pages)==1
