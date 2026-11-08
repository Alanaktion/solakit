/* Layout QA against the isolated mock server. No live configuration or services. */
const {chromium, request} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const base = 'http://127.0.0.1:18382';
const output = process.env.SOLA_BROWSER_ARTIFACTS || '/tmp/solakit-layout-qa';
const chatOnly = process.env.SOLA_LAYOUT_ONLY === 'chat';
const themes = (process.env.SOLA_LAYOUT_THEMES || 'light,dark').split(',');
const widths = (process.env.SOLA_LAYOUT_WIDTHS || '320,390,600,601,640,641,768,850,851,1024,1440,2048').split(',').map(Number);

async function verify(page, name) {
  page.verifiedStates = (page.verifiedStates || 0) + 1;
  const issues = await page.evaluate(() => {
    const issues = [];
    const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
    const rect = el => el.getBoundingClientRect();
    if (document.documentElement.scrollWidth > innerWidth + 1) issues.push('document overflow');
    if (document.body.classList.contains('chat-app') && document.documentElement.scrollHeight > innerHeight + 1) issues.push('chat escapes its viewport vertically');
    const controls = [...document.querySelectorAll('button, input:not([type="hidden"]), select, textarea, summary, .primary-button, .secondary-button, .quiet-button')].filter(visible);
    for (const control of controls) {
      const box = rect(control);
      const label = control.id || control.textContent.trim().slice(0, 45) || control.type;
      // Compact tool-call rows are 30px for fine pointers and 44px under (pointer: coarse).
      const compact = control.matches('.tool-call > summary') && !matchMedia('(pointer: coarse)').matches;
      if (!['checkbox', 'radio'].includes(control.type) && !compact && box.height < 43.5) issues.push(`${label}: height ${box.height}`);
      if (!control.closest('.table-scroll, .archive-toolbar, .conversation-list') && (box.left < -1 || box.right > innerWidth + 1)) issues.push(`${label}: outside viewport`);
      if (control.matches('.primary-button, .secondary-button, .quiet-button, summary')) {
        const range = document.createRange(); range.selectNodeContents(control);
        const text = range.getBoundingClientRect();
        if (text.height && Math.abs((text.top + text.bottom - box.top - box.bottom) / 2) > 3) issues.push(`${label}: text not centered`);
      }
    }
    for (const row of document.querySelectorAll('.managed-session .session-row')) {
      const bounds = rect(row);
      for (const cell of row.querySelectorAll('.session-name, .profile-tag, .session-row-facts, time')) {
        const box = rect(cell);
        if (box.right > bounds.right + 1 || box.left < bounds.left - 1) issues.push('session metadata exceeds its record');
      }
    }
    for (const group of document.querySelectorAll('.action-group, .archive-confirm-actions')) {
      const actions = [...group.children].map(el => el.tagName === 'FORM' ? el.querySelector('button') : el).filter(el => el && visible(el) && el.matches('button,a,label'));
      for (let i = 1; i < actions.length; i++) {
        const a = rect(actions[i-1]), b = rect(actions[i]);
        const difference = Math.abs((a.top + a.bottom - b.top - b.bottom) / 2);
        if (Math.abs(a.top - b.top) < 22 && difference > 1.5) issues.push('action row center mismatch');
      }
    }
    for (const form of document.querySelectorAll('.session-filters, .inspector-form, .new-session-form')) {
      const controls = [...form.querySelectorAll('input:not([type="hidden"]),select,button')].filter(visible);
      for (let i = 1; i < controls.length; i++) {
        const a = rect(controls[i-1]), b = rect(controls[i]);
        if (Math.abs(a.top - b.top) < 22 && Math.abs(a.bottom - b.bottom) > 1.5) issues.push('filter input/button alignment mismatch');
      }
    }
    return issues;
  });
  assert.deepEqual(issues, [], `${page.qaLabel}/${name}: ${issues.join(', ')}`);
  await page.screenshot({path:path.join(output, `${page.qaLabel}-${name}.png`), fullPage:true});
}

