from copy import deepcopy
from dataclasses import asdict, replace
from io import BytesIO
import json
from pathlib import Path

from fastapi.testclient import TestClient
import numpy as np
from pypdf import PdfReader
import pytest

from app.evidence import load_snapshot
from app.failure_search import FailureSearch, SearchAxis, search_failures
from app.fleet import GROUPS, RETENTION_FLOWS, capacity_paths
from app.model_archive import capture_source, deterministic, replay_saved, run_preserved
from app.monthly import MonthlyScenario, _rollout, _world, baseline, month_sequence, shock_weights, simulate_monthly
from app.server import app
from app.target_inversion import TargetInversion, invert_target
from app.thesis import ThesisNotes, make_record, render_pdf

ROOT=Path(__file__).resolve().parents[1]


def plan():return json.loads((ROOT/'data/retention_plan_example.json').read_text())


def scenario(**kw):
    return MonthlyScenario(**dict(paths=20,stochastic=False,fuel_shock_pct=0,
        delivery_delay_months=9,delivery_aircraft_count=30,delivery_group='narrowbody',
        purchase_plan=json.loads((ROOT/'data/purchase_plan_example.json').read_text()),retention_plan=plan(),**kw))


def fleet(s):
    b=baseline(load_snapshot(),s.information_cutoff,s.cash_plan,s.purchase_plan)
    return capacity_paths(b,s,b['fleet_plan'],month_sequence(s.information_cutoff,s.horizon_months),shock_weights(s))


def test_retirement_extension_reuses_existing_aircraft_and_conserves_fleet():
    f=fleet(scenario());a=f['intervention'];c=f['control']
    assert [sum(r['retention']['retained_by_group'].values()) for r in a['monthly']]==[0,0,10,10,10,18,18,18,18,18,18,8]
    assert all(sum(r['retention']['retained_by_group'].values())==0 for r in c['monthly'])
    assert [(e['month'],e['aircraft']) for e in a['effective_queue'] if 'retention_id' in e]==[('2027-06',10),('2027-09',8)]
    assert a['monthly'][2]['opening_inventory']['narrowbody']==892
    assert a['monthly'][11]['opening_inventory']['narrowbody']==922  # June old exit and new delivery, operational July
    for t in (a,c):
        for r in t['monthly']:
            for g in GROUPS:assert r['closing_inventory'][g]==r['opening_inventory'][g]+r['entries'][g]-r['exits'][g]
    assert a['retention']['pending_retirements'][0]['aircraft']==8
    long=fleet(replace(scenario(),horizon_months=24))
    assert long['intervention']['monthly'][-1]['closing_inventory']==long['control']['monthly'][-1]['closing_inventory']
    assert not long['intervention']['retention']['pending_retirements']
    assert sum(long['intervention']['monthly'][-1]['retention']['retained_by_group'].values())==0
    assert a['purchase_cash']==fleet(replace(scenario(),retention_plan=None))['intervention']['purchase_cash']


def test_capacity_uses_retained_productivity_and_existing_committed_schedule():
    s=scenario(fleet_spare_fraction=0);f=fleet(s);plain=fleet(replace(s,retention_plan=None))
    split=np.array(f['reference_split_asm_million']);initial=np.array([885,237,430])
    effective=np.array([[r['retention']['effective_available_aircraft'][g] for g in GROUPS] for r in f['intervention']['monthly']])
    assert effective[2,0]==891  # 892 aircraft less 10 * 10% productivity loss
    assert f['intervention']['monthly'][2]['retention']['fuel_burn_premium_fraction_by_group']['narrowbody']==pytest.approx(9/891*.15)
    assert (f['intervention_components']<=split[None,:,:]*(effective/initial)[:,None,:]+1e-9).all()
    assert (f['intervention_components']<=f['committed_components']+1e-9).all()
    np.testing.assert_array_equal(f['committed_components'],plain['committed_components'])
    assert f['intervention_components'][2].sum()>plain['intervention_components'][2].sum()
    p=plan()
    for link in p['links']:link['productivity_fraction']=0
    idle=fleet(replace(s,retention_plan=p))
    np.testing.assert_allclose(idle['intervention_components'],plain['intervention_components'])
    assert idle['intervention']['monthly'][2]['retention']['fuel_burn_premium_fraction_by_group']['narrowbody']==0


