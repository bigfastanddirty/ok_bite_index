const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox'],executablePath:process.env.CHROMIUM_EXECUTABLE||undefined});
 try{
  const page=await browser.newPage({viewport:{width:1600,height:1100},timezoneId:'America/Los_Angeles'}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  const base=process.env.BASE_URL||'http://127.0.0.1:8088';
  await page.goto(base);await page.waitForFunction(()=>document.querySelector('#cc-lake-select option')&&document.querySelector('#cc-load-status').hidden,{timeout:30000});
  const catalog=await page.request.get(base+'/api/lakes');const lakes=await catalog.json();assert.equal(await page.locator('#cc-lake-select option').count(),lakes.length);
  for(const code of ['ARCA','FOSS','HEFN']){
   await page.locator('#cc-lake-select').selectOption(code);await page.waitForFunction(code=>document.querySelector('#cc-lake-phase').textContent.endsWith(code)&&document.querySelector('#cc-load-status').hidden,code,{timeout:30000});
   const response=await page.request.get(base+'/api/lakes/'+code+'/analysis');const data=await response.json();assert.equal(await page.locator('#cc-score').textContent(),(data.bite_score??'—')+' /100');
   assert((await page.locator('#cc-lake-title').textContent()).includes(data.lake_name));console.log(code+': '+data.bite_rating+' '+data.bite_score+'/100');
  }
  await page.locator('[data-hours="48"]').click();await page.locator('[data-block="3"]').click();assert.equal(await page.locator('.cc-hour').count(),3);
  await page.locator('#cc-open-species').click();assert(await page.locator('#cc-species-dialog').isVisible());await page.keyboard.press('Escape');
  await page.locator('[data-page="trends"]').click();await page.waitForFunction(()=>document.querySelector('#cc-history-note').textContent.includes('hourly buckets'),{timeout:30000});
  await page.locator('[data-days="90"]').click();await page.locator('[data-overlay="surface_pressure_hpa"]').click();assert.equal(await page.locator('.cc-history-legend-item').count(),2);
  await page.goto(base+'/methodology');assert(await page.locator('#cc-methodology-page').isVisible());assert((await page.locator('#cc-methodology-page').textContent()).includes('365.2425'));
  await page.waitForFunction(()=>document.querySelector('#cc-load-status').hidden,{timeout:30000});
  await page.locator('[data-page="overview"]').click();
  if(process.env.SCREENSHOT_PATH)await page.screenshot({path:process.env.SCREENSHOT_PATH,fullPage:true});
  assert.deepEqual(errors,[]);console.log('PASS: all '+lakes.length+' lake choices, live score matching, forecast selection, species popup, 90-day history overlays, direct methodology; no browser exceptions');
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
