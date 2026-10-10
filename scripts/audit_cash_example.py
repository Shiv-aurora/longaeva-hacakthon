"""Independent fixed-event delta check against the archived pre-ledger run.

No cash_bridge calculations are imported. Both saved runs are independently
replayed with their registered cores before their shared outputs are compared.
"""
from decimal import Decimal
from pathlib import Path
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.model_archive import digest, replay_saved


def main():
    original=ROOT/'data/runs/951752edf9e632c5b4bd9ed594d06c1ad451c809ac9d638c799d537273fc48aa.json'
    funded=ROOT/'data/runs/7c6ce50f49eac423f92fea986439ec683f48ac249c6662f437754eb4602db7fc.json'
    old=replay_saved(original,'monthly');new=replay_saved(funded,'monthly')
    assert {k:new['scenario'][k] for k in old['scenario']}==old['scenario']
    assert old['replay_inputs']['fuel_observations']==new['replay_inputs']['fuel_observations']
    for group in ('records','sources'):
        assert all(r in new['replay_inputs']['evidence_snapshot'][group] for r in old['replay_inputs']['evidence_snapshot'][group])
    assert new['scenario']['cash_plan']['events']==[
        {'month':'2026-08','kind':'revolver_draw','amount_musd':800,'scope':'intervention'},
        {'month':'2026-08','kind':'financing_fee','amount_musd':5,'scope':'intervention'},
        {'month':'2026-08','kind':'advance_sales_change','amount_musd':-100,'scope':'intervention'}]
    checked=0
    def delta(left,right,expected):
        nonlocal checked
        assert abs(Decimal(str(right))-Decimal(str(left))-Decimal(str(expected)))<=Decimal('0.0001')
        checked+=1
    for container in ('control','intervention','incremental','frozen_incremental'):
        assert old[container]['markets']==new[container]['markets']
        for field,months in old[container]['monthly'].items():
            for t,quantiles in enumerate(months):
                expected=0
                if container!='control':
                    if field=='cash_musd':expected=800-5-100
                    elif field in ('operating_cash_flow_musd','free_cash_flow_musd') and t==0:expected=-100
                for q,value in quantiles.items():delta(value,new[container]['monthly'][field][t][q],expected)
    for q in ('p10','p50','p90'):
        delta(old['summary']['ending_cash_musd'][q],new['summary']['ending_cash_musd'][q],695)
        delta(old['summary']['cumulative_fcf_impact_musd'][q],new['summary']['cumulative_fcf_impact_musd'][q],-100)
        delta(old['summary']['response_contribution_to_fcf_musd'][q],new['summary']['response_contribution_to_fcf_musd'][q],0)
        old_fcf=Decimal(str(old['summary']['ending_cash_musd'][q]))-Decimal('10166')
        delta(old_fcf,new['summary']['cumulative_fcf_musd'][q],-100)
    report={'status':'passed','original_run_id':old['manifest']['run_id'],'funded_run_id':new['manifest']['run_id'],
            'quantile_checks':checked,'independent_fixed_event_deltas_musd':{'ending_cash':695,'cumulative_fcf':-100,'operating_income':0,'response_contribution':0},
            'scope':'Fixed deterministic event deltas on shared 10000 stochastic worlds and all original monthly output fields; not a source-extraction audit or empirical validation.',
            'funded_result_sha256':new['replay_code']['result_sha256']}
    target=ROOT/'data/cash_bridge_checks'/f'{digest(report)}.json';target.parent.mkdir(exist_ok=True)
    if target.exists():assert json.loads(target.read_text())==report
    else:target.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({**report,'report_file':str(target)},indent=2))


if __name__=='__main__':main()