def test_explicit_additional_maintenance_carrying_and_cash_tax_cost_identity():
    s=scenario(schedule_growth_fraction=0,fleet_spare_fraction=.2,pass_through_fraction=0)
    p=plan()
    for link in p['links']:link.update(productivity_fraction=1,extra_fuel_burn_fraction=0)
    a=simulate_monthly(replace(s,retention_plan=p));plain=simulate_monthly(replace(s,retention_plan=None))
    m=a['intervention']['monthly'];b=plain['intervention']['monthly']
    counts=[0,0,10,10,10,18,18,18,18,18,18,8]
    for i,n in enumerate(counts):
        assert m['retention_maintenance_expense_musd'][i]['p50']==pytest.approx(n*.02)
        assert m['retention_carrying_expense_musd'][i]['p50']==pytest.approx(n*.01)
        assert m['retention_extra_fuel_expense_musd'][i]['p50']==0
        assert m['operating_income_musd'][i]['p50']-b['operating_income_musd'][i]['p50']==pytest.approx(-n*.03,abs=.0001)
        assert m['free_cash_flow_musd'][i]['p50']-b['free_cash_flow_musd'][i]['p50']==pytest.approx(-n*.03*.79,abs=.0001)
    assert sum(counts)==146
    assert a['summary']['cumulative_fcf_musd']['p50']-plain['summary']['cumulative_fcf_musd']['p50']==pytest.approx(-4.38*.79,abs=.0001)
    assert a['intervention']['quarterly'][1]['retention_maintenance_expense_musd']['p50']==.6
    assert a['intervention']['quarterly'][1]['ending_retained_aircraft']['p50']==10


def test_fuel_penalty_is_only_on_operated_retained_equivalent_share_and_not_regional_fuel():
    s=scenario(schedule_growth_fraction=0,fleet_spare_fraction=.2,pass_through_fraction=0)
    p=plan()
    for link in p['links']:link.update(extra_fuel_burn_fraction=0,additional_maintenance_musd_per_aircraft_month=0,additional_carrying_musd_per_aircraft_month=0)
    low=simulate_monthly(replace(s,retention_plan=p));high=simulate_monthly(s)
    m=high['intervention']['monthly'];b=low['intervention']['monthly']
    # Equal operated schedule isolates extra fuel from productivity/capacity differences.
    for i in range(12):
        assert m['asm_million'][i]==b['asm_million'][i]
        assert m['regional_fuel_expense_musd'][i]==b['regional_fuel_expense_musd'][i]
        # Three independently rounded four-decimal values allow at most 0.00015 error.
        assert m['fuel_expense_musd'][i]['p50']-b['fuel_expense_musd'][i]['p50']==pytest.approx(m['retention_extra_fuel_expense_musd'][i]['p50'],abs=.000151)
    assert m['retention_extra_fuel_expense_musd'][2]['p50']>0


def test_outages_and_direct_cut_keep_carrying_costs_but_scale_extra_fuel():
    s=scenario(aircraft_unavailable_pct=90,availability_group='narrowbody',duration_months=12)
    f=fleet(s);row=f['intervention']['monthly'][2]
    assert row['retention']['retention_maintenance_expense_musd']==pytest.approx(.2)
    effective=row['retention']['effective_available_aircraft']['narrowbody']
    assert effective==pytest.approx(row['available']['narrowbody']*(1-1/892))
    a=simulate_monthly(s);cut=simulate_monthly(replace(s,capacity_shock_pct=-90))
    assert cut['intervention']['monthly']['retention_maintenance_expense_musd']==a['intervention']['monthly']['retention_maintenance_expense_musd']
    assert cut['intervention']['monthly']['retention_extra_fuel_expense_musd'][2]['p50']==pytest.approx(a['intervention']['monthly']['retention_extra_fuel_expense_musd'][2]['p50']*.1,abs=.0001)


def test_notice_support_and_maximum_extension_are_enforced_and_visible():
    for field in ('maintenance_supported','crew_supported'):
        p=plan();p['links'][0][field]=False
        f=fleet(replace(scenario(),retention_plan=p));d=f['intervention']['retention']['decisions'][0]
        assert d['retained_aircraft']==0 and d['unapplied_aircraft']==10 and d['reasons']
        assert f['intervention']['monthly'][2]['retention']['retained_by_group']['narrowbody']==0
    notice=fleet(replace(scenario(),onset_month=2))['intervention']['retention']['decisions'][0]
    assert notice['retained_aircraft']==0 and notice['reasons']==['Retention notice window is missed']
    p=plan();p['links'][0]['max_extension_months']=2
    a=fleet(replace(scenario(),retention_plan=p))['intervention']
    assert a['retention']['decisions'][0]['effective_exit_month']=='2026-11'
    assert a['monthly'][4]['retention']['retained_by_group']['narrowbody']==0


def test_simultaneous_cap_partial_replacements_and_priority_prevent_unfunded_extra_aircraft():
    p=plan();p['max_retained_aircraft']=12
    a=fleet(replace(scenario(),retention_plan=p))['intervention']
    assert [(d['retained_aircraft'],d['unapplied_aircraft']) for d in a['retention']['decisions']]==[(10,0),(2,6)]
    assert max(sum(r['retention']['retained_by_group'].values()) for r in a['monthly'])==12
    one=fleet(replace(scenario(),delivery_aircraft_count=5))['intervention']['retention']['decisions']
    assert [d['retained_aircraft'] for d in one]==[5,0]
    p['max_retained_aircraft']=0
    a=fleet(replace(scenario(),retention_plan=p))['intervention']
    assert not any(sum(r['retention']['retained_by_group'].values()) for r in a['monthly'])


