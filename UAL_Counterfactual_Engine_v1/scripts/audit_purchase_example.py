"""Independent Decimal identities for the illustrative 30-aircraft/9-month case.

Expected deliveries are declared independently here, not imported from the fleet
or purchase-cash calculations. This is an accounting audit, not price calibration
or reconciliation of the complete gross SEC purchase queue.
"""
from decimal import Decimal
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.evidence import digest
from app.model_archive import replay_saved


def audit(path):
    run = replay_saved(path, 'monthly')
    s = run['scenario']
    example = json.loads((ROOT/'data/purchase_plan_example.json').read_text())
    if s['purchase_plan'] != example or s['cash_plan'] is not None:
        raise ValueError('Audit requires the exact illustrative purchase plan without other cash events')
    if (s['delivery_delay_months'], s['delivery_aircraft_count'], s['delivery_group'], s['onset_month'], s['duration_months'],
            s['information_cutoff'], s['horizon_months']) != (9,30,'narrowbody',1,6,'2026-07-16',12):
        raise ValueError('Audit requires the declared 12-month 30-aircraft/9-month experiment')
    expected = {'control': {'2026-09':20,'2026-12':20},
                'intervention': {'2026-12':10,'2027-06':20,'2027-09':10}}
    checks = 0
    def check(actual, expected, label):
        nonlocal checks
        if abs(Decimal(str(actual))-Decimal(str(expected))) > Decimal('.00011'):
            raise ValueError(f'Independent accounting audit failed: {label}')
        checks += 1
    financial_rows = {}
    for mode, deliveries in expected.items():
        track = run['fleet'][mode]
        observed = sorted((e['month'],e['aircraft']) for e in track['effective_queue'] if 'purchase_id' in e)
        if observed != sorted(deliveries.items()):
            raise ValueError('Independent delivery-count calendar differs')
        checks += 1
        deposits, debt, additions = Decimal(160), Decimal(0), Decimal(0)
        financial_rows[mode] = []
        for i, month in enumerate(run['months']):
            count = Decimal(deliveries.get(month,0));payment = Decimal(80) if month=='2026-08' else Decimal(0)
            interest = debt*Decimal('.06')/12
            settlement = count*34; borrowing = settlement*Decimal('.75');fee = borrowing*Decimal('.01')
            deposits += payment-count*6; debt += borrowing;additions += count*40
            values = {'purchase_deposit_cash_musd':payment,'purchase_settlement_cash_musd':settlement,
                'purchase_borrowing_musd':borrowing,'purchase_interest_musd':interest,'purchase_financing_fee_musd':fee,
                'purchase_asset_additions_musd':count*40,'residual_cash_capex_musd':Decimal(100),
                'allocated_purchase_deposits_musd':deposits,'purchase_debt_principal_musd':debt,
                'cumulative_purchase_asset_additions_musd':additions,'cash_capex_musd':100+payment+settlement}
            m = run[mode]['monthly']
            for key,value in values.items():
                check(track['purchase_cash']['monthly'][i][key],value,f'{mode}/{month}/{key}/ledger')
                for q in ('p10','p50','p90'):check(m[key][i][q],value,f'{mode}/{month}/{key}/{q}')
            for q in ('p10','p50','p90'):
                check(m['debt_principal_musd'][i][q],23081+debt,f'{mode}/{month}/total debt')
                check(m['financing_cash_flow_musd'][i][q],borrowing-fee,f'{mode}/{month}/financing cash')
                check(m['incremental_cash_interest_musd'][i][q],interest,f'{mode}/{month}/interest')
                check(m['net_cash_change_musd'][i][q],Decimal(str(m['free_cash_flow_musd'][i][q]))+borrowing-fee,f'{mode}/{month}/net cash')
            financial_rows[mode].append(values)
    # A deterministic funding shift preserves the entire cumulative-FCF quantile
    # ordering; monthly FCF medians are never summed into horizon quantiles.
    for q in ('p10','p50','p90'):
        check(run['summary']['ending_cash_musd'][q],Decimal(10166)+Decimal(str(run['summary']['cumulative_fcf_musd'][q]))+Decimal('757.35'),f'horizon/{q}/cash-vs-FCF')
    for i,month in enumerate(run['months']):
        delta_capex=financial_rows['intervention'][i]['cash_capex_musd']-financial_rows['control'][i]['cash_capex_musd']
        delta_interest=financial_rows['intervention'][i]['purchase_interest_musd']-financial_rows['control'][i]['purchase_interest_musd']
        for q in ('p10','p50','p90'):
            inc=run['incremental']['monthly']
            expected_ocf=Decimal(str(inc['operating_income_musd'][i][q]))*(1-Decimal(str(s['marginal_cash_tax_fraction'])))-delta_interest
            check(inc['operating_cash_flow_musd'][i][q],expected_ocf,f'{month}/{q}/paired OCF')
            check(inc['free_cash_flow_musd'][i][q],expected_ocf-delta_capex,f'{month}/{q}/paired FCF')
    result={'version':'purchase-example-accounting-audit-0.1.0','run_id':run['manifest']['run_id'],
            'checks':checks,'passed':True,'expected_delivery_calendar':expected,
            'scope':'Independent fixed calendar and Decimal accounting identities after registered archived-core replay. Prices, deposits, residual capex and loan terms are illustrative assumptions; no empirical validation or complete gross purchase reconciliation.'}
    result['audit_id']=digest(result)
    target=ROOT/'data/purchase_bridge_checks'/(result['audit_id']+'.json');target.parent.mkdir(exist_ok=True)
    content=json.dumps(result,indent=2,sort_keys=True)+'\n'
    if target.exists():
        if target.read_text()!=content:raise ValueError('Immutable purchase audit differs')
    else:
        with target.open('x') as stream:stream.write(content)
    return {'report':str(target),**result}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('run',type=Path)
    print(json.dumps(audit(parser.parse_args().run),indent=2))
