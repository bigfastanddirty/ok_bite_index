const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox'],executablePath:process.env.CHROMIUM_EXECUTABLE||undefined});
 try{
  const page=await browser.newPage({timezoneId:'America/Los_Angeles'}),base=process.env.BASE_URL||'http://127.0.0.1:8088';
  await page.route(base+'/',route=>route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8')}));
  const analysis=JSON.parse(fs.readFileSync(path.join(__dirname,'dashboard-fixture.json'))).lakes.ARCA.analysis;
  const start=new Date();start.setUTCMinutes(0,0,0);start.setUTCHours(start.getUTCHours()+1);
  let forecast=[95,71,82,10,85,85,81,...Array(17).fill(10),100].map((score,i)=>({time:new Date(start.getTime()+i*3600000).toISOString(),bite_score:score,rating:score>=80?'EPIC':'TOUGH'}));
  let best={start:forecast[4].time,end:new Date(start.getTime()+7*3600000).toISOString(),average_score:84};
  await page.route('**/api/lakes',route=>route.fulfill({json:[{lake_code:'ARCA',lake_name:'Arcadia Lake'}]}));
  await page.route('**/api/lakes/ARCA/analysis',route=>route.fulfill({json:analysis}));
  await page.route('**/api/lakes/ARCA/forecast',route=>route.fulfill({json:{forecast,best_window:best}}));
  await page.goto(base+'/');await page.waitForFunction(()=>document.querySelector('#cc-window-note').textContent.includes('84/100'));
  assert((await page.locator('#cc-peak-note').textContent()).includes('95/100'));
  const expected=new Intl.DateTimeFormat('en-US',{timeZone:'America/Chicago',hour:'numeric',minute:'2-digit'}).format(start);
  assert.equal(await page.locator('#cc-peak').textContent(),expected);
  await page.locator('[data-block="0"]').click();assert.equal(await page.locator('.cc-hour').count(),3);
  forecast=[];best=null;await page.reload();await page.waitForFunction(()=>document.querySelector('#cc-load-status').hidden);
  assert.equal(await page.locator('#cc-peak').textContent(),'Unavailable');assert.equal(await page.locator('#cc-window').textContent(),'Unavailable');assert.equal(await page.locator('.cc-hour').count(),0);
  console.log('PASS: strongest upcoming hour, 24-hour peak horizon, three-hour selection, Central time independent of browser timezone, empty forecast resets window and peak');
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
