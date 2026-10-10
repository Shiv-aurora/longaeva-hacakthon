"""Independent original-source cohort and reviewed-history quantity checks.

No correction builder, operating parser or traffic estimator is imported.
"""
from collections import Counter, defaultdict
from datetime import date
import hashlib
from io import BytesIO, TextIOWrapper
import json
from pathlib import Path
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.evidence import digest
from app.source_store import read_source
from scripts.audit_monthly_traffic import raw_fields, region


def original_cohort(source, period):
    content, metadata = read_source(source["sha256"])
    if metadata["source_url"] != source["source_url"]: raise ValueError("Independent cohort URL differs")
    values, payloads, lines = defaultdict(lambda:[0,0,set()]), Counter(), []
    with ZipFile(BytesIO(content)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".asc")]
        if len(names) != 1: raise ValueError("Independent cohort member differs")
        with z.open(names[0]) as b:
            for line, text in enumerate(TextIOWrapper(b,encoding="utf-8-sig"),1):
                if not text.strip(): continue
                f = raw_fields(text)
                p = f"{int(f[0]):04d}-{int(f[1]):02d}"
                if p != period or f[10] != "AA" or f[14] != "F" or f[17] not in ("1","3"): continue
                payloads[tuple(f[2:])] += 1; lines.append(line)
                market = region(int(f[4]),int(f[8]))
                if market is None: continue
                seats = sum(int(f[i]) for i in (21,22,23)) if len(f) == 32 else int(f[21])
                pax = sum(int(f[i]) for i in (24,25,26)) if len(f) == 32 else int(f[22])
                values[market][0] += pax*int(f[13]); values[market][1] += seats*int(f[13]); values[market][2].add(f[11])
    return {"payloads":payloads,"source_lines":lines,"values":values,"source_member":names[0]}


