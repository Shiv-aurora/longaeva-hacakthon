"""Quarterly US-only DB1B survey fare proxies, separate from airline yield.

The public product has ten ticket fields plus eleven fields per coupon. Preserve
carrier roles and raw source lines; never infer monthly fares or population totals.
"""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from io import TextIOWrapper
import json
from pathlib import Path
import re
from zipfile import ZipFile

from .evidence import digest
from .source_store import ROOT, read_source

VERSION='db1b-fare-history-0.1.0'
CARRIERS=('UA','DL','AA')
COHORTS=('ticketed_online','mainline_online')
SCOPE='Quarterly sampled US-only itinerary/market fares including ticket taxes; not airline all-in passenger yield, Canada/international fare coverage, unconstrained demand, causal elasticity or monthly observations.'


def integer(value, label, minimum=0):
    if not re.fullmatch(r'\d+', value.strip()): raise ValueError('Invalid DB1B '+label)
    n=int(value)
    if n<minimum:raise ValueError('Invalid DB1B '+label)
    return n


def parse_ticket(text, expected_quarter):
    f=text.rstrip('\r\n').split('|')
    if len(f)<10:raise ValueError('Incomplete DB1B ticket header')
    # Date is checked before any fare/passenger quantity, including locked data.
    token=f[2].strip()
    if not re.fullmatch(r'\d{4}[1-4]',token) or f'{token[:4]}-Q{token[4]}'!=expected_quarter or token[:4]>'2023':
        raise ValueError('DB1B source quarter or locked boundary differs')
    n=integer(f[3],'coupon count',1)
    if n>23:raise ValueError('DB1B coupon count exceeds documented bound')
    if len(f)==11+11*n and f[-1]=='':f.pop()
    if len(f)!=10+11*n:raise ValueError('DB1B repeating coupon layout differs')
    try:fare=Decimal(f[0].strip())
    except InvalidOperation as e:raise ValueError('Invalid DB1B dollar amount') from e
    if not fare.is_finite() or fare<0:raise ValueError('Invalid DB1B dollar amount')
    pax=integer(f[4],'sample passenger count',1)
    indicator=f[6].strip()
    if indicator not in ('','*'):raise ValueError('Unknown DB1B dollar credibility indicator')
    origin=f[5].strip();city=integer(f[7],'origin city market',1);wac=integer(f[9],'origin WAC',1)
    if not re.fullmatch('[A-Z0-9]{3}',origin):raise ValueError('Invalid DB1B origin airport')
    coupons=[]
    for i in range(n):
        s=[v.strip() for v in f[10+11*i:21+11*i]]
        if not re.fullmatch('[A-Z0-9]{3}',s[6]):raise ValueError('Invalid DB1B coupon destination')
        coupons.append({'operating_carrier':s[0],'operating_type':s[1],'ticketed_carrier':s[2],'ticketed_type':s[3],
            'fare_basis':s[4], 'distance_miles':integer(s[5],'coupon miles'),'destination':s[6],
            'trip_break':s[7],'destination_city_market_id':integer(s[8],'destination city market',1),
            'destination_wac':integer(s[10],'destination WAC',1)})
    return {'quarter':expected_quarter,'fare_usd':fare,'reporting_carrier':f[1].strip(),'sample_passengers':pax,
        'origin':origin,'origin_city_market_id':city,'origin_wac':wac,'credibility':indicator,'coupons':coupons}


def directional_markets(ticket):
    result=[];origin=ticket['origin'];city=ticket['origin_city_market_id'];current=[]
    for i,c in enumerate(ticket['coupons']):
        current.append(c)
        if c['trip_break']=='X' or i==len(ticket['coupons'])-1:
            result.append({'origin':origin,'origin_city_market_id':city,'destination':c['destination'],
                'destination_city_market_id':c['destination_city_market_id'], 'coupon_count':len(current),
                'distance_miles':sum(s['distance_miles'] for s in current),
                'fare_basis_group':'/'.join(sorted({s['fare_basis'] or '(blank)' for s in current}))})
            origin=c['destination'];city=c['destination_city_market_id'];current=[]
    return result


def eligibility(ticket):
    c=ticket['coupons'];ticketed={s['ticketed_carrier'] for s in c}
    carrier=next(iter(ticketed)) if len(ticketed)==1 and next(iter(ticketed)) in CARRIERS else None
    if carrier is None:return None,[],['mixed_or_other_ticketed_carrier']
    flags=[]
    if ticket['credibility']=='*':flags.append('questionable_dollar_value')
    if ticket['fare_usd']==0:flags.append('zero_fare')
    if any(s['operating_carrier']=='--' or s['ticketed_carrier']=='--' for s in c):flags.append('surface_coupon')
    if ticket['origin_wac']>=100 or any(s['destination_wac']>=100 for s in c):flags.append('outside_US_WAC')
    if any(s['distance_miles']==0 for s in c):flags.append('zero_coupon_distance')
    if any(s['trip_break'] not in ('','X') for s in c):flags.append('unsupported_trip_break')
    if any(s['fare_basis'] not in ('','F','G','C','D','Y','X','U') for s in c):flags.append('unknown_fare_basis')
    if any(s['operating_type'] not in ('A','D') or s['ticketed_type'] not in ('A','D') for s in c):flags.append('unknown_coupon_type')
    if any(m['origin']==m['destination'] for m in directional_markets(ticket)):flags.append('circular_market')
    if flags:return carrier,[],flags
    cohorts=['ticketed_online']
    if ticket['reporting_carrier']==carrier and all(s['operating_carrier']==carrier for s in c):cohorts.append('mainline_online')
    return carrier,cohorts,[]


