/* Automated WCAG checks plus real keyboard/layout checks on the scratch app. */
const {chromium} = require('playwright');
const {default: AxeBuilder} = require('@axe-core/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const base = 'http://127.0.0.1:18382';
const output = process.env.SOLA_BROWSER_ARTIFACTS || '/private/tmp/solakit-accessibility-qa';
(async () => {
  await fs.mkdir(output,{recursive:true});
  const browser = await chromium.launch({executablePath:process.env.SOLA_BROWSER_EXECUTABLE || '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser', headless:true});
  let scans=0;
  const findings=[];
  async function audit(page,label) {
    const result = await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa','wcag22aa','best-practice']).analyze();
    findings.push({label,violations:result.violations});
    await fs.writeFile(`${output}/findings.json`,JSON.stringify(findings,null,2));
    assert.deepEqual(result.violations.map(v=>({rule:v.id,nodes:v.nodes.map(n=>n.target)})),[],label);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,label+' overflow');
    scans++;
  }
  try {
    for (const colorScheme of ['light','dark']) for (const width of [1440,390,320]) {
      const context = await browser.newContext({viewport:{width,height:844},colorScheme,reducedMotion:'reduce'});
      const page=await context.newPage();
      page.setDefaultTimeout(10000);
      const errors=[];
      page.on('pageerror',error=>errors.push(String(error)));
      await page.goto(base+'/login');
      await audit(page,`${colorScheme}-${width}-login`);
      await page.locator('#token').fill('scratch-browser-token');
      await page.getByRole('button',{name:/Sign in/}).click();
      await page.goto(base+'/?new=1');
      await audit(page,`${colorScheme}-${width}-new`);
      const id=`a11y-${colorScheme}-${width}-${Date.now()}`;
      await page.request.post(base+'/api/sessions',{data:{id,source:'web',title:'A thoughtful topic with a long title to check truncation and narrow layouts',default_profile:'interactive',memory_scope:'ephemeral'}});
      await page.request.post(`${base}/api/sessions/${id}/messages`,{data:{message:'Accessibility test'}});
      await page.goto(base+'/sessions/'+id);
      await audit(page,`${colorScheme}-${width}-chat`);
      assert.equal(await page.getByRole('link',{name:'Solakit home'}).count(),1);
      await page.keyboard.press('Tab');
      await page.getByRole('link',{name:'Skip to content'}).focus();
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('#main').evaluate(el=>el===document.activeElement),true);
      // Delay Details to exercise focus preservation during asynchronous refresh.
      await page.route('**/sessions/*/details',async route=>{
        await new Promise(resolve=>setTimeout(resolve,250));
        await route.continue();
      });
      await page.getByRole('button',{name:'Details',exact:true}).click();
      await page.getByRole('button',{name:'Close details'}).focus();
      await page.waitForFunction(()=>!document.getElementById('topic-details').hasAttribute('aria-busy'));
      assert.equal(await page.getByRole('button',{name:'Close details'}).evaluate(el=>el===document.activeElement),true);
      await audit(page,`${colorScheme}-${width}-details`);
      if(width<=900) {
        assert.equal(await page.locator('.conversation-shell').evaluate(el=>el.inert),true);
        await page.keyboard.press('Tab');
        assert.equal(await page.evaluate(()=>document.getElementById('topic-details').contains(document.activeElement)),true);
        await page.setViewportSize({width:1100,height:844});
        await page.waitForFunction(()=>!document.querySelector('.conversation-shell').inert);
        assert.equal(await page.locator('.conversation-shell').evaluate(el=>el.inert),false);
        assert.equal(await page.locator('#topic-details').getAttribute('aria-modal'),null);
        await page.setViewportSize({width,height:844});
        await page.waitForFunction(()=>document.querySelector('.conversation-shell').inert);
      }
      await page.screenshot({path:`${output}/${colorScheme}-${width}-details.png`});
      await page.keyboard.press('Escape');
      assert.equal(await page.getByRole('button',{name:'Details',exact:true}).evaluate(el=>el===document.activeElement),true);
      assert.equal(await page.locator('.site-header').evaluate(el=>el.inert),false);
      if(width<=900) {
        await page.getByRole('button',{name:'Topics',exact:true}).click();
        await audit(page,`${colorScheme}-${width}-topics`);
      }
      await page.locator('#topic-filter').fill('No such topic');
      assert.match(await page.locator('#topic-filter-status').innerText(),/No matching topics/);
      await page.locator('#topic-filter').fill('');
      if(width<=900) {
        await page.setViewportSize({width:1100,height:844});
        await page.waitForFunction(()=>!document.body.classList.contains('panel-modal-open'));
        await page.waitForFunction(()=>!document.querySelector('.conversation-shell').inert);
        assert.equal(await page.locator('.conversation-shell').evaluate(el=>el.inert),false);
        await page.setViewportSize({width,height:844});
      }
      await page.screenshot({path:`${output}/${colorScheme}-${width}-chat.png`});
      for(const section of ['system','jobs','tools','sessions','search','evidence/runs','evidence/prompt']) {
        await page.goto(base+'/settings/'+section);
        await audit(page,`${colorScheme}-${width}-${section}`);
      }
      assert.deepEqual(errors,[]);
      await context.close();
    }
    console.log(`Passed ${scans} axe scans plus keyboard, delayed refresh, resize, reduced-motion and overflow checks. Artifacts: ${output}`);
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