def audit(target):
    checks, changed_histories, changed_targets = 0, 0, 0
    def check(value,name):
        nonlocal checks
        if not value: raise ValueError("Independent reviewed traffic audit failed: " + name)
        checks += 1
    target = Path(target)
    manifest=json.loads((target/'manifest.json').read_text());a=json.loads((target/'artifact.json').read_text())
    check(digest({k:v for k,v in manifest.items() if k!='snapshot_id'}) == target.name == manifest['snapshot_id'],"reviewed manifest")
    check(digest(a)==manifest['artifact_digest'],"reviewed artifact")
    design=json.loads((target.parent/'designs'/manifest['design_id']/'design.json').read_text());spec=design['specification']
    check(digest(design)==manifest['design_id']==a['design_id'],"correction design")
    native_root=ROOT/'data/traffic_vintages'/spec['vintage_snapshot_id']
    nm=json.loads((native_root/'manifest.json').read_text());native=json.loads((native_root/'artifact.json').read_text())
    check(digest({k:v for k,v in nm.items() if k!='snapshot_id'})==native_root.name,"native manifest")
    check(digest(native)==nm['artifact_digest']==design['upstream_artifact_digest'],"native artifact")
    nd=json.loads((native_root.parent/'designs'/nm['design_id']/'design.json').read_text())
    check(digest(nd)==nm['design_id'],"native design")
    source_index={s['sha256']:s for s in nd['spec']['sources']}
    candidates=[s for s in nd['spec']['sources'] if s['period_first'] <= '2021-01' <= s['period_last']]
    references={scope:original_cohort(source_index[sha],'2020-01') for scope,sha in spec['reference_sources'].items()}
    replacements={scope:original_cohort(source_index[sha],'2021-01') for scope,sha in spec['replacement_sources'].items()}
    raw={s['sha256']:original_cohort(s,'2021-01') for s in candidates}
    detected={sha for sha,r in raw.items() if r['payloads'] and r['payloads']==references[source_index[sha]['scope']]['payloads']}
    check(set(a['patches'])==detected,"exact raw whole-cohort detection")
    check({r['source_sha256'] for r in a['cohort_review']}==set(raw),"complete candidate review")
    for row in a['cohort_review']:
        sha=row['source_sha256'];r=raw[sha];scope=source_index[sha]['scope']
        check(row['scope']==scope and row['period']=='2021-01',"scope/month of reviewed cohort")
        check(row['cohort_rows']==len(r['source_lines']) and row['source_member']==r['source_member'],"raw cohort count/member")
        check(row['literal_payload_digest']==digest(sorted([list(k),v] for k,v in r['payloads'].items())),"all literal non-date fields/multiplicity")
        check((row['status']=='confirmed_prior_year_cohort_reuse')==(sha in detected),"correction classification")
        if sha not in detected: continue
        check(a['patches'][sha]==row,"exact typed correction")
        check(row['replacement_source_sha256']==spec['replacement_sources'][scope] and row['reference_source_sha256']==spec['reference_sources'][scope],"replacement/reference source binding")
        check(row['rejected_source_lines']==r['source_lines'] and row['reference_source_lines']==references[scope]['source_lines'] and row['replacement_source_lines']==replacements[scope]['source_lines'],"complete raw correction lines")
        check(row['replacement_rows']==len(replacements[scope]['source_lines']) and replacements[scope]['payloads']!=references[scope]['payloads'],"replacement differs from prior-year cohort")
        check(row['replacement_published_on']==source_index[spec['replacement_sources'][scope]]['published_on']<=row['rejected_published_on']==source_index[sha]['published_on'],"earlier released replacement")
    def inspect(before, after, releases, issue, applied):
        original={(r['carrier'],r['market'],r['period']):r for r in before}
        saved={(r['carrier'],r['market'],r['period']):r for r in after}
        check(len(saved)==len(after) and set(saved)==set(original),"identical retained target coverage")
        selected=[r for r in releases if r['period']=='2021-01']
        expected_patches=[{**a['patches'][r['source_sha256']], 'selected_release':r,'review_known_on':spec['review_known_on'],'knowledge_label':a['knowledge_label']}
            for r in selected if r['source_sha256'] in detected]
        check(applied==expected_patches,"selected-release-only correction actions")
        changed=0
        for key,row in saved.items():
            check(row['review_known_on']==spec['review_known_on']>'2023-12-31' and 'hindsight' in row['knowledge_label'],"present-day knowledge labeling")
            check(row['published_on']<=issue and row['period']<=issue[:7] and row['period']<='2023-12',"historical source/locked date gates")
            cleaned={k:v for k,v in row.items() if k not in ('source_corrections','review_known_on','knowledge_label')}
            relevant=expected_patches if key[0]=='AA' and key[2]=='2021-01' else []
            check(row['source_corrections']==relevant,"exact record correction provenance")
            if not relevant:
                check(cleaned==original[key],"unaffected original record preserved")
                continue
            values=[0,0,set(),set()];contributor_dates=[]
            for release in selected:
                sha=release['source_sha256'];scope=release['scope']
                effective=spec['replacement_sources'][scope] if sha in detected else sha
                source = replacements[scope] if sha in detected else raw[sha]
                if key[1] not in source['values']: continue
                v=source['values'][key[1]];values[0]+=v[0];values[1]+=v[1];values[2]|=v[2];values[3].add(effective)
                contributor_dates.append(source_index[effective]['published_on'])
                check(source_index[effective]['published_on']<=issue,"replacement available at issue")
            check(row['rpm_million']==values[0]/1e6 and row['asm_million']==values[1]/1e6,"original effective-source RPM/ASM sums")
            check(row['carrier_entities']==sorted(values[2]) and row['source_sha256s']==sorted(values[3]),"actual effective contributors")
            check(row['contributor_published_on']==max(contributor_dates),"actual contributor release date")
            check(row['scope_releases']==selected and row['published_on']==max([*contributor_dates,*(r['published_on'] for r in selected)]),"both original scope gates retained")
            changed += (row['rpm_million'],row['asm_million'])!=(original[key]['rpm_million'],original[key]['asm_million'])
        return changed
    check(len(a['origins'])==len(native['origins'])==48,"all proposal origins retained")
    for original,origin in zip(native['origins'],a['origins']):
        check(origin['issue_on']==original['issue_on'] and origin['selected_releases']==original['selected_releases'],"native issue and release selections")
        for field in ('newest_complete_period','information_lag_months','observations_by_carrier'):
            check(origin[field]==original[field],"unchanged availability/count statistic")
        changed_histories += inspect([native['observation_versions'][i] for i in original['observation_ids']],
            [a['observation_versions'][i] for i in origin['observation_ids']],origin['selected_releases'],origin['issue_on'],origin['corrections_applied'])
    truth_releases={}
    for row in native['evaluation_targets']['observations']:
        for release in row['scope_releases']:truth_releases[release['scope'],release['period']]=release
    changed_targets=inspect(native['evaluation_targets']['observations'],a['evaluation_targets']['observations'],
        [truth_releases[k] for k in sorted(truth_releases)],nd['spec']['acquisition_cutoff'],a['evaluation_corrections'])
    return {'checks_passed':checks,'cohorts_reviewed':len(raw),'confirmed_reuse_archives':len(detected),
        'history_observations_with_quantity_changes':changed_histories,'evaluation_targets_with_quantity_changes':changed_targets,
        'scope':'Independent whole-cohort raw payload proof, typed source-line corrections, all 48 histories and evaluation RPM/ASM/contributors/dates/coverage/hindsight labels. No estimation acceptance, FF/regional reconciliation or forecast validation.'}


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot',type=Path);args=parser.parse_args()
    root=ROOT/'data/reviewed_traffic_vintages';target=args.snapshot or root/json.loads((root/'latest.json').read_text())['snapshot_id']
    result={'snapshot_id':target.name,'audit_code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),**audit(target)}
    p=ROOT/'data/reviewed_traffic_checks'/(digest(result)+'.json');p.parent.mkdir(exist_ok=True)
    value=json.dumps(result,indent=2)+'\n'
    if p.exists() and p.read_text()!=value:raise ValueError('Immutable reviewed audit differs')
    if not p.exists():p.write_text(value)
    print(json.dumps({'audit_file':str(p),**result},indent=2))


if __name__=='__main__':main()