def summarize_archive(path, source):
    groups={};quality=Counter();cohort_tickets=Counter();cohort_pax=Counter();total_records=total_pax=0
    with ZipFile(path) as z:
        members=[n for n in z.namelist() if n.lower().endswith('.asc')]
        if len(members)!=1:raise ValueError('DB1B archive must have one ASC product member')
        member=members[0]
        with z.open(member) as b:
            for line,text in enumerate(TextIOWrapper(b,encoding='utf-8-sig'),1):
                if not text.strip():continue
                t=parse_ticket(text,source['quarter']);total_records+=1;total_pax+=t['sample_passengers']
                carrier,cohorts,flags=eligibility(t)
                for flag in flags:quality[carrier or '(other/mixed)',flag]+=1
                if not cohorts:continue
                miles=sum(c['distance_miles'] for c in t['coupons'])
                markets=directional_markets(t)
                for cohort in cohorts:
                    cohort_tickets[carrier,cohort]+=1;cohort_pax[carrier,cohort]+=t['sample_passengers']
                    for m in markets:
                        k=(carrier,cohort,m['origin'],m['origin_city_market_id'],m['destination'],m['destination_city_market_id'],m['coupon_count'],m['fare_basis_group'])
                        if k not in groups:groups[k]={'sample_passengers':0,'sample_market_records':0,'fare_weighted_sum_usd':0.,'passenger_miles':0,'source_line_first':line,'source_line_last':line,'hasher':hashlib.sha256(),'witnesses':[]}
                        v=groups[k];v['sample_passengers']+=t['sample_passengers'];v['sample_market_records']+=1
                        # Same distance-proration concept documented for DB1B MktFare.
                        allocated=t['fare_usd']*Decimal(m['distance_miles'])/Decimal(miles)
                        v['fare_weighted_sum_usd']+=float(allocated*t['sample_passengers'])
                        v['passenger_miles']+=m['distance_miles']*t['sample_passengers'];v['source_line_last']=line
                        v['hasher'].update(f'{line}:'.encode()+text.encode())
                        if len(v['witnesses'])<2:v['witnesses'].append(line)
    rows=[]
    for k,v in sorted(groups.items()):
        hashed=v.pop('hasher').hexdigest();witnesses=v.pop('witnesses')
        rows.append({'quarter':source['quarter'],'carrier':k[0],'cohort':k[1], 'origin':k[2],'origin_city_market_id':k[3],
            'destination':k[4],'destination_city_market_id':k[5],'coupon_count':k[6],'fare_basis_group':k[7], **v,
            'mean_prorated_market_fare_usd':v['fare_weighted_sum_usd']/v['sample_passengers'],
            'survey_fare_usd_per_flown_mile':v['fare_weighted_sum_usd']/v['passenger_miles'],
            'source_sha256':source['sha256'],'source_member':member,'contributor_digest':hashed,'source_line_witnesses':witnesses,
            'published_on':source['published_on'],'knowledge_label':'Current-download quarterly reconstruction; filename publication proxy; not historical captured bytes.'})
    return {'quarter':source['quarter'],'source_sha256':source['sha256'],'source_member':member,'records':total_records,'sample_ticket_passengers':total_pax,
        'cohort_counts':[{'carrier':c,'cohort':h,'sample_ticket_records':n,'sample_ticket_passengers':cohort_pax[c,h]} for (c,h),n in sorted(cohort_tickets.items())],
        'quality_record_counts':[{'carrier':c,'flag':f,'records':n} for (c,f),n in sorted(quality.items())], 'rows':rows}


