from pathlib import Path
import pytest
from app.data import load_fuel,load_ual,parse_eia_week_table,fuel_volatility,manifest

def test_eia_genuine_snapshot():
    f=load_fuel()
    assert len(f)==190
    assert f[0]=={'date':'2026-01-05','usd_per_gallon':1.88}
    assert f[-1]=={'date':'2026-10-06','usd_per_gallon':4.342}
    assert {x['date']:x['usd_per_gallon'] for x in f}['2026-09-30']==4.677
    assert all(row['usd_per_gallon']>0 for row in f)

def test_eia_weekly_reproducible():
    source=Path('data/eia_2026_weekly_source.txt').read_text()
    assert parse_eia_week_table(source)==load_fuel()

def test_week_parser_rejects_duplicates_bad_dates_and_values():
    s='2026 Jan- 5 to Jan- 9 | 2 | 2 | 2 | 2 | 2'
    with pytest.raises(ValueError): parse_eia_week_table(s+'\n'+s)
    with pytest.raises(ValueError): parse_eia_week_table('2026 Jan- 6 to Jan-10 | 2 | 2 | 2 | 2 | 2')
    with pytest.raises(ValueError): parse_eia_week_table('2026 Jan- 5 to Jan- 9 | -2 | 2 | 2 | 2 | 2')

def test_ual_financial_inputs_and_units():
    u=load_ual()
    assert u['asm_million']==87279 and u['fuel_gallons_million']==1219
    assert u['prasm_cents_per_asm']==18.45
    assert 1.39 < (u['fuel_gallons_million']/u['asm_million'])*100 < 1.41  # cents per ASM per $1/gal
    m=manifest(load_fuel(),u)
    assert 'No observed daily UAL fare panel bundled' in m['not_in_v1']

def test_fuel_process_sigma_finite():
    assert .005 <= fuel_volatility(load_fuel()) <= .09


def test_reported_q2_unit_metrics_reconcile_with_gaap_totals_within_rounding():
    u=load_ual()
    assert u['total_operating_revenue_million']-u['total_operating_expense_million']==u['operating_income_million']
    assert abs(u['asm_million']*u['trasm_cents_per_asm']/100-u['total_operating_revenue_million'])<5
    assert abs(u['asm_million']*u['casm_cents_per_asm']/100-u['total_operating_expense_million'])<5
    assert abs(u['asm_million']*u['prasm_cents_per_asm']/100-u['passenger_revenue_million'])<5
    assert abs(u['fuel_gallons_million']*u['average_paid_fuel_usd_per_gallon']-u['reported_fuel_expense_million'])<5