(async () => {
  await fs.mkdir(output,{recursive:true});
  const api = await request.newContext({baseURL:base});
  await api.post('/login',{form:{token:'scratch-browser-token'}});
  const identity = 'layout-' + Date.now();
  const longTitle = 'A long session title for checking wrapping, labels, actions, and responsive alignment';
  for (const [id, scope] of [[identity,'shared'],[identity+'-history','ephemeral'],[identity+'-readonly','ephemeral']]) {
    assert.equal((await api.post('/api/sessions',{data:{id,memory_scope:scope}})).status(),201);
    await api.post(`/api/sessions/${id}/messages`,{data:{message:'tool-browser layout verification'}});
    await api.patch(`/api/sessions/${id}`,{data:{title:longTitle}});
  }
  await api.post(`/api/sessions/${identity}-history/archive`);
  await api.post(`/api/sessions/${identity}-readonly/lifecycle`,{data:{archived:true}});
  // Keep pagination visible in every viewport instead of depending on prior cases.
  for (let n=0;n<20;n++) await api.post(`/api/sessions/${identity}/messages`,{data:{message:`Layout history ${n}`}});
  const run = (await (await api.get(`/api/runs?session_id=${identity}`)).json()).at(-1);
  const imported = await (await api.post('/qa/session-lifecycle')).json();
  const jobId = identity + '-job';
  assert.equal((await api.post('/api/jobs',{data:{id:jobId,cron:'0 19 * * *',timezone:'America/Denver',profile:'interactive',task:'Layout verification',bind_session:'ephemeral',enabled:false}})).status(),201);
  const browser = await chromium.launch({executablePath:process.env.SOLA_BROWSER_EXECUTABLE || '/usr/bin/chromium', headless:true});
  try {
    for (const colorScheme of themes) for (const width of widths) {
      // A recent tool turn stays in the tail as cancelled pending fixtures accumulate.
      await api.post(`/api/sessions/${identity}/messages`,{data:{message:'tool-browser layout verification'}});
      const context = await browser.newContext({viewport:{width,height:1000},colorScheme,timezoneId:'America/Denver'});
      const page = await context.newPage();
      page.qaLabel = `${colorScheme}-${width}`;
      const errors = [];
      page.on('pageerror',error => errors.push(error.message));
      await page.goto(base+'/login');
      await verify(page,'login');
      await page.getByLabel('Operator token').fill('scratch-browser-token');
      await page.getByRole('button',{name:'Sign in',exact:true}).click();
      const routes = [
        ['sessions','/?new=1'], ['search','/search?mode=text&q=verification'],
        ['prompt',`/prompt?session=${identity}&profile=interactive`], ['runs','/runs'], ['run',`/runs/${run.id}`],
        ['operations','/operations'], ['job-new','/operations/jobs/new'],
        ['job-edit',`/operations/jobs/${jobId}/edit`], ['job-delete',`/operations/jobs/${jobId}/delete`],
        ['tools','/tools'], ['rename',`/sessions/${identity}/rename`],
        ['compact',`/sessions/${identity}/compact`], ['archive',`/sessions/${identity}/archive`],
        ['lifecycle',`/sessions/${identity}/lifecycle/archive`], ['history',`/sessions/${identity}-history/archives/1`],
        ['readonly',`/sessions/${identity}-readonly`], ['cleanup',`/sessions/cleanup/preview?days=90&q=${imported.prefix}`],
        ['error','/sessions/layout-does-not-exist'], ['chat',`/sessions/${identity}`],
      ];
      for (const [name,url] of routes.filter(([name]) => !chatOnly || ['chat','history','readonly'].includes(name))) {
        await page.goto(base+url);
        if (name==='sessions') {
          await page.locator('.session-cleanup').evaluate(el=>el.open=true);
          if (width <= 850) assert(await page.locator('.session-name').first().evaluate(node => node.getBoundingClientRect().width > node.closest('.session-row').clientWidth * .8));
        }
        if (name==='job-new') {
          await page.getByLabel('Schedule type').selectOption('once');
          await verify(page,'job-once');
          await page.getByLabel('Schedule type').selectOption('cron');
        }
        if (name==='rename') {
          const geometry = await page.locator('.edit-form').evaluate(form=>{
            const save=form.querySelector('button').getBoundingClientRect(), cancel=form.querySelector('a').getBoundingClientRect(), hint=form.querySelector('small').getBoundingClientRect();
            return {saveWidth:save.width,saveTop:save.top,cancelTop:cancel.top,hintBottom:hint.bottom};
          });
          assert(geometry.saveWidth < 160);
          assert(Math.abs(geometry.saveTop-geometry.cancelTop)<1);
          assert(geometry.saveTop>=geometry.hintBottom+12);
        }
        if (name==='chat') {
          await page.locator('.tool-call').first().evaluate(el=>el.open=true);
          await page.locator('#message').fill('A long draft that should wrap and grow without overlapping the send button. '.repeat(8));
          await verify(page,'chat-draft');
          await page.locator('.chat-health summary').click();
          await verify(page,'chat-status');
          await page.locator('.chat-health summary').click();
          await page.locator('.archive-menu summary').click();
          assert(await page.locator('.archive-menu').evaluate(node => { const trigger=node.querySelector('summary').getBoundingClientRect(), popup=node.querySelector('nav').getBoundingClientRect(); return popup.top >= trigger.bottom - 1 && popup.left >= 0 && popup.right <= innerWidth; }));
          await verify(page,'chat-history-menu');
          await page.locator('.archive-menu summary').click();
        }
        await verify(page,name);
      }
      if (!chatOnly) {
        await page.goto(base+'/search?mode=text&q=layoutnonexistentphrase');
        await verify(page,'empty-search');
        await page.goto(base+'/?q=layoutnonexistentphrase');
        await verify(page,'empty-sessions');
        await page.goto(`${base}/prompt?session=${identity}&profile=interactive`);
        await page.locator('.schema-details').first().evaluate(node=>node.open=true);
        await verify(page,'prompt-details');
        await page.goto(base+'/tools');
        await page.locator('.tool-card details').first().evaluate(node=>node.open=true);
        await verify(page,'tool-details');
        await page.goto(base+'/operations/jobs/new');
        await page.getByLabel('Job ID',{exact:true}).fill('layout-invalid');
        await page.getByLabel('Schedule (cron)',{exact:true}).fill('invalid');
        await page.getByLabel('Task',{exact:true}).fill('Layout validation state');
        await page.getByRole('button',{name:'Create job',exact:true}).click();
        await page.locator('.notice-error').waitFor();
        await verify(page,'job-error');
        await page.goto(base+'/?new=1');
        await page.locator('#session-id').evaluate(node=>{node.removeAttribute('pattern');node.value='invalid session id';});
        await page.getByRole('button',{name:'Create session',exact:false}).click();
        await page.locator('.notice-error').waitFor();
        await verify(page,'session-error');
      }
      const pending = await (await api.post(`/api/sessions/${identity}/messages?wait=false`, {data:{message:'hold-browser'}})).json();
      try {
        await page.goto(`${base}/sessions/${identity}`);
        await page.waitForFunction(()=>document.getElementById('composer').getAttribute('aria-busy')==='true');
        assert(await page.locator('#message').isEnabled());
        assert(await page.locator('#composer button').isDisabled());
        await verify(page,'chat-pending');
        await page.goto(`${base}/runs/${pending.id}`);
        await page.getByRole('button',{name:'Cancel run'}).waitFor();
        await verify(page,'run-pending');
      } finally { await api.post(`/api/runs/${pending.id}/cancel`); }
      if (!chatOnly) {
        // Validate the rename error layout as well as the normal form.
        await page.goto(`${base}/sessions/${identity}/rename`);
        await page.locator('#session-title').evaluate(el=>{el.removeAttribute('maxlength');el.value='x'.repeat(201);});
        await page.getByRole('button',{name:'Save name'}).click();
        await page.locator('.notice-error').waitFor();
        await verify(page,'rename-error');
        // Exercise the review/result/undo action rows using scratch data only.
        await page.goto(`${base}/?q=${imported.ids[0]}`);
        await page.locator('input[name="session_ids"]').check();
        await page.getByRole('button',{name:'Review selected for archive'}).click();
        await verify(page,'cleanup-review');
        await page.getByRole('button',{name:'Archive selected sessions'}).click();
        await verify(page,'cleanup-result');
        await page.getByRole('button',{name:'Undo this batch'}).click();
        await verify(page,'cleanup-undo');
      }
      assert.deepEqual(errors,[]);
      console.log(`PASS layout ${page.qaLabel}: ${page.verifiedStates} screens/states`);
      await context.close();
    }
  } finally { await browser.close();await api.dispose(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
