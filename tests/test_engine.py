import pytest
from app.data import load_fuel,load_ual
from app.engine import Scenario,simulate

F=load_fuel();U=load_ual()

def run(**kw):return simulate(F,U,Scenario(paths=45,seed=46,**kw))

def test_zero_shock_zero_incremental_operating_income():
    r=run(fuel_shock_pct=0)
    s=r['summary']
    assert s['cumulative_operating_income_impact_musd']=={'p10':0,'p50':0,'p90':0}
    assert s['fuel_cost_increment_musd']=={'p10':0,'p50':0,'p90':0}
    assert s['modeled_revenue_recovery_musd']=={'p10':0,'p50':0,'p90':0}

def test_lag_delays_response_no_revenue_recovery_before_lag():
    r=run(response_lag_days=10)
    assert all(x['p50']==0 for x in r['series']['modeled_fare_change_pct'][:10])
    assert any(x['p50']>0 for x in r['series']['modeled_fare_change_pct'][10:])
    assert r['series']['daily_operating_income_delta_musd'][0]['p50']<0

def test_higher_lag_makes_pnl_impact_more_negative():
    fast=run(response_lag_days=0)['summary']['cumulative_operating_income_impact_musd']['p50']
    slow=run(response_lag_days=35)['summary']['cumulative_operating_income_impact_musd']['p50']
    assert fast > slow

def test_no_pass_through_less_revenue_and_worse_impact():
    no=run(pass_through_fraction=0)['summary']
    yes=run(pass_through_fraction=.9)['summary']
    assert no['modeled_revenue_recovery_musd']['p50']==0
    assert yes['cumulative_operating_income_impact_musd']['p50']>no['cumulative_operating_income_impact_musd']['p50']

def test_conditional_distribution_order_and_reproducibility():
    a=run();b=run();assert a['series']==b['series']
    for series in a['series'].values():
        assert len(series)==60
        assert all(x['p10']<=x['p50']<=x['p90'] for x in series)
    for val in a['summary'].values():
        if isinstance(val,dict) and all(k in val for k in ('p10','p50','p90')):
            assert val['p10']<=val['p50']<=val['p90']

def test_negative_fuel_shock_can_help_relative_operating_income():
    r=run(fuel_shock_pct=-25)
    assert r['summary']['cumulative_operating_income_impact_musd']['p50']>0

def test_parameter_boundaries():
    with pytest.raises(ValueError):simulate(F,U,Scenario(fuel_shock_pct=1000))
    with pytest.raises(ValueError):simulate(F,U,Scenario(paths=0))

def test_initial_required_prasm_accurate():
    s=run()['summary']
    expected=100*(F[-1]['usd_per_gallon']*.2*(1219/87279))/(18.45/100)
    assert abs(s['gross_prasm_increase_required_pct_initial']-expected)<.001
