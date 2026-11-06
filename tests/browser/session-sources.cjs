const {chromium, request} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
(async () => {
 const base='http://127.0.0.1:18382';
 const output=process.env.SOLA_BROWSER_ARTIFACTS || '/tmp/solakit-session-source-qa';
 await fs.mkdir(output,{recursive:true});
 const api=await request.newContext({baseURL:base,extraHTTPHeaders:{Authorization:'Bearer scratch-browser-token'}});
 for (const [id,source] of [['qa-cron-source','cron'],['qa-web-source','web']]) {
  const response=await api.post('/api/sessions',{data:{id,source}});
  assert.equal(response.status(),201);
 }
 const browser=await chromium.launch({headless:true, ...(process.env.SOLA_CHROMIUM_PATH ? {executablePath:process.env.SOLA_CHROMIUM_PATH} : {})});
 try {
  for(const width of [390,1440]) for(const colorScheme of ['light','dark']) {
   const page=await browser.newPage({viewport:{width,height:900},colorScheme});
   await page.goto(base+'/login');
   await page.locator('input[name=token]').fill('scratch-browser-token');
   await page.locator('button[type=submit]').click();
   await page.waitForURL(base+'/');
   assert.equal(await page.locator('#session-source').inputValue(),'cron');
   assert.equal(await page.locator('a.session-row[href="/sessions/qa-cron-source"]').count(),0);
   assert.equal(await page.locator('a.session-row[href="/sessions/qa-web-source"]').count(),1);
   await page.selectOption('#session-source','');
   await page.getByRole('button',{name:'Filter sessions',exact:true}).click();
   await page.waitForURL(/exclude_source=/);
   assert.equal(await page.locator('a.session-row[href="/sessions/qa-cron-source"]').count(),1);
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
   await page.screenshot({path:path.join(output,`session-source-${width}-${colorScheme}.png`),fullPage:true});
   await page.close();
  }
  console.log('Cron filter and no overflow verified at 390/1440px in light/dark Chromium.');
 } finally {await browser.close();await api.dispose();}
})().catch(error=>{console.error(error);process.exitCode=1});
