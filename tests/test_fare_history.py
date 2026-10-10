from decimal import Decimal
from io import BytesIO
import json
from zipfile import ZipFile

import pytest

from app.fare_history import parse_ticket, directional_markets, eligibility, summarize_archive


def row(fare='400',credibility='',reporter='UA',segments=None):
    segments=segments or [('UA','A','UA','A','X','500','ORD','X','30977','0','41'),('UA','A','UA','A','X','500','JFK','X','31703','0','22')]
    return '|'.join([fare,reporter,'20131',str(len(segments)),'2','JFK',credibility,'31703','0','22',*(v for s in segments for v in s)])+'\n'


def test_raw_record_preserves_carrier_roles_and_two_directional_markets():
    r=parse_ticket(row(),'2013-Q1');markets=directional_markets(r)
    assert r['fare_usd']==Decimal(400) and r['sample_passengers']==2
    assert [(m['origin'],m['destination'],m['distance_miles']) for m in markets]==[('JFK','ORD',500),('ORD','JFK',500)]
    assert eligibility(r)==('UA',['ticketed_online','mainline_online'],[])


def test_connections_stay_one_market_and_proration_uses_track_miles():
    segments=[('UA','A','UA','A','X','500','ORD','','30977','0','41'),('UA','A','UA','A','Y','1000','SFO','X','32457','0','91')]
    r=parse_ticket(row(segments=segments),'2013-Q1');m=directional_markets(r)
    assert len(m)==1 and m[0]['distance_miles']==1500 and m[0]['coupon_count']==2 and m[0]['fare_basis_group']=='X/Y'


def test_regional_branding_is_not_relabelled_mainline_or_summed_with_it():
    segments=[('OO','D','UA','A','X','500','ORD','X','30977','0','41')]
    r=parse_ticket(row(segments=segments),'2013-Q1')
    assert eligibility(r)==('UA',['ticketed_online'],[])
    segments[0]=('OO','D','DL','A','X','500','ORD','X','30977','0','41')
    assert eligibility(parse_ticket(row(segments=segments),'2013-Q1'))==('DL',['ticketed_online'],[])


def test_quality_flags_are_retained_without_fare_thresholds_or_imputation():
    assert eligibility(parse_ticket(row(credibility='*'),'2013-Q1'))==('UA',[],['questionable_dollar_value'])
    assert 'zero_fare' in eligibility(parse_ticket(row(fare='0'),'2013-Q1'))[2]
    assert eligibility(parse_ticket(row(fare='0.01'),'2013-Q1'))[1] # No ad hoc outlier trimming.
    s=[('UA','A','UA','A','X','500','YYZ','Y','32457','0','936')]
    flags=eligibility(parse_ticket(row(segments=s),'2013-Q1'))[2]
    assert 'outside_US_WAC' in flags and 'unsupported_trip_break' in flags


def test_layout_and_locked_date_fail_before_poisoned_quantities():
    with pytest.raises(ValueError,match='layout'):parse_ticket(row().replace('|500|ORD','|500|EXTRA|ORD'),'2013-Q1')
    with pytest.raises(ValueError,match='locked'):parse_ticket(row(fare='poison').replace('20131','20241'),'2024-Q1')
    with pytest.raises(ValueError,match='quarter'):parse_ticket(row(fare='poison'),'2013-Q2')
    for value in ('NaN','Infinity','-1'):
        with pytest.raises(ValueError,match='dollar'):parse_ticket(row(fare=value),'2013-Q1')


def test_aggregate_conserves_market_fare_and_sample_weights_with_provenance(tmp_path):
    p=tmp_path/'source.zip'
    with ZipFile(p,'w') as z:z.writestr('db1b.public.201303.asc',row()+row(fare='600')+row(credibility='*'))
    source={'quarter':'2013-Q1','sha256':'a'*64,'published_on':'2017-10-03'}
    a=summarize_archive(p,source)
    assert a['records']==3 and a['sample_ticket_passengers']==6
    mainline=[r for r in a['rows'] if r['cohort']=='mainline_online']
    assert len(mainline)==2 and sum(r['fare_weighted_sum_usd'] for r in mainline)==2000
    assert sum(r['sample_passengers'] for r in mainline)==8 # Four sampled ticket passengers take two markets.
    assert all(r['mean_prorated_market_fare_usd']==250 and r['survey_fare_usd_per_flown_mile']==.5 for r in mainline)
    assert all(r['source_line_witnesses']==[1,2] and r['source_line_first']==1 and r['source_line_last']==2 and len(r['contributor_digest'])==64 for r in mainline)
    assert a['quality_record_counts']==[{'carrier':'UA','flag':'questionable_dollar_value','records':1}]