def build_history(sources, specification, destination=None, progress=None):
    destination=Path(destination or ROOT/'data/fare_history');destination.mkdir(parents=True,exist_ok=True)
    expected={f'{y}-Q{q}' for y in range(2013,2024) for q in (1,2,3,4)}
    if len(sources)!=44 or {s['quarter'] for s in sources}!=expected:raise ValueError('Fare history requires all 44 declared quarters')
    declared={s['quarter']:s for s in specification['sources']}
    if set(declared)!=expected or any(s['url']!=declared[s['quarter']]['url'] or s['published_on']!=declared[s['quarter']]['published_on'] for s in sources):raise ValueError('Fare inputs differ from declared observed catalog')
    code=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    definition,_=read_source(specification['definition_source_sha256'])
    if not definition.startswith(b'%PDF'):raise ValueError('DB1B field definition is not a preserved PDF')
    design={'version':VERSION,'specification':specification,'sources':sources,'code_sha256':code,
        'scope':SCOPE,'aggregation':'Two overlapping cohorts: all-coupon single-ticketed-brand itineraries and its same-brand all-mainline/reporting subset. Split directional markets at X or itinerary end; prorate fare by coupon miles. No assumed ticket-to-carrier revenue allocation, fare cutoffs, sample expansion or monthly interpolation.'}
    design_id=digest(design);d=destination/'designs'/design_id;d.mkdir(parents=True,exist_ok=True)
    def write(p,value):
        content=json.dumps(value,indent=2,allow_nan=False)+'\n'
        if p.exists() and p.read_text()!=content:raise ValueError('Immutable fare history file differs')
        if not p.exists():p.write_text(content)
    write(d/'design.json',design)
    if not (d/'declaration_time.json').exists():write(d/'declaration_time.json',{'declared_at_utc':datetime.now(timezone.utc).isoformat(),'scope':'Local design saved before parsing; not external preregistration.'})
    parts=[]
    for s in sorted(sources,key=lambda r:r['quarter']):
        content,metadata=read_source(s['sha256'])
        if metadata['source_url']!=s['url'] or metadata['bytes']!=s['bytes']:raise ValueError('Fare raw input metadata differs')
        part_id=digest({'design_id':design_id,'source_sha256':s['sha256']});p=destination/'parts'/part_id;p.mkdir(parents=True,exist_ok=True)
        if (p/'part.json').exists():
            part=json.loads((p/'part.json').read_text());meta=json.loads((p/'manifest.json').read_text())
            if digest(part)!=meta['part_digest'] or meta['design_id']!=design_id or meta['source_sha256']!=s['sha256']:raise ValueError('Cached fare part differs')
        else:
            # Verified raw path; parsing streams the uncompressed member.
            path=ROOT/'data/raw'/s['sha256']/'source.bin'
            part=summarize_archive(path,s);write(p/'part.json',part)
            write(p/'manifest.json',{'design_id':design_id,'source_sha256':s['sha256'],'part_digest':digest(part),'rows':len(part['rows'])})
        parts.append({'part_id':part_id,'quarter':s['quarter'],'part_digest':digest(part),'source_sha256':s['sha256'],'route_cells':len(part['rows']),
            **{k:part[k] for k in ('records','sample_ticket_passengers','cohort_counts','quality_record_counts')}})
        if progress:progress({'phase':'quarter_parsed','quarter':s['quarter'],'records':part['records'],'route_cells':len(part['rows'])})
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=code:raise ValueError('Fare builder changed during parsing')
    import duckdb
    con=duckdb.connect();con.execute('SET threads=1');files=[str(destination/'parts'/r['part_id']/'part.json') for r in parts]
    # A native Parquet route table, without loading every quarter's rows into Python.
    staging=destination/'staging'/design_id;staging.mkdir(parents=True,exist_ok=True);temp=staging/'routes.parquet'
    con.execute('CREATE TABLE routes AS SELECT unnest(rows, recursive := true) FROM read_json_auto(?, maximum_object_size=536870912)',[files])
    con.execute('COPY (SELECT * FROM routes ORDER BY quarter, carrier, cohort, origin, origin_city_market_id, destination, destination_city_market_id, coupon_count, fare_basis_group) TO ? (FORMAT PARQUET)',[str(temp)])
    count=con.execute('SELECT count(*) FROM routes').fetchone()[0];con.close()
    if count!=sum(r['route_cells'] for r in parts):raise ValueError('Fare Parquet route count differs')
    table={'rows':count,'sha256':hashlib.sha256(temp.read_bytes()).hexdigest()}
    manifest={'version':VERSION,'design_id':design_id,'parts':parts,'code_sha256':code,'scope':SCOPE,'tables':{'routes':table},'duckdb_version':duckdb.__version__}
    identity=digest(manifest);manifest['snapshot_id']=identity;p=destination/identity;p.mkdir(exist_ok=True)
    write(p/'manifest.json',manifest)
    saved_code=p/'fare_history.py'
    if saved_code.exists() and hashlib.sha256(saved_code.read_bytes()).hexdigest()!=code:raise ValueError('Immutable fare builder differs')
    if not saved_code.exists():saved_code.write_bytes(Path(__file__).read_bytes())
    output=p/'routes.parquet'
    if output.exists() and hashlib.sha256(output.read_bytes()).hexdigest()!=table['sha256']:raise ValueError('Immutable fare Parquet differs')
    temp.replace(output);write(p/'tables.json',{'routes':table})
    # Publish only after all declared quarters and Parquet readback pass.
    if hashlib.sha256(output.read_bytes()).hexdigest()!=table['sha256']:raise ValueError('Fare Parquet readback differs')
    pointer=destination/'latest.tmp';pointer.write_text(json.dumps({'snapshot_id':identity})+'\n');pointer.replace(destination/'latest.json')
    return {'snapshot_id':identity,'design_id':design_id,'quarters':len(parts),'records':sum(r['records'] for r in parts),'route_cells':count,'scope':SCOPE}
