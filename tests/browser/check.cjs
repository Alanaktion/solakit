/* Optional browser QA. Install Playwright outside the app; see README.md. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const base = 'http://127.0.0.1:18382';
const output = process.env.SOLA_BROWSER_ARTIFACTS || '/private/tmp/solakit-browser-artifacts';
const phase = process.argv[2] || '12';

async function noOverflow(page) {
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
    `Page overflows: ${page.url()}`);
}

async function operations(page) {
  await page.goto(base + '/operations');
  await page.getByRole('heading', {name: 'Operations', exact: true}).waitFor();
  const row = page.locator('tr').filter({hasText: 'browser-job'});
  assert.match(await row.innerText(), /America\/Denver/);
  await row.getByRole('button', {name: 'Pause', exact: true}).click();
  await row.getByRole('button', {name: 'Resume', exact: true}).waitFor();
  assert.match(await row.innerText(), /paused/);
  await row.getByRole('button', {name: 'Resume', exact: true}).click();
  await row.getByRole('button', {name: 'Pause', exact: true}).waitFor();
  assert.match(await row.innerText(), /not paused/);
  assert(await page.locator('tr').filter({hasText: 'disabled-job'})
    .getByRole('button', {name: 'Run now'}).isDisabled());
  assert.match(await page.locator('tr').filter({hasText: 'disabled-channel'}).innerText(),
    /disabled/);
  const timestamp = row.locator('time');
  assert.match(await timestamp.getAttribute('title'), /UTC|[+-]|Z/);
  assert.match(await timestamp.innerText(), /GMT|BST/);
  await noOverflow(page);
  if (page.viewportSize().width < 640) {
    const scroller = page.locator('.table-scroll').first();
    assert(await scroller.evaluate(el => el.scrollWidth > el.clientWidth));
    await scroller.evaluate(el => { el.scrollLeft = el.scrollWidth; });
    assert(await scroller.evaluate(el => el.scrollLeft > 0));
    await scroller.evaluate(el => { el.scrollLeft = 0; });
  }
  await page.screenshot({path: path.join(output, `12-${page.qaLabel}.png`), fullPage: true});
  await row.getByRole('button', {name: 'Run now'}).click();
  await page.waitForURL('**/runs/*');
  assert.match(await page.locator('.intro').innerText(), /completed/);
}

async function jobManagement(page) {
  const id = `managed-${page.qaLabel}-${Date.now()}`;
  await page.goto(base + '/operations');
  await page.getByRole('link', {name: 'New job', exact: true}).click();
  await page.getByLabel('Job ID', {exact: true}).fill(id);
  await page.getByLabel('Schedule (cron)', {exact: true}).fill('bad');
  await page.getByLabel('Task', {exact: true}).fill('Reflect on {date}');
  await page.getByRole('button', {name: 'Create job', exact: true}).click();
  await page.getByRole('alert').waitFor();
  assert.equal(await page.getByLabel('Task', {exact: true}).inputValue(), 'Reflect on {date}');
  await page.getByLabel('Schedule (cron)', {exact: true}).fill('0 19 * * *');
  await page.getByLabel('Timezone', {exact: true}).fill('America/Denver');
  await page.getByLabel('Enable scheduled runs', {exact: true}).check();
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `jobs-form-${page.qaLabel}.png`), fullPage: true});
  await page.getByRole('button', {name: 'Create job', exact: true}).click();
  await page.waitForURL(base + '/operations');
  let row = page.locator('tr').filter({hasText: id});
  await row.getByRole('link', {name: 'Edit', exact: true}).click();
  assert.equal(await page.getByLabel('Job ID', {exact: true}).inputValue(), id);
  assert(await page.getByLabel('Job ID', {exact: true}).getAttribute('readonly') !== null);
  await page.getByLabel('Task', {exact: true}).fill('Updated job <literal> {date}');
  await page.getByLabel('Schedule (cron)', {exact: true}).fill('0 20 * * *');
  await page.getByRole('button', {name: 'Save changes', exact: true}).click();
  await page.waitForURL(base + '/operations');
  row = page.locator('tr').filter({hasText: id});
  assert.match(await row.innerText(), /0 20/);
  await row.getByRole('button', {name: 'Run now', exact: true}).click();
  await page.waitForURL('**/runs/*');
  assert.match(await page.locator('.intro').innerText(), /completed/);
  await page.goto(base + '/operations');
  row = page.locator('tr').filter({hasText: id});
  await row.getByRole('button', {name: 'Pause', exact: true}).click();
  await row.getByRole('button', {name: 'Resume', exact: true}).waitFor();
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `jobs-list-${page.qaLabel}.png`), fullPage: true});
  await row.getByRole('link', {name: 'Delete', exact: true}).click();
  await page.getByRole('heading', {name: `Delete job ${id}?`, exact: true}).waitFor();
  await noOverflow(page);
  await page.getByRole('button', {name: 'Delete job', exact: true}).click();
  await page.waitForURL(base + '/operations');
  assert.equal(await page.locator('tr').filter({hasText: id}).count(), 0);
  assert.equal((await page.request.get(`${base}/api/jobs/${id}`)).status(), 404);
}

async function oneTimeJobs(page) {
  const id = `once-${page.qaLabel}-${Date.now()}`;
  await page.goto(base + '/operations/jobs/new');
  await page.getByLabel('Job ID', {exact: true}).fill(id);
  await page.getByLabel('Schedule type', {exact: true}).selectOption('once');
  assert(await page.getByLabel('Run once at', {exact: true}).isVisible());
  assert(await page.getByLabel('Schedule (cron)', {exact: true}).isDisabled());
  await page.getByLabel('Run once at', {exact: true}).fill('2099-01-02T09:00');
  await page.getByLabel('Timezone', {exact: true}).fill('America/Denver');
  await page.getByLabel('Task', {exact: true}).fill('Check on this tomorrow morning.');
  await page.getByLabel('Enable scheduled runs', {exact: true}).check();
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `once-form-${page.qaLabel}.png`), fullPage: true});
  await page.getByRole('button', {name: 'Create job', exact: true}).click();
  await page.waitForURL(base + '/operations');
  let row = page.locator('tr').filter({hasText: id});
  assert.match(await row.innerText(), /One time/);
  await row.getByRole('button', {name: 'Run now', exact: true}).click();
  await page.waitForURL('**/runs/*');
  await page.goto(base + '/operations');
  row = page.locator('tr').filter({hasText: id});
  assert.match(await row.innerText(), /attempted once/);
  assert(await row.getByRole('button', {name: 'Run now', exact: true}).isDisabled());
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `once-finished-${page.qaLabel}.png`), fullPage: true});
  await row.getByRole('link', {name: 'Edit', exact: true}).click();
  assert.equal(await page.getByLabel('Schedule type', {exact: true}).inputValue(), 'once');
  await page.getByLabel('Task', {exact: true}).fill('Only edit the task.');
  await page.getByRole('button', {name: 'Save changes', exact: true}).click();
  await page.waitForURL(base + '/operations');
  let job = await (await page.request.get(`${base}/api/jobs/${id}`)).json();
  assert.equal(job.consumed, true);
  assert.equal(job.next_fire, null);
  assert.equal((await page.request.post(`${base}/api/jobs/${id}/run?wait=false`)).status(), 409);
  await page.locator('tr').filter({hasText: id}).getByRole('link', {name: 'Edit', exact: true}).click();
  await page.getByLabel('Run once at', {exact: true}).fill('2099-01-03T09:00');
  await page.getByRole('button', {name: 'Save changes', exact: true}).click();
  await page.waitForURL(base + '/operations');
  job = await (await page.request.get(`${base}/api/jobs/${id}`)).json();
  assert.equal(job.consumed, false);
  assert.equal(job.run_at, '2099-01-03T16:00:00+00:00');
  await page.request.delete(`${base}/api/jobs/${id}`);
}

async function vision(page) {
  const response = await page.request.post(base + '/qa/vision');
  assert.equal(response.status(), 200);
  const fixture = await response.json();
  assert.equal(fixture.status, 'completed');
  await page.goto(`${base}/sessions/${fixture.session}`);
  const picture = page.locator('.message-image');
  await picture.waitFor();
  await page.waitForFunction(() => document.querySelector('.message-image')?.naturalWidth === 1024);
  assert((await picture.boundingBox()).width <= Math.min(512, page.viewportSize().width));
  assert.equal((await page.request.get(`${base}/api/media/${fixture.image}`)).status(), 200);
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `vision-${page.qaLabel}.png`), fullPage: true});
  assert.equal((await page.request.post(`${base}/api/sessions/${fixture.session}/archive`)).status(), 200);
  await page.goto(`${base}/sessions/${fixture.session}/archives/1`);
  await page.locator('.message-image').waitFor();
  assert.equal(await page.locator('#composer').count(), 0);
  await noOverflow(page);
  await page.request.post(base + '/logout');
  assert.equal((await page.request.get(`${base}/api/media/${fixture.image}`)).status(), 401);
}

async function memoryDirection(page) {
  const id = await createSession(page, 'memory-notes', 'shared');
  const run = await (await page.request.post(`${base}/api/sessions/${id}/messages`, {
    data: {message: 'remember-browser'},
  })).json();
  assert.equal(run.status, 'completed');
  const evidence = await (await page.request.get(`${base}/api/runs/${run.id}`)).json();
  assert.equal(evidence.memory_notes.length, 1);
  assert.equal(evidence.memory_notes[0].text, 'Browser exact <note> & retained wording.');
  assert.equal(JSON.parse(evidence.tools[0].result.content).status, 'staged');
  await page.goto(`${base}/runs/${run.id}`);
  await page.getByText('Staged memory notes', {exact: true}).click();
  const notes = page.locator('details').filter({has: page.getByText('Staged memory notes', {exact: true})});
  assert.match(await notes.innerText(), /retained wording/);
  assert.equal(await notes.locator('note').count(), 0);
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `memory-notes-${page.qaLabel}.png`), fullPage: true});
  const skipped = await (await page.request.post(`${base}/api/sessions/${id}/messages`, {
    data: {message: 'thanks'},
  })).json();
  assert.equal(skipped.recall_status, 'skipped_trivial');
  await page.goto(`${base}/runs/${skipped.id}`);
  assert.match(await page.locator('.evidence-facts').innerText(), /skipped_trivial/);
  await noOverflow(page);
}

async function sessionRename(page) {
  const fixture = await (await page.request.post(base + '/qa/session-lifecycle')).json();
  const id = fixture.ids[0];
  await page.request.post(`${base}/api/sessions/${id}/lifecycle`, {data: {archived: true}});
  await page.goto(`${base}/sessions/${id}`);
  await page.getByRole('link', {name: 'Rename session', exact: true}).click();
  await page.getByLabel('Session name', {exact: true}).fill('<Moonlight & notes>');
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `rename-form-${page.qaLabel}.png`), fullPage: true});
  await page.getByRole('button', {name: 'Save name', exact: true}).click();
  await page.waitForURL(`**/sessions/${id}`);
  assert.equal(await page.locator('.chat-title h1').innerText(), '<Moonlight & notes>');
  assert.match(await page.locator('#conversation').innerText(), /searchable proof/);
  assert.equal(await page.locator('#composer').count(), 0);
  await noOverflow(page);
  await page.goto(`${base}/search?q=${encodeURIComponent('<Moonlight & notes>')}`);
  assert(await page.locator(`.search-result a[href="/sessions/${id}"]`).count());
}

async function sessionSearch(page) {
  const fixture = await (await page.request.post(base + '/qa/session-lifecycle')).json();
  const id = fixture.ids[0];
  assert.equal((await page.request.post(`${base}/api/sessions/${id}/archive`)).status(), 200);
  assert.equal((await page.request.post(`${base}/api/sessions/${id}/lifecycle`,
    {data: {archived: true}})).status(), 200);
  await page.goto(base + '/search');
  await page.getByLabel('Search query', {exact: true}).fill(fixture.prefix);
  await page.getByRole('button', {name: 'Search', exact: true}).click();
  assert.equal(await page.locator('.search-result').count(), 50);
  assert.match(await page.locator('.search-results').innerText(), /Archived/);
  await page.getByRole('link', {name: 'Next →', exact: true}).click();
  assert.equal(await page.locator('.search-result').count(), 5);
  await page.getByLabel('Search in', {exact: true}).selectOption('text');
  await page.getByLabel('Search query', {exact: true}).fill(`proof ${id}`);
  await page.getByRole('button', {name: 'Search', exact: true}).click();
  assert.equal(await page.locator('.search-result').count(), 1);
  assert.match(await page.locator('.search-result').innerText(), /Archive 1/);
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `search-${page.qaLabel}.png`), fullPage: true});
  await page.locator('.search-result a').click();
  await page.waitForURL(`**/sessions/${id}/archives/1`);
  assert.match(await page.locator('#conversation').innerText(), /searchable proof/);
  assert.equal(await page.locator('#composer').count(), 0);
}

async function sessionLifecycle(page) {
  const fixture = await (await page.request.post(base + '/qa/session-lifecycle')).json();
  const root = `${base}/?q=${fixture.prefix}`;
  await page.goto(root);
  assert.equal(await page.locator('.managed-session').count(), 50);
  await page.getByRole('link', {name: 'Next →', exact: true}).click();
  assert.equal(await page.locator('.managed-session').count(), 5);
  await noOverflow(page);
  await page.goto(`${base}/sessions/cleanup/preview?days=90&q=${fixture.prefix}`);
  await page.locator(`input[name="session_ids"][value="${fixture.ids[0]}"]`).uncheck();
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `lifecycle-preview-${page.qaLabel}.png`), fullPage: true});
  await page.getByRole('button', {name: 'Archive selected sessions', exact: true}).click();
  await page.getByRole('heading', {name: 'Archived 54 sessions', exact: true}).waitFor();
  let active = await (await page.request.get(`${base}/api/sessions?q=${fixture.prefix}`)).json();
  assert.deepEqual(active.map(s => s.id), [fixture.ids[0]]);
  const archive = await page.context().newPage();
  await archive.goto(`${base}/sessions/${fixture.ids[54]}`);
  assert.equal(await archive.locator('#composer').count(), 0);
  await archive.getByRole('button', {name: 'Restore session', exact: true}).waitFor();
  assert.match(await archive.locator('#conversation').innerText(), /Lifecycle searchable proof/);
  await noOverflow(archive);
  const hits = await (await page.request.get(`${base}/api/search?query=proof&session_id=${fixture.ids[54]}`)).json();
  assert.equal(hits[0].session_id, fixture.ids[54]);
  await archive.getByRole('button', {name: 'Restore session', exact: true}).click();
  await archive.locator('#composer').waitFor();
  await archive.close();
  await page.getByRole('button', {name: 'Undo this batch', exact: true}).click();
  await page.getByRole('heading', {name: 'Restored 53 sessions', exact: true}).waitFor();
  const catalog = await (await page.request.get(`${base}/api/sessions?q=${fixture.prefix}&page=true`)).json();
  assert.equal(catalog.total, 55);
  await page.goto(root);
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `lifecycle-list-${page.qaLabel}.png`), fullPage: true});
}

async function createSession(page, prefix, scope = 'ephemeral') {
  const id = `${prefix}-${page.qaLabel}-${Date.now()}`;
  await page.goto(base + '/?new=1');
  await page.getByLabel('Session ID', {exact: true}).fill(id);
  await page.getByLabel('Memory', {exact: true}).selectOption(scope);
  await page.getByRole('button', {name: 'Create session'}).click();
  await page.waitForURL(`**/sessions/${id}`);
  return id;
}

async function foundation(page) {
  const id = await createSession(page, 'keyboard');
  const input = page.locator('#message');
  await input.focus();
  await input.fill('First line');
  await input.press('Shift+Enter');
  assert.equal(await input.inputValue(), 'First line\n');
  await input.fill('IME draft');
  await input.evaluate(el => {
    el.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
    el.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Enter', code: 'Enter', isComposing: true, bubbles: true, cancelable: true,
    }));
    el.dispatchEvent(new CompositionEvent('compositionend', {bubbles: true}));
  });
  const before = await page.request.get(`${base}/api/sessions/${id}`);
  assert.equal((await before.json()).messages.length, 0);
  await input.fill('Browser keyboard acceptance');
  await input.press('Enter');
  await page.getByText('Browser reply.', {exact: true}).waitFor();
  await page.waitForFunction(() => !document.querySelector('#message').disabled);
  assert.equal(await input.inputValue(), '');
  await input.focus();
  await page.keyboard.press('Tab');
  assert(await page.getByRole('button', {name: 'Send message'}).evaluate(el => el === document.activeElement));
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `10-chat-${page.qaLabel}.png`), fullPage: true});
  await input.fill('hold-browser');
  await input.press('Enter');
  await page.waitForFunction(() => document.querySelector('#message').disabled);
  await page.locator('#composer').evaluate(el => el.requestSubmit());
  const runs = await (await page.request.get(base + '/api/runs')).json();
  const active = runs.filter(r => r.session === id && ['queued', 'running'].includes(r.status));
  assert.equal(active.length, 1);
  assert.equal((await page.request.post(`${base}/api/runs/${active[0].id}/cancel`)).status(), 200);
  await page.waitForFunction(() => !document.querySelector('#message').disabled);
  if (page.viewportSize().width < 600) {
    await page.getByRole('link', {name: 'All sessions', exact: true}).click();
    await page.waitForURL(base + '/');
  } else {
    await page.getByRole('link', {name: 'Manage sessions'}).click();
    await page.waitForURL(base + '/');
  }
  await page.getByRole('link', {name: 'Tools', exact: true}).click();
  await page.getByRole('heading', {name: 'Tools', exact: true}).waitFor();
  await noOverflow(page);
}

async function evidence(page) {
  const id = await createSession(page, 'evidence');
  await page.goto(`${base}/prompt?profile=interactive&session=${id}&origin=api&at=2026-11-04T19%3A00%3A00Z`);
  await page.getByRole('heading', {name: 'Assembled prompt'}).waitFor();
  const api = await (await page.request.get(`${base}/api/prompt?profile=interactive&session=${id}&origin=api&at=2026-11-04T19%3A00%3A00Z`)).json();
  assert.equal(await page.locator('.digest').innerText(), api.sha256);
  assert.equal(await page.locator('.evidence-text code').innerText(), api.prompt);
  await page.locator('.schema-details summary').click();
  assert(await page.locator('.schema-details pre').isVisible());
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `11-prompt-${page.qaLabel}.png`), fullPage: true});
  const completed = await (await page.request.post(`${base}/api/sessions/${id}/messages`, {
    data: {message: 'tool-browser'},
  })).json();
  assert.equal(completed.status, 'completed');
  await page.goto(`${base}/runs/${completed.id}`);
  await page.locator('.tool-call summary').click();
  assert(await page.locator('.tool-details').isVisible());
  assert.match(await page.locator('.tool-details').innerText(), /Arguments|Result/);
  await page.getByText('Stored prompt', {exact: true}).click();
  assert.match(await page.locator('.evidence-text').innerText(), /Browser QA persona/);
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `11-run-${page.qaLabel}.png`), fullPage: true});
  const pending = await (await page.request.post(`${base}/api/sessions/${id}/messages?wait=false`, {
    data: {message: 'hold-browser'},
  })).json();
  await page.goto(`${base}/runs/${pending.id}`);
  await page.getByRole('button', {name: 'Cancel run'}).click();
  await page.waitForFunction(() => document.querySelector('.intro')?.textContent.includes('cancelled'));
  assert.equal(await page.getByRole('button', {name: 'Cancel run'}).count(), 0);
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `11-cancel-${page.qaLabel}.png`), fullPage: true});
}

async function compaction(page) {
  const ephemeral = await createSession(page, 'compact-disabled');
  await page.goto(`${base}/sessions/${ephemeral}/compact`);
  assert(await page.getByRole('button', {name: 'Summarize and continue'}).isDisabled());
  assert.match(await page.locator('.notice-error').innerText(), /requires enabled shared/);
  const id = await createSession(page, 'compact', 'shared');
  await page.request.post(`${base}/api/sessions/${id}/messages`, {data: {message: 'Before compaction'}});
  await page.reload();
  await page.getByRole('link', {name: 'Compact conversation', exact: true}).click();
  await page.getByRole('heading', {name: 'Compact this conversation?'}).waitFor();
  assert.match(await page.locator('main').innerText(), /long-term extraction disabled/);
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `21-confirm-${page.qaLabel}.png`), fullPage: true});
  await page.getByRole('button', {name: 'Summarize and continue'}).click();
  await page.waitForURL(`**/sessions/${id}`);
  await page.request.post(`${base}/api/sessions/${id}/messages`, {data: {message: 'After compaction'}});
  await page.reload();
  await page.getByText('Earlier context was summarized', {exact: true}).click();
  const history = await page.locator('#conversation').innerText();
  assert(history.indexOf('Browser scratch conversation summary.') < history.indexOf('After compaction'));
  assert(await page.locator('.context-boundary pre').isVisible());
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `21-summary-${page.qaLabel}.png`), fullPage: true});
  const pending = await (await page.request.post(`${base}/api/sessions/${id}/messages?wait=false`, {data: {message: 'hold-browser'}})).json();
  await page.goto(`${base}/sessions/${id}/compact`);
  await page.getByRole('button', {name: 'Summarize and continue'}).click();
  assert.match(await page.locator('main').innerText(), /queued|running|finish/);
  assert.equal((await page.request.post(`${base}/api/runs/${pending.id}/cancel`)).status(), 200);
}

async function archival(page) {
  const id = await createSession(page, 'archive');
  const response = await page.request.post(`${base}/api/sessions/${id}/messages`, {
    data: {message: 'Preserve this browser archive evidence.'},
  });
  assert.equal(response.status(), 200);
  assert.equal((await response.json()).status, 'completed');
  await page.reload();
  await page.getByRole('link', {name: 'Archive conversation'}).click();
  await page.getByRole('heading', {name: 'Archive this conversation?'}).waitFor();
  assert.match(await page.locator('main').innerText(), /Nothing is deleted/);
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `13-confirm-${page.qaLabel}.png`), fullPage: true});
  await page.getByRole('button', {name: 'Archive and start fresh'}).click();
  await page.waitForURL(`**/sessions/${id}`);
  assert(await page.locator('#composer').isVisible());
  assert(!(await page.locator('#conversation').innerText()).includes('Preserve this'));
  await page.locator('.archive-menu summary').click();
  await page.getByRole('link', {name: 'Archive 1', exact: true}).click();
  await page.waitForURL('**/archives/1');
  assert.equal(await page.locator('#composer').count(), 0);
  assert.match(await page.locator('#conversation').innerText(), /Preserve this browser archive evidence/);
  assert.match(await page.locator('.archive-readonly').innerText(), /read only/);
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `13-archive-${page.qaLabel}.png`), fullPage: true});
  await page.getByRole('link', {name: 'Return to the current conversation'}).click();
  assert(await page.locator('#composer').isVisible());
}

async function signIn(page) {
  await page.goto(base + '/login');
  await page.getByLabel('Operator token').fill('scratch-browser-token');
  await page.getByRole('button', {name: 'Sign in'}).click();
  await page.waitForURL(base + '/');
}

async function sessionRuns(page, id) {
  return (await (await page.request.get(base + '/api/runs')).json()).filter(r => r.session === id);
}

async function send(page, text) {
  await page.locator('#message').fill(text);
  await page.locator('#message').press('Enter');
}

async function settled(page) {
  await page.waitForFunction(() => !document.querySelector('#message')?.disabled);
  assert.equal(await page.locator('.live-preview').count(), 0);
}

async function streaming(page) {
  const id = await createSession(page, 'stream');
  await send(page, 'tool-browser stream-browser');
  await page.locator('.tool-call summary').waitFor();
  await page.locator('.tool-call summary').click();
  await page.locator('.live-preview').waitFor();
  await page.locator('.live-preview strong').waitFor();
  assert.equal(await page.locator('.live-preview strong').innerText(), 'partial');
  assert(await page.locator('.tool-call').evaluate(el => el.open));
  const live = await (await page.request.get(`${base}/api/sessions/${id}`)).json();
  assert(!live.messages.some(m => m.content?.includes('Stream finished.')));
  await page.screenshot({path: path.join(output, `22-preview-${page.qaLabel}.png`), fullPage: true});
  await page.waitForFunction(() => {
    const el = document.querySelector('#conversation');
    return el.scrollHeight > el.clientHeight + 200;
  });
  await page.locator('#conversation').evaluate(el => { el.scrollTop = 0; });
  await settled(page);
  assert(await page.locator('#conversation').evaluate(el => el.scrollTop < 30));
  assert(await page.locator('.tool-call').evaluate(el => el.open));
  assert(await page.locator('#message').evaluate(el => el === document.activeElement));
  const localLink = page.getByRole('link', {name: 'local link', exact: true});
  assert.equal(await localLink.getAttribute('href'), '/tools');
  assert.match(await localLink.getAttribute('rel'), /noopener/);
  assert(await page.locator('.markdown-content pre').last().evaluate(el => el.scrollWidth > el.clientWidth));
  const table = page.locator('.markdown-content table').last();
  assert(await table.evaluate(el => el.scrollWidth > el.clientWidth));
  await table.evaluate(el => { el.scrollLeft = el.scrollWidth; });
  assert(await table.evaluate(el => el.scrollLeft > 0));
  await table.evaluate(el => { el.scrollLeft = 0; });
  await noOverflow(page);
  await page.screenshot({path: path.join(output, `22-finished-${page.qaLabel}.png`), fullPage: true});
  await localLink.click();
  await page.getByRole('heading', {name: 'Tools', exact: true}).waitFor();
  await page.goto(`${base}/sessions/${id}`);

  // Reload observes the accepted run; it never resubmits its POST.
  const beforeReload = (await sessionRuns(page, id)).length;
  await send(page, 'stream-browser reload');
  await page.locator('.live-preview').waitFor();
  await page.reload();
  await page.locator('.live-preview').waitFor();
  await settled(page);
  assert.equal((await sessionRuns(page, id)).length, beforeReload + 1);

  // Block the observer GET while allowing the accepted backend run to finish.
  const events = `${base}/sessions/${id}/events`;
  await page.route(events, route => route.abort('connectionfailed'));
  const beforeDisconnect = (await sessionRuns(page, id)).length;
  await send(page, 'stream-browser disconnect');
  await page.locator('#connection-error').waitFor({state: 'visible'});
  await page.waitForFunction(() => !document.querySelector('#message').disabled);
  await page.unroute(events);
  await page.reload();
  await settled(page);
  assert.equal((await sessionRuns(page, id)).length, beforeDisconnect + 1);

  // Drop the network during an already-open observer stream.
  const offlineId = await createSession(page, 'stream-offline');
  await send(page, 'stream-browser offline');
  await page.locator('.live-preview').waitFor();
  await page.request.post(`${base}/qa/disconnect/${offlineId}`);
  await page.locator('#connection-error').waitFor({state: 'visible'});
  await page.reload();
  await settled(page);
  assert.equal((await sessionRuns(page, offlineId)).length, 1);

  // Both a queued and running turn remain observable, then cancel independently.
  const queueId = await createSession(page, 'stream-queue');
  const first = await (await page.request.post(`${base}/api/sessions/${queueId}/messages?wait=false&stream=true`, {data: {message: 'hold-browser'}})).json();
  const second = await (await page.request.post(`${base}/api/sessions/${queueId}/messages?wait=false&stream=true`, {data: {message: 'stream-browser queued'}})).json();
  await page.reload();
  await page.locator(`[data-run-id="${second.id}"][data-run-status="queued"]`).waitFor();
  assert(await page.locator('#message').isDisabled());
  await page.request.post(`${base}/api/runs/${first.id}/cancel`);
  await page.locator('.live-preview').waitFor();
  await page.request.post(`${base}/api/runs/${second.id}/cancel`);
  await settled(page);
  const cancelled = await sessionRuns(page, queueId);
  assert.equal(cancelled.length, 2);
  assert(cancelled.every(r => r.status === 'cancelled'));

  // A backend stream failure removes previews without saving a partial message.
  const brokenId = await createSession(page, 'stream-broken');
  await send(page, 'break-browser');
  await page.locator('.live-preview').waitFor();
  await settled(page);
  assert.equal((await sessionRuns(page, brokenId))[0].status, 'model_unavailable');
  assert.equal((await (await page.request.get(`${base}/api/sessions/${brokenId}`)).json()).messages.length, 0);

  // Revocation and real expiry are checked against an already-open EventSource.
  for (const mode of ['logout', 'expire']) {
    const authId = await createSession(page, `stream-${mode}`);
    await send(page, 'stream-browser auth');
    await page.locator('.live-preview').waitFor();
    await page.request.post(base + (mode === 'logout' ? '/logout' : '/qa/expire'));
    await page.waitForURL('**/login');
    assert.equal((await page.request.get(`${base}/api/sessions/${authId}`)).status(), 401);
    await signIn(page);
    await page.goto(`${base}/sessions/${authId}`);
    await settled(page);
    assert.equal((await sessionRuns(page, authId)).length, 1);
  }
}

(async () => {
  await fs.mkdir(output, {recursive: true});
  const browser = await chromium.launch({
    executablePath: process.env.SOLA_BROWSER_EXECUTABLE ||
      '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser', headless: true,
  });
  try {
    for (const colorScheme of ['light', 'dark']) {
      for (const viewport of [{width: 1440, height: 1000}, {width: 390, height: 844}]) {
        const context = await browser.newContext({colorScheme, viewport, timezoneId: 'Europe/London'});
        const page = await context.newPage();
        page.qaLabel = `${colorScheme}-${viewport.width}`;
        const errors = [];
        page.on('pageerror', e => errors.push(e.message));
        await page.goto(base + '/login');
        await page.getByLabel('Operator token').fill('scratch-browser-token');
        await page.getByRole('button', {name: 'Sign in'}).click();
        await page.waitForURL(base + '/');
        if (phase === 'memory') await memoryDirection(page);
        else if (phase === 'rename') await sessionRename(page);
        else if (phase === 'search') await sessionSearch(page);
        else if (phase === 'lifecycle') await sessionLifecycle(page);
        else if (phase === 'vision') await vision(page);
        else if (phase === 'once') await oneTimeJobs(page);
        else if (phase === 'jobs') await jobManagement(page);
        else if (phase === '12') await operations(page);
        else if (phase === '13') await archival(page);
        else if (phase === '10') await foundation(page);
        else if (phase === '11') await evidence(page);
        else if (phase === '21') await compaction(page);
        else if (phase === '22') await streaming(page);
        else throw new Error(`Unknown phase ${phase}`);
        assert.deepEqual(errors, []);
        console.log(`PASS phase ${phase}: ${page.qaLabel}`);
        await context.close();
      }
    }
  } finally { await browser.close(); }
})().catch(e => {console.error(e); process.exitCode = 1;});
