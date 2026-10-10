from dataclasses import replace
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.data import load_fuel
from app.failure_search import SearchAxis
from app.monthly import MonthlyScenario, simulate_monthly
from app.server import app
from app.target_inversion import OperatingTarget, ValuationScenario, TargetInversion, invert_target, value_operating_assets
from scripts.run_target_inversion import replay


def base(**kw):
    return MonthlyScenario(**{'paths': 40, 'horizon_months': 3, **kw})


def spec(**kw):
    return TargetInversion(**{'x': SearchAxis('fuel_shock_pct', 0, 30, 3), 'y': SearchAxis('demand_shock_pct', -5, 5, 3), **kw})


def valuation(**kw):
    # Deliberate illustrative test assumptions, not reported UAL inputs.
    return ValuationScenario(**{'annual_discount_rate': .1, 'terminal_growth_rate': .02,
        'terminal_annual_fcff_musd': 4000, 'annual_non_cash_charges_musd': 3000,
        'annual_working_capital_investment_musd': 500, 'cash_operating_tax_rate': .25,
        'enterprise_value_target_musd': 50000, 'enterprise_value_tolerance_musd': 10000, **kw})


def test_fcff_valuation_matches_independent_monthly_discounting_and_loss_taxes():
    v = valuation(annual_discount_rate=.12, terminal_growth_rate=.02, terminal_annual_fcff_musd=1000,
        annual_non_cash_charges_musd=120, annual_working_capital_investment_musd=24)
    r = {'operating_income_musd': np.array([[120., -120.]]), 'cash_capex_musd': np.array([[10., 20.]])}
    out = value_operating_assets(r, v)
    np.testing.assert_allclose(out['monthly_cash_operating_taxes_musd'], [[30, 0]])
    np.testing.assert_allclose(out['monthly_fcff_musd'], [[88, -132]])
    explicit = 88/(1.12**(1/12)) - 132/(1.12**(2/12))
    assert out['explicit_fcff_pv_musd'][0] == pytest.approx(explicit)
    assert out['terminal_value_musd'] == pytest.approx(10000)
    assert out['enterprise_value_musd'][0] == pytest.approx(explicit + 10000/(1.12**(2/12)))


def test_operating_target_bounds_relations_and_inclusive_band():
    for relation, expected in [('at_least', [False, True, True]), ('at_most', [True, True, False]), ('within_band', [True, True, True])]:
        target = OperatingTarget(value=100, tolerance=10, relation=relation)
        np.testing.assert_array_equal(target.matches(np.array([90, 100, 110])), expected)
    bad = [OperatingTarget(metric='stock_price'), OperatingTarget(value=float('nan')), OperatingTarget(tolerance=-1),
        OperatingTarget(metric='cumulative_revenue', value=-1), OperatingTarget(statistic='mean'), OperatingTarget(minimum_cash_floor_musd=-1)]
    for target in bad:
        with pytest.raises(ValueError): target.validate()
    for v in (valuation(terminal_growth_rate=.1), valuation(annual_discount_rate=float('nan')), valuation(terminal_annual_fcff_musd=-1)):
        with pytest.raises(ValueError): v.validate()


def test_target_search_matches_full_scenario_and_returns_multiple_compatible_points():
    result = invert_target(base(), spec(target=OperatingTarget(value=-1000000)))
    assert result['counts']['frozen']['compatible_points'] == 9
    assert len(result['compatible_sets']['assumed_constrained']) == 9
    for cell in (result['cells'][0], result['cells'][-1]):
        for mode in ('frozen', 'assumed_constrained'):
            full = simulate_monthly(replace(base(), **cell['overrides'], response_mode=mode))
            out = cell['policies'][mode]
            assert out['ending_cash_musd'] == full['summary']['ending_cash_musd']
            assert out['cumulative_fcf_musd']['p50'] == pytest.approx(full['summary']['ending_cash_musd']['p50']-10166, abs=.0002)
            assert out['compatible_path_count'] == 40 and len(out['witness_paths']) == 5
            for w in out['witness_paths']:
                assert sum(w['monthly']['free_cash_flow_musd']) == pytest.approx(w['cumulative_fcf_musd'])
                assert w['operating_margin_pct'] == pytest.approx(sum(w['monthly']['operating_income_musd'])/sum(w['monthly']['revenue_musd'])*100)
                assert w['minimum_cash_musd'] == min([10166, *w['monthly']['cash_musd']])


