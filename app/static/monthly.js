'use strict';
(() => {
  const el = id => document.getElementById(id);
  const fmt = n => Number(n).toLocaleString('en-US', {maximumFractionDigits:1});
  const money = n => (n < 0 ? '−' : '+') + '$' + fmt(Math.abs(n)) + 'M';
  // Views retain their forms and result state; navigation never resets a scenario.
  const tabs=[...document.querySelectorAll('[data-view]')];
  function showView(name,focus=false){
    const selected=tabs.find(tab=>tab.dataset.view===name);if(!selected)return;
    for(const tab of tabs){const active=tab===selected;tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;el(tab.getAttribute('aria-controls')).hidden=!active}
    if(focus)selected.focus();
  }
  for(const tab of tabs){
    tab.addEventListener('click',()=>showView(tab.dataset.view));
    tab.addEventListener('keydown',event=>{const index=tabs.indexOf(tab);let next;if(event.key==='ArrowRight')next=(index+1)%tabs.length;else if(event.key==='ArrowLeft')next=(index+tabs.length-1)%tabs.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=tabs.length-1;else return;event.preventDefault();showView(tabs[next].dataset.view,true)});
  }
  const sectionViews={'failure-search':'failure','target-inversion':'target','thesis-export':'thesis',evidence:'evidence','cash-funding':'evidence','purchase-funding':'evidence','retention-result':'evidence','convergence-check':'evidence','traffic-vintage-evidence':'evidence','traffic-evidence':'evidence','capacity-evidence':'evidence','utilization-evidence':'evidence'};
  function followSection(){const section=location.hash.slice(1);if(sectionViews[section])showView(sectionViews[section]);else if(section.startsWith('view-'))showView(section.slice(5))}
  window.addEventListener('hashchange',followSection);followSection();
  // Hidden disclosures must open before browser validation focuses a required field.
  el('monthlyForm').addEventListener('invalid',event=>{showView('scenario');for(let node=event.target.parentElement;node;node=node.parentElement)if(node.tagName==='DETAILS')node.open=true},true);

  function comparisonTable(headers,rows,captionText){
    const node=document.createElement('table'),caption=document.createElement('caption');caption.textContent=captionText;node.append(caption);
    const head=document.createElement('thead'),header=document.createElement('tr');for(const text of headers){const cell=document.createElement('th');cell.scope='col';cell.textContent=text;header.append(cell)}head.append(header);node.append(head);
    const body=document.createElement('tbody');for(const values of rows){const row=document.createElement('tr');for(const text of values){const cell=document.createElement('td');cell.textContent=text;row.append(cell)}body.append(row)}node.append(body);return node;
  }
  let current = null;
  let presetBusy=false, hostedDemo=false;
  let thesisBusy=false, thesisNotesVersion=0;
  let convergenceBusy=false,currentConvergence=null,convergenceControlsVersion=0;
  const cashKinds={borrow:'Debt borrowing',repay:'Debt principal repayment',revolver_draw:'Revolver draw',revolver_repay:'Revolver repayment',advance_sales_change:'Advance sales balance change',loyalty_balance_change:'Loyalty deferred balance change',receivables_change:'Receivables balance change',inventory_change:'Inventory balance change',prepaids_change:'Prepaids balance change',payables_change:'Payables balance change',additional_capex:'Additional cash capex',capex_refund:'Cash capex refund',cash_interest:'Additional cash interest',financing_fee:'Financing cash fee',asset_sale:'Asset sale cash proceeds',investment_trade:'Investment purchase (+) / sale (−)',restricted_transfer:'To (+) / from (−) restricted cash'};
  function cashPlanSettings(){
    if(!el('cashPlanEnabled').checked)return null;
    return {basis:el('cashPlanBasis').value,events:[...el('cashEventRows').children].map(row=>({month:row.querySelector('[data-field="month"]').value,kind:row.querySelector('[data-field="kind"]').value,amount_musd:row.querySelector('[data-field="amount_musd"]').value===''?null:Number(row.querySelector('[data-field="amount_musd"]').value),scope:row.querySelector('[data-field="scope"]').value}))};
  }
  function addCashEvent(event){
    if(el('cashEventRows').children.length>=96)return;
    const row=document.createElement('div');row.className='cash-event';
    function field(name,label,node){const wrap=document.createElement('label');wrap.append(document.createTextNode(label));node.dataset.field=name;wrap.append(node);row.append(wrap);return node}
    const month=field('month','Settlement month',document.createElement('input'));month.type='month';month.required=true;month.value=event?.month||(()=>{const [year,m]=el('cutoff').value.split('-').map(Number);return new Date(Date.UTC(year,m,1)).toISOString().slice(0,7)})();
    const kind=field('kind','Event',document.createElement('select'));for(const [value,label] of Object.entries(cashKinds)){const option=document.createElement('option');option.value=value;option.textContent=label;kind.append(option)}kind.value=event?.kind||'borrow';
    const amount=field('amount_musd','Amount ($M)',document.createElement('input'));amount.type='number';amount.step='any';amount.required=true;amount.min='-100000';amount.max='100000';if(event)amount.value=event.amount_musd;
    const scope=field('scope','Applies to',document.createElement('select'));for(const [value,label] of [['intervention','Shock only'],['both','Both paths']]){const option=document.createElement('option');option.value=value;option.textContent=label;scope.append(option)}scope.value=event?.scope||'intervention';
    const remove=document.createElement('button');remove.type='button';remove.className='secondary-btn';remove.textContent='Remove event';remove.addEventListener('click',()=>{row.remove();el('monthlyForm').dispatchEvent(new Event('input',{bubbles:true}))});row.append(remove);el('cashEventRows').append(row);
  }
  function restoreCashPlan(plan){el('cashPlanEnabled').checked=plan!=null;el('cashPlanFields').hidden=plan==null;el('cashPlanFields').disabled=plan==null;el('cashPlanBasis').value=plan?.basis||'';el('cashEventRows').replaceChildren();if(plan)for(const event of plan.events)addCashEvent(event)}
  el('cashPlanEnabled').addEventListener('change',()=>{el('cashPlanFields').hidden=!el('cashPlanEnabled').checked;el('cashPlanFields').disabled=!el('cashPlanEnabled').checked});
  el('addCashEvent').addEventListener('click',()=>{addCashEvent();el('monthlyForm').dispatchEvent(new Event('input',{bubbles:true}))});
  const numericField=node=>node.value===''?null:Number(node.value);
  function purchasePlanSettings(){
    if(!el('purchasePlanEnabled').checked)return null;
    return {basis:el('purchasePlanBasis').value,residual_monthly_capex_musd:numericField(el('residualCapex')),purchases:[...el('purchaseRows').children].map(row=>{
      const read=name=>row.querySelector(`[data-purchase-field="${name}"]`);
      return {purchase_id:read('purchase_id').value,fleet_record_id:read('fleet_record_id').value,aircraft:numericField(read('aircraft')),unit_price_musd:numericField(read('unit_price_musd')),opening_deposits_musd:numericField(read('opening_deposits_musd')),debt_fraction:numericField(read('debt_fraction'))==null?null:Number(read('debt_fraction').value)/100,annual_interest_rate:numericField(read('annual_interest_rate'))==null?null:Number(read('annual_interest_rate').value)/100,financing_fee_fraction:numericField(read('financing_fee_fraction'))==null?null:Number(read('financing_fee_fraction').value)/100,deposits:[...row.querySelector('.purchase-deposits').children].map(d=>({month:d.querySelector('[data-deposit-field="month"]').value,amount_musd:numericField(d.querySelector('[data-deposit-field="amount_musd"]'))}))};
    })};
  }
  function changedPlan(){el('monthlyForm').dispatchEvent(new Event('input',{bubbles:true}))}
  function addPurchase(p){
    if(el('purchaseRows').children.length>=48)return;
    const row=document.createElement('div');row.className='purchase-row';
    function field(name,label,node){const wrap=document.createElement('label');wrap.append(document.createTextNode(label));node.dataset.purchaseField=name;node.required=true;wrap.append(node);row.append(wrap);return node}
    const id=field('purchase_id','Purchase batch ID',document.createElement('input'));id.maxLength=60;id.pattern='[A-Za-z0-9_-]+';id.value=p?.purchase_id||'';
    const ref=field('fleet_record_id','Linked fleet entry',document.createElement('select'));const blank=document.createElement('option');blank.value='';blank.textContent='Select a future mainline entry';ref.append(blank);
    const entries=current?.fleet.plan.events.filter(e=>e.kind==='entry'&&e.group!=='regional')||[];
    for(const e of entries){const o=document.createElement('option');o.value=e.record_id;o.textContent=`${e.month} · ${e.family} · ${e.aircraft} net entries`;ref.append(o)}
    if(p&&!entries.some(e=>e.record_id===p.fleet_record_id)){const o=document.createElement('option');o.value=p.fleet_record_id;o.textContent=p.fleet_record_id;ref.append(o)}ref.value=p?.fleet_record_id||'';
    for(const [name,label,min,max,scale,step] of [['aircraft','Aircraft in this purchase batch',1,1000,1,1],['unit_price_musd','Cash purchase price per aircraft ($M)',.01,500,1,'any'],['opening_deposits_musd','Allocated deposits already paid ($M)',0,100000,1,'any'],['debt_fraction','Delivery payment funded by cash borrowing (%)',0,100,100,'any'],['annual_interest_rate','Annual purchase debt interest (%)',0,50,100,'any'],['financing_fee_fraction','Cash financing fee (% of borrowing)',0,10,100,'any']]){
      const n=field(name,label,document.createElement('input'));n.type='number';n.min=min;n.max=max;n.step=step;if(p)n.value=p[name]*scale;
    }
    const deposits=document.createElement('div');deposits.className='purchase-deposits';row.append(deposits);
    function addDeposit(d){if(deposits.children.length>=24)return;const r=document.createElement('div');r.className='purchase-deposit';for(const [name,label,type] of [['month','Deposit payment month','month'],['amount_musd','Deposit payment ($M)','number']]){const l=document.createElement('label'),n=document.createElement('input');l.append(document.createTextNode(label));n.type=type;n.required=true;n.dataset.depositField=name;if(type==='number'){n.min=0;n.max=100000;n.step='any'}n.value=d?.[name]??'';l.append(n);r.append(l)}const remove=document.createElement('button');remove.type='button';remove.className='secondary-btn';remove.textContent='Remove deposit';remove.addEventListener('click',()=>{r.remove();changedPlan()});r.append(remove);deposits.append(r)}
    if(p)for(const d of p.deposits)addDeposit(d);
    const add=document.createElement('button');add.type='button';add.className='secondary-btn';add.textContent='Add dated deposit';add.addEventListener('click',()=>{addDeposit();changedPlan()});row.append(add);
    const remove=document.createElement('button');remove.type='button';remove.className='secondary-btn';remove.textContent='Remove purchase batch';remove.addEventListener('click',()=>{row.remove();changedPlan()});row.append(remove);el('purchaseRows').append(row);
  }
  function restorePurchasePlan(plan){el('purchasePlanEnabled').checked=plan!=null;el('purchasePlanFields').hidden=plan==null;el('purchasePlanFields').disabled=plan==null;el('purchasePlanBasis').value=plan?.basis||'';el('residualCapex').value=plan?.residual_monthly_capex_musd??'';el('purchaseRows').replaceChildren();if(plan)for(const p of plan.purchases)addPurchase(p);el('purchasePlanStatus').textContent=plan?'Purchase assumptions loaded; review amounts, dates and residual capex before running.':'No purchase schedule enabled.'}
  el('purchasePlanEnabled').addEventListener('change',()=>{el('purchasePlanFields').hidden=!el('purchasePlanEnabled').checked;el('purchasePlanFields').disabled=!el('purchasePlanEnabled').checked;if(el('purchasePlanEnabled').checked&&!el('purchaseRows').children.length)addPurchase();el('purchasePlanStatus').textContent=el('purchasePlanEnabled').checked?'Enter explicit purchase assumptions.':'No purchase schedule enabled.'});
  el('addPurchase').addEventListener('click',()=>{addPurchase();changedPlan()});
  el('loadPurchaseExample').addEventListener('click',async()=>{el('loadPurchaseExample').disabled=true;try{const response=await fetch('/api/monthly/purchase-example');if(!response.ok)throw Error('Example unavailable');restorePurchasePlan(await response.json());el('purchasePlanStatus').textContent='Illustrative assumptions loaded. This is not a United forecast. Edit the visible terms before running.';changedPlan()}catch(error){el('purchasePlanStatus').textContent=error.message}finally{el('loadPurchaseExample').disabled=false}});
  function renderPurchaseFunding(run){
    const active=run.scenario.purchase_plan!=null;el('purchase-funding').hidden=!active;if(!active)return;
    const track=run.fleet.intervention,control=run.fleet.control.purchase_cash.monthly,actual=track.purchase_cash.monthly;
    const count=track.effective_queue.filter(e=>e.original_month&&e.purchase_id).reduce((sum,e)=>sum+e.aircraft,0),unallocated=track.purchase_mapping.unallocated_mainline_entries.reduce((sum,e)=>sum+e.aircraft,0),pending=track.purchase_cash.pending_settlements.reduce((sum,e)=>sum+e.aircraft,0);
    el('purchaseFundingStatus').textContent=`Saved run ${run.manifest.run_id.slice(0,12)} · ${track.purchase_mapping.allocated_aircraft} purchase aircraft allocated · ${count} delayed · ${pending} purchase aircraft remain queued. ${unallocated} mainline net entries are unallocated; their funding must be covered by assumed residual capex. This is a partial allocation, not a reconciled gross delivery forecast.`;
    el('purchaseFundingBasis').textContent='Analyst assumptions: '+run.scenario.purchase_plan.basis;
    el('purchaseFundingRows').replaceChildren();for(let i=0;i<run.months.length;i++){const row=document.createElement('tr');const values=[run.months[i],...['purchase_deposit_cash_musd','purchase_settlement_cash_musd','purchase_borrowing_musd','cash_capex_musd','allocated_purchase_deposits_musd','purchase_asset_additions_musd'].map(key=>`${fmt(control[i][key])} / ${fmt(actual[i][key])}`)];for(const value of values){const td=document.createElement('td');td.textContent=value;row.append(td)}el('purchaseFundingRows').append(row)}
    el('purchaseFundingDetails').textContent=JSON.stringify({plan:run.scenario.purchase_plan,control:run.fleet.control,intervention:track},null,2);
  }
  function retentionPlanSettings(){
    if(!el('retentionPlanEnabled').checked)return null;
    return {basis:el('retentionPlanBasis').value,max_retained_aircraft:numericField(el('retentionCap')),links:[...el('retentionRows').children].map(row=>{
      const read=name=>row.querySelector(`[data-retention-field="${name}"]`),link={};
      for(const name of ['retention_id','exit_record_id','purchase_id'])link[name]=read(name).value;
      for(const name of ['aircraft','max_extension_months','minimum_notice_months','additional_maintenance_musd_per_aircraft_month','additional_carrying_musd_per_aircraft_month'])link[name]=numericField(read(name));
      for(const name of ['productivity_fraction','extra_fuel_burn_fraction'])link[name]=numericField(read(name))==null?null:Number(read(name).value)/100;
      for(const name of ['maintenance_supported','crew_supported'])link[name]=read(name).checked;
      return link;
    })};
  }
  function addRetention(link){
    if(el('retentionRows').children.length>=48)return;
    const row=document.createElement('details');row.className='retention-link';row.open=!link;const summary=document.createElement('summary');summary.textContent=link?'Retirement link: '+link.retention_id:'New retirement link';row.append(summary);
    function field(name,label,node){const wrap=document.createElement('label');wrap.append(document.createTextNode(label));node.dataset.retentionField=name;if(node.type!=='checkbox')node.required=true;else wrap.className='support-label';wrap.append(node);row.append(wrap);return node}
    const id=field('retention_id','Retirement link ID',document.createElement('input'));id.maxLength=60;id.pattern='[A-Za-z0-9_-]+';id.value=link?.retention_id||'';
    const exits=current?.fleet.plan.events.filter(e=>e.kind==='exit'&&e.group!=='regional')||[];
    const purchases=purchasePlanSettings()?.purchases||[];
    for(const [name,label,options] of [['exit_record_id','Planned mainline exit',exits.map(e=>[e.record_id,`${e.month} · ${e.family} · ${e.aircraft} net exits`])],['purchase_id','Mapped replacement purchase',purchases.map(p=>[p.purchase_id,p.purchase_id])]]){
      const select=field(name,label,document.createElement('select'));const blank=document.createElement('option');blank.value='';blank.textContent='Select a link';select.append(blank);for(const [value,text] of options){const o=document.createElement('option');o.value=value;o.textContent=text;select.append(o)}if(link&&!options.some(o=>o[0]===link[name])){const o=document.createElement('option');o.value=link[name];o.textContent=link[name];select.append(o)}select.value=link?.[name]||'';
    }
    for(const [name,label,min,max,scale,step] of [['aircraft','Aircraft requested for retention',1,1000,1,1],['max_extension_months','Maximum retirement extension (months)',0,24,1,1],['minimum_notice_months','Minimum notice before retirement (months)',0,24,1,1],['additional_maintenance_musd_per_aircraft_month','Additional maintenance per aircraft-month ($M)',0,10,1,'any'],['additional_carrying_musd_per_aircraft_month','Additional rent / carrying per aircraft-month ($M)',0,10,1,'any'],['productivity_fraction','Retained productivity (% of reference aircraft)',0,100,100,'any'],['extra_fuel_burn_fraction','Extra fuel burn on retained operated capacity (%)',0,100,100,'any']]){const n=field(name,label,document.createElement('input'));n.type='number';n.min=min;n.max=max;n.step=step;if(link)n.value=link[name]*scale}
    for(const [name,label] of [['maintenance_supported','Assume maintenance supports this extension'],['crew_supported','Assume crew supports this extension']]){const n=document.createElement('input');n.type='checkbox';n.checked=link?.[name]===true;field(name,label,n)}
    const remove=document.createElement('button');remove.type='button';remove.className='secondary-btn';remove.textContent='Remove retirement link';remove.addEventListener('click',()=>{row.remove();changedPlan()});row.append(remove);el('retentionRows').append(row);
  }
  function restoreRetentionPlan(plan){el('retentionPlanEnabled').checked=plan!=null;el('retentionPlanFields').hidden=plan==null;el('retentionPlanFields').disabled=plan==null;el('retentionPlanBasis').value=plan?.basis||'';el('retentionCap').value=plan?.max_retained_aircraft??'';el('retentionRows').replaceChildren();if(plan)for(const link of plan.links)addRetention(link);el('retentionPlanStatus').textContent=plan?'Retention assumptions loaded; review each linked exit and eligibility assumption.':'No retention plan enabled.'}
  el('retentionPlanEnabled').addEventListener('change',()=>{el('retentionPlanFields').hidden=!el('retentionPlanEnabled').checked;el('retentionPlanFields').disabled=!el('retentionPlanEnabled').checked;if(el('retentionPlanEnabled').checked&&!el('retentionRows').children.length)addRetention();el('retentionPlanStatus').textContent=el('retentionPlanEnabled').checked?'Enter linked retirement and eligibility assumptions.':'No retention plan enabled.'});
  el('addRetention').addEventListener('click',()=>{addRetention();changedPlan()});
  el('loadRetentionExample').addEventListener('click',async()=>{if(!el('purchasePlanEnabled').checked){el('retentionPlanStatus').textContent='Enable and map replacement purchases first. The example links to the illustrative MAX purchase batches.';return}el('loadRetentionExample').disabled=true;try{const response=await fetch('/api/monthly/retention-example');if(!response.ok)throw Error('Example unavailable');restoreRetentionPlan(await response.json());el('retentionPlanStatus').textContent='Illustrative retention assumptions loaded. Maintenance, crew, costs and replacement links are not verified United values.';changedPlan()}catch(error){el('retentionPlanStatus').textContent=error.message}finally{el('loadRetentionExample').disabled=false}});
  function renderRetention(run){
    const active=run.scenario.retention_plan!=null;el('retention-result').hidden=!active;if(!active)return;
    const m=run.intervention.monthly,retention=run.fleet.intervention.retention,peak=Math.max(...m.retained_aircraft.map(r=>r.p50)),unapplied=retention.decisions.reduce((sum,d)=>sum+d.unapplied_aircraft,0);
    el('retentionResultStatus').textContent=`Saved run ${run.manifest.run_id.slice(0,12)} · peak ${fmt(peak)} retained aircraft · ${unapplied} requested aircraft were not retained · ${retention.pending_retirements.reduce((sum,e)=>sum+e.aircraft,0)} extended exits remain queued. Notice, extension, crew/maintenance support and capacity are analyst assumptions. Fitted retirement policy and full crew/hub feasibility remain pending.`;
    el('retentionResultBasis').textContent='Analyst assumptions: '+run.scenario.retention_plan.basis;
    el('retentionResultRows').replaceChildren();for(let i=0;i<run.months.length;i++){const row=document.createElement('tr');const values=[run.months[i],...['retained_aircraft','retention_maintenance_expense_musd','retention_carrying_expense_musd','retention_extra_fuel_expense_musd','unserved_schedule_asm_million'].map(key=>key.includes('retention_')?Number(m[key][i].p50).toLocaleString('en-US',{maximumFractionDigits:3}):fmt(m[key][i].p50))];for(const value of values){const td=document.createElement('td');td.textContent=value;row.append(td)}el('retentionResultRows').append(row)}
    el('retentionDecisionRows').replaceChildren();for(const d of retention.decisions){const row=document.createElement('tr');const values=[d.retention_id,`${d.aircraft} / ${d.retained_aircraft}`,d.original_exit_month,d.effective_exit_month,d.reasons.join('; ')||'Applied within entered limits; support assumed'];for(const value of values){const td=document.createElement('td');td.textContent=value;row.append(td)}el('retentionDecisionRows').append(row)}
    el('retentionResultDetails').textContent=JSON.stringify({plan:run.scenario.retention_plan,control:run.fleet.control.retention,intervention:retention,monthly:run.fleet.intervention.monthly},null,2);
  }
  function renderCashFunding(run){
    const active=run.scenario.cash_plan!=null||run.scenario.purchase_plan!=null;el('cash-funding').hidden=!active;if(!active)return;
    const initial=run.baseline_inputs.cash_ledger,monthly=run.intervention.monthly;
    el('cashFundingStatus').textContent=`Saved run ${run.manifest.run_id.slice(0,12)} · ${run.scenario.cash_plan?.events.length||0} explicit cash events · ${run.scenario.purchase_plan?.purchases.length||0} purchase batches · opening debt principal $${fmt(initial.state.debt_principal_musd)}M · credit limit $${fmt(initial.state.revolver_limit_musd)}M as of ${initial.effective_on}. Cumulative FCF P10 / P50 / P90: ${['p10','p50','p90'].map(q=>fmt(run.summary.cumulative_fcf_musd[q])).join(' / ')} $M.`;
    el('cashFundingBasis').textContent='Analyst assumptions: '+[run.scenario.cash_plan?.basis,run.scenario.purchase_plan?.basis].filter(Boolean).join(' ');
    el('cashFundingRows').replaceChildren();for(let i=0;i<run.months.length;i++){const row=document.createElement('tr');const values=[run.months[i],...['free_cash_flow_musd','other_investing_cash_flow_musd','financing_cash_flow_musd','restricted_cash_transfer_musd','cash_musd','debt_principal_musd','revolver_available_musd'].map(key=>fmt(monthly[key][i].p50))];for(const value of values){const cell=document.createElement('td');cell.textContent=value;row.append(cell)}el('cashFundingRows').append(row)}
    el('cashFundingDetails').textContent=JSON.stringify({opening:initial,plan:run.scenario.cash_plan,purchase_plan:run.scenario.purchase_plan,monthly},null,2);
  }
  function thesisIsCurrent(){return current&&Object.entries(scenarioSettings()).every(([key,value])=>typeof value==='number'?Math.abs(value-current.scenario[key])<1e-12:JSON.stringify(value)===JSON.stringify(current.scenario[key]??null))}
  function updateThesisState(){el('thesisRun').disabled=thesisBusy||el('monthlyRun').disabled||!thesisIsCurrent();updateConvergenceState()}
  function plot(run) {
    // Pointwise monthly quantiles cannot be summed into cumulative quantiles.
    // The API supplies quantiles computed after each path is accumulated.
    const cumulative = run.cumulative_fcf_impact_musd;
    const frozenCumulative = run.frozen_cumulative_fcf_impact_musd;
    const values = cumulative.flatMap(x => [x.p10,x.p90]).concat(frozenCumulative.map(x=>x.p50),[0]);
    const span = Math.max(10,Math.max(...values)-Math.min(...values));
    const min = Math.min(...values)-span*.12, max = Math.max(...values)+span*.12;
    const left=75,right=795,top=25,bottom=285;
    const x = i => left+(right-left)*i/Math.max(1,cumulative.length-1);
    const y = v => bottom-(v-min)/(max-min)*(bottom-top);
    const line = points => points.map((p,i)=>(i?'L':'M')+p[0].toFixed(2)+','+p[1].toFixed(2)).join(' ');
    let svg='';
    for(let i=0;i<5;i++) {
      const value=min+(max-min)*i/4;
      svg+=`<line x1="${left}" x2="${right}" y1="${y(value)}" y2="${y(value)}" stroke="#2b3745"/><text x="${left-10}" y="${y(value)+4}" text-anchor="end" fill="#8395a7" font-size="12">${fmt(value)}</text>`;
    }
    const upper=cumulative.map((v,i)=>[x(i),y(v.p90)]), lower=cumulative.map((v,i)=>[x(i),y(v.p10)]).reverse();
    svg+=`<path d="${line(upper)} ${line(lower).replace(/^M/,'L')} Z" fill="#817bd7" opacity=".25"/>`;
    svg+=`<path d="${line(cumulative.map((v,i)=>[x(i),y(v.p50)]))}" fill="none" stroke="#e9edfb" stroke-width="3"/>`;
    svg+=`<path d="${line(frozenCumulative.map((v,i)=>[x(i),y(v.p50)]))}" fill="none" stroke="#61d6c9" stroke-width="2"/>`;
    svg+=`<text x="${left}" y="${bottom+25}" fill="#8395a7" font-size="12">${run.months[0]}</text><text x="${right}" y="${bottom+25}" text-anchor="end" fill="#8395a7" font-size="12">${run.months.at(-1)}</text>`;
    el('monthlyChart').innerHTML=svg;
  }
  function renderJudgeSummary(run) {
    const scenario=run.scenario, summary=run.summary;
    const movements=[
      scenario.fuel_shock_pct ? `fuel ${scenario.fuel_shock_pct>0?'+':''}${scenario.fuel_shock_pct}%` : null,
      scenario.demand_shock_pct ? `demand ${scenario.demand_shock_pct>0?'+':''}${scenario.demand_shock_pct}%` : null,
      scenario.delivery_delay_months ? `${scenario.delivery_delay_months}-month fleet entry delay` : null,
      scenario.aircraft_unavailable_pct ? `${scenario.aircraft_unavailable_pct}% aircraft outage` : null
    ].filter(Boolean);
    el('judgeSummaryDescription').textContent=(movements.length?movements.join(' · '):'No external shock selected')+` across ${scenario.geography==='all'?'all four markets':scenario.geography.replace('_',' ')}. ${scenario.horizon_months}-month comparison uses paired no-shock paths and ${scenario.response_mode==='frozen'?'frozen pricing':'an assumed constrained pricing response'}.`;
    el('judgeOperatingIncome').textContent=money(summary.cumulative_operating_income_impact_musd.p50);
    el('judgeFcf').textContent=money(summary.cumulative_fcf_impact_musd.p50);
    el('judgePolicy').textContent=money(summary.response_contribution_to_fcf_musd.p50);
    el('judgeRange').textContent=money(summary.cumulative_fcf_impact_musd.p10)+' to '+money(summary.cumulative_fcf_impact_musd.p90);
    const count=scenario.delivery_aircraft_count;
    el('judgeSummaryScope').textContent=`Inputs: ${fmt(scenario.paths)} paired paths · ${scenario.information_cutoff} information cutoff · ${scenario.response_lag_months}-month pricing lag`+(scenario.delivery_delay_months?` · ${count==null?'all eligible fleet entries':count+' requested fleet entries'}; actual scheduled/queued counts are shown in fleet feasibility`:'')+'. This is a modeled sensitivity, not a forecast from a validated management policy.';
    el('judgeSummary').hidden=false;
  }
  function render(run) {
    current=run; plot(run);renderCashFunding(run);renderPurchaseFunding(run);renderRetention(run);renderJudgeSummary(run);
    invalidateConvergence('Saved run updated · check its sampling stability.');
    el('fcfImpact').textContent=money(run.summary.cumulative_fcf_impact_musd.p50);
    el('policyImpact').textContent=money(run.summary.response_contribution_to_fcf_musd.p50);
    el('endingCash').textContent='$'+fmt(run.summary.ending_cash_musd.p50)+'M';
    el('cashBreach').textContent=fmt(run.summary.cash_floor_breach_path_fraction*100)+'%';
    // Capacity is deterministic under the current fleet assumptions, unlike cash.
    const controlCapacity=run.control.monthly.asm_million.reduce((sum,row)=>sum+row.p50,0);
    const actualCapacity=run.intervention.monthly.asm_million.reduce((sum,row)=>sum+row.p50,0);
    const capacityPct=controlCapacity?100*(actualCapacity/controlCapacity-1):0;
    el('capacityImpact').textContent=(Math.abs(capacityPct)<0.00005?'0':(capacityPct<0?'−':'+')+Math.abs(capacityPct).toLocaleString('en-US',{maximumFractionDigits:3}))+'%';
    const delayed=run.fleet.intervention.effective_queue.filter(event=>event.original_month&&event.kind==='entry').reduce((sum,event)=>sum+event.aircraft,0);
    const retained=run.intervention.monthly.retained_aircraft?.some(row=>row.p50>0);
    el('scenarioExplanation').textContent=delayed
      ? `${delayed} aircraft entries are delayed. ${Math.abs(capacityPct)<0.00005?'Assumed '+(retained?'retention and spare capacity':'spare capacity')+' preserve operated capacity in this case.':'Offered capacity '+(capacityPct<0?'falls':'rises')+' relative to the no-shock case.'} ${run.scenario.purchase_plan?'Delivery payments and borrowing follow the delayed purchases; earlier deposits keep their dates.':'Purchase cash timing remains unchanged without an explicit schedule.'}`
      : 'The selected operating shocks flow through capacity, revenue and costs. Pricing response assumes delayed fare adjustment and its effect on demand; compare it with the frozen-policy line.';
    el('scenarioBasis').textContent=`Cutoff ${run.scenario.information_cutoff} · ${run.scenario.horizon_months} months · ${fmt(run.scenario.nonfuel_fixed_fraction*100)}% fixed nonfuel costs · ${run.scenario.response_mode==='frozen'?'frozen pricing':'assumed constrained pricing'} · ${run.scenario.purchase_plan?'explicit purchase schedule enabled':'historical aggregate capex'} · ${run.scenario.retention_plan?'retention enabled':'no retention plan'}. Financing and retirement support are not verified United terms.`;

    const table=el('quarterlyRows');table.replaceChildren();
    for(const q of run.intervention.quarterly) {
      const row=document.createElement('tr');
      const values=[q.quarter+(q.complete_quarter?'':' · partial'),q.revenue_musd.p50,q.operating_income_musd.p50,q.free_cash_flow_musd.p50,q.ending_cash_musd.p50];
      values.forEach((v,i)=>{const cell=document.createElement('td');cell.textContent=i?fmt(v):v;row.append(cell)});table.append(row);
    }
    const fleetTable=el('fleetRows');fleetTable.replaceChildren();
    run.fleet.intervention.monthly.forEach((r,i)=>{
      const planned=run.fleet.control.monthly[i].opening_inventory;
      const row=document.createElement('tr');
      const values=[r.month,...['narrowbody','widebody','regional'].map(g=>`${r.available[g]} / ${planned[g]}`),fmt(run.intervention.monthly.unserved_schedule_asm_million[i].p50),fmt(run.intervention.monthly.regional_contract_expense_musd[i].p50)];
      values.forEach(v=>{const cell=document.createElement('td');cell.textContent=v;row.append(cell)});fleetTable.append(row);
    });
    const pending=run.fleet.intervention.remaining_queue.filter(e=>e.kind==='entry').reduce((sum,e)=>sum+e.aircraft,0);
    el('fleetQueueStatus').textContent=`${pending} expected fleet entries remain queued beyond this horizon. Opening inventory dated ${run.fleet.plan.effective_on}; future inventory follows disclosed guidance assumptions.`;
    const assumptions=el('monthlyAssumptions');assumptions.replaceChildren();
    for(const text of run.assumptions){const item=document.createElement('li');item.textContent=text==='No crew or hub constraint, rival feedback, maturity schedule, new financing or valuation is currently simulated.'?'Monthly dynamics do not simulate crew or hub constraints, rival feedback, maturity schedules or new financing. Optional valuation uses a separate disclosed FCFF bridge in What must be true?.':text;assumptions.append(item)}
    el('snapshotStatus').textContent=`${run.evidence.record_count} reviewed records · ${run.evidence.vintage_status} · opening cash dated ${run.baseline_inputs.initial_state_effective_on}`;
    el('monthlyManifest').textContent=JSON.stringify({manifest:run.manifest,fleet:run.fleet,evidence:run.evidence},null,2);
    el('monthlyStatus').textContent=`${fmt(run.scenario.paths)} paths · ${run.summary.runtime_seconds}s engine time · ${run.readiness}`;
    el('downloadRun').disabled=false;
    el('thesisDownloads').hidden=true;
    el('thesisStatus').textContent=`Ready to export saved run ${run.manifest.run_id.slice(0,12)}. Complete the analyst notes below.`;
    const firstCheckpoint=new Date(run.scenario.information_cutoff+'T00:00:00Z');firstCheckpoint.setUTCDate(firstCheckpoint.getUTCDate()+1);el('thesisCheckpointOn').min=firstCheckpoint.toISOString().slice(0,10);
  }
  function scenarioSettings() {return {information_cutoff:el('cutoff').value,fuel_shock_pct:Number(el('fuelShock').value),demand_shock_pct:Number(el('demandShock').value),capacity_shock_pct:Number(el('capacityShock').value),geography:el('market').value,horizon_months:Number(el('horizonMonths').value),onset_month:Number(el('onsetMonths').value),duration_months:Number(el('durationMonths').value),delivery_delay_months:Number(el('entryDelay').value),delivery_aircraft_count:numericField(el('deliveryCount')),delivery_group:el('deliveryGroup').value,aircraft_unavailable_pct:Number(el('unavailable').value),availability_group:el('availabilityGroup').value,regional_contract_shock_pct:Number(el('contractShock').value),fleet_spare_fraction:Number(el('sparePct').value)/100,schedule_growth_fraction:Number(el('scheduleGrowthPct').value)/100,regional_contract_fixed_fraction:Number(el('contractFixedPct').value)/100,nonfuel_fixed_fraction:Number(el('nonfuelFixedPct').value)/100,response_lag_months:Number(el('lagMonths').value),response_mode:el('mode').value,cash_plan:cashPlanSettings(),purchase_plan:purchasePlanSettings(),retention_plan:retentionPlanSettings()}}
  async function run(event) {
    if(event)event.preventDefault();
    if(presetBusy||el('monthlyRun').disabled)return null;
    if(!el('monthlyForm').reportValidity())return null;
    el('failureWitness').hidden=true;
    el('monthlyRun').disabled=true;for(const button of document.querySelectorAll('[data-preset]'))button.disabled=true;el('monthlyStatus').textContent='Running paired monthly paths…';
    updateThesisState();el('thesisDownloads').hidden=true;
    const settings=scenarioSettings();
    try {
      const response=await fetch('/api/monthly/simulate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(settings)});
      const body=await response.json();
      if(!response.ok)throw Error(typeof body.detail==='string'?body.detail:'Check the scenario inputs');
      render(body);return body;
    } catch(error) {el('monthlyStatus').textContent='Scenario failed: '+error.message;return null} finally {el('monthlyRun').disabled=false;for(const button of document.querySelectorAll('[data-preset]'))button.disabled=false;updateThesisState();if(current&&!thesisIsCurrent())el('monthlyStatus').textContent='Saved scenario displayed · inputs changed during the run. Run again to update results.'}
  }
  el('judgeDemo').addEventListener('click', async () => {
    if(el('monthlyRun').disabled){el('judgeDemoStatus').textContent='A scenario is still running. Please try again when it completes.';return}
    const settings={cutoff:'2026-07-16',fuelShock:'20',demandShock:'0',capacityShock:'0',market:'all',
      horizonMonths:'12',onsetMonths:'1',durationMonths:'9',entryDelay:'9',
      deliveryGroup:'all',deliveryCount:'30',unavailable:'0',availabilityGroup:'all',
      contractShock:'0',lagMonths:'2',mode:'assumed_constrained',
      sparePct:'2',scheduleGrowthPct:'100',contractFixedPct:'70',nonfuelFixedPct:'70'};
    for(const [id,value] of Object.entries(settings))el(id).value=value;
    restoreCashPlan(null);restorePurchasePlan(null);restoreRetentionPlan(null);
    for(const button of document.querySelectorAll('[data-preset]'))button.setAttribute('aria-pressed','false');
    el('presetStatus').textContent='Earlier judge stress case · 20% fuel · aggregate capex · no explicit purchase or retention schedule.';
    const fleetDetails=el('entryDelay').closest('details');if(fleetDetails)fleetDetails.open=true;
    el('monthlyForm').dispatchEvent(new Event('input',{bubbles:true}));
    el('judgeDemo').disabled=true;
    el('judgeDemoStatus').textContent='Running illustrative judge scenario against the current monthly engine…';
    try {
      const result=await run();
      if(result){el('judgeDemoStatus').textContent='Scenario complete. Review modeled operating income, FCF and uncertainty below.';el('judgeSummary').scrollIntoView({behavior:'smooth',block:'start'})}
      else el('judgeDemoStatus').textContent='Scenario did not complete. Inspect the visible run status and inputs.';
    }finally{el('judgeDemo').disabled=false}
  });
  el('monthlyForm').addEventListener('submit',run);
  el('monthlyForm').addEventListener('input',()=>{if(current)el('monthlyStatus').textContent='Inputs changed · run to update results'});
  el('monthlyForm').addEventListener('input',()=>{updateThesisState();el('thesisDownloads').hidden=true;el('thesisStatus').textContent=thesisIsCurrent()?'Scenario matches the saved run · export to save the current notes.':'Scenario inputs changed · run again before exporting.'});
  function updateConvergenceState(){el('convergenceRun').disabled=hostedDemo||convergenceBusy||el('monthlyRun').disabled||!thesisIsCurrent()}
  function invalidateConvergence(message){currentConvergence=null;el('convergenceResults').hidden=true;el('downloadConvergence').disabled=true;el('convergenceStatus').textContent=message;updateConvergenceState()}
  el('monthlyForm').addEventListener('input',()=>invalidateConvergence(thesisIsCurrent()?'Scenario matches saved run · check sampling stability.':'Scenario changed · run again before checking sampling stability.'));
  el('convergenceForm').addEventListener('input',()=>{convergenceControlsVersion++;invalidateConvergence('Sampling controls changed · check again to use these tolerances.')});
  function renderConvergence(study,cached=false){
    currentConvergence=study;const summary=study.summary,base=study.manifest.base_scenario;
    const displayed=study.levels.find(level=>level.paths===base.paths);
    el('convergenceStatus').textContent=`Saved run ${study.manifest.reference_run_id.slice(0,12)} · ${study.seeds.length} seed repeats · ${summary.status==='tolerance_met'?'Stopping tolerance met':'Path limit reached; tolerance not met'} at ${fmt(summary.stopping_paths)} paths per repeat. Saved ${fmt(base.paths)}-path run: ${displayed.failed_checks} of ${displayed.diagnostics.length} checks outside tolerance. ${fmt(summary.evaluated_path_draws)} generated paths; nested prefixes rescored · ${summary.runtime_seconds}s study time.`;
    if(cached)el('convergenceStatus').textContent+=' Reused the unchanged study after its complete archived replay.';
    el('convergenceLevels').replaceChildren();for(const level of study.levels){const row=document.createElement('tr');for(const value of [fmt(level.paths),`${level.failed_checks} / ${level.diagnostics.length}`,String(level.consecutive_passing_levels),level.passes?'Within tolerance':level.diagnostics[0].paired_rms_change===null?'Initial comparison':'Outside tolerance']){const cell=document.createElement('td');cell.textContent=value;row.append(cell)}el('convergenceLevels').append(row)}
    el('convergenceViewPolicy').value=base.response_mode;el('convergenceViewHorizon').replaceChildren();for(const horizon of [...new Set(displayed.diagnostics.map(row=>row.horizon_months))].sort((a,b)=>a-b)){const option=document.createElement('option');option.value=horizon;option.textContent=`${horizon} months`;el('convergenceViewHorizon').append(option)}el('convergenceViewHorizon').value=String(base.horizon_months);el('convergenceViewQuantile').value='p50';
    el('convergenceViewPaths').replaceChildren();for(const level of study.levels){const option=document.createElement('option');option.value=level.paths;option.textContent=`${fmt(level.paths)} paths${level.paths===base.paths?' · original run':''}`;el('convergenceViewPaths').append(option)}el('convergenceViewPaths').value=String(summary.status==='tolerance_met'?summary.stopping_paths:base.paths);
    renderConvergenceMetrics(study);
    el('convergenceManifest').textContent=JSON.stringify(study,null,2);el('convergenceResults').hidden=false;el('downloadConvergence').disabled=false;
  }
  function renderConvergenceMetrics(study){
    const displayed=study.levels.find(level=>level.paths===Number(el('convergenceViewPaths').value)),mode=el('convergenceViewPolicy').value,horizon=Number(el('convergenceViewHorizon').value),quantile=el('convergenceViewQuantile').value;
    const names={cumulative_fcf_musd:'Cumulative FCF',cumulative_fcf_impact_musd:'Paired FCF impact',cumulative_operating_income_impact_musd:'Paired operating profit impact',ending_cash_musd:'Ending cash',minimum_cash_musd:'Path minimum cash',cumulative_revenue_musd:'Cumulative revenue',response_contribution_to_fcf_musd:'Response contribution',cumulative_operating_margin_pct:'Horizon operating margin',cash_floor_breach:'Cash-floor fraction'};
    const precision=n=>Number(n).toLocaleString('en-US',{maximumFractionDigits:3});
    el('convergenceMetrics').replaceChildren();for(const check of displayed.diagnostics.filter(row=>row.policy===mode&&row.horizon_months===horizon&&(row.statistic===quantile||row.statistic==='fraction'))){
      const fraction=check.statistic==='fraction',pct=check.metric.endsWith('_pct'),scale=fraction?100:1,unit=fraction||pct?'percentage points':'$M';const row=document.createElement('tr');
      const values=[`${names[check.metric]} · ${fraction?'fraction':quantile.toUpperCase()} · ${fraction||pct?'%':'$M'}`,precision(check.selected_seed_estimate*scale),`±${precision(check.approximate_mc_half_width95*scale)} ${unit}`,`${precision(check.tolerance*scale)} ${unit}`,check.passes?'Within tolerance':check.paired_rms_change===null?'Needs next count':'Outside tolerance'];
      for(const value of values){const cell=document.createElement('td');cell.textContent=value;row.append(cell)}el('convergenceMetrics').append(row);
    }
  }
  for(const id of ['convergenceViewPaths','convergenceViewPolicy','convergenceViewHorizon','convergenceViewQuantile'])el(id).addEventListener('change',()=>{if(currentConvergence)renderConvergenceMetrics(currentConvergence)});
  el('convergenceExtended').addEventListener('click',()=>{el('convergenceCounts').value=[...new Set([1000,5000,10000,50000,100000,200000,500000,750000,1000000,current?.scenario.paths||10000])].sort((a,b)=>a-b).join(', ');el('convergenceBatch').value='10000';el('convergenceForm').dispatchEvent(new Event('input',{bubbles:true}))});
  el('convergenceForm').addEventListener('submit',async event=>{
    event.preventDefault();if(!thesisIsCurrent()||convergenceBusy||el('monthlyRun').disabled)return;
    const saved=current,controlsVersion=convergenceControlsVersion;
    const counts=el('convergenceCounts').value.split(',').map(value=>value.trim());if(counts.some(value=>!/^\d+$/.test(value))){el('convergenceStatus').textContent='Enter increasing whole-number path counts separated by commas.';return}
    const controls={path_counts:counts.map(Number),replicates:Number(el('convergenceReplicates').value),absolute_tolerance_musd:Number(el('convergenceMoney').value),relative_tolerance:Number(el('convergenceRelative').value)/100,margin_tolerance_pp:Number(el('convergenceMargin').value),breach_tolerance_fraction:Number(el('convergenceBreach').value)/100,consecutive_levels:Number(el('convergenceConsecutive').value),batch_paths:Number(el('convergenceBatch').value)};
    convergenceBusy=true;invalidateConvergence('Replaying the saved run, then measuring independent-seed sampling variation…');
    try{const response=await fetch('/api/monthly/convergence',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run_record:saved,convergence:controls})});const study=await response.json();if(!response.ok)throw Error(typeof study.detail==='string'?study.detail:'Check the sampling controls');
      if(current!==saved||!thesisIsCurrent()||controlsVersion!==convergenceControlsVersion){invalidateConvergence('Inputs changed during the study · check again for the current saved run and tolerances.');return}renderConvergence(study,response.headers.get('X-UAL-Study-Cache')==='verified');
    }catch(error){el('convergenceStatus').textContent='Sampling check failed: '+error.message}finally{convergenceBusy=false;updateConvergenceState()}
  });
  el('downloadConvergence').addEventListener('click',()=>{if(!currentConvergence)return;const url=URL.createObjectURL(new Blob([JSON.stringify(currentConvergence,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='ual-convergence-'+currentConvergence.manifest.run_id.slice(0,12)+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)});
  el('thesisForm').addEventListener('input',()=>{thesisNotesVersion++;el('thesisDownloads').hidden=true;el('thesisStatus').textContent='Analyst notes changed · export to save the updated record.'});
  el('thesisForm').addEventListener('submit',async event=>{
    event.preventDefault();if(!thesisIsCurrent()||thesisBusy||el('monthlyRun').disabled)return;
    const saved=current,notesVersion=thesisNotesVersion;
    const notes={title:el('thesisTitle').value,hypothesis:el('thesisHypothesis').value,reference:el('thesisReference').value,checkpoint_on:el('thesisCheckpointOn').value,checkpoint:el('thesisCheckpoint').value,checkpoint_source:el('thesisCheckpointSource').value,falsification:el('thesisFalsification').value};
    thesisBusy=true;updateThesisState();el('thesisDownloads').hidden=true;el('thesisStatus').textContent='Replaying the saved run and preparing the one-page record…';
    try{const response=await fetch('/api/monthly/thesis',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run_record:saved,notes})});const result=await response.json();if(!response.ok)throw Error(typeof result.detail==='string'?result.detail:'Check the thesis notes');
      if(current!==saved||!thesisIsCurrent()||notesVersion!==thesisNotesVersion){el('thesisStatus').textContent='Inputs changed during export · export again to use the current scenario and notes.';return}
      el('thesisPdf').href=result.pdf_url;el('thesisJson').href=result.json_url;el('thesisPdf').download='ual-thesis-'+result.artifact_id.slice(0,12)+'.pdf';el('thesisJson').download='ual-thesis-'+result.artifact_id.slice(0,12)+'.json';el('thesisDownloads').hidden=false;el('thesisStatus').textContent=`One page saved · run ${result.run_id.slice(0,12)} · ${saved.scenario.paths.toLocaleString('en-US')} paths independently replayed · PDF and companion JSON ready.`;
    }catch(error){el('thesisStatus').textContent='Thesis export failed: '+error.message}finally{thesisBusy=false;updateThesisState()}
  });
  el('downloadRun').addEventListener('click',()=>{
    if(!current)return;
    const url=URL.createObjectURL(new Blob([JSON.stringify(current,null,2)],{type:'application/json'}));
    const a=document.createElement('a');a.href=url;a.download='ual-monthly-'+current.manifest.run_id.slice(0,12)+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  let currentSearch=null;
  const fieldMap={information_cutoff:'cutoff',fuel_shock_pct:'fuelShock',demand_shock_pct:'demandShock',capacity_shock_pct:'capacityShock',geography:'market',horizon_months:'horizonMonths',onset_month:'onsetMonths',duration_months:'durationMonths',delivery_delay_months:'entryDelay',delivery_aircraft_count:'deliveryCount',delivery_group:'deliveryGroup',aircraft_unavailable_pct:'unavailable',availability_group:'availabilityGroup',regional_contract_shock_pct:'contractShock',response_lag_months:'lagMonths',response_mode:'mode'};
  const percentMap={fleet_spare_fraction:'sparePct',schedule_growth_fraction:'scheduleGrowthPct',regional_contract_fixed_fraction:'contractFixedPct',nonfuel_fixed_fraction:'nonfuelFixedPct'};
  const driverNames={fuel_shock_pct:'Fuel shock (%)',demand_shock_pct:'Demand shock (%)',capacity_shock_pct:'Capacity reduction (%)',aircraft_unavailable_pct:'Aircraft unavailable (%)',regional_contract_shock_pct:'Regional contract cost (%)',delivery_delay_months:'Fleet entry delay (months)'};
  async function inspectFailure(result,cell,mode) {
    if(el('monthlyRun').disabled)return;
    const settings={...result.manifest.base_scenario,...cell.overrides,response_mode:mode};
    for(const [key,id] of Object.entries(fieldMap))el(id).value=settings[key]??'';
    for(const [key,id] of Object.entries(percentMap))el(id).value=settings[key]*100;
    restoreCashPlan(settings.cash_plan);restorePurchasePlan(settings.purchase_plan);restoreRetentionPlan(settings.retention_plan);
    showView('scenario');el('monthlyStatus').scrollIntoView({behavior:'smooth',block:'center'});
    const witness=await run();
    if(witness){const spec=result.manifest.search,outcome=cell.policies[mode];el('failureWitness').textContent=`Search point: ${result.manifest.base_scenario.horizon_months}-month ${spec.statistic.toUpperCase()} ${spec.criterion==='cumulative_fcf'?'absolute cumulative FCF':'path minimum cash'} ${fmt(outcome.criterion_value_musd)} $M, ${outcome.fails?'below':'meeting'} the ${fmt(spec.threshold_musd)} $M threshold. ${mode==='frozen'?'Frozen':'Assumed constrained'} pricing. The chart shows paired FCF impact relative to control.`;el('failureWitness').hidden=false}
  }
  function renderFailure(result) {
    currentSearch=result;
    const spec=result.manifest.search,base=result.manifest.base_scenario;
    const matrices=el('failureMatrices');matrices.replaceChildren();
    for(const mode of ['frozen','assumed_constrained']) {
      const wrap=document.createElement('article'),title=document.createElement('h3');
      title.textContent=mode==='frozen'?'Frozen pricing':'Assumed constrained pricing';wrap.append(title);
      const description=document.createElement('p');description.className='micro';
      const counts=result.counts[mode];description.textContent=`${counts.failed_tested_points} of ${counts.tested_points} tested combinations below ${fmt(spec.threshold_musd)} $M · ${base.horizon_months}-month ${spec.criterion==='cumulative_fcf'?'cumulative FCF':'path minimum cash'} · ${spec.statistic.toUpperCase()}`;wrap.append(description);
      const scroll=document.createElement('div');scroll.className='table-scroll';const table=document.createElement('table');
      const caption=document.createElement('caption');caption.textContent=`Rows: ${driverNames[spec.y.driver]} · Columns: ${driverNames[spec.x.driver]}`;table.append(caption);
      const head=document.createElement('thead'),header=document.createElement('tr');const corner=document.createElement('th');corner.textContent='↓ / →';header.append(corner);
      result.x_values.forEach(x=>{const th=document.createElement('th');th.textContent=fmt(x);header.append(th)});head.append(header);table.append(head);
      const body=document.createElement('tbody');
      result.y_values.forEach((y,j)=>{const row=document.createElement('tr'),label=document.createElement('th');label.scope='row';label.textContent=fmt(y);row.append(label);
        result.x_values.forEach((x,i)=>{const cell=result.cells.find(c=>c.x_index===i&&c.y_index===j),outcome=cell.policies[mode],td=document.createElement('td'),button=document.createElement('button');
          button.type='button';button.className='failure-point '+(outcome.fails?'fails':'meets');button.textContent=fmt(outcome.criterion_value_musd);
          button.setAttribute('aria-label',`${mode}: ${driverNames[spec.x.driver]} ${x}, ${driverNames[spec.y.driver]} ${y}; ${outcome.fails?'below':'meets'} threshold; ${fmt(outcome.criterion_value_musd)} million dollars. Inspect scenario`);
          button.addEventListener('click',()=>inspectFailure(result,cell,mode));td.append(button);row.append(td)});body.append(row)});
      table.append(body);scroll.append(table);wrap.append(scroll);matrices.append(wrap);
    }
    const regions=el('failureRegions');regions.replaceChildren();
    for(const mode of ['frozen','assumed_constrained']) {
      const label=document.createElement('p');label.textContent=(mode==='frozen'?'Frozen':'Assumed constrained')+' — failing tested values:';regions.append(label);
      const list=document.createElement('ul');
      for(const region of result.failure_regions[mode]){const li=document.createElement('li');li.textContent=`${driverNames[spec.y.driver]} = ${fmt(region.y)}: ${driverNames[spec.x.driver]} ∈ {${region.x_values.map(fmt).join(', ')}}`;list.append(li)}
      if(!list.children.length){const li=document.createElement('li');li.textContent='No tested points below the selected threshold.';list.append(li)}regions.append(list);
    }
    el('failureStatus').textContent=`${result.cells.length} tested combinations · ${fmt(base.paths)} common paths each · ${result.policy_disagreement_points} combinations change failure status between pricing modes · ${result.runtime_seconds}s engine time${result.exploratory?' · months 13–24 exploratory':''}. Saved cutoff ${base.information_cutoff}.`;
    el('failureManifest').textContent=JSON.stringify(result,null,2);el('downloadFailure').disabled=false;
  }
  el('failureForm').addEventListener('submit',async event=>{
    event.preventDefault();if(!el('monthlyForm').reportValidity())return;
    el('failureRun').disabled=true;el('failureStatus').textContent='Searching common world paths…';currentSearch=null;el('downloadFailure').disabled=true;
    el('failureMatrices').replaceChildren();el('failureRegions').replaceChildren();el('failureManifest').textContent='';
    const points=Number(el('gridPoints').value),payload={base_scenario:scenarioSettings(),x:{driver:el('searchX').value,low:Number(el('xLow').value),high:Number(el('xHigh').value),points},y:{driver:el('searchY').value,low:Number(el('yLow').value),high:Number(el('yHigh').value),points},criterion:el('failureCriterion').value,threshold_musd:Number(el('failureThreshold').value),statistic:el('failureStatistic').value};
    try {const response=await fetch('/api/monthly/failure-search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const body=await response.json();if(!response.ok)throw Error(typeof body.detail==='string'?body.detail:'Check the search inputs');renderFailure(body)}
    catch(error){el('failureStatus').textContent='Search failed: '+error.message}finally{el('failureRun').disabled=false}
  });
  for(const id of ['failureForm','monthlyForm'])el(id).addEventListener('input',()=>{if(currentSearch)el('failureStatus').textContent='Inputs changed · displayed search uses its saved inputs. Search again to update.'});
  el('failureCriterion').addEventListener('change',()=>{el('failureThreshold').value=el('failureCriterion').value==='minimum_cash'?5000:0});
  el('downloadFailure').addEventListener('click',()=>{if(!currentSearch)return;const url=URL.createObjectURL(new Blob([JSON.stringify(currentSearch,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='ual-failure-search-'+currentSearch.manifest.run_id.slice(0,12)+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)});
  let currentTarget=null;
  const targetNames={cumulative_fcf:'cumulative FCF',cumulative_revenue:'cumulative revenue',operating_margin_pct:'operating margin'};
  async function inspectTarget(result,cell,mode) {
    if(el('monthlyRun').disabled)return;
    const spec=result.manifest.inversion,target=spec.target,outcome=cell.policies[mode];
    const settings={...result.manifest.base_scenario,...cell.overrides,response_mode:mode};
    for(const [key,id] of Object.entries(fieldMap))el(id).value=settings[key]??'';
    for(const [key,id] of Object.entries(percentMap))el(id).value=settings[key]*100;
    restoreCashPlan(settings.cash_plan);restorePurchasePlan(settings.purchase_plan);restoreRetentionPlan(settings.retention_plan);
    const rows=el('targetPathRows');rows.replaceChildren();
    for(const w of outcome.witness_paths){const row=document.createElement('tr');const values=[w.path_index,fmt(w.cumulative_revenue_musd),fmt(w.operating_margin_pct),fmt(w.cumulative_fcf_musd),fmt(w.minimum_cash_musd),w.enterprise_value_musd===undefined?'Not requested':fmt(w.enterprise_value_musd)];for(const value of values){const td=document.createElement('td');td.textContent=value;row.append(td)}rows.append(row)}
    el('targetWitness').hidden=false;
    el('targetWitnessLabel').textContent=`${mode==='frozen'?'Frozen':'Assumed constrained'} pricing · ${driverNames[spec.x.driver]} ${fmt(cell.x)} · ${driverNames[spec.y.driver]} ${fmt(cell.y)}. ${outcome.compatible_path_count} of ${settings.paths} assumed paths jointly meet the path-level conditions. First ${outcome.witness_paths.length} matching indexes shown; these examples are not ranked by likelihood. The selected quantile ${outcome.compatible?'also meets':'does not meet'} the grid-point tests.`;
    if(spec.valuation){const v=outcome.valuation;el('targetWitnessLabel').textContent+=` ${target.statistic.toUpperCase()} operating-asset value ${fmt(v.enterprise_value_musd[target.statistic])} $M = explicit operating FCFF present value ${fmt(v.explicit_fcff_pv_musd[target.statistic])} $M + discounted terminal contribution ${fmt(v.terminal_pv_musd)} $M.`}
    showView('scenario');el('monthlyStatus').scrollIntoView({behavior:'smooth',block:'center'});
    const witness=await run();
    if(witness){el('failureWitness').textContent=`Target point: ${settings.horizon_months}-month ${target.statistic.toUpperCase()} absolute ${targetNames[target.metric]} ${fmt(outcome.selected_target_value)} ${target.metric==='operating_margin_pct'?'%':'$M'}. ${outcome.compatible?'Meets':'Does not meet'} the requested joint tests. ${spec.valuation?'The separate FCFF valuation assumptions are recorded in the target search. ':''}The chart shows paired FCF impact relative to control.`;el('failureWitness').hidden=false}
  }
  function renderTarget(result) {
    currentTarget=result;
    const spec=result.manifest.inversion,target=spec.target,base=result.manifest.base_scenario;
    const matrices=el('targetMatrices');matrices.replaceChildren();el('targetWitness').hidden=true;
    for(const mode of ['frozen','assumed_constrained']) {
      const wrap=document.createElement('article'),h=document.createElement('h3');h.textContent=mode==='frozen'?'Frozen pricing':'Assumed constrained pricing';wrap.append(h);
      const counts=result.counts[mode],p=document.createElement('p');p.className='micro';
      p.textContent=`${counts.operating_target_points} of ${counts.tested_points} tested points meet the operating target · ${counts.compatible_points} meet all requested tests with joint path witnesses.`;wrap.append(p);
      if(spec.valuation){const v=spec.valuation,detail=document.createElement('p');detail.className='micro';const terminal=result.cells[0].policies[mode].valuation;detail.textContent=`Operating-asset value target ${fmt(v.enterprise_value_target_musd)} ± ${fmt(v.enterprise_value_tolerance_musd)} $M. Assumed cost of capital ${fmt(v.annual_discount_rate*100)}%, growth ${fmt(v.terminal_growth_rate*100)}%, terminal-year FCFF ${fmt(v.terminal_annual_fcff_musd)} $M. Terminal value ${fmt(terminal.terminal_value_musd)} $M; discounted terminal contribution ${fmt(terminal.terminal_pv_musd)} $M, shared by every point. Inspect a point for its operating FCFF contribution.`;wrap.append(detail)}
      const scroll=document.createElement('div');scroll.className='table-scroll';const table=document.createElement('table'),caption=document.createElement('caption');caption.textContent=`${base.horizon_months}-month ${target.statistic.toUpperCase()} ${targetNames[target.metric]} (${target.metric==='operating_margin_pct'?'%':'$M'}) · Rows: ${driverNames[spec.y.driver]} · Columns: ${driverNames[spec.x.driver]}`;table.append(caption);
      const head=document.createElement('thead'),header=document.createElement('tr'),corner=document.createElement('th');corner.textContent='↓ / →';header.append(corner);result.x_values.forEach(x=>{const th=document.createElement('th');th.textContent=fmt(x);header.append(th)});head.append(header);table.append(head);
      const body=document.createElement('tbody');result.y_values.forEach((y,j)=>{const row=document.createElement('tr'),label=document.createElement('th');label.scope='row';label.textContent=fmt(y);row.append(label);
        result.x_values.forEach((x,i)=>{const cell=result.cells.find(c=>c.x_index===i&&c.y_index===j),out=cell.policies[mode],td=document.createElement('td'),button=document.createElement('button');button.type='button';button.className='target-point '+(out.compatible?'compatible':'incompatible');button.textContent=fmt(out.selected_target_value);button.setAttribute('aria-label',`${mode}: target ${driverNames[spec.x.driver]} ${x}, ${driverNames[spec.y.driver]} ${y}; ${out.compatible?'compatible':'does not meet joint tests'}; ${fmt(out.selected_target_value)} ${target.metric==='operating_margin_pct'?'percent':'million dollars'}. Inspect target scenario`);button.addEventListener('click',()=>inspectTarget(result,cell,mode));td.append(button);row.append(td)});body.append(row)});table.append(body);scroll.append(table);wrap.append(scroll);matrices.append(wrap);
    }
    const relation=target.relation==='within_band'?`within ${fmt(target.value)} ± ${fmt(target.tolerance)}`:(target.relation==='at_least'?'at least ':'at most ')+fmt(target.value);
    const unit=target.metric==='operating_margin_pct'?'%':'$M';
    el('targetStatus').textContent=`Target: ${targetNames[target.metric]} ${relation} ${unit} · ${target.statistic.toUpperCase()} · ${result.cells.length} tested combinations · ${fmt(base.paths)} common paths per point · ${result.runtime_seconds}s engine time. ${spec.valuation?'Enterprise-value scenario enabled. ':'Operating target only. '}${target.minimum_cash_floor_musd===null?'No additional cash-floor filter. ':'Cash floor '+fmt(target.minimum_cash_floor_musd)+' $M. '}Saved cutoff ${base.information_cutoff}${result.exploratory?' · months 13–24 exploratory':''}.`;
    el('targetManifest').textContent=JSON.stringify(result,null,2);el('downloadTarget').disabled=false;
  }
  el('targetRelation').addEventListener('change',()=>{el('targetTolerance').disabled=el('targetRelation').value!=='within_band'});
  el('targetRequireCash').addEventListener('change',()=>{el('targetCashFloor').disabled=!el('targetRequireCash').checked});
  el('includeValuation').addEventListener('change',()=>{el('valuationFields').disabled=!el('includeValuation').checked;el('valuationFields').hidden=!el('includeValuation').checked});
  el('targetForm').addEventListener('submit',async event=>{
    event.preventDefault();if(!el('monthlyForm').reportValidity())return;
    currentTarget=null;el('downloadTarget').disabled=true;el('targetRun').disabled=true;el('targetStatus').textContent='Finding compatible assumptions on common world paths…';el('targetMatrices').replaceChildren();el('targetWitness').hidden=true;el('targetManifest').textContent='';
    const points=Number(el('targetPoints').value),payload={base_scenario:scenarioSettings(),x:{driver:el('targetX').value,low:Number(el('targetXLow').value),high:Number(el('targetXHigh').value),points},y:{driver:el('targetY').value,low:Number(el('targetYLow').value),high:Number(el('targetYHigh').value),points},target:{metric:el('targetMetric').value,value:Number(el('targetValue').value),relation:el('targetRelation').value,tolerance:el('targetRelation').value==='within_band'?Number(el('targetTolerance').value):0,statistic:el('targetStatistic').value,minimum_cash_floor_musd:el('targetRequireCash').checked?Number(el('targetCashFloor').value):null},valuation:el('includeValuation').checked?{annual_discount_rate:Number(el('valuationDiscount').value)/100,terminal_growth_rate:Number(el('valuationGrowth').value)/100,terminal_annual_fcff_musd:Number(el('valuationTerminal').value),annual_non_cash_charges_musd:Number(el('valuationNoncash').value),annual_working_capital_investment_musd:Number(el('valuationWorking').value),cash_operating_tax_rate:Number(el('valuationTax').value)/100,enterprise_value_target_musd:Number(el('valuationTarget').value),enterprise_value_tolerance_musd:Number(el('valuationTolerance').value)}:null};
    try{const response=await fetch('/api/monthly/target-inversion',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const body=await response.json();if(!response.ok)throw Error(typeof body.detail==='string'?body.detail:'Check the target and valuation inputs');renderTarget(body)}catch(error){el('targetStatus').textContent='Target search failed: '+error.message}finally{el('targetRun').disabled=false}
  });
  for(const id of ['targetForm','monthlyForm'])el(id).addEventListener('input',()=>{if(currentTarget)el('targetStatus').textContent='Inputs changed · displayed target search uses its saved assumptions. Run again to update.'});
  el('downloadTarget').addEventListener('click',()=>{if(!currentTarget)return;const url=URL.createObjectURL(new Blob([JSON.stringify(currentTarget,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='ual-target-inversion-'+currentTarget.manifest.run_id.slice(0,12)+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)});
  fetch('/api/traffic-vintages/status').then(async response=>{
    const data=await response.json();if(!response.ok)throw Error(data.detail||'Source comparison unavailable');
    el('trafficVintageCoverage').textContent=JSON.stringify(data,null,2);
    if(data.status!=='available'){el('trafficVintageStatus').textContent=data.message;return}
    el('trafficVintageStatus').textContent=`${data.label} ${fmt(data.checks_passed)} independent numerical/input checks. ${fmt(data.common_predictions_per_model_per_case)} of ${fmt(data.requested_predictions_per_model_per_case)} requested forecasts per model/case scored on common supported keys.`;
    const labels={'rolling_native_history_native_targets':'Rolling native → native targets','rolling_native_history_reviewed_targets':'Rolling native → reviewed targets','rolling_reviewed_history_reviewed_targets':'Rolling reviewed → reviewed targets','annual_native_history_reviewed_targets':'Annual native → reviewed targets'};
    const rows=data.cases.map(c=>{const state=c.scorecard.find(s=>s.model===c.selected_name),best=c.scorecard.filter(s=>!s.model.startsWith('state_')).reduce((a,b)=>a.equal_cell_MASE<=b.equal_cell_MASE?a:b);return [labels[c.name],state.equal_cell_MASE.toFixed(3),best.equal_cell_MASE.toFixed(3),fmt(c.failures),(100*c.selected_interval_coverage_80).toFixed(1)+'%']});
    el('trafficVintageScores').replaceChildren(comparisonTable(['History → evaluation','Best state MASE','Best baseline MASE','AR failures','Native 80% coverage'],rows,'All four cases use the same 1,763 supported comparisons. Lower MASE is better. Regularized AR is strongest in rolling cases; the fixed initial seasonal mean is strongest for annual history. Coverage is conditional and uncalibrated.'));el('trafficVintageScores').hidden=false;
    el('trafficVintageFinding').textContent='The best state model loses to the strongest declared baseline in every source case. Each rolling case retains 37 unsupported AR forecasts; those keys are excluded from every scorecard. The shared subset does not establish whole-domain robustness. The financial simulator still uses its disclosed behavioral assumptions.';el('trafficVintageFinding').hidden=false;
    for(const id of ['trafficVintageDownload','trafficVintageAudit'])el(id).hidden=false;
  }).catch(error=>{el('trafficVintageStatus').textContent='Source comparison: '+error.message;for(const id of ['trafficVintageFinding','trafficVintageScores','trafficVintageDownload','trafficVintageAudit'])el(id).hidden=true});
  fetch('/api/traffic/status').then(async response=>{
    const data=await response.json();if(!response.ok)throw Error(data.detail||'Traffic study unavailable');
    el('trafficCoverage').textContent=JSON.stringify(data,null,2);
    if(data.status!=='available'){el('trafficStatus').textContent=data.message;return}
    el('trafficStatus').textContent=`Scheduled-passenger certificate traffic · monthly expanding fits at 1, 3, 6 and 12 months · ${fmt(data.common_predictions_per_model)} common forecast comparisons per model · 2013–2019 initial sample fully released ${data.first_initial_release}. Current-download reconstruction.`;
    const best=data.scorecard.reduce((a,b)=>a.equal_cell_MASE<=b.equal_cell_MASE?a:b),names={'state_rho_0':'Independent state levels','state_rho_0.5':'State levels with moderate peer pooling','state_rho_0.9':'State levels with strong peer pooling','released_seasonal_persistence':'Released seasonal persistence','fixed_initial_seasonal_mean':'Fixed initial seasonal mean','expanding_regularized_log_ar':'Expanding regularized autoregression'};
    el('trafficFinding').textContent=data.beats_best_declared_baseline?'The best state-model candidate improves the declared baseline score on this selection sample. Locked evaluation and calibration remain pending.':`The best state-model candidate does not beat the best declared baseline (${names[best.model]}). This comparison does not support adding peer-state complexity to the simulator.`;el('trafficFinding').hidden=false;
    const table=(headers,rows,captionText)=>{const t=document.createElement('table'),caption=document.createElement('caption');caption.textContent=captionText;t.append(caption);const head=document.createElement('thead'),hr=document.createElement('tr');for(const v of headers){const c=document.createElement('th');c.scope='col';c.textContent=v;hr.append(c)}head.append(hr);t.append(head);const body=document.createElement('tbody');for(const values of rows){const r=document.createElement('tr');for(const v of values){const c=document.createElement('td');c.textContent=String(v);r.append(c)}body.append(r)}t.append(body);return t};
    el('trafficScores').replaceChildren(table(['Model','Scaled error (MASE)','Error (million RPM)','Comparisons'],data.scorecard.map(s=>[names[s.model]+(s.model===best.model?' · best comparison':s.model===data.selected_name?' · best state candidate':''),s.equal_cell_MASE.toFixed(3),fmt(s.equal_cell_MAE_rpm_million),fmt(s.prediction_count)]),'Mean absolute errors, equally weighted across 48 carrier / market / horizon cells. MASE scales each cell by its initial lag-12 error. Lower is better; all models use identical targets.'));el('trafficScores').hidden=false;
    const counts=data.scorecard[0].series_horizon_scores,rows=[];for(const c of ['UA','DL','AA'])for(const m of ['domestic','atlantic','pacific','latin_america'])rows.push([c+' '+m.replace('_',' '),...[1,3,6,12].map(h=>counts.find(r=>r.carrier===c&&r.market===m&&r.horizon_months===h).targets)]);
    el('trafficCounts').replaceChildren(table(['Carrier / market','1 month','3 months','6 months','12 months'],rows,'Every candidate uses these targets. Forecast horizons overlap.'));el('trafficSamples').hidden=false;
    el('trafficLimits').textContent=`In the preserved archive set, the newest available observations are ${data.information_lag_months.minimum}–${data.information_lag_months.maximum} months behind issue dates. Missing domestic/international contributions stay unknown until both source releases are available. Served traffic is a demand proxy; fare/yield, unconstrained demand, regional consolidation, fitted pricing and integration remain pending. Native model intervals are uncalibrated; 2024+ forecast targets remain locked.`;
    el('trafficDownload').hidden=false;
  }).catch(error=>{el('trafficStatus').textContent='Traffic study: '+error.message;for(const id of ['trafficFinding','trafficScores','trafficSamples','trafficDownload'])el(id).hidden=true});
  fetch('/api/capacity/status').then(async response=>{
    const data=await response.json();
    if(!response.ok)throw Error(data.detail || 'Capacity estimate unavailable');
    el('capacityCoverage').textContent=JSON.stringify(data,null,2);
    if(data.status!=='available'){el('capacityStatus').textContent=data.message;return}
    el('capacityStatus').textContent=`T2-covered aircraft: available seat miles per assigned aircraft-day · fitted on 2013–2019 · ${data.common_predictions_per_model} common forecast comparisons in 2020–2023 · current-download reconstruction.`;
    const selected=data.scorecard.find(r=>r.model===data.selected_name);
    const sparse=selected.series_horizon_scores.reduce((a,b)=>a.targets<=b.targets?a:b);
    el('capacitySparse').textContent=`Sparse validation: ${sparse.carrier} ${sparse.group} at ${sparse.horizon_quarters*3} months has only ${sparse.targets} usable ${sparse.targets===1?'target':'targets'}. The average score gives this comparison equal weight. This is insufficient evidence for reliable United capacity forecasts.`;
    el('capacitySparse').hidden=false;
    const makeTable=(headers,rows,captionText)=>{
      const table=document.createElement('table'),caption=document.createElement('caption');caption.textContent=captionText;table.append(caption);
      const head=document.createElement('thead'),hr=document.createElement('tr');for(const text of headers){const cell=document.createElement('th');cell.scope='col';cell.textContent=text;hr.append(cell)}head.append(hr);table.append(head);
      const body=document.createElement('tbody');for(const values of rows){const row=document.createElement('tr');for(const value of values){const cell=document.createElement('td');cell.textContent=String(value);row.append(cell)}body.append(row)}table.append(body);return table;
    };
    const names={direct_capacity:'Direct capacity',utilization_times_productivity:'Utilization × productivity'};
    const rows=Object.entries(data.best_by_architecture).map(([architecture,name])=>{const score=data.scorecard.find(r=>r.model===name);return [names[architecture]+(name===data.selected_name?' (selected)':''),(score.selection_equal_weight_MAE_asm_per_day/1000).toFixed(1),score.prediction_count]});
    for(const [name,label] of [['seasonal_capacity_persistence','Seasonal persistence'],['fixed_training_seasonal_capacity_mean','Fixed training seasonal mean']]){const score=data.scorecard.find(r=>r.model===name);rows.push([label,(score.selection_equal_weight_MAE_asm_per_day/1000).toFixed(1),score.prediction_count])}
    el('capacityScores').replaceChildren(makeTable(['Model','Error (thousand ASM/day)','Comparisons'],rows,'Mean absolute error, equally weighted across 18 carrier / fleet / horizon cells. Lower is better. All models use the same targets.'));el('capacityScores').hidden=false;
    const counts=[];for(const carrier of ['UA','DL','AA'])for(const group of ['narrowbody','widebody'])counts.push([carrier+' '+group,...[1,2,4].map(h=>selected.series_horizon_scores.find(r=>r.carrier===carrier&&r.group===group&&r.horizon_quarters===h).targets)]);
    el('capacityCounts').replaceChildren(makeTable(['Carrier / fleet','3 months','6 months','12 months'],counts,'Counts apply to every candidate and baseline. Overlapping forecast horizons are not independent observations.'));el('capacitySamples').hidden=false;
    el('capacityLimits').textContent=`${data.excluded_group_quarters} group-quarters excluded for source eligibility; ${data.excluded_forecasts} forecast comparisons withheld across every model. These are covered-aircraft ratios, with unresolved P52 exposure differences; whole-fleet, monthly, crew and hub capacity remain pending. No 2024+ forecast scoring or calibrated intervals. This study does not yet change the simulator's assumed capacity.`;
  }).catch(error=>{el('capacityStatus').textContent='Capacity study: '+error.message;for(const id of ['capacityScores','capacitySamples','capacitySparse'])el(id).hidden=true});
  fetch('/api/utilization/status').then(async response=>{
    const data=await response.json();
    if(!response.ok)throw Error(data.detail || 'Utilization estimate unavailable');
    el('utilizationCoverage').textContent=JSON.stringify(data,null,2);
    if(data.status!=='available'){el('utilizationStatus').textContent=data.message;return}
    el('utilizationStatus').textContent=`Airborne hours per aircraft-day · fitted on 2013–2019 · ${data.selection_predictions_per_model} common forecast comparisons in 2020–2023 · quarterly targets at 3, 6 and 12 months · current-download reconstruction.`;
    const table=document.createElement('table'),caption=document.createElement('caption');
    caption.textContent='Mean absolute error: equal weight across carrier, fleet group and horizon comparisons. Lower is better.';table.append(caption);
    const header=document.createElement('thead'),tr=document.createElement('tr');
    for(const text of ['Model','Error (hours/day)','Forecast comparisons']){const th=document.createElement('th');th.textContent=text;tr.append(th)}header.append(tr);table.append(header);
    const body=document.createElement('tbody'),names={[data.selected_name]:'Seasonal autoregression (selected)',seasonal_persistence:'Seasonal persistence',fixed_training_seasonal_mean:'Fixed training seasonal mean'};
    for(const name of [data.selected_name,'seasonal_persistence','fixed_training_seasonal_mean']){const score=data.scorecard.find(r=>r.model===name),row=document.createElement('tr');for(const value of [names[name],score.selection_equal_weight_MAE_hpd.toFixed(2),String(score.prediction_count)]){const cell=document.createElement('td');cell.textContent=value;row.append(cell)}body.append(row)}table.append(body);
    el('utilizationScores').replaceChildren(table);el('utilizationScores').hidden=false;
    el('utilizationLimits').textContent=`${data.excluded_group_quarters} group-quarters excluded for missing or invalid source quantities, including ${data.excluded_disruption_group_quarters} during 2020–2021; ${data.excluded_forecast_count} forecast comparisons withheld across every model. The sparsest comparison has ${data.minimum_selection_cell_targets} targets. Forecast horizons overlap. The 2024+ test and interval calibration remain pending. These estimates do not yet change the simulation's assumed capacity.`;
  }).catch(error=>{el('utilizationStatus').textContent='Utilization study: '+error.message;el('utilizationScores').hidden=true});
  fetch('/api/history/status').then(async response=>{
    const data=await response.json();
    if(!response.ok)throw Error(data.detail || 'Historical data unavailable');
    el('historyStatus').textContent=data.status==='available'
      ? 'Cached SEC financial history: '+data.coverage.map(c=>`${c.entity} ${c.quarters} quarters (${c.first}–${c.last})`).join(' · ')+'. Financial model fitting and evaluation are pending.'
      : data.message;
    el('historyCoverage').textContent=JSON.stringify(data,null,2);
  }).catch(error=>{el('historyStatus').textContent='Historical data: '+error.message});
  fetch('/api/operating-history/status').then(async response=>{
    const data=await response.json();
    if(!response.ok)throw Error(data.detail || 'Operating history unavailable');
    el('operatingHistoryStatus').textContent=data.status==='available'
      ? 'Cached BTS operating history (snapshot cutoff '+data.cutoff+'): '+data.coverage.filter(c=>['UA','DL','AA'].includes(c.operating_carrier)).map(c=>`${c.operating_carrier} ${c.months} months (${c.first}–${c.last})`).join(' · ')+'. Imported observations do not yet feed this simulation. Operating-carrier totals exclude unassigned feeder flying and foreign-to-foreign segments. Demand fitting and issuer reconciliation are pending.'
      : data.message;
    el('operatingHistoryCoverage').textContent=JSON.stringify(data,null,2);
  }).catch(error=>{el('operatingHistoryStatus').textContent='Operating history: '+error.message});
  fetch('/api/form41-history/status').then(async response=>{
    const data=await response.json();
    if(!response.ok)throw Error(data.detail || 'Form 41 data unavailable');
    el('form41HistoryStatus').textContent=data.status==='available'
      ? 'Cached Form 41 history (acquisition cutoff '+data.cutoff+'): '+['UA','DL','AA'].map(carrier=>{const rows=data.coverage.filter(c=>c.unique_carrier===carrier);return carrier+' '+rows.filter(c=>c.schedule!=='P12A').map(c=>c.schedule+' '+c.periods+' quarters').join(', ')+', P12A '+rows.find(c=>c.schedule==='P12A').periods+' fuel months'}).join(' · ')+'. Current-download reconstruction; no historical publication vintages. Certificate/region/aircraft records remain separate from SEC consolidated totals and US-plus-Canada markets. Cost fitting and simulator integration are pending.'
      : data.message;
    el('form41HistoryCoverage').textContent=JSON.stringify(data,null,2);
  }).catch(error=>{el('form41HistoryStatus').textContent='Form 41 history: '+error.message});

  async function applyPreset(name){
    if(presetBusy||el('monthlyRun').disabled)return false;
    presetBusy=true;el('monthlyRun').disabled=true;updateThesisState();const buttons=[...document.querySelectorAll('[data-preset]')];for(const button of buttons)button.disabled=true;
    el('presetStatus').textContent='Loading illustrative purchase and retention assumptions…';
    try{
      const responses=await Promise.all([fetch('/api/monthly/purchase-example'),fetch('/api/monthly/retention-example')]);
      if(responses.some(response=>!response.ok))throw Error('Illustrative assumptions unavailable. Existing inputs were retained.');
      const [purchase,retention]=await Promise.all(responses.map(response=>response.json()));
      const values={cutoff:'2026-07-16',fuelShock:name==='delay'?0:25,demandShock:0,capacityShock:0,market:'all',horizonMonths:12,onsetMonths:1,durationMonths:6,entryDelay:name==='fuel'?0:9,deliveryCount:30,deliveryGroup:'narrowbody',unavailable:0,availabilityGroup:'all',contractShock:0,lagMonths:1,mode:'assumed_constrained',sparePct:2,scheduleGrowthPct:100,contractFixedPct:70,nonfuelFixedPct:70};
      for(const [id,value] of Object.entries(values))el(id).value=value;
      restoreCashPlan(null);restorePurchasePlan(purchase);restoreRetentionPlan(name==='no-retention'?null:retention);
      changedPlan();for(const button of buttons)button.setAttribute('aria-pressed',String(button.dataset.preset===name));
      el('presetStatus').textContent=(name==='no-retention'?'Illustrative terms; retention disabled. ':'Illustrative purchase and retention terms. ')+'12-month horizon. Run to update; review terms in Advanced assumptions.';
      return true;
    }catch(error){el('presetStatus').textContent=error.message;return false}
    finally{presetBusy=false;el('monthlyRun').disabled=false;for(const button of buttons)button.disabled=false;updateThesisState()}
  }
  for(const button of document.querySelectorAll('[data-preset]'))button.addEventListener('click',()=>applyPreset(button.dataset.preset));
  fetch('/api/health').then(response=>response.json()).then(data=>{hostedDemo=Boolean(data.hosted_demo);if(hostedDemo){el('cloudNotice').hidden=false;el('convergenceRun').textContent='Sampling studies · local application';updateConvergenceState()}}).catch(()=>{});
  applyPreset('combined').then(()=>run());
})();
