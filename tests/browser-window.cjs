const path = require('node:path');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  const browser = await chromium.launch({headless:true,args:['--no-sandbox'],executablePath:process.env.CHROMIUM_EXECUTABLE || undefined});
  try {
    const page = await browser.newPage({timezoneId:'America/Chicago'});
    if (!process.env.TEST_DEPLOYED) await page.route((process.env.BASE_URL || 'http://127.0.0.1:8088/'),route=>route.fulfill({contentType:'text/html',body:fs.readFileSync(path.resolve(__dirname,'../web/index.html'),'utf8')}));
    const start = new Date(); start.setUTCMinutes(0,0,0); start.setUTCHours(start.getUTCHours()+1);
    let forecast = [95,71,82,10,85,85,81,...Array(17).fill(10),100].map((score,i)=>({time:new Date(start.getTime()+i*3600000).toISOString(),bite_score:score,bite_rating:'EPIC'}));
    let best = {start:forecast[4].time,end:new Date(start.getTime()+7*3600000).toISOString(),average_score:84,rating:'EPIC'};
    await page.route('**/api/lakes/*/forecast?*',route=>route.fulfill({json:{forecast,best_window:best}}));
    await page.goto((process.env.BASE_URL || 'http://127.0.0.1:8088/'),{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>document.querySelector('#bestWindowScore')?.textContent==='84/100');
    assert.match(await page.locator('#bestWindowCard').textContent(),/Best 3-hour window/i);
    assert.match(await page.locator('#bestWindowTime').textContent(),/Sun|Mon|Tue|Wed|Thu|Fri|Sat/);
    assert.equal(await page.locator('#peakHourScore').textContent(),'95/100');
    const expected = await page.evaluate(time=>new Date(time).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'}),forecast[0].time);
    assert.ok((await page.locator('#peakHourTime').textContent()).includes(expected));
    forecast=[]; best=null;
    await page.evaluate(()=>loadSelectedLakeData());
    assert.equal(await page.locator('#peakHourTime').textContent(),'Unavailable');
    assert.equal(await page.locator('#peakHourScore').textContent(),'--');
    console.log('PASS: dated window, strongest single hour, same 24-hour horizon, stale peak cleared');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
