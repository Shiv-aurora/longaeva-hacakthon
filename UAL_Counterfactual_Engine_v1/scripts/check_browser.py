"""Browser smoke test of offline UI. Uses actual API payloads, no local network in Chromium."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from fastapi.testclient import TestClient
from app.server import app

root=Path(__file__).resolve().parents[1]
client=TestClient(app)
bootstrap=client.get('/api/bootstrap').json()
def scenario(shock):return client.post('/api/simulate',json={'fuel_shock_pct':shock,'paths':300,'horizon_days':60}).json()
payloads={'bootstrap':bootstrap,'base':scenario(20),'zero':scenario(0),'quotes':client.get('/api/quotes').json()}
html=(root/'app/static/index.html').read_text()
css=(root/'app/static/styles.css').read_text()
css='\n'.join(line for line in css.splitlines() if not line.strip().startswith('@import'))
js=(root/'app/static/app.js').read_text()
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
    for name,dim in [('desktop',{'width':1440,'height':950}),('mobile',{'width':390,'height':844})]:
        page=browser.new_page(viewport=dim,device_scale_factor=1)
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.set_content(html.replace('<script defer src="/static/app.js"></script>','').replace('<link rel="stylesheet" href="/static/styles.css" />',''),wait_until='domcontentloaded')
        page.add_style_tag(content=css)
        page.evaluate('''payload => {window.fetch=async (url,opts)=>{
            const path=String(url);
            const obj=path.includes('/api/bootstrap')? payload.bootstrap : path.includes('/api/quotes')?payload.quotes : JSON.parse(opts.body).fuel_shock_pct===0?payload.zero:payload.base;
            return {ok:true,json:async()=>obj};
        }}''',payloads)
        page.add_script_tag(content=js)
        page.wait_for_function("document.querySelector('#impactValue').textContent !== '—'",timeout=15000)
        print(name,'spot=',page.locator('#spotValue').inner_text(),'impact=',page.locator('#impactValue').inner_text(),'js_errors=',errors,'scrollWidth=',page.evaluate('document.documentElement.scrollWidth'),'innerWidth=',page.evaluate('window.innerWidth'))
        if name=='desktop':
            page.locator('#shock').fill('0')
            page.locator('#runButton').click()
            page.wait_for_function("document.querySelector('#impactValue').textContent === '+$0.0M'",timeout=12000)
            print('zero-shock browser result:',page.locator('#impactValue').inner_text())
            page.locator('#shock').fill('20')
            page.locator('#runButton').click()
            page.wait_for_function("document.querySelector('#impactValue').textContent !== '+$0.0M'",timeout=12000)
        (root/'screenshots').mkdir(exist_ok=True)
        page.screenshot(path=str(root/f'screenshots/{name}.png'),full_page=True)
        page.close()
    browser.close()
