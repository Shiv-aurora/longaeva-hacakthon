'use strict';
(() => {
  const byId = id => document.getElementById(id);
  const fmt = (n, digits=1) => Number(n).toLocaleString('en-US', {minimumFractionDigits:digits,maximumFractionDigits:digits});
  const usdM = v => (v < 0 ? '−' : '+') + '$' + fmt(Math.abs(v),1) + 'M';
  let boot = null, latest = null, quotes = null;
  const controls = ['shock','lag','pass','speed','persistence','horizon','paths'];
  const vals = () => ({
    fuel_shock_pct:Number(byId('shock').value),
    fuel_shock_half_life_days:Number(byId('persistence').value),
    response_lag_days:Number(byId('lag').value),
    response_half_life_days:Number(byId('speed').value),
    pass_through_fraction:Number(byId('pass').value)/100,
    demand_elasticity:.55,
    horizon_days:Number(byId('horizon').value),
    paths:Number(byId('paths').value),
    seed:2026
  });

  function updateControlLabels(){
    byId('shockOut').textContent = (Number(byId('shock').value)>0?'+':'')+byId('shock').value+'%';
    byId('lagOut').textContent = byId('lag').value+' days';
    byId('passOut').textContent = byId('pass').value+'%';
    byId('speedOut').textContent = byId('speed').value+' days';
    byId('persistenceOut').textContent = byId('persistence').value+' days';
    if(boot){
      const v=Number(byId('shock').value)/100 * boot.fuel_last_price * (boot.ual.fuel_gallons_million/boot.ual.asm_million) *100;
      byId('pressureValue').textContent = (v>0?'+':'') + fmt(v,2) + '¢';
    }
  }
  function line(points){return points.map((p,i)=>(i===0?'M':'L')+p[0].toFixed(2)+','+p[1].toFixed(2)).join(' ')}
  function area(upper,lower){return line(upper)+' '+line(lower.slice().reverse()).replace(/^M/,'L')+' Z'}
  function chartMarkup(g, xAxis, yAxis, formatter){
    let out='';
    const {left,right,top,bottom,min,max} = g;
    for(let i=0;i<=4;i++){
      const v=min+(max-min)*i/4, y=yAxis(v);
      out+=`<line x1="${left}" x2="${right}" y1="${y}" y2="${y}" stroke="#2b3745" stroke-width="1"/>`;
      out+=`<text x="${left-9}" y="${y+4}" fill="#8698aa" text-anchor="end" font-size="12" font-family="IBM Plex Mono, monospace">${formatter(v)}</text>`;
    }
    return out;
  }
  function renderFuel(data){
    const svg=byId('fuelChart');
    const h=data.history, s=data.series.fuel_scenario_usd_gal;
    if(!h.length || !s.length){svg.textContent='No fuel data';return;}
    const left=55,right=800,top=20,bottom=263;
    const hWidth=470;
    const xh=i=>left+(hWidth-left)*i/Math.max(h.length-1,1);
    const xf=i=>hWidth+(right-hWidth)*i/Math.max(s.length,1);
    const prices=[...h.map(d=>d.usd_per_gallon),...s.flatMap(d=>[d.p10,d.p90])];
    const min0=Math.min(...prices),max0=Math.max(...prices), margin=Math.max(.12,(max0-min0)*.13);
    const min=Math.max(0,min0-margin),max=max0+margin;
    const y=v=>bottom-(v-min)/(max-min)*(bottom-top);
    const historyPoints=h.map((d,i)=>[xh(i),y(d.usd_per_gallon)]);
    const median=[[xh(h.length-1),y(h[h.length-1].usd_per_gallon)],...s.map((d,i)=>[xf(i+1),y(d.p50)])];
    const upper=[[xh(h.length-1),y(h[h.length-1].usd_per_gallon)],...s.map((d,i)=>[xf(i+1),y(d.p90)])];
    const lower=[[xh(h.length-1),y(h[h.length-1].usd_per_gallon)],...s.map((d,i)=>[xf(i+1),y(d.p10)])];
    let html=`<rect x="${hWidth}" y="${top}" width="${right-hWidth}" height="${bottom-top}" fill="#161f32" opacity=".55"/>`;
    html+=chartMarkup({left,right,top,bottom,min,max},null,y,v=>'$'+v.toFixed(1));
    html+=`<line x1="${hWidth}" x2="${hWidth}" y1="${top}" y2="${bottom}" stroke="#617184" stroke-dasharray="5 6"/>`;
    html+=`<path d="${area(upper,lower)}" fill="#817bd7" opacity=".23"/>`;
    html+=`<path d="${line(historyPoints)}" fill="none" stroke="#61d6c9" stroke-width="2.4" stroke-linejoin="round"/>`;
    html+=`<path d="${line(median)}" fill="none" stroke="#e9edfb" stroke-width="2.4" stroke-linejoin="round"/>`;
    html+=`<circle cx="${hWidth}" cy="${y(h[h.length-1].usd_per_gallon)}" r="4.5" fill="#61d6c9" stroke="#101822" stroke-width="2"/>`;
    html+=`<text x="${left}" y="${bottom+24}" fill="#8395a7" font-size="12" font-family="IBM Plex Mono">${h[0].date.slice(5)}</text>`;
    html+=`<text x="${hWidth}" y="${bottom+24}" fill="#95d2c8" text-anchor="middle" font-size="12" font-family="IBM Plex Mono">${h[h.length-1].date.slice(5)}</text>`;
    html+=`<text x="${right}" y="${bottom+24}" fill="#8395a7" text-anchor="end" font-size="12" font-family="IBM Plex Mono">${data.dates.at(-1).slice(5)}</text>`;
    html+=`<text x="${(left+hWidth)/2}" y="13" fill="#74889b" text-anchor="middle" font-size="11" font-family="IBM Plex Mono">OBSERVED · ${h.length} SESSIONS</text>`;
    html+=`<text x="${(hWidth+right)/2}" y="13" fill="#a7a0e7" text-anchor="middle" font-size="11" font-family="IBM Plex Mono">CONDITIONAL PATHS</text>`;
    svg.innerHTML=html;
    byId('fuelCaption').textContent=`Historical EIA Gulf Coast jet fuel through ${h.at(-1).date}. Shaded future is a simulated fuel-price shock, not an observed or validated forecast.`;
  }
  function renderImpact(data){
    const svg=byId('impactChart');const s=data.series.cumulative_operating_income_delta_musd;
    const left=63,right=778,top=17,bottom=247;
    const vals=[0,...s.flatMap(d=>[d.p10,d.p90])]; const spread=Math.max(10,Math.max(...vals)-Math.min(...vals));
    const min=Math.min(...vals)-spread*.16, max=Math.max(...vals)+spread*.16;
    const y=v=>bottom-(v-min)/(max-min)*(bottom-top);const x=i=>left+(right-left)*i/Math.max(s.length-1,1);
    const hi=s.map((d,i)=>[x(i),y(d.p90)]),lo=s.map((d,i)=>[x(i),y(d.p10)]),mid=s.map((d,i)=>[x(i),y(d.p50)]);
    let html=chartMarkup({left,right,top,bottom,min,max},null,y,v=>(v<0?'−':'')+'$'+(Math.abs(v)>=1000?fmt(Math.abs(v)/1000,1)+'B':fmt(Math.abs(v),0)+'M'));
    if(min<=0 && max>=0)html+=`<line x1="${left}" x2="${right}" y1="${y(0)}" y2="${y(0)}" stroke="#69798c" stroke-dasharray="5 6"/>`;
    html+=`<path d="${area(hi,lo)}" fill="#8a87dc" opacity=".24"/>`;
    html+=`<path d="${line(mid)}" fill="none" stroke="#f6c278" stroke-width="3" stroke-linejoin="round"/>`;
    html+=`<circle cx="${x(s.length-1)}" cy="${y(s.at(-1).p50)}" r="4.5" fill="#f6c278"/>`;
    html+=`<text x="${left}" y="${bottom+23}" fill="#8497aa" font-size="12" font-family="IBM Plex Mono">DAY 1</text>`;
    html+=`<text x="${right}" y="${bottom+23}" text-anchor="end" fill="#8497aa" font-size="12" font-family="IBM Plex Mono">DAY ${s.length}</text>`;
    svg.innerHTML=html;
  }
  function showRun(data){
    latest=data;
    renderFuel(data);renderImpact(data);
    const sum=data.summary,impact=sum.cumulative_operating_income_impact_musd;
    byId('impactValue').textContent=usdM(impact.p50);
    byId('impactValue').style.color=impact.p50<0?'var(--red)':'var(--mint)';
    byId('impactInterval').textContent='P10 '+usdM(impact.p10)+'   ·   P90 '+usdM(impact.p90)+'  / '+data.scenario.horizon_days+' DAYS';
    byId('fuelCostValue').textContent=(sum.fuel_cost_increment_musd.p50>0?'+':'')+fmt(sum.fuel_cost_increment_musd.p50,1)+'M';
    byId('recoveryValue').textContent=(sum.modeled_revenue_recovery_musd.p50>0?'+':'')+fmt(sum.modeled_revenue_recovery_musd.p50,1)+'M';
    byId('fareValue').textContent=(sum.ending_modeled_fare_change_pct.p50>0?'+':'')+fmt(sum.ending_modeled_fare_change_pct.p50,2)+'%';
    byId('breakevenValue').textContent=fmt(sum.gross_prasm_increase_required_pct_initial,2)+'%';
    byId('runStatus').textContent=`${data.scenario.paths} PATHS · ${data.scenario.horizon_days} DAYS · SCENARIO ONLY`;
  }
  async function run(e){
    if(e)e.preventDefault();if(!boot)return;
    const btn=byId('runButton');btn.disabled=true;byId('runStatus').textContent='Simulating paired scenarios…';
    try{
      const r=await fetch('/api/simulate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(vals())});
      if(!r.ok)throw Error((await r.json()).detail||'The simulation request failed');
      showRun(await r.json());
    }catch(e){byId('runStatus').textContent='ERROR · '+e.message;console.error(e);}finally{btn.disabled=false;}
  }
  function showQuotes(q){
    quotes=q;
    const c=q.coverage,d=q.diagnostic;
    byId('clearQuotes').disabled=c.rows===0;
    byId('coverageRow').textContent=c.rows?`${c.rows} sourced observations · ${c.routes.length} routes · latest ${c.last_observed}`:'No quote observations loaded.';
    const state=byId('signalState');
    const message=byId('signalDetail');
    if(d.ready){
      state.textContent='DESCRIPTIVE DATA AVAILABLE · NOT VALIDATED ALPHA';
      state.style.color='var(--mint)';state.style.borderColor='#315950';state.style.background='#1b3130';
      message.textContent=`${d.route}, ${d.first_date} to ${d.last_date}: offered fare ${d.fare_change_pct>0?'+':''}${fmt(d.fare_change_pct,2)}%; spot fuel ${d.spot_fuel_delta_usd_gal>0?'+':''}$${fmt(d.spot_fuel_delta_usd_gal,3)}/gal. This is not UAL's realized yield or a backtested investable signal.`;
    }else{
      state.textContent='NOT YET IDENTIFIED';state.style.color='var(--amber)';state.style.borderColor='#564631';state.style.background='#25241c';
      message.textContent=d.reason;
    }
  }
  async function loadQuotes(){
    try{const r=await fetch('/api/quotes');if(r.ok)showQuotes(await r.json())}catch(e){byId('quoteFeedback').textContent='Could not load local fare observations.'}
  }
  async function upload(){
    const file=byId('quoteFile').files[0];if(!file)return;
    const btn=byId('uploadButton');btn.disabled=true;byId('quoteFeedback').textContent='Validating CSV locally…';
    try{
      const csv=await file.text();
      const r=await fetch('/api/quotes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csv_text:csv})});
      const content=await r.json();if(!r.ok)throw Error(typeof content.detail==='string'?content.detail:'CSV rejected');
      byId('quoteFeedback').textContent=`Imported ${content.count} source-backed observations into the local data folder.`;
      await loadQuotes();
    }catch(e){byId('quoteFeedback').textContent='Import rejected: '+e.message}finally{btn.disabled=false;}
  }
  async function clear(){
    if(!confirm('Delete the locally stored fare quote CSV?'))return;
    await fetch('/api/quotes',{method:'DELETE'});
    byId('quoteFile').value='';byId('uploadButton').disabled=true;
    byId('quoteFeedback').textContent='Local observations cleared.';
    await loadQuotes();
  }
  async function start(){
    for(const id of controls){const el=byId(id);el.addEventListener('input',()=>{updateControlLabels();if(latest)byId('runStatus').textContent='PARAMETERS CHANGED · RE-RUN TO UPDATE';})}
    byId('scenarioForm').addEventListener('submit',run);
    byId('quoteFile').addEventListener('change',()=>{byId('uploadButton').disabled=!byId('quoteFile').files.length});
    byId('uploadButton').addEventListener('click',upload);
    byId('clearQuotes').addEventListener('click',clear);
    updateControlLabels();
    try{
      const r=await fetch('/api/bootstrap');if(!r.ok)throw Error('Bootstrap failed');
      boot=await r.json();
      byId('fuelCoverageText').textContent=`${boot.fuel.length} genuine EIA observations, ${boot.fuel[0].date} to ${boot.fuel_last_date}.`;
      byId('spotValue').textContent='$'+fmt(boot.fuel_last_price,3);
      byId('spotDate').textContent=`OBSERVED ${boot.fuel_last_date} · USD/GAL`;
      const h=boot.fuel, earlier=h[Math.max(0,h.length-21)].usd_per_gallon, change=100*(boot.fuel_last_price/earlier-1);
      byId('spotChange').textContent=(change>0?'+':'')+fmt(change,1)+'%';
      byId('intensityValue').textContent=fmt(1000*boot.ual.fuel_gallons_million/boot.ual.asm_million,2);
      updateControlLabels();byId('runButton').disabled=false;
      await Promise.all([run(),loadQuotes()]);
    }catch(e){byId('runStatus').textContent='DATA LOAD FAILURE · '+e.message;console.error(e)}
  }
  start();
})();
