"""Preserve catalog-observed public archive downloads and build quarterly proxies."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.source_store import preserve_source
from app.fare_history import build_history


def main():
    spec=json.loads((ROOT/'data/fare_acquisition_spec.json').read_text())
    downloads=json.loads((ROOT/'tmp/fare-downloads.json').read_text())
    by_url={r['url']:r for r in downloads}
    if len(downloads)!=44 or len(by_url)!=44 or set(by_url)!={s['url'] for s in spec['sources']}:raise ValueError('All 44 declared quarter downloads are required')
    sources=[]
    for source in spec['sources']:
        p=Path(by_url[source['url']]['path']);saved=preserve_source(p.read_bytes(),source['url'],'application/zip')
        sources.append({**source,'sha256':saved['sha256'],'bytes':saved['bytes'],'retrieved_at_utc':saved['retrieved_at_utc']})
    print(json.dumps(build_history(sources,spec,progress=lambda event:print(json.dumps(event),file=sys.stderr,flush=True)),indent=2))


if __name__=='__main__':main()
