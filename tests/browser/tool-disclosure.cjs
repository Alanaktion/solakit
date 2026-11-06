/* Conditional disclosure form/inspection QA against the isolated mock fixture. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const base = 'http://127.0.0.1:18382';
const output = process.env.SOLA_BROWSER_ARTIFACTS || '/tmp/solakit-disclosure';
async function capture(page, name) {
  assert(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth+1));
  await page.screenshot({path:path.join(output,`${page.qaLabel}-${name}.png`),fullPage:true});
}
(async()=>{
  await fs.mkdir(output,{recursive:true});
  const browser = await chromium.launch({executablePath:process.env.SOLA_BROWSER_EXECUTABLE||'/usr/bin/chromium',headless:true});
  try {
    for(const colorScheme of ['light','dark']) for(const width of [320,390,768,1440]) {
      const context = await browser.newContext({viewport:{width,height:1000},colorScheme});
      const page = await context.newPage();page.qaLabel=`${colorScheme}-${width}`;
      const errors=[];page.on('pageerror',error=>errors.push(error.message));
      await page.goto(base+'/login');
      await page.getByLabel('Operator token').fill('scratch-browser-token');
      await page.getByRole('button',{name:'Sign in',exact:true}).click();
      const id=`disclosure-${colorScheme}-${width}`;
      const saved=async()=> (await page.request.get(base+'/api/jobs/'+id)).json();
      await page.goto(base+'/operations/jobs/new');
      await page.getByLabel('Job ID',{exact:true}).fill(id);
      await page.getByLabel('Schedule (cron)',{exact:true}).fill('0 0 1 1 *');
      await page.getByLabel('Task',{exact:true}).fill('Browser disclosure verification');
      await page.getByLabel('Enable scheduled runs',{exact:true}).check();
      await page.getByLabel('Tool disclosure',{exact:true}).selectOption('deferred');
      await page.getByLabel('Resident tools',{exact:true}).selectOption('selected');
      await page.getByLabel('Initially loaded names',{exact:true}).fill('runtime.status');
      await capture(page,'form');
      await page.getByRole('button',{name:'Create job',exact:true}).click();
      await page.waitForURL(base+'/operations');
      assert.equal((await saved()).tool_disclosure,'deferred');
      assert.deepEqual((await saved()).tool_resident,['runtime.status']);
      await page.goto(`${base}/operations/jobs/${id}/edit`);
      assert.equal(await page.getByLabel('Tool disclosure',{exact:true}).inputValue(),'deferred');
      assert.equal(await page.getByLabel('Initially loaded names',{exact:true}).inputValue(),'runtime.status');
      await page.getByLabel('Initially loaded names',{exact:true}).fill('tools.search');
      await page.getByRole('button',{name:'Save changes',exact:true}).click();
      await page.getByRole('alert').waitFor();
      assert.match(await page.getByRole('alert').innerText(),/resident tools/);
      await capture(page,'invalid');
      await page.getByLabel('Initially loaded names',{exact:true}).fill('');
      await page.getByRole('button',{name:'Save changes',exact:true}).click();
      await page.waitForURL(base+'/operations');
      assert.deepEqual((await saved()).tool_resident,[]);
      await page.goto(base+'/prompt?job='+id);
      assert.deepEqual(await page.locator('.schema-details summary').allTextContents(),['tools.search','tools.load']);
      assert.match(await page.locator('main').innerText(),/tool-index/);
      await capture(page,'prompt');
      const run=await (await page.request.post(`${base}/api/jobs/${id}/run`)).json();
      assert.equal(run.tool_view.mode,'deferred');
      assert.deepEqual(run.tool_view.resident,[]);
      await page.goto(`${base}/runs/${run.id}`);
      await page.getByRole('link',{name:'Step 1 schemas',exact:true}).click();
      await page.waitForURL('**/prompt?run_id=*&step=1');
      assert.deepEqual(await page.locator('.schema-details summary').allTextContents(),['tools.search','tools.load']);
      await capture(page,'recorded');
      await page.goto(base+'/tools');
      await page.getByText('Disclosure diagnostics',{exact:true}).click();
      await capture(page,'catalog');
      await page.request.delete(base+'/api/jobs/'+id);
      assert.deepEqual(errors,[]);
      console.log('PASS disclosure: '+page.qaLabel);await context.close();
    }
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
