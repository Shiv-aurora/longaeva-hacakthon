from copy import deepcopy
from dataclasses import replace
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.cash_bridge import KINDS, month_events, opening_state, step
from app.evidence import load_snapshot
from app.failure_search import FailureSearch, SearchAxis, search_failures
from app.model_archive import capture_source, deterministic, replay_saved, run_preserved
from app.monthly import MonthlyScenario, _rollout, _world, baseline, simulate_monthly
from app.server import app
from app.target_inversion import TargetInversion, invert_target
from app.thesis import ThesisNotes, make_record
from app.thesis import render_pdf
from io import BytesIO
from pypdf import PdfReader


def event(kind, amount, month='2026-08', scope='intervention'):
    return {'month': month, 'kind': kind, 'amount_musd': amount, 'scope': scope}


def plan(*events):
    return {'basis': 'Illustrative analyst cash timing and funding assumptions, not a fitted policy.', 'events': list(events)}


def scenario(*events, **kwargs):
    return MonthlyScenario(paths=20, horizon_months=3, stochastic=False, fuel_shock_pct=0,
                           cash_plan=plan(*events), **kwargs)


def opening():
    s=load_snapshot();b=baseline(s,'2026-07-16')
    return opening_state(s,'2026-07-16',b)['state']


def test_reported_stock_definitions_and_publication_gate():
    state=opening()
    assert state['debt_principal_musd']==23081  # not debt/leases/other liabilities 26464
    assert state['loyalty_deferred_musd']==3939+4032==7971
    assert state['cash_musd']+state['restricted_cash_musd']==10308
    assert state['revolver_drawn_musd']==0 and state['revolver_available_musd']==3000
    s=load_snapshot();b=baseline(s,'2026-07-16')
    with pytest.raises(ValueError,match='reported'):opening_state(s,'2026-07-15',b)
    for field in ('review_status','effective_end','units'):
        changed=deepcopy(s);r=next(r for r in changed['records'] if r['variable']=='debt_principal')
        r[field]={'review_status':'pending','effective_end':'2026-03-31','units':'aircraft'}[field]
        with pytest.raises(ValueError,match='debt_principal'):opening_state(changed,'2026-07-16',b)


def test_empty_optional_ledger_preserves_every_operating_and_cash_path():
    b=simulate_monthly(MonthlyScenario(paths=20,horizon_months=3,fuel_shock_pct=0,stochastic=False))
    a=simulate_monthly(scenario())
    for name,values in b['intervention']['monthly'].items():assert a['intervention']['monthly'][name]==values
    assert a['intervention']==a['control']


def test_funding_does_not_enter_fcf_earnings_or_paired_operating_impact():
    a=simulate_monthly(scenario(event('revolver_draw',800),event('financing_fee',5)))
    assert a['summary']['cumulative_fcf_impact_musd']['p50']==0
    assert a['summary']['cumulative_operating_income_impact_musd']['p50']==0
    assert a['incremental']['monthly']['cash_musd'][0]['p50']==795
    assert a['intervention']['monthly']['debt_principal_musd'][0]['p50']==23081+800
    assert a['intervention']['monthly']['revolver_available_musd'][0]['p50']==2200
    assert a['summary']['ending_cash_musd']['p50']-10166==pytest.approx(a['summary']['cumulative_fcf_musd']['p50']+795)


def test_restricted_transfer_preserves_total_cash_and_investment_trade_stays_outside_fcf():
    state,flows=step(opening(),month_events(plan(event('restricted_transfer',100),event('investment_trade',200)),'2026-08',True),np.array([40.,-20.]),np.array([10.,10.]))
    np.testing.assert_allclose(state['cash_musd']+state['restricted_cash_musd'],10166+142+flows['free_cash_flow_musd']-200)
    assert state['short_term_investments_musd']==6471+200
    np.testing.assert_allclose(flows['free_cash_flow_musd'],[30,-30])


def test_working_capital_signs_interest_and_capex_reconcile_without_earnings_double_count():
    events=[event('advance_sales_change',100),event('loyalty_balance_change',-25),event('receivables_change',40),
            event('inventory_change',20),event('prepaids_change',5),event('payables_change',60),event('cash_interest',9),
            event('additional_capex',30),event('capex_refund',2)]
    a=simulate_monthly(scenario(*events))
    m=a['incremental']['monthly']
    assert m['operating_income_musd'][0]['p50']==0
    assert m['working_capital_cash_adjustment_musd'][0]['p50']==70
    assert m['operating_cash_flow_musd'][0]['p50']==61
    assert m['cash_capex_musd'][0]['p50']==28
    assert m['cash_musd'][0]['p50']==33
    assert m['cash_musd'][1]['p50']==33  # a one-time event, not a permanent monthly flow


def test_scope_and_event_months_are_identical_across_pricing_modes():
    s=scenario(event('borrow',700,scope='both'),event('repay',200,'2026-09'),event('asset_sale',50,'2026-10'))
    a=simulate_monthly(s)
    assert a['control']['monthly']['financing_cash_flow_musd'][0]['p50']==700
    assert a['incremental']['monthly']['cash_musd'][0]['p50']==0
    assert a['incremental']['monthly']['cash_musd'][1]['p50']==-200
    b=simulate_monthly(replace(s,response_mode='frozen'))
    assert a['intervention']==b['intervention']


