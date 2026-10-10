"""Independent original-ASCII DB1B fare aggregation and Parquet audit.

Does not import the fare parser, market splitter, eligibility rules or builder.
Prices are distance-prorated survey amounts, not an identified revenue allocation.
"""
from collections import Counter
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sys
from io import TextIOWrapper
from zipfile import ZipFile

import duckdb

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.evidence import digest
from app.source_store import read_source

CARRIERS={'UA','DL','AA'}


def original_cells(path,quarter):
    groups={};flags=Counter();tickets=Counter();passengers=Counter();records=total_pax=0
    with ZipFile(path) as z:
        names=[n for n in z.namelist() if n.lower().endswith('.asc')]
        if len(names)!=1:raise ValueError('Independent DB1B member count differs')
        member=names[0]
        with z.open(member) as binary:
            for line,text in enumerate(TextIOWrapper(binary,encoding='utf-8-sig'),1):
                if not text.strip():continue
                f=text.rstrip('\r\n').split('|');n=int(f[3]);date=f[2].strip()
                if f'{date[:4]}-Q{date[4:]}'!=quarter or date[:4]>'2023':raise ValueError('Independent raw quarter/locked gate differs')
                if len(f)==11+11*n and not f[-1]:f.pop()
                if len(f)!=10+11*n:raise ValueError('Independent coupon cardinality differs')
                pax=int(f[4]);records+=1;total_pax+=pax
                brand={f[12+11*i].strip() for i in range(n)}
                if len(brand)!=1 or not brand<=CARRIERS:
                    flags['(other/mixed)','mixed_or_other_ticketed_carrier']+=1;continue
                carrier=next(iter(brand));fare=Decimal(f[0]);distances=[int(f[15+11*i]) for i in range(n)]
                breaks=[f[17+11*i].strip() for i in range(n)]
                cuts=sorted({0,n,*[i+1 for i,b in enumerate(breaks) if b=='X']})
                markets=[]
                for start,end in zip(cuts,cuts[1:]):
                    origin=f[5].strip() if start==0 else f[16+11*(start-1)].strip()
                    origin_city=int(f[7]) if start==0 else int(f[18+11*(start-1)])
                    last=end-1;dest=f[16+11*last].strip();dest_city=int(f[18+11*last])
                    basis='/'.join(sorted({f[14+11*i].strip() or '(blank)' for i in range(start,end)}))
                    markets.append((origin,origin_city,dest,dest_city,end-start,basis,sum(distances[start:end])))
                rejected=[]
                if f[6].strip()=='*':rejected.append('questionable_dollar_value')
                if fare==0:rejected.append('zero_fare')
                if any(f[10+11*i].strip()=='--' or f[12+11*i].strip()=='--' for i in range(n)):rejected.append('surface_coupon')
                if int(f[9])>=100 or any(int(f[20+11*i])>=100 for i in range(n)):rejected.append('outside_US_WAC')
                if 0 in distances:rejected.append('zero_coupon_distance')
                if any(b not in ('','X') for b in breaks):rejected.append('unsupported_trip_break')
                if any(f[14+11*i].strip() not in ('','F','G','C','D','Y','X','U') for i in range(n)):rejected.append('unknown_fare_basis')
                if any(f[11+11*i].strip() not in ('A','D') or f[13+11*i].strip() not in ('A','D') for i in range(n)):rejected.append('unknown_coupon_type')
                if any(m[0]==m[2] for m in markets):rejected.append('circular_market')
                if rejected:
                    for flag in rejected:flags[carrier,flag]+=1
                    continue
                cohorts=['ticketed_online']
                if f[1].strip()==carrier and all(f[10+11*i].strip()==carrier for i in range(n)):cohorts.append('mainline_online')
                for cohort in cohorts:
                    tickets[carrier,cohort]+=1;passengers[carrier,cohort]+=pax
                    for market in markets:
                        k=(carrier,cohort,*market[:6])
                        if k not in groups:groups[k]={'pax':0,'records':0,'amount':Decimal(0),'miles':0,'first':line,'last':line,'hash':hashlib.sha256(),'witnesses':[]}
                        v=groups[k];v['pax']+=pax;v['records']+=1;v['amount']+=fare*Decimal(market[-1])/Decimal(sum(distances))*pax
                        v['miles']+=market[-1]*pax;v['last']=line;v['hash'].update(str(line).encode()+b':'+text.encode())
                        if len(v['witnesses'])<2:v['witnesses'].append(line)
    return groups,{'records':records,'sample_ticket_passengers':total_pax,'source_member':member,
        'cohort_counts':[{'carrier':c,'cohort':h,'sample_ticket_records':n,'sample_ticket_passengers':passengers[c,h]} for (c,h),n in sorted(tickets.items())],
        'quality_record_counts':[{'carrier':c,'flag':f,'records':n} for (c,f),n in sorted(flags.items())]}


