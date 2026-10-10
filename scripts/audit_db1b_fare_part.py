"""Independent raw-record check of one completed fare import quarter.

Useful while the full import is running. Does not certify the full history,
Parquet export, historical prediction or revenue-yield matching.
"""
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.evidence import digest
from app.source_store import read_source
from scripts.audit_db1b_fares import original_cells


def audit(design_path,quarter):
    checks=0
    def check(value,label):
        nonlocal checks
        if not value:raise ValueError('Independent fare quarter audit failed: '+label)
        checks+=1
    def close(a,b,label):check(math.isclose(a,float(b),rel_tol=1e-10,abs_tol=1e-6),label)
    design_path=Path(design_path);d=json.loads(design_path.read_text());did=design_path.parent.name
    check(digest(d)==did,'design digest')
    source=next(s for s in d['sources'] if s['quarter']==quarter)
    content,metadata=read_source(source['sha256']);check(metadata['source_url']==source['url'] and len(content)==source['bytes'],'original source bytes and URL')
    pid=digest({'design_id':did,'source_sha256':source['sha256']});root=design_path.parents[2]/'parts'/pid
    part=json.loads((root/'part.json').read_text());m=json.loads((root/'manifest.json').read_text())
    check(digest(part)==m['part_digest'] and m['design_id']==did and m['source_sha256']==source['sha256'],'quarter part binding')
    original,summary=original_cells(ROOT/'data/raw'/source['sha256']/'source.bin',quarter)
    for field,value in summary.items():check(part[field]==value,'complete raw '+field)
    rows=part['rows'];keys=[(r['carrier'],r['cohort'],r['origin'],r['origin_city_market_id'],r['destination'],r['destination_city_market_id'],r['coupon_count'],r['fare_basis_group']) for r in rows]
    check(len(rows)==m['rows']==len(original) and len(set(keys))==len(keys) and set(keys)==set(original),'exact route/cohort support')
    for k,row in zip(keys,rows):
        v=original[k]
        check(row['quarter']==quarter and row['published_on']==source['published_on'] and row['source_sha256']==source['sha256'] and row['source_member']==summary['source_member'],'quarter provenance')
        for field,expected in (('sample_passengers',v['pax']),('sample_market_records',v['records']),('passenger_miles',v['miles']),('source_line_first',v['first']),('source_line_last',v['last']),('source_line_witnesses',v['witnesses']),('contributor_digest',v['hash'].hexdigest())):check(row[field]==expected,'original '+field)
        close(row['fare_weighted_sum_usd'],v['amount'],'Decimal weighted fare allocation')
        close(row['mean_prorated_market_fare_usd'],v['amount']/v['pax'],'weighted mean market fare')
        close(row['survey_fare_usd_per_flown_mile'],v['amount']/v['miles'],'survey fare per mile')
    return {'design_id':did,'quarter':quarter,'source_sha256':source['sha256'],'part_digest':m['part_digest'],
        'checks_passed':checks,'raw_ticket_records':summary['records'],'route_cells':len(rows),'quality_record_counts':summary['quality_record_counts'],
        'scope':'One quarter only: independent original-record partition, sample weights, Decimal fare allocation and complete raw-line digests. Full history/Parquet audit pending. US-only survey fare proxy, not airline revenue yield, causal pricing, calibrated forecast or financial integration.'}


def main():
    import argparse
    p=argparse.ArgumentParser();p.add_argument('design',type=Path);p.add_argument('quarter');args=p.parse_args()
    result={'audit_code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'numerical_helper_sha256':hashlib.sha256((ROOT/'scripts/audit_db1b_fares.py').read_bytes()).hexdigest(),**audit(args.design,args.quarter)}
    out=ROOT/'data/fare_history_checks/parts'/(digest(result)+'.json');out.parent.mkdir(parents=True,exist_ok=True)
    value=json.dumps(result,indent=2)+'\n'
    if out.exists() and out.read_text()!=value:raise ValueError('Immutable quarter fare audit differs')
    if not out.exists():out.write_text(value)
    print(json.dumps({'audit_file':str(out),**result},indent=2))


if __name__=='__main__':main()