def test_zero_delay_placebo_and_both_pricing_modes_share_fixed_retention_actions():
    s=replace(scenario(),delivery_delay_months=0,stochastic=True)
    r=simulate_monthly(s);assert r['intervention']==r['control'] and r['fleet']['intervention']==r['fleet']['control']
    a=simulate_monthly(scenario());b=simulate_monthly(replace(scenario(),response_mode='frozen'))
    assert a['fleet']==b['fleet']
    for k in ('retained_aircraft','retention_maintenance_expense_musd','retention_carrying_expense_musd'):
        assert a['intervention']['monthly'][k]==b['intervention']['monthly'][k]


@pytest.mark.parametrize('mutate',[
    lambda p:p.update(max_retained_aircraft=-1),lambda p:p.update(max_retained_aircraft=True),
    lambda p:p.update(basis=''),lambda p:p.update(automatic_retirement=True),
    lambda p:p['links'][0].update(aircraft=11),lambda p:p['links'][0].update(aircraft=True),
    lambda p:p['links'][0].update(max_extension_months=25),lambda p:p['links'][0].update(minimum_notice_months=-1),
    lambda p:p['links'][0].update(maintenance_supported='yes'),lambda p:p['links'][0].update(productivity_fraction=1.1),
    lambda p:p['links'][0].update(extra_fuel_burn_fraction=float('nan')),
    lambda p:p['links'][0].update(additional_maintenance_musd_per_aircraft_month=-.01),
    lambda p:p['links'][0].update(purchase_id='unknown'),lambda p:p['links'][0].update(purchase_id='max-december'),
    lambda p:p['links'][0].update(exit_record_id='unknown'),
    lambda p:p['links'].append(deepcopy(p['links'][0])|{'retention_id':'excess'}),
])
def test_invalid_retention_boundaries_reject_instead_of_inventing_eligibility(mutate):
    p=plan();mutate(p)
    with pytest.raises(ValueError):simulate_monthly(replace(scenario(),retention_plan=p))


def test_unmapped_replacements_and_historical_retirements_are_unsupported():
    with pytest.raises(ValueError,match='purchase plan'):simulate_monthly(replace(scenario(),purchase_plan=None))
    with pytest.raises(ValueError,match='historical'):simulate_monthly(replace(scenario(),information_cutoff='2026-10-09'))


def test_all_paths_identities_and_full_archived_replay(tmp_path):
    s=replace(scenario(),stochastic=True,fuel_shock_pct=25)
    b=baseline(load_snapshot(),s.information_cutoff,s.cash_plan,s.purchase_plan)
    a=_rollout(b,s,*_world(s,4,.2),4,True,s.response_mode)
    np.testing.assert_allclose(a['mainline_fuel_expense_musd']+a['regional_fuel_expense_musd'],a['fuel_expense_musd'])
    np.testing.assert_allclose(a['other_nonfuel_expense_musd']+a['regional_contract_expense_musd'],a['nonfuel_expense_musd'])
    np.testing.assert_allclose(a['revenue_musd']-a['fuel_expense_musd']-a['nonfuel_expense_musd'],a['operating_income_musd'])
    np.testing.assert_allclose(10166+a['net_cash_change_musd'].cumsum(axis=1),a['cash_musd'])
    r=run_preserved(capture_source('monthly'),lambda:simulate_monthly(s));p=tmp_path/'run.json';p.write_text(json.dumps(r))
    assert deterministic(replay_saved(p,'monthly'))==deterministic(r)
    changed=deepcopy(r);changed['fleet']['intervention']['retention']['decisions'][0]['retained_aircraft']+=1
    p.write_text(json.dumps(changed))
    with pytest.raises(ValueError):replay_saved(p,'monthly')


def test_searches_api_and_thesis_preserve_retention_actions():
    s=scenario();axes={'x':SearchAxis('delivery_delay_months',0,9,2),'y':SearchAxis('fuel_shock_pct',0,25,2)}
    for result in (search_failures(s,FailureSearch(**axes)),invert_target(s,TargetInversion(**axes))):
        assert result['manifest']['base_scenario']['retention_plan']==s.retention_plan
    c=TestClient(app);r=c.post('/api/monthly/simulate',json=asdict(s))
    assert r.status_code==200
    notes=ThesisNotes('Illustrative retention','Test temporary retention costs','paired_no_shock','2026-10-31',
        'Check replacement and retirement dates','https://ir.united.com/','Maintenance or crew support is unavailable')
    record=make_record(r.json(),notes)
    assert record['scenario']['retention_plan']==s.retention_plan
    assert record['retention']['peak_retained_aircraft']==18
    assert record['retention']['retained_aircraft_months']==146
    assert record['retention']['additional_maintenance_and_carrying_musd']==4.38
    reader=PdfReader(BytesIO(render_pdf(record)))
    assert len(reader.pages)==1 and 'retention peaks at 18' in reader.pages[0].extract_text()
    longest=replace(notes,title='W'*70,hypothesis='W'*220,checkpoint='W'*180,
        falsification='W'*220,checkpoint_source='https://example.com/'+'w'*160)
    assert len(PdfReader(BytesIO(render_pdf(make_record(r.json(),longest)))).pages)==1