def audit(target,progress=None):
    target=Path(target);checks=0
    def check(value,label):
        nonlocal checks
        if not value:raise ValueError('Independent DB1B audit failed: '+label)
        checks+=1
    def close(a,b,label):check(math.isclose(a,float(b),rel_tol=1e-10,abs_tol=1e-6),label)
    def load(p):return json.loads(p.read_text())
    m=load(target/'manifest.json')
    check(digest({k:v for k,v in m.items() if k!='snapshot_id'})==m['snapshot_id']==target.name,'snapshot identity')
    d=load(target.parent/'designs'/m['design_id']/'design.json')
    check(digest(d)==m['design_id'] and d['code_sha256']==m['code_sha256'],'design identity')
    check(hashlib.sha256((target/'fare_history.py').read_bytes()).hexdigest()==m['code_sha256'],'preserved builder')
    check(len(d['sources'])==len(m['parts'])==44 and {r['quarter'] for r in d['sources']}=={f'{y}-Q{q}' for y in range(2013,2024) for q in (1,2,3,4)},'all declared initial/selection quarters')
    definition,metadata=read_source(d['specification']['definition_source_sha256']);check(definition.startswith(b'%PDF'),'original field definition')
    check(hashlib.sha256((target/'routes.parquet').read_bytes()).hexdigest()==m['tables']['routes']['sha256'],'bound Parquet checksum')
    con=duckdb.connect();con.execute('SET threads=1');total_cells=total_records=0
    for source,part in zip(sorted(d['sources'],key=lambda r:r['quarter']),m['parts']):
        check(source['quarter']==part['quarter'] and source['sha256']==part['source_sha256'],'quarter/source order')
        content,metadata=read_source(source['sha256']);check(metadata['source_url']==source['url'] and len(content)==source['bytes'],'original archive bytes/URL')
        p=target.parent/'parts'/part['part_id'];saved=load(p/'part.json');pm=load(p/'manifest.json')
        check(digest(saved)==part['part_digest']==pm['part_digest'] and pm['design_id']==m['design_id'] and pm['source_sha256']==source['sha256'],'quarter part/design/source binding')
        original,summary=original_cells(ROOT/'data/raw'/source['sha256']/'source.bin',source['quarter'])
        for field,value in summary.items():check(saved[field]==value and (field not in part or part[field]==value),'complete raw '+field)
        rows=saved['rows'];keys=[(r['carrier'],r['cohort'],r['origin'],r['origin_city_market_id'],r['destination'],r['destination_city_market_id'],r['coupon_count'],r['fare_basis_group']) for r in rows]
        check(len(rows)==len(original)==part['route_cells']==pm['rows'] and len(set(keys))==len(keys) and set(keys)==set(original),'exact route/cohort support')
        for k,row in zip(keys,rows):
            v=original[k]
            check(row['quarter']==source['quarter'] and row['published_on']==source['published_on'] and row['source_sha256']==source['sha256'] and row['source_member']==summary['source_member'],'route source/date provenance')
            for field,expected in (('sample_passengers',v['pax']),('sample_market_records',v['records']),('passenger_miles',v['miles']),('source_line_first',v['first']),('source_line_last',v['last']),('source_line_witnesses',v['witnesses']),('contributor_digest',v['hash'].hexdigest())):check(row[field]==expected,'original '+field)
            close(row['fare_weighted_sum_usd'],v['amount'],'Decimal distance-prorated weighted amount')
            close(row['mean_prorated_market_fare_usd'],v['amount']/v['pax'],'sample-weighted market fare')
            close(row['survey_fare_usd_per_flown_mile'],v['amount']/v['miles'],'survey fare-per-mile proxy')
        physical=[json.loads(r[0]) for r in con.execute('SELECT to_json(t) FROM read_parquet(?) t WHERE quarter=? ORDER BY carrier, cohort, origin, origin_city_market_id, destination, destination_city_market_id, coupon_count, fare_basis_group',[str(target/'routes.parquet'),source['quarter']]).fetchall()]
        ordered=sorted(rows,key=lambda r:(r['carrier'],r['cohort'],r['origin'],r['origin_city_market_id'],r['destination'],r['destination_city_market_id'],r['coupon_count'],r['fare_basis_group']))
        check(digest(physical)==digest(ordered),'native Parquet all-column equality')
        total_cells+=len(rows);total_records+=summary['records']
        if progress:progress({'phase':'quarter_audited','quarter':source['quarter'],'raw_records':summary['records'],'checks':checks})
    check(con.execute('SELECT count(*) FROM read_parquet(?)',[str(target/'routes.parquet')]).fetchone()[0]==total_cells==m['tables']['routes']['rows'],'complete native table');con.close()
    return {'checks_passed':checks,'source_archives':44,'raw_ticket_records':total_records,'route_cells':total_cells,
        'scope':'Independent original-record carrier/market/quality partition, sample weights, Decimal fare allocation, raw-line digests, complete quarters and all-column Parquet equality. Current-download quarterly US-only reconstruction; not airline revenue yield, point-in-time forecasting, causal demand/pricing, calibration or simulator integration.'}


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot',type=Path);args=parser.parse_args()
    root=ROOT/'data/fare_history';target=args.snapshot or root/json.loads((root/'latest.json').read_text())['snapshot_id']
    result={'snapshot_id':target.name,'audit_code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),**audit(target,lambda event:print(json.dumps(event),file=sys.stderr,flush=True))}
    out=ROOT/'data/fare_history_checks'/(digest(result)+'.json');out.parent.mkdir(exist_ok=True)
    content=json.dumps(result,indent=2)+'\n'
    if out.exists() and out.read_text()!=content:raise ValueError('Immutable fare audit differs')
    if not out.exists():out.write_text(content)
    print(json.dumps({'audit_file':str(out),**result},indent=2))


if __name__=='__main__':main()
