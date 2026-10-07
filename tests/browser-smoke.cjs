const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  const browser = await chromium.launch({ headless: true, args:['--no-sandbox'],
    executablePath:process.env.CHROMIUM_EXECUTABLE || undefined });
  try {
    const page=await browser.newPage({viewport:{width:1600,height:1000},timezoneId:'America/Los_Angeles'});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto((process.env.BASE_URL || 'http://127.0.0.1:8088/'),{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>/\/100$/.test(document.querySelector('#biteScore')?.textContent),{timeout:30000});
    console.log('Live rating:',await page.locator('#biteRating').textContent());
    await page.selectOption('#lakeSelect','FOSS');
    await page.waitForFunction(()=>latestAnalysis?.lake_name==='Foss Lake',{timeout:30000});
    await page.waitForFunction(()=>document.querySelector('#humidityVal')?.textContent.endsWith('%'),{timeout:30000});
    await page.waitForFunction(()=>/AM|PM/.test(document.querySelector('#bestWindowTime')?.textContent),{timeout:30000});
    console.log('Current humidity:',await page.locator('#humidityVal').textContent());
    console.log('Future window in browser timezone:',await page.locator('#bestWindowTime').textContent());
    await page.route('**/api/lakes/FOSS/analysis?*',async route=>{
      const response=await route.fetch();const data=await response.json();
      Object.assign(data,{bite_score:null,bite_rating:'UNAVAILABLE',data_status:'unavailable',species_ranking:[],analysis_commentary:'Fresh inputs unavailable.'});
      await route.fulfill({response,json:data});
    });
    await page.route('**/api/lakes/FOSS/forecast?*',async route=>route.fulfill({json:{forecast:[],best_window:null}}));
    await page.evaluate(()=>loadSelectedLakeData());
    if((await page.locator('#biteRating').textContent())!=='DATA UNAVAILABLE') throw Error('Unknown data rendered as a bite rating');
    if((await page.locator('#biteScore').textContent())!=='--') throw Error('Unknown data rendered as zero');
    if((await page.locator('#bestWindowTime').textContent())!=='Unavailable') throw Error('Old best window survived missing forecast');
    await page.unroute('**/api/lakes/FOSS/analysis?*');
    let calls=0;
    await page.route('**/api/lakes/FOSS/analysis?*',async route=>{
      const first=++calls===1;
      const response=await route.fetch();const data=await response.json();
      Object.assign(data,{bite_score:first?99:null,bite_rating:first?'EPIC':'UNAVAILABLE',species_ranking:[]});
      if(first) await new Promise(resolve=>setTimeout(resolve,500));
      await route.fulfill({response,json:data});
    });
    await page.evaluate(()=>Promise.all([loadSelectedLakeData(),loadSelectedLakeData()]));
    if((await page.locator('#biteScore').textContent())!=='--') throw Error('Older refresh overwrote newer response');
    if(errors.length) throw Error(errors.join('\n'));
    console.log('PASS: live page, non-Central timezone, unknown-score state, stale-window reset; no JavaScript exceptions');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
