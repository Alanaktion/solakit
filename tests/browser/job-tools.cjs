/* Job tool policy UI QA on the isolated mock fixture; no external services. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const base = 'http://127.0.0.1:18382';
const output = process.env.SOLA_BROWSER_ARTIFACTS || '/tmp/solakit-job-tools';
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
      const id='tool-job-'+Date.now();
      const saved=async()=> (await (await page.request.get(base+'/api/jobs/'+id)).json());
      await page.goto(base+'/operations/jobs/new');
      await page.getByLabel('Job ID',{exact:true}).fill(id);
      await page.getByLabel('Schedule (cron)',{exact:true}).fill('0 0 1 1 *');
      await page.getByLabel('Task',{exact:true}).fill('Browser job tools');
      await page.getByLabel('Enable scheduled runs',{exact:true}).check();
      assert.equal(await page.getByLabel('Tool access',{exact:true}).inputValue(),'inherit');
      assert(await page.locator('#job-tool-options').isHidden());
      await page.getByLabel('Tool access',{exact:true}).selectOption('selected');
      await page.getByLabel('memory.remember',{exact:true}).check();
      await page.getByLabel('runtime.status',{exact:true}).check();
      await capture(page,'selected');
      await page.getByRole('button',{name:'Create job',exact:true}).click();
      await page.waitForURL(base+'/operations');
      assert.deepEqual((await saved()).tools.sort(),['memory.remember','runtime.status']);
      await page.goto(`${base}/operations/jobs/${id}/edit`);
      assert(await page.getByLabel('memory.remember',{exact:true}).isChecked());
      await page.getByLabel('Profile',{exact:true}).selectOption('status-only');
      assert(await page.locator('[data-job-tool]').filter({hasText:'memory.remember'}).locator('[data-tool-warning]').isVisible());
      await page.getByRole('button',{name:'Save changes',exact:true}).click();
      await page.getByRole('alert').waitFor();
      assert.match(await page.getByRole('alert').innerText(),/Job tools not granted by profile status-only: memory.remember/);
      assert.deepEqual((await saved()).tools.sort(),['memory.remember','runtime.status']);
      await capture(page,'invalid');
      await page.getByLabel('memory.remember',{exact:true}).uncheck();
      await page.getByRole('button',{name:'Save changes',exact:true}).click();
      await page.waitForURL(base+'/operations');
      assert.deepEqual((await saved()).tools,['runtime.status']);
      // Profile changes later retain the job definition, warn, and shrink exposure.
      try {
        await page.request.post(base+'/qa/tool-grants?trim=true');
        await page.reload();
        const row=page.locator('tr').filter({hasText:id});
        assert.match(await row.innerText(),/No longer granted by profile \(excluded\): runtime.status/);
        await capture(page,'drift');
        const run=await (await page.request.post(`${base}/api/jobs/${id}/run`)).json();
        assert.deepEqual(run.effective_tools,[]);
        assert.deepEqual(run.tool_schemas,[]);
        await page.goto(`${base}/runs/${run.id}`);
        await page.getByRole('link',{name:'Inspect recorded prompt and tool schemas'}).click();
        await page.waitForURL('**/prompt?run_id=*');
        assert.match(await page.locator('main').innerText(),/Recorded prompt and tool schemas/);
        assert.equal(await page.locator('.schema-details').count(),0);
        await capture(page,'recorded');
      } finally { await page.request.post(base+'/qa/tool-grants?trim=false'); }
      // All three persisted states round-trip through the same form.
      await page.goto(`${base}/operations/jobs/${id}/edit`);
      await page.getByLabel('Tool access',{exact:true}).selectOption('none');
      await page.getByRole('button',{name:'Save changes',exact:true}).click();
      await page.waitForURL(base+'/operations');assert.deepEqual((await saved()).tools,[]);
      await page.goto(`${base}/operations/jobs/${id}/edit`);
      assert.equal(await page.getByLabel('Tool access',{exact:true}).inputValue(),'none');
      await page.getByLabel('Tool access',{exact:true}).selectOption('inherit');
      await page.getByRole('button',{name:'Save changes',exact:true}).click();
      await page.waitForURL(base+'/operations');assert.equal((await saved()).tools,null);
      await page.request.delete(base+'/api/jobs/'+id);
      assert.deepEqual(errors,[]);
      console.log('PASS job tools: '+page.qaLabel);await context.close();
    }
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