def test_mixed_ticketed_carriers_are_not_assigned_to_reporting_carrier():
    s=[('UA','A','UA','A','X','500','ORD','','30977','0','41'),('DL','A','DL','A','X','500','JFK','X','31703','0','22')]
    assert eligibility(parse_ticket(row(segments=s),'2013-Q1'))==(None,[],['mixed_or_other_ticketed_carrier'])


def test_complete_quarter_build_parquet_rebuild_corruption_and_atomic_failure(tmp_path,monkeypatch):
    import hashlib
    from pathlib import Path
    import duckdb
    import app.fare_history as module
    from app.source_store import preserve_source,read_source
    from app.evidence import digest
    raw=tmp_path/'data/raw';raw.mkdir(parents=True)
    monkeypatch.setattr(module,'ROOT',tmp_path)
    monkeypatch.setattr(module,'read_source',lambda sha:read_source(sha,destination=raw))
    doc=preserve_source(b'%PDF synthetic field-definition fixture','https://example.org/definition.pdf','application/pdf',destination=raw)
    sources=[]
    for y in range(2013,2024):
        for q in (1,2,3,4):
            quarter=f'{y}-Q{q}';text=row(segments=[('UA','A','UA','A','X','500','ORD','X','30977','0','41')]).replace('20131',f'{y}{q}')
            b=BytesIO()
            with ZipFile(b,'w') as z:z.writestr('source.asc',text)
            url=f'https://example.org/{quarter}.zip';s=preserve_source(b.getvalue(),url,'application/zip',destination=raw)
            sources.append({'quarter':quarter,'url':url,'published_on':f'{y+1}-03-01','sha256':s['sha256'],'bytes':s['bytes']})
    spec={'sources':sources,'definition_source_sha256':doc['sha256']};destination=tmp_path/'data/fare_history'
    first=module.build_history(sources,spec,destination)
    second=module.build_history(sources,spec,destination)
    assert first==second and first['route_cells']==88 and first['records']==44
    target=destination/first['snapshot_id'];manifest=json.loads((target/'manifest.json').read_text())
    assert digest({k:v for k,v in manifest.items() if k!='snapshot_id'})==first['snapshot_id']
    assert hashlib.sha256((target/'routes.parquet').read_bytes()).hexdigest()==manifest['tables']['routes']['sha256']
    con=duckdb.connect();values=con.execute('SELECT DISTINCT mean_prorated_market_fare_usd, survey_fare_usd_per_flown_mile, sample_passengers FROM read_parquet(?)',[str(target/'routes.parquet')]).fetchall();con.close()
    assert values==[(400.,.8,2)]
    # A changed cached quantity must not silently enter the rebuilt panel.
    part=destination/'parts'/manifest['parts'][0]['part_id']/'part.json';original=part.read_text();a=json.loads(original);a['rows'][0]['fare_weighted_sum_usd']+=100;part.write_text(json.dumps(a))
    with pytest.raises(ValueError,match='Cached fare part'):module.build_history(sources,spec,destination)
    part.write_text(original)
    # Rehashing the forgery still cannot pass the independent original-record oracle.
    import scripts.audit_db1b_fare_part as oracle
    monkeypatch.setattr(oracle,'ROOT',tmp_path)
    monkeypatch.setattr(oracle,'read_source',lambda sha:read_source(sha,destination=raw))
    design=destination/'designs'/first['design_id']/'design.json'
    assert oracle.audit(design,'2013-Q1')['raw_ticket_records']==1
    meta=part.parent/'manifest.json';original_meta=meta.read_text()
    part.write_text(json.dumps(a));changed=json.loads(original_meta);changed['part_digest']=digest(a);meta.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='Decimal weighted fare allocation'):oracle.audit(design,'2013-Q1')
    part.write_text(original);meta.write_text(original_meta)
    # Failure on the last quarter leaves the existing successful pointer intact.
    bad=BytesIO()
    with ZipFile(bad,'w') as z:z.writestr('source.asc',row(fare='poison').replace('20131','20241'))
    saved=preserve_source(bad.getvalue(),sources[-1]['url'],'application/zip',destination=raw)
    altered=[*sources[:-1],{**sources[-1],'sha256':saved['sha256'],'bytes':saved['bytes']}]
    with pytest.raises(ValueError,match='locked'):module.build_history(altered,spec,destination)
    assert json.loads((destination/'latest.json').read_text())['snapshot_id']==first['snapshot_id']
