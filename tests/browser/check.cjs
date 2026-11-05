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

async function createSession(page, prefix) {
  const id = `${prefix}-${page.qaLabel}-${Date.now()}`;
  await page.goto(base + '/?new=1');
  await page.getByLabel('Session ID', {exact: true}).fill(id);
  await page.getByLabel('Memory', {exact: true}).selectOption('ephemeral');
  await page.getByRole('button', {name: 'Create session'}).click();
  await page.waitForURL(`**/sessions/${id}`);
  return id;
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
        if (phase === '12') await operations(page);
        else if (phase === '13') await archival(page);
        else throw new Error(`Unknown phase ${phase}`);
        assert.deepEqual(errors, []);
        console.log(`PASS phase ${phase}: ${page.qaLabel}`);
        await context.close();
      }
    }
  } finally { await browser.close(); }
})().catch(e => {console.error(e); process.exitCode = 1;});
