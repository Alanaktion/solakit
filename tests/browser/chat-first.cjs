/* Chat-first UI acceptance against the isolated fixture, never live services. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const base = 'http://127.0.0.1:18382';
const output = '/tmp/solakit-chat-first-qa';
(async () => {
  await fs.mkdir(output, {recursive:true});
  const browser = await chromium.launch({executablePath:process.env.SOLA_BROWSER_EXECUTABLE || '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser', headless:true});
  let checks = 0;
  try {
    for (const colorScheme of ['light','dark']) for (const viewport of [{width:1440,height:1000},{width:390,height:844}]) {
      const context = await browser.newContext({viewport,colorScheme});
      const page = await context.newPage();
      page.setDefaultTimeout(10000);
      page.setDefaultNavigationTimeout(10000);
      const errors=[];
      page.on('pageerror', error=>errors.push(String(error)));
      await page.goto(base+'/login');
      await page.locator('#token').fill('scratch-browser-token');
      await page.getByRole('button',{name:/Sign in/}).click();
      const id = `chat-first-${colorScheme}-${viewport.width}-${Date.now()}`;
      let response = await page.request.post(base+'/api/sessions',{data:{id,source:'web',title:'Browser acceptance topic',default_profile:'interactive',memory_scope:'ephemeral'}});
      assert.equal(response.status(),201);
      await page.goto(base+'/');
      await page.waitForURL('**/sessions/'+id);
      await page.locator('#composer').waitFor();
      assert.equal(await page.locator('.session-badges, .chat-health, .archive-toolbar').count(),0);
      assert.equal(await page.locator('#health-indicator').count(),1);
      await page.locator('#health-indicator.health-green').waitFor();
      if (viewport.width===390) {
        await page.getByRole('button',{name:'Topics',exact:true}).click();
        await page.locator('#chat-sidebar.is-open').waitFor();
        assert.equal(await page.locator('#chat-sidebar').getAttribute('aria-modal'),'true');
      }
      await page.locator('#topic-filter').fill('does not match');
      assert.equal(await page.locator('#chat-sidebar .conversation-link:visible').count(),0);
      await page.locator('#topic-filter').fill('Browser acceptance');
      await page.locator(`#chat-sidebar a[href="/sessions/${id}"]`).click();
      await page.getByRole('button',{name:'Details',exact:true}).click();
      await page.locator('#topic-details').waitFor({state:'visible'});
      assert.match(await page.locator('#topic-details').innerText(),/Memory scope/);
      await page.keyboard.press('Escape');
      assert.equal(await page.getByRole('button',{name:'Details',exact:true}).evaluate(el=>el===document.activeElement),true);
      // Send a real fixture-model run, preserving the existing streaming path.
      await page.locator('#message').fill('Chat-first acceptance');
      await page.locator('#message').press('Enter');
      await page.waitForFunction(()=>document.querySelector('[data-run-status="completed"]'));
      await page.evaluate(()=>{globalThis.qaSettled=document.querySelector('#conversation [data-run-id]');});
      await page.locator('#message').fill('stream-browser');
      await page.locator('#message').press('Enter');
      await page.locator('.live-preview').waitFor();
      assert.equal(await page.evaluate(()=>globalThis.qaSettled===document.querySelector('#conversation [data-run-id]')),true);
      await page.waitForFunction(()=>document.querySelectorAll('[data-run-status="completed"]').length===2);
      assert.equal(await page.evaluate(()=>globalThis.qaSettled===document.querySelector('#conversation [data-run-id]')),true);
      assert.equal(await page.locator('#conversation .run-meta:visible').count(),0);
      await page.getByRole('button',{name:'Details',exact:true}).click();
      await page.locator('#topic-details .topic-runs a').first().waitFor();
      await page.screenshot({path:`${output}/${colorScheme}-${viewport.width}-details.png`});
      await page.locator('#topic-details .topic-runs a').first().click();
      await page.waitForURL('**/settings/evidence/runs/*');
      await page.getByRole('link',{name:'Jobs',exact:true}).click();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
      await page.screenshot({path:`${output}/${colorScheme}-${viewport.width}-jobs.png`});
      await page.locator('form[action="/settings/jobs/browser-job/run"] button').click();
      await page.waitForURL('**/settings/evidence/runs/*');
      await page.goto(base+'/sessions/'+id);
      await page.locator('.session-menu summary').click();
      await page.getByRole('link',{name:'Archive topic',exact:true}).click();
      await page.locator('form[action*="/lifecycle/archive"] button').click();
      if (viewport.width===390) await page.getByRole('button',{name:'Topics',exact:true}).click();
      await page.locator('.archived-topics summary').click();
      await page.locator(`.archived-topics a[href="/sessions/${id}"]`).click();
      assert.equal(await page.locator('#composer').count(),0);
      await page.getByRole('button',{name:'Restore session',exact:true}).click();
      await page.locator('#composer').waitFor();
      await page.request.post(base+'/qa/model/false');
      await page.reload();
      await page.locator('#health-indicator.health-red').waitFor();
      await page.request.post(base+'/qa/model/true');
      await page.reload();
      await page.locator('#health-indicator.health-green').waitFor();
      const layout = await page.evaluate(()=>({width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight,composer:document.querySelector('#composer').getBoundingClientRect().bottom,header:document.querySelector('.site-header').getBoundingClientRect().height}));
      assert.ok(layout.width<=viewport.width+1,JSON.stringify(layout));
      assert.ok(layout.height<=viewport.height+1,JSON.stringify(layout));
      assert.ok(layout.composer<=viewport.height,JSON.stringify(layout));
      assert.ok(layout.header<=64,JSON.stringify(layout));
      await page.screenshot({path:`${output}/${colorScheme}-${viewport.width}.png`});
      // Aggregation, seam-crossing history, and archive search anchors stay in chat.
      const fixture = await (await page.request.post(base+'/qa/ux-history?count=44')).json();
      assert.equal((await page.request.post(`${base}/api/sessions/${fixture.session}/archive`)).status(),200);
      await page.request.post(`${base}/api/sessions/${fixture.session}/messages`,{data:{message:'After archive'}});
      await page.goto(`${base}/sessions/${fixture.session}`);
      assert.equal(await page.locator('#conversation [data-run-id]').count(),1);
      await page.locator('.load-earlier').click();
      await page.waitForFunction(()=>document.querySelectorAll('#conversation [data-run-id]').length===21);
      await page.locator('.context-boundary').first().waitFor();
      await page.locator('.load-earlier').click();
      await page.waitForFunction(()=>document.querySelectorAll('#conversation [data-run-id]').length===41);
      await page.goto(`${base}/sessions/${fixture.session}?turn=${fixture.first}&anchor=message-archive-1-1#message-archive-1-1`);
      await page.locator('#message-archive-1-1').waitFor();
      assert.match(await page.locator('#message-archive-1-1').innerText(),/Question 0000/);
      assert.deepEqual(errors,[]);
      checks++;
      await context.close();
    }
    console.log(`Passed ${checks} theme/viewport scenarios; screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
