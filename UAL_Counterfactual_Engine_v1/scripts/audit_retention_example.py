"""Independent fixed-calendar and Decimal audit of the illustrative retention case.

Uses registered replay and independently declared aircraft-month intervals. Does
not import fleet/retention calculations or sum stochastic monthly quantiles.
"""
from decimal import Decimal
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.evidence import digest
from app.model_archive import replay_saved


def audit(retained_path,plain_path):
    retained=replay_saved(retained_path,'monthly');plain=replay_saved(plain_path,'monthly')
    s=retained['scenario'];p=plain['scenario']
    expected_plan=json.loads((ROOT/'data/retention_plan_example.json').read_text())
    expected_purchase=json.loads((ROOT/'data/purchase_plan_example.json').read_text())
    if s['retention_plan']!=expected_plan or p['retention_plan'] is not None or s['purchase_plan']!=expected_purchase:
        raise ValueError('Audit requires the exact illustrative purchase and retention assumptions')
    if {k:v for k,v in s.items() if k!='retention_plan'}!={k:v for k,v in p.items() if k!='retention_plan'}:
        raise ValueError('Comparator must use identical settings apart from retention')
    if (s['information_cutoff'],s['horizon_months'],s['delivery_delay_months'],s['delivery_aircraft_count'],s['delivery_group'],
            s['onset_month'],s['duration_months'],s['aircraft_unavailable_pct'],s['capacity_shock_pct'],s['geography'])!=('2026-07-16',12,9,30,'narrowbody',1,6,0,0,'all'):
        raise ValueError('Audit requires the declared 12-month 30-aircraft/9-month case without outages or direct cuts')
    counts=[0,0,10,10,10,18,18,18,18,18,18,8]
    closing_counts=[0,10,10,10,18,18,18,18,18,18,8,8]
    D=lambda v:Decimal(str(v))
    checks=0
    def check(actual,expected,label):
        nonlocal checks
        if abs(D(actual)-D(expected))>D('.00016'):
            raise ValueError('Independent retention audit failed: '+label)
        checks+=1
    actual_exits=sorted((e['month'],e['aircraft']) for e in retained['fleet']['intervention']['effective_queue'] if 'retention_id' in e)
    if actual_exits!=[('2027-06',10),('2027-09',8)]:raise ValueError('Independent retirement calendar differs')
    checks+=1
    # Retention must leave the no-shock reference and every purchase cash payment intact.
    for key,values in plain['control']['monthly'].items():
        for i,row in enumerate(values):
            for q,value in row.items():check(retained['control']['monthly'][key][i][q],value,'reference/'+key)
    for market,series in plain['control']['markets'].items():
        for key,values in series.items():
            for i,row in enumerate(values):
                for q,value in row.items():check(retained['control']['markets'][market][key][i][q],value,'reference/'+market+'/'+key)
    if retained['fleet']['intervention']['purchase_cash']!=plain['fleet']['intervention']['purchase_cash']:
        raise ValueError('Retention changed purchase settlement or funding')
    checks+=1
    f=retained['fleet'];split=f['reference_split_asm_million'];initial=f['plan']['opening_by_group']
    for i,(month,n) in enumerate(zip(retained['months'],counts)):
        row=f['intervention']['monthly'][i];base=plain['fleet']['intervention']['monthly'][i];m=retained['intervention']['monthly']
        check(row['opening_inventory']['narrowbody'],base['opening_inventory']['narrowbody']+n,month+'/opening existing aircraft')
        check(row['closing_inventory']['narrowbody'],base['closing_inventory']['narrowbody']+closing_counts[i],month+'/closing existing aircraft')
        for group in ('narrowbody','widebody','regional'):
            check(row['closing_inventory'][group],row['opening_inventory'][group]+row['entries'][group]-row['exits'][group],month+'/'+group+'/conservation')
        effective=D(row['available']['narrowbody'])-D(n)*D('.1')
        check(row['retention']['effective_available_aircraft']['narrowbody'],effective,month+'/productivity')
        premium=D(n)*D('.9')*D('.15')/effective if effective else D(0)
        check(row['retention']['fuel_burn_premium_fraction_by_group']['narrowbody'],premium,month+'/fuel premium')
        operated=[]
        for j,group in enumerate(('narrowbody','widebody','regional')):
            committed=1+D(s['schedule_growth_fraction'])*(D(f['control']['monthly'][i]['opening_inventory'][group])/D(initial[group])-1)
            available=effective if group=='narrowbody' else D(row['available'][group])
            ceiling=available/D(initial[group])/(1-D(s['fleet_spare_fraction']))
            operated.append(sum(D(r[j]) for r in split)*min(committed,ceiling))
        # All-market fuel uses one common delivered price. This fixed positive
        # fraction therefore preserves its quantile ordering within this case.
        extra_fraction=operated[0]*premium/(operated[0]*(1+premium)+operated[1])
        for q in ('p10','p50','p90'):
            check(m['retained_aircraft'][i][q],n,month+'/'+q+'/count')
            check(m['retention_maintenance_expense_musd'][i][q],D(n)*D('.02'),month+'/'+q+'/maintenance')
            check(m['retention_carrying_expense_musd'][i][q],D(n)*D('.01'),month+'/'+q+'/carrying')
            check(m['asm_million'][i][q],sum(operated),month+'/'+q+'/capacity')
            check(m['retention_extra_fuel_expense_musd'][i][q],D(m['mainline_fuel_expense_musd'][i][q])*extra_fraction,month+'/'+q+'/extra fuel')
    check(sum(counts),146,'retained aircraft-months')
    check(sum(D(r['p50']) for key in ('retention_maintenance_expense_musd','retention_carrying_expense_musd') for r in retained['intervention']['monthly'][key]),D('4.38'),'additional fixed expense total')
    for q in ('p10','p50','p90'):
        check(retained['summary']['ending_cash_musd'][q],D(10166)+D(retained['summary']['cumulative_fcf_musd'][q])+D('757.35'),'funded horizon cash/'+q)
    result={'version':'retention-example-accounting-audit-0.1.0','retained_run_id':retained['manifest']['run_id'],
            'comparator_run_id':plain['manifest']['run_id'],'checks':checks,'passed':True,
            'aircraft_months':146,'additional_maintenance_and_carrying_musd':4.38,
            'scope':'Independent fixed retirement calendar, control invariance, unchanged purchase cash, existing-aircraft conservation, productivity/capacity, fixed upkeep and all-market common-price fuel identities. Assumed support and costs; no empirical eligibility, fitted retention or forecast validation.'}
    result['audit_id']=digest(result)
    target=ROOT/'data/retention_checks'/(result['audit_id']+'.json');target.parent.mkdir(exist_ok=True)
    content=json.dumps(result,indent=2,sort_keys=True)+'\n'
    if target.exists():
        if target.read_text()!=content:raise ValueError('Immutable retention audit differs')
    else:
        with target.open('x') as stream:stream.write(content)
    return {'report':str(target),**result}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('retained_run',type=Path);parser.add_argument('comparator_run',type=Path)
    args=parser.parse_args();print(json.dumps(audit(args.retained_run,args.comparator_run),indent=2))