@pytest.mark.parametrize('events,message',[
    ([event('revolver_draw',3001)],'available credit'),
    ([event('revolver_repay',1)],'drawn principal'),
    ([event('repay',23082)],'nonrevolver principal'),
    ([event('revolver_draw',3000),event('repay',23082)],'nonrevolver principal'),
    ([event('restricted_transfer',-143)],'restricted_cash'),
    ([event('investment_trade',-6472)],'short_term_investments'),
    ([event('advance_sales_change',-10753)],'advance_sales'),
    ([event('loyalty_balance_change',-7972)],'loyalty_deferred'),
])
def test_invalid_transactions_are_rejected_atomically(events,message):
    original=opening();before=deepcopy(original)
    with pytest.raises(ValueError,match=message):step(original,month_events(plan(*events),'2026-08',True),100,10)
    assert original==before


def test_negative_cash_is_reported_without_automatic_borrowing():
    a=simulate_monthly(scenario(event('additional_capex',20000)))
    assert a['intervention']['monthly']['cash_musd'][0]['p50']<0
    assert a['intervention']['monthly']['revolver_drawn_musd'][0]['p50']==0
    assert a['summary']['cash_floor_breach_path_fraction']==1


def test_plan_validation_rejects_silent_dropped_events_and_nonfinite_amounts():
    for e in (event('borrow',1,'2026-07'),event('borrow',float('nan')),event('borrow',True),event('borrow',-1),event('unsupported',1),event([],1),event('borrow',1,scope={})):
        with pytest.raises(ValueError):scenario(e).validate()
    with pytest.raises(ValueError):replace(scenario(),cash_plan={'basis':'','events':[]}).validate()
    with pytest.raises(ValueError):replace(scenario(),cash_plan={'basis':'x','events':[],'automatic_draw':True}).validate()


def test_all_path_cash_identities_quarter_aggregation_and_replay(tmp_path):
    s=replace(scenario(event('borrow',1000),event('restricted_transfer',200),event('cash_interest',4,'2026-09')),stochastic=True,fuel_shock_pct=25)
    snapshot=load_snapshot();b=baseline(snapshot,s.information_cutoff,s.cash_plan)
    r=_rollout(b,s,*_world(s,4,.2),4,True,s.response_mode)
    np.testing.assert_allclose(10166+r['net_cash_change_musd'].cumsum(axis=1),r['cash_musd'])
    np.testing.assert_allclose(r['net_cash_change_musd'],r['free_cash_flow_musd']+r['other_investing_cash_flow_musd']+r['financing_cash_flow_musd']-r['restricted_cash_transfer_musd'])
    result=run_preserved(capture_source('monthly'),lambda:simulate_monthly(s))
    assert result['intervention']['quarterly'][0]['financing_cash_flow_musd']['p50']==1000
    path=tmp_path/'run.json';path.write_text(json.dumps(result))
    assert deterministic(replay_saved(path,'monthly'))==deterministic(result)


def test_both_search_workflows_use_financed_cash_and_preserve_absolute_fcf():
    base=MonthlyScenario(paths=20,horizon_months=3,stochastic=False,fuel_shock_pct=0)
    funded=replace(base,cash_plan=plan(event('borrow',1000)))
    axes={'x':SearchAxis('fuel_shock_pct',0,10,2),'y':SearchAxis('demand_shock_pct',-1,0,2)}
    plain=search_failures(base,FailureSearch(**axes));financed=search_failures(funded,FailureSearch(**axes))
    for p,f in zip(plain['cells'],financed['cells']):
        for mode in ('frozen','assumed_constrained'):
            assert p['policies'][mode]['cumulative_fcf_musd']==f['policies'][mode]['cumulative_fcf_musd']
            assert f['policies'][mode]['ending_cash_musd']['p50']-p['policies'][mode]['ending_cash_musd']['p50']==pytest.approx(1000)
    target=invert_target(funded,TargetInversion(**axes))
    assert target['manifest']['base_scenario']['cash_plan']==funded.cash_plan


def test_thesis_uses_fcf_path_sum_with_financing_and_api_rejects_invalid_credit():
    a=simulate_monthly(scenario(event('borrow',1000)))
    notes=ThesisNotes('Test','Illustrative financing test','paired_no_shock','2026-10-31','Check funding','https://ir.united.com/','Funding is unavailable')
    record=make_record(a,notes)
    assert record['distributions'][0]['quantiles']==a['summary']['cumulative_fcf_musd']
    assert record['distributions'][0]['quantiles']['p50']!=a['summary']['ending_cash_musd']['p50']-10166
    assert record['cash_funding']['financing_cash_flow_musd']==1000
    pdf=PdfReader(BytesIO(render_pdf(record)))
    assert len(pdf.pages)==1
    assert 'net financing +1,000.0' in pdf.pages[0].extract_text()
    client=TestClient(app)
    assert client.post('/api/monthly/simulate',json={'paths':20,'cash_plan':plan(event('revolver_draw',3001))}).status_code==422
    response=client.post('/api/monthly/simulate',json={'paths':20,'cash_plan':plan(event('borrow',1000))})
    assert response.status_code==200 and response.json()['baseline_inputs']['cash_ledger']['state']['debt_principal_musd']==23081
