"""One-page analyst handoff derived from an independently replayed monthly run."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from html import escape
from importlib.metadata import version
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

import reportlab
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .model_archive import deterministic, digest, replay_saved
from .source_store import ROOT

VERSION='thesis-export-0.4.0'
EXPORTS=ROOT/'data/theses'
CODE_BYTES=Path(__file__).read_bytes()
FONT_ROOT=Path(reportlab.__file__).parent/'fonts'
FONTS={'Vera.ttf':(FONT_ROOT/'Vera.ttf').read_bytes(),'VeraBd.ttf':(FONT_ROOT/'VeraBd.ttf').read_bytes()}
FONT_LICENSE=(FONT_ROOT/'bitstream-vera-license.txt').read_bytes()
for name,file in (('ThesisSans','Vera.ttf'),('ThesisBold','VeraBd.ttf')):
    if name not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont(name,BytesIO(FONTS[file])))
pdfmetrics.registerFontFamily('ThesisSans',normal='ThesisSans',bold='ThesisBold',italic='ThesisSans',boldItalic='ThesisBold')
REFERENCES={'paired_no_shock':'Paired no-shock path', 'frozen_policy':'Frozen policy: difference of paired shock impacts'}


@dataclass(frozen=True)
class ThesisNotes:
    title:str
    hypothesis:str
    reference:str
    checkpoint_on:str
    checkpoint:str
    checkpoint_source:str
    falsification:str

    def validate(self,cutoff):
        for name,maximum in (('title',70),('hypothesis',220),('checkpoint',180),('checkpoint_source',180),('falsification',220)):
            text=getattr(self,name)
            if not isinstance(text,str) or text!=text.strip() or not 1<=len(text)<=maximum:
                raise ValueError(f'{name} requires 1–{maximum} characters, without leading/trailing whitespace')
            glyphs=pdfmetrics.getFont('ThesisSans').face.charToGlyph
            if any(ord(c)<32 or ord(c) not in glyphs for c in text):raise ValueError(f'{name} contains unsupported/control characters; use plain text')
        if self.reference not in REFERENCES:raise ValueError('Choose a paired no-shock or frozen-policy reference')
        try:when=date.fromisoformat(self.checkpoint_on)
        except (TypeError,ValueError) as e:raise ValueError('Checkpoint requires a YYYY-MM-DD date') from e
        if when.isoformat()!=self.checkpoint_on or when<=date.fromisoformat(cutoff):raise ValueError('Checkpoint must be after the saved information cutoff')
        source=urlsplit(self.checkpoint_source)
        if source.scheme not in ('http','https') or not source.hostname or source.username or source.password:
            raise ValueError('Checkpoint source requires an HTTP(S) URL without embedded credentials')


def make_record(verified,notes):
    """Only call with the monthly record returned by registered archived replay."""
    s=verified['scenario'];notes.validate(s['information_cutoff'])
    monthly=verified['intervention']['monthly']['unserved_schedule_asm_million']
    peak=max(range(len(monthly)),key=lambda i:monthly[i]['p50'])
    deficits=[]
    for i,actual in enumerate(verified['fleet']['intervention']['monthly']):
        planned=verified['fleet']['control']['monthly'][i]['opening_inventory']
        missing={g:max(0,planned[g]-actual['available'][g]) for g in ('narrowbody','widebody','regional')}
        if any(missing.values()):deficits.append({'month':actual['month'],'aircraft_below_planned_available':missing})
    summary=verified['summary'];opening=verified['baseline_inputs']['cash_musd']
    cumulative=summary.get('cumulative_fcf_musd')
    if cumulative is None:
        # Verified legacy cores have no financing: their ending cash minus
        # deterministic opening cash equals cumulative FCF path by path.
        if verified['scenario'].get('cash_plan') is not None or verified['scenario'].get('purchase_plan') is not None:
            raise ValueError('Cash-plan run must contain pathwise cumulative FCF quantiles')
        cumulative={q:round(summary['ending_cash_musd'][q]-opening,4) for q in ('p10','p50','p90')}
    metric='cumulative_fcf_impact_musd' if notes.reference=='paired_no_shock' else 'response_contribution_to_fcf_musd'
    distributions=[{'label':'Scenario cumulative FCF','units':'USD_million','quantiles':cumulative},
        {'label':'FCF shock impact vs paired no-shock','units':'USD_million','quantiles':summary['cumulative_fcf_impact_musd']},
        {'label':'FCF response contribution vs frozen','units':'USD_million','quantiles':summary['response_contribution_to_fcf_musd']},
        {'label':'Ending scenario cash','units':'USD_million','quantiles':summary['ending_cash_musd']}]
    cash_funding=None
    if s.get('cash_plan') is not None or s.get('purchase_plan') is not None:
        # This version's explicit events are fixed amounts across every path.
        # These four event-only series are deterministic, unlike OCF/FCF/cash.
        cash_funding={'event_count':len((s.get('cash_plan') or {}).get('events',[])),
            'purchase_batch_count':len((s.get('purchase_plan') or {}).get('purchases',[])),
            **{name:round(sum(row['p50'] for row in verified['intervention']['monthly'][name]),4)
               for name in ('financing_cash_flow_musd','other_investing_cash_flow_musd',
                            'restricted_cash_transfer_musd','working_capital_cash_adjustment_musd')},
            'definition':'Fixed cash-event and purchase-financing totals across the horizon; complete timing, scope and basis in scenario.cash_plan and scenario.purchase_plan.'}
    retention=None
    if s.get('retention_plan') is not None:
        # Counts and maintenance/carrying assumptions are fixed across paths.
        # Variable fuel costs are kept as monthly distributions, never summed medians.
        m=verified['intervention']['monthly']
        retention={'peak_retained_aircraft':max(r['p50'] for r in m['retained_aircraft']),
            'retained_aircraft_months':sum(r['p50'] for r in m['retained_aircraft']),
            'additional_maintenance_and_carrying_musd':round(sum(r['p50'] for k in ('retention_maintenance_expense_musd','retention_carrying_expense_musd') for r in m[k]),4),
            'unapplied_aircraft':sum(d['unapplied_aircraft'] for d in verified['fleet']['intervention']['retention']['decisions']),
            'definition':'Fixed analyst retention assumptions. Full notice/cap/support decisions and effective retirement queue are in the source run; additional fuel expense is path-dependent.'}
    return {'version':VERSION,'analyst_notes':asdict(notes),'run_id':verified['manifest']['run_id'],
        'model_version':verified['model_version'],'scenario':s,'months':verified['months'],'readiness':verified['readiness'],
        'distributions':distributions,'chosen_reference':{'name':notes.reference,'label':REFERENCES[notes.reference],'difference_quantiles_musd':summary[metric],
            'definition':'Quantiles of pathwise paired differences, not differences of marginal quantiles'},
        'constraints':{'peak_monthly_median_schedule_shortfall_asm_million':monthly[peak]['p50'],
            'peak_month':verified['months'][peak], 'months_with_positive_median_schedule_shortfall':sum(v['p50']>0 for v in monthly),
            'fleet_deficits':deficits,'remaining_net_entries':sum(e['aircraft'] for e in verified['fleet']['intervention']['remaining_queue'] if e['kind']=='entry'),
            'scope':'Monthly median modeled schedule shortfall; not pathwise peak quantiles. Crew/hub and fitted policy constraints are unavailable.'},
        'cash_floor':{'musd':s['cash_floor_musd'],'below_month_end_floor_path_fraction':summary['cash_floor_breach_path_fraction'],
            'definition':'Conditional simulation fraction over month-end cash balances; excludes opening balance. Not a calibrated event probability.'},
        'opening_cash':{'musd':opening,'effective_on':verified['baseline_inputs']['initial_state_effective_on']},
        'cash_funding':cash_funding,
        'retention':retention,
        'evidence':verified['evidence'],'manifest':verified['manifest'],'assumptions':verified['assumptions'],
        'limitations':['Assumed sensitivity; no fitted management policy, calibrated intervals, locked historical scores or investment-performance validation.',
            ('Purchase and retention links, residual capex, costs, loan terms and eligibility are assumptions; gross delivery reconciliation, consolidated PPE/leases and fitted collections remain pending.' if s.get('retention_plan') is not None else 'Purchase allocations, residual capex, deposits and loan terms are assumptions; the gross delivery queue is incomplete. Consolidated PPE, leases, retained-aircraft costs and fitted collections remain pending.' if s.get('purchase_plan') is not None else 'Optional cash events are assumptions over provisional OCF/capex; collections, debt timing and credit availability are not fitted. PPE/lease/deposit accounting remains pending.' if s.get('cash_plan') is not None else 'Quarterly anchors divided into monthly run rates; simplified OCF/capex bridge excludes working-capital timing and financing.'),
            'Capacity study remains separate; this simulator uses assumed productivity, fleet assignments and spare capacity.',
            'Source ledger contains normalized reviewed excerpts; complete original-passage extraction remains pending.',
            'Hypothesis, checkpoint and falsification are analyst-authored; the engine does not verify their truth or fetch the checkpoint URL.']}


def _plain(text):
    return text.replace('\u2011','-').replace('\u2013','-').replace('\u2014','-')


def render_pdf(record):
    styles={name:ParagraphStyle(name,fontName='ThesisSans',fontSize=size,leading=leading,textColor=colors.HexColor('#213145'),spaceAfter=after,splitLongWords=True)
        for name,size,leading,after in [('body',8.2,11,6),('small',7.0,9,4),('title',17,21,7),('section',8.6,11,5)]}
    def p(text,style='body'):return Paragraph(text,styles[style])
    def plain(text):return escape(_plain(str(text)))
    def section(label,text):return p('<b>'+plain(label)+'</b> '+plain(text))
    n=record['analyst_notes'];s=record['scenario'];c=record['constraints'];ref=record['chosen_reference']
    policy='Frozen' if s['response_mode']=='frozen' else 'Assumed constrained'
    story=[p('<b>UAL / '+plain(n['title'])+'</b>','title'),p('ANALYST THESIS RECORD / RESEARCH SENSITIVITY / VALIDATION PENDING','small')]
    story.append(p(f"<b>Saved cutoff</b> {plain(s['information_cutoff'])} | <b>Horizon</b> {plain(record['months'][0])} to {plain(record['months'][-1])} ({s['horizon_months']} months){' - exploratory beyond month 12' if s['horizon_months']>12 else ''}<br/><b>Policy</b> {policy} | <b>Paths</b> {s['paths']:,} | <b>Seed</b> {s['seed']} | <b>Worlds</b> {'stochastic' if s['stochastic'] else 'deterministic'}"))
    story.append(section('Analyst hypothesis.',n['hypothesis']))
    shock=f"Fuel {s['fuel_shock_pct']:+g}%; demand {s['demand_shock_pct']:+g}%; capacity {s['capacity_shock_pct']:+g}% in {s['geography'].replace('_',' ')}. Start month {s['onset_month']}, duration {s['duration_months']} months, recovery half-life {s['recovery_half_life_months']:g} months."
    selection=str(s.get('delivery_aircraft_count'))+' aircraft' if s.get('delivery_aircraft_count') is not None else 'all eligible entries'
    fleet=f"Fleet entry delay {s['delivery_delay_months']} months ({s['delivery_group']}, {selection}); aircraft unavailable {s['aircraft_unavailable_pct']:g}% ({s['availability_group']}); regional contract shock {s['regional_contract_shock_pct']:+g}%. "+('Mapped purchase settlement and funding follow delayed entries; prior deposits stay paid.' if s.get('purchase_plan') is not None else 'Entry delays do not defer modeled capex.')
    story.append(section('Scenario.',shock+' '+fleet))
    funding=record.get('cash_funding')
    if funding is not None:
        story.append(p(f"<b>Assumed cash and funding.</b> {funding['event_count']} dated cash events, {funding.get('purchase_batch_count',0)} purchase batches; net financing {funding['financing_cash_flow_musd']:+,.1f}, other investing {funding['other_investing_cash_flow_musd']:+,.1f}, transfer to restricted cash {funding['restricted_cash_transfer_musd']:+,.1f}, working-capital cash {funding['working_capital_cash_adjustment_musd']:+,.1f} $M. Full timing, scope and analyst basis are in the companion JSON.",'small'))
    rows=[[p('<b>Conditional distribution / USD millions</b>'),p('<b>P10</b>'),p('<b>P50</b>'),p('<b>P90</b>')]]
    for d in record['distributions']:
        rows.append([p(plain(d['label'])),*[p(f"{d['quantiles'][q]:,.1f}") for q in ('p10','p50','p90')]])
    table=Table(rows,colWidths=[310,66,66,66],hAlign='LEFT')
    table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e9eff4')),('LINEBELOW',(0,0),(-1,-1),.35,colors.HexColor('#c9d2dc')),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),1)]))
    story.extend([table,Spacer(1,7)])
    q=ref['difference_quantiles_musd']
    story.append(section('Disagreement with chosen reference.',ref['label']+f"; paired FCF difference P10/P50/P90: {q['p10']:,.1f} / {q['p50']:,.1f} / {q['p90']:,.1f} $M. These are quantiles of paired path differences."))
    constraint=(f"Largest monthly median schedule shortfall: {c['peak_monthly_median_schedule_shortfall_asm_million']:,.1f} million ASMs in {c['peak_month']}." if c['months_with_positive_median_schedule_shortfall'] else 'No positive schedule shortfall in the monthly medians.')
    constraint+=f" {c['remaining_net_entries']} planned net entries remain queued. Crew/hub and fitted-policy constraints are not modeled."
    if record.get('retention') is not None:
        r=record['retention'];constraint+=f" Assumed retention peaks at {r['peak_retained_aircraft']:g} aircraft, with ${r['additional_maintenance_and_carrying_musd']:,.1f}M additional maintenance/carrying expense."
    floor=record['cash_floor'];constraint+=f" {floor['below_month_end_floor_path_fraction']*100:.1f}% of assumed paths breach ${floor['musd']:,.0f}M at a month-end; this is not a calibrated probability."
    story.append(section('Modeled binding constraint.',constraint))
    source=escape(n['checkpoint_source'],quote=True)
    story.append(p('<b>Next observable checkpoint.</b> '+plain(n['checkpoint_on'])+' - '+plain(n['checkpoint'])+'<br/><b>Analyst-specified evidence source:</b> <link href="'+source+'" color="#145680">'+plain(n['checkpoint_source'])+'</link>'))
    story.append(section('Evidence that would overturn the view.',n['falsification']))
    refs=[]
    for src in record['evidence']['sources']:
        link=escape(src['url'],quote=True);label=plain(src['source_id'].replace('_',' '))
        refs.append('<link href="'+link+'" color="#145680">'+label+'</link> ('+plain(src['published_on'])+')')
    story.append(p('<b>Run evidence / publication dates.</b> '+'; '.join(refs)+'. Reviewed source excerpts, input hashes, fleet details and all assumptions are in the companion JSON.','small'))
    fcf_definition=('FCF sums OCF minus capex per path; financing and other investing cash are outside FCF.' if s.get('cash_plan') is not None or s.get('purchase_plan') is not None else 'Cumulative FCF equals ending minus opening cash in this unfinanced run.')
    limits=' '.join(record['limitations'][:4])+f" Opening cash ${record['opening_cash']['musd']:,.0f}M dated {record['opening_cash']['effective_on']}. "+fcf_definition
    story.append(p('<b>Interpretation limits.</b> '+plain(limits),'small'))
    out=BytesIO();document=SimpleDocTemplate(out,pagesize=letter,rightMargin=52,leftMargin=52,topMargin=35,bottomMargin=41,title=n['title'],author='UAL Counterfactual Lab',invariant=1)
    def footer(canvas,doc):
        canvas.setFont('ThesisSans',6.8);canvas.setFillColor(colors.HexColor('#53667a'))
        canvas.drawString(52,27,'Run '+record['run_id'][:16]+' / '+VERSION+' / '+record['model_version'])
        canvas.drawRightString(560,27,'No automated trading / 1 page')
    document.build(story,onFirstPage=footer,onLaterPages=footer)
    content=out.getvalue()
    if len(PdfReader(BytesIO(content)).pages)!=1:raise ValueError('Thesis exceeds one page; shorten the analyst notes')
    return content


def _json(value):
    # Browser JSON.stringify drops integral float notation. Preserve one canonical
    # representation so equivalent displayed records produce the same export ID.
    def canonical(item):
        if isinstance(item,float) and item.is_integer():return int(item)
        if isinstance(item,dict):return {k:canonical(v) for k,v in item.items()}
        if isinstance(item,list):return [canonical(v) for v in item]
        return item
    return (json.dumps(canonical(value),sort_keys=True,indent=2,ensure_ascii=False,allow_nan=False)+'\n').encode()


def _write_immutable(path,content):
    fd,temporary=tempfile.mkstemp(prefix='.write-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(content)
        try:os.link(temporary,path)
        except FileExistsError:
            if path.read_bytes()!=content:raise ValueError('Immutable thesis export differs')
    finally:Path(temporary).unlink(missing_ok=True)


def export_thesis(run_record,notes,destination=EXPORTS):
    """Reject fabricated outputs by replaying the displayed record before export."""
    if not isinstance(run_record,dict):raise ValueError('Thesis requires a saved monthly run record')
    try:payload=_json(run_record)
    except (TypeError,ValueError) as e:raise ValueError('Run record must contain finite JSON values') from e
    if len(payload)>2_000_000:raise ValueError('Monthly run record exceeds the 2 MB thesis limit')
    with tempfile.TemporaryDirectory(prefix='ual-thesis-') as temp:
        runfile=Path(temp)/'run.json';runfile.write_bytes(payload)
        verified=replay_saved(runfile,'monthly')
    record=make_record(verified,notes)
    normalized=deterministic(verified);normalized['replay_code']=verified['replay_code']
    companion={'thesis':record,'source_run':normalized}
    files={'thesis.json':_json(companion),'thesis.pdf':render_pdf(record),'builder.py':CODE_BYTES,
        **FONTS,'font-license.txt':FONT_LICENSE}
    manifest={'version':VERSION,'run_id':record['run_id'],'reportlab_version':version('reportlab'),'pypdf_version':version('pypdf'),
        'record_digest':digest(record),'source_run_digest':digest(deterministic(normalized)),
        'files':{n:hashlib.sha256(v).hexdigest() for n,v in files.items()},
        'replay_status':'Monthly numerical record independently recomputed using locally registered archived source; PDF/JSON/renderer/font bytes preserved. Runtime binaries are not bundled.'}
    manifest['artifact_id']=digest(manifest);identity=manifest['artifact_id'];target=Path(destination)/identity
    target.mkdir(parents=True,exist_ok=True)
    for name,content in {**files,'manifest.json':_json(manifest)}.items():
        path=target/name
        _write_immutable(path,content)
    return {'artifact_id':identity,'run_id':record['run_id'],'path':str(target),'thesis':record}


def verified_export(identity,destination=EXPORTS):
    if not isinstance(identity,str) or not re.fullmatch('[a-f0-9]{64}',identity):raise ValueError('Invalid thesis artifact ID')
    folder=Path(destination)/identity;manifest=json.loads((folder/'manifest.json').read_text())
    if manifest['artifact_id']!=identity or digest({k:v for k,v in manifest.items() if k!='artifact_id'})!=identity:raise ValueError('Thesis manifest checksum failed')
    if set(manifest['files'])!={'thesis.json','thesis.pdf','builder.py','Vera.ttf','VeraBd.ttf','font-license.txt'}:raise ValueError('Unsupported thesis file manifest')
    for name,sha in manifest['files'].items():
        if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=sha:raise ValueError('Thesis artifact checksum failed')
    companion=json.loads((folder/'thesis.json').read_text())
    if digest(companion['thesis'])!=manifest['record_digest'] or digest(deterministic(companion['source_run']))!=manifest['source_run_digest']:raise ValueError('Thesis record checksum failed')
    return folder,manifest,companion


def replay_thesis(identity,destination=EXPORTS):
    folder,manifest,companion=verified_export(identity,destination)
    if manifest['files']['builder.py']!=hashlib.sha256(CODE_BYTES).hexdigest() or manifest['reportlab_version']!=version('reportlab'):
        raise ValueError('Saved thesis renderer/version differs; preserved PDF remains verifiable, current-renderer replay is unsupported')
    notes=ThesisNotes(**companion['thesis']['analyst_notes'])
    with tempfile.TemporaryDirectory(prefix='ual-thesis-replay-') as temp:
        path=Path(temp)/'run.json';path.write_bytes(_json(companion['source_run']));verified=replay_saved(path,'monthly')
    recomputed=make_record(verified,notes)
    if digest(recomputed)!=manifest['record_digest']:raise ValueError('Replayed thesis derivation differs')
    if hashlib.sha256(render_pdf(recomputed)).hexdigest()!=manifest['files']['thesis.pdf']:raise ValueError('Replayed PDF differs')
    return {'artifact_id':identity,'run_id':manifest['run_id'],'status':'numerical derivation and one-page PDF replay verified'}