def test_margin_uses_total_income_over_total_revenue_not_mean_of_monthly_margins(monkeypatch):
    from app import target_inversion
    def rollout(*args):
        return {'revenue_musd': np.tile([100., 900.], (20, 1)), 'operating_income_musd': np.tile([90., 90.], (20, 1)),
            'free_cash_flow_musd': np.zeros((20, 2)), 'cash_musd': np.full((20, 2), 10166.), 'unserved_schedule_asm_million': np.zeros((20, 2))}
    monkeypatch.setattr(target_inversion, '_rollout', rollout)
    r = invert_target(base(paths=20, horizon_months=2), spec(target=OperatingTarget(metric='operating_margin_pct', value=20)))
    assert r['cells'][0]['policies']['frozen']['selected_target_value'] == 18
    assert r['counts']['frozen']['compatible_points'] == 0


def test_valuation_and_cash_constraints_filter_operating_matches_and_joint_witnesses():
    t = OperatingTarget(value=-1000000, minimum_cash_floor_musd=5000)
    r = invert_target(base(), spec(target=t, valuation=valuation()))
    for c in r['cells']:
        for outcome in c['policies'].values():
            for w in outcome['witness_paths']:
                assert w['minimum_cash_musd'] >= 5000
                assert 40000 <= w['enterprise_value_musd'] <= 60000
    impossible = invert_target(base(), spec(target=t, valuation=valuation(enterprise_value_target_musd=1000000, enterprise_value_tolerance_musd=1)))
    assert impossible['counts']['frozen']['operating_target_points'] == 9
    assert impossible['compatible_sets']['frozen'] == []
    assert all(not c['policies']['frozen']['witness_paths'] for c in impossible['cells'])
    opening_above_floor = invert_target(base(), spec(target=OperatingTarget(value=-1000000, minimum_cash_floor_musd=11000)))
    assert opening_above_floor['counts']['frozen']['compatible_points'] == 0


def test_quantile_match_requires_an_actual_joint_path(monkeypatch):
    from app import target_inversion
    def rollout(*args):
        # Median 100 is an interpolated statistic; no actual path is inside [99,101].
        fcf = np.array([[0.]]*10 + [[200.]]*10)
        return {'revenue_musd': np.full((20,1), 1000.), 'operating_income_musd': fcf,
            'free_cash_flow_musd': fcf, 'cash_musd': 10166+fcf, 'unserved_schedule_asm_million': np.zeros((20,1))}
    monkeypatch.setattr(target_inversion, '_rollout', rollout)
    r = invert_target(base(paths=20, horizon_months=1), spec(target=OperatingTarget(value=100, relation='within_band', tolerance=1)))
    out = r['cells'][0]['policies']['frozen']
    assert out['operating_statistic_matches']
    assert not out['compatible'] and out['compatible_path_count'] == 0


def test_target_replay_checks_inputs_results_and_valuation_assumptions(tmp_path):
    saved = invert_target(base(), spec(valuation=valuation()))
    path = tmp_path/'inversion.json';path.write_text(json.dumps(saved))
    assert replay(path)['manifest'] == saved['manifest']
    saved['cells'][0]['policies']['frozen']['selected_target_value'] += 1
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match='checksum'): replay(path)
    saved = invert_target(base(), spec(valuation=valuation()))
    saved['manifest']['inversion']['valuation']['annual_discount_rate'] += .01
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match='differ'): replay(path)


def test_target_seed_and_cutoff_reconstruction_are_preserved():
    a = invert_target(base(), spec())
    b = invert_target(base(), spec(), fuel=load_fuel()+[{'date': '2099-01-01', 'usd_per_gallon': 99}])
    assert a['manifest'] == b['manifest'] and a['result_digest'] == b['result_digest']
    assert a['result_digest'] != invert_target(base(seed=12), spec())['result_digest']
    assert 'User-defined' in a['reference_status'] and a['valuation_status'] == 'Valuation not requested'


def test_target_api_rejects_incomplete_or_mismatched_valuation_and_unknown_fields():
    client = TestClient(app)
    body = {'base_scenario': {'paths': 20, 'horizon_months': 3}, 'x': {'driver': 'fuel_shock_pct','low': 0,'high': 20,'points': 2}, 'y': {'driver': 'demand_shock_pct','low': -5,'high': 5,'points': 2}}
    response = client.post('/api/monthly/target-inversion', json=body)
    assert response.status_code == 200 and len(response.json()['cells']) == 4
    for patch in ({'target': {'metric': 'share_price'}}, {'valuation': {'annual_discount_rate': .1}}, {'market_consensus': 123}, {'target': {'value': 100, 'tolerance': -1}}):
        assert client.post('/api/monthly/target-inversion', json={**body, **patch}).status_code == 422
