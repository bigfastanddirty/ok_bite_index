// Run with PLAYWRIGHT_MODULE and CHROMIUM_PATH pointing to installed browser tooling.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const http=require('node:http'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const fixture=JSON.parse(fs.readFileSync(path.join(__dirname,'dashboard-fixture.json')));
fixture.lakes.ARCA.analysis.species_ranking.find(row=>row.species==='Largemouth Bass').species='Bass, Largemouth';
const lakes=Object.entries(fixture.lakes).map(([lake_code,x])=>({lake_code,lake_name:x.analysis.lake_name}));
let failAnalysis=false,emptyForecast=false,slowArcadia=false;
const server=http.createServer((req,res)=>{
 const url=new URL(req.url,'http://localhost');
 if(url.pathname==='/api/lakes'){res.setHeader('Content-Type','application/json');setTimeout(()=>res.end(JSON.stringify(lakes)),700);return}
 const match=url.pathname.match(/^\/api\/lakes\/([^/]+)\/(analysis|forecast|history)$/);
 if(match){const [,code,kind]=match;res.setHeader('Content-Type','application/json');
  if(failAnalysis&&kind==='analysis'){res.writeHead(503);return res.end('{}')}
  let data=fixture.lakes[code]?.[kind];if(kind==='forecast'&&emptyForecast)data={forecast:[],best_window:null};
  const send=()=>res.end(JSON.stringify(data));if(code==='ARCA'&&slowArcadia)setTimeout(send,500);else send();return;
 }
 res.setHeader('Content-Type','text/html');res.end(fs.readFileSync(path.join(__dirname,'../web/index.html')));
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const base='http://127.0.0.1:'+server.address().port;
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox']});
 try{
 const page=await browser.newPage({viewport:{width:1100,height:1400}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(({time})=>{const OriginalDate=Date;globalThis.Date=class extends OriginalDate{constructor(...args){super(...(args.length?args:[time]))}static now(){return time}}},{time:Date.parse(fixture.captured_at)});
 await page.goto(base);await page.locator('[data-page="trends"]').click();await page.locator('[data-days="90"]').click();await page.locator('[data-page="overview"]').click();await page.locator('#cc-lake-title').waitFor({timeout:3000});
 await page.waitForFunction(()=>document.querySelector('#cc-lake-select option')&&document.querySelector('#cc-score').textContent.includes('64'));
 assert.deepEqual(errors,[],'Controls during catalog loading should not throw');
 assert.equal(await page.locator('#cc-lake-select option').count(),2);
 assert((await page.locator('#cc-species').textContent()).includes('remaining submerged cover'),'Canonical ODWC labels should show tactics rather than scoring reasons');
 assert.equal(await page.locator('.cc-hour').count(),3);
 assert(!/Humidity|Pressure|Solunar/.test(await page.locator('#cc-hourstrip').textContent()));
 const initial=await page.locator('#cc-block-label').textContent();await page.locator('[data-block="2"]').click();assert.notEqual(await page.locator('#cc-block-label').textContent(),initial);
 for(const kind of ['score','species']){
  const height=await page.locator('#cc-overview').evaluate(n=>n.getBoundingClientRect().height);
  await page.locator('#cc-open-'+kind).click();assert(await page.locator('#cc-'+kind+'-dialog').isVisible());
  assert.equal(await page.locator('#cc-overview').evaluate(n=>n.getBoundingClientRect().height),height);
  await page.keyboard.press('Escape');assert(!(await page.locator('#cc-'+kind+'-dialog').isVisible()));
 }
 await page.locator('[data-page="trends"]').click();await page.waitForFunction(()=>document.querySelector('#cc-history-note').textContent.includes('hourly buckets'));
 await page.locator('[data-days="90"]').click();
 assert((await page.locator('#cc-history-chart .cc-history-series').evaluateAll(nodes=>nodes.map(n=>n.getAttribute('d')))).every(path=>!path.includes('L')),'Absent hourly buckets must remain gaps in observed history');
 await page.locator('[data-overlay="surface_pressure_hpa"]').click();assert.equal(await page.locator('.cc-history-legend-item').count(),2);
 await page.locator('[data-page="methodology"]').click();assert(await page.locator('#cc-color-methodology').isVisible());
 assert((await page.locator('#cc-methodology-page').textContent()).includes('365.2425'));
 await page.goto(base+'/methodology');assert(await page.locator('#cc-methodology-page').isVisible());
 await page.waitForFunction(()=>document.querySelector('#cc-lake-select option'));
 await page.locator('[data-page="overview"]').click();
 slowArcadia=true;await page.locator('#cc-lake-select').selectOption('HEFN');await page.locator('#cc-lake-select').selectOption('ARCA');await page.locator('#cc-lake-select').selectOption('HEFN');
 await page.waitForFunction(()=>document.querySelector('#cc-lake-title').textContent.includes('Hefner'));await page.waitForTimeout(600);
 assert((await page.locator('#cc-lake-title').textContent()).includes('Hefner'));
 assert((await page.locator('#cc-score').textContent()).includes(String(fixture.lakes.HEFN.analysis.bite_score)));
 slowArcadia=false;emptyForecast=true;await page.locator('#cc-lake-select').selectOption('ARCA');await page.waitForFunction(()=>document.querySelector('#cc-block-label').textContent.includes('unavailable'));
 assert.equal(await page.locator('.cc-hour').count(),0);emptyForecast=false;
 failAnalysis=true;await page.locator('#cc-lake-select').selectOption('HEFN');await page.waitForFunction(()=>document.querySelector('#cc-load-status').textContent.includes('Unable'));
 assert((await page.locator('#cc-score').textContent()).includes('—'));failAnalysis=false;
 await page.locator('#cc-retry').click();await page.waitForFunction(()=>document.querySelector('#cc-score').textContent.includes(String(window.expectedScore||64))||document.querySelector('#cc-load-status').hidden);
 for(const width of [1024,736,390,320]){await page.setViewportSize({width,height:1400});for(const tab of ['overview','trends','methodology']){await page.locator('[data-page="'+tab+'"]').click();const bounds=await page.locator('#bite-command').evaluate(n=>[n.clientWidth,n.scrollWidth]);assert(bounds[1]<=bounds[0]+1,'Overflow '+tab+' '+width)}}
 assert.deepEqual(errors,[]);console.log('PASS: live API UI, popup geometry, forecast blocks, compact cards, history overlays, direct methodology, request races, empty/failed data, retry and responsive layouts');
 }finally{await browser.close();await new Promise(r=>server.close(r))}
})().catch(e=>{console.error(e);server.close();process.exitCode=1});
