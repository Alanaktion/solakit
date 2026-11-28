// Real Chromium acceptance against scripts/chat_fixture.py; no production credentials.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { setTimeout as delay } from 'node:timers/promises';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.SOLA_PLAYWRIGHT_PATH || 'playwright');
const { default: AxeBuilder } = require(
  require.resolve('@axe-core/playwright', {
    paths: [process.env.SOLA_PLAYWRIGHT_PATH || process.cwd()]
  })
);
const options = Object.fromEntries(
  process.argv
    .slice(2)
    .reduce(
      (pairs, value, i, args) =>
        i % 2 ? pairs : [...pairs, [value.replace(/^--/, ''), args[i + 1]]],
      []
    )
);
const fixture = JSON.parse(await fs.readFile(options.fixture, 'utf8'));
const agentName = fixture.agent_name || 'Sola';
await fs.mkdir(options.output, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  ...(process.env.SOLA_BROWSER ? { executablePath: process.env.SOLA_BROWSER } : {})
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  colorScheme: 'light'
});
// LAN HTTP has getRandomValues but no secure-context randomUUID API.
await context.addInitScript(() =>
  Object.defineProperty(crypto, 'randomUUID', { value: undefined })
);
const page = await context.newPage();
const errors = [],
  report = {
    fixture: '500 synthetic turns',
    browser: browser.version(),
    node: process.version,
    checks: []
  };
page.on('pageerror', (error) => errors.push(error.message));
const check = async (name, action) => {
  await action();
  report.checks.push(name);
  console.log(`passed: ${name}`);
};
const waitFor = async (predicate) => page.waitForFunction(predicate, null, { timeout: 20000 });
const send = async (content) => {
  await page.locator('#draft').fill(content);
  await page.locator('#send').click();
};
const audit = async (name) => {
  const result = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
    .analyze();
  report.accessibility ??= {};
  report.accessibility[name] = {
    violations: result.violations,
    incomplete: result.incomplete.map((rule) => rule.id)
  };
  assert.deepEqual(result.violations, [], `${name}: accessibility violations`);
};
try {
  await page.goto(fixture.origin);
  await check('configured agent name and optional user.css override', async () => {
    await page.locator('#login-dialog[open]').waitFor();
    assert.equal(await page.locator('.brand').innerText(), `🌙 ${agentName}`);
    assert.equal(await page.locator('#login-title').innerText(), `Welcome to ${agentName}`);
    assert.equal(await page.locator('#draft').getAttribute('placeholder'), `Message ${agentName}…`);
    assert.equal(
      await page.getByRole('textbox', { name: `Message ${agentName}`, exact: true }).count(),
      1
    );
    assert.equal(
      await page.locator('#empty-hint').innerText(),
      `Start a topic, then send ${agentName} a message.`
    );
    assert.equal(
      (await page.locator('#jobs-empty').innerText()).trim(),
      `No jobs yet. Create a job when you want ${agentName} to follow up.`
    );
    const cssPath = path.join(path.dirname(options.fixture), 'data', 'user.css');
    const original = await fs.readFile(cssPath).catch((error) => {
      if (error.code !== 'ENOENT') throw error;
      return null;
    });
    const brandColor = () => page.locator('.brand').evaluate((el) => getComputedStyle(el).color);
    const defaultColor = await brandColor();
    try {
      for (const color of ['rgb(12, 34, 56)', 'rgb(65, 43, 21)']) {
        await fs.writeFile(cssPath, `.brand { color: ${color}; }`);
        await page.reload();
        await page.locator('#login-dialog[open]').waitFor();
        assert.equal(await brandColor(), color);
      }
    } finally {
      if (original === null) await fs.unlink(cssPath);
      else await fs.writeFile(cssPath, original);
    }
    await page.reload();
    await page.locator('#login-dialog[open]').waitFor();
    assert.equal(await brandColor(), defaultColor);
  });
  await check(
    'login has an accessible name, description and no automated WCAG violations',
    async () => {
      await page.locator('#login-dialog[open]').waitFor();
      await audit('login');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'password');
      assert(
        (await page.locator('#password').getAttribute('aria-describedby')).includes('login-reason')
      );
    }
  );
  await check('password login and recent 500-turn history', async () => {
    await page.evaluate((id) => sessionStorage.setItem('sola.selected', JSON.stringify(id)), fixture.topic);
    await page.locator('#password').fill('phase11-disposable-fixture');
    await page.locator('#login-form button').click();
    await page.locator('#topic-' + fixture.topic + '[aria-current=page]').waitFor();
    await page.getByText('Settled answer 500.', { exact: false }).first().waitFor();
    await waitFor(() => document.querySelector('#connection').textContent === 'Connected');
    await page.waitForTimeout(400);
    report.initial = await page.evaluate(() => ({
      payloadBytes: performance
        .getEntriesByType('resource')
        .filter((r) => r.name.includes('/api/v1/'))
        .reduce((n, r) => n + r.encodedBodySize, 0),
      turns: document.querySelectorAll('.turn').length,
      messages: document.querySelectorAll('[data-message-id]').length,
      nodes: document.querySelectorAll('*').length
    }));
    assert(report.initial.turns <= 50);
    assert(report.initial.payloadBytes < 512 * 1024);
    assert(report.initial.messages <= 100);
    assert.equal(
      await page.locator('#turns img, #turns script, #turns a[href^="javascript:"]').count(),
      0
    );
    assert.equal(await page.evaluate(() => window.PWNED), undefined);
    report.snapshot = await page.evaluate(async (topic) => {
      const response = await fetch(`/api/v1/sessions/${topic}/events`),
        reader = response.body.getReader();
      let data = '';
      while (!data.includes('\n\n')) {
        const chunk = await reader.read();
        data += new TextDecoder().decode(chunk.value);
      }
      await reader.cancel();
      const snapshot = JSON.parse(
        data
          .split('\n')
          .find((line) => line.startsWith('data: '))
          .slice(6)
      );
      return {
        bytes: new TextEncoder().encode(data).length,
        messages: snapshot.messages.length,
        runs: snapshot.runs.length
      };
    }, fixture.topic);
    assert(report.snapshot.messages <= 50);
    assert(report.snapshot.runs <= 50);
    assert(report.snapshot.bytes < 20000);
  });
  await check(
    'topic title search, pagination, archives and clearing preserve the conversation',
    async () => {
      const tag = String(Date.now());
      const ids = await page.evaluate(async (tag) => {
        const csrf = decodeURIComponent(
          document.cookie
            .split('; ')
            .find((part) => part.startsWith('sola_csrf='))
            .split('=')[1]
        );
        const ids = [];
        for (let i = 0; i < 52; i++) {
          const response = await fetch('/api/v1/sessions', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-CSRF-Token': csrf
            },
            body: JSON.stringify({
              title: `Topic search fixture ${tag} ${i}${i === 51 ? ` Straße & 100%_ ${tag}` : ''}`
            })
          });
          if (!response.ok) throw new Error('Could not create search fixture');
          ids.push((await response.json()).id);
        }
        return ids;
      }, tag);
      const search = page.getByRole('searchbox', {
        name: 'Search topics by title'
      });
      const searchFor = async (query) => {
        const response = page.waitForResponse((response) => {
          const url = new URL(response.url());
          return url.pathname === '/api/v1/sessions' && url.searchParams.get('q') === query;
        });
        await search.fill(query);
        await response;
      };
      const setPins = async (pinned) => page.evaluate(async ({ ids, pinned }) => {
        const csrf = document.cookie.split('; ')
          .find((part) => part.startsWith('sola_csrf=')).split('=')[1];
        for (const id of ids) {
          const response = await fetch(`/api/v1/sessions/${id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
            body: JSON.stringify({ pinned })
          });
          if (!response.ok) throw new Error('Could not update fixture pins');
        }
      }, { ids: ids.slice(50), pinned });
      await setPins(true);
      await page.reload();
      await page.locator('#topic-' + ids[51]).waitFor();
      assert.deepEqual(await page.locator('#topics button').evaluateAll(
        (buttons) => buttons.slice(0, 2).map((button) => button.id)
      ), ids.slice(50).map((id) => 'topic-' + id));
      await searchFor('no matching unpinned topic');
      await waitFor(() => document.querySelectorAll('#topics button').length === 2);
      await page.screenshot({ path: path.join(options.output, 'pinned-topics.png'), fullPage: true });
      await searchFor(`TOPIC SEARCH FIXTURE ${tag}`);
      await waitFor(() => document.querySelectorAll('#topics button').length === 52);
      await page.locator('#more-topics').click();
      await page.locator('#more-topics').waitFor({ state: 'hidden' });
      assert.equal(await page.locator('#topic-' + ids[51]).count(), 1);
      await page.locator('#topic-' + ids[50]).click();
      await page.locator('#topic-edit').click();
      await page.locator('#pinned').uncheck();
      await page.locator('#topic-form button.primary').click();
      await page.locator('#topic-dialog').waitFor({ state: 'hidden' });
      await waitFor(() => document.querySelector('#topics button sola-icon[name=Pin]'));
      assert.equal(await page.locator('#topics button').first().getAttribute('id'), 'topic-' + ids[51]);
      await searchFor('no matching unpinned topic');
      await waitFor(() => document.querySelectorAll('#topics button').length === 1);
      await page.locator('#archived-topics').check();
      await page.getByRole('status').filter({ hasText: 'No matching topics.' }).waitFor();
      await page.locator('#archived-topics').uncheck();
      await page.locator('#topic-' + ids[51]).waitFor();
      await setPins(false);
      await searchFor('');
      await page.locator('#topic-' + fixture.topic).click();
      await page.locator('#draft').fill('Keep this draft during search');
      await searchFor(` strasse & 100%_ ${tag} `);
      await page.locator('#topic-' + ids[51]).waitFor();
      assert.equal(await page.locator('#topics button').count(), 1);
      assert.equal(await page.locator('#draft').inputValue(), 'Keep this draft during search');
      assert.equal(
        await page.locator('#topic-title').textContent(),
        'A long conversation · 500 turns'
      );
      await page.screenshot({
        path: path.join(options.output, 'topic-search-desktop.png'), fullPage: true
      });
      await searchFor(`TOPIC SEARCH FIXTURE ${tag}`);
      await waitFor(() => document.querySelectorAll('#topics button').length === 50);
      await page.locator('#more-topics').click();
      await page.locator('#topic-' + ids[51]).waitFor();
      assert.equal(await page.locator('#topics button').count(), 52);
      assert(await page.locator('#more-topics').isHidden());
      let release;
      const held = new Promise((resolve) => {
        release = resolve;
      });
      const staleRoute = async (route) => {
        const response = await route.fetch();
        await held;
        await route.fulfill({ response });
      };
      const staleURL = /\/api\/v1\/sessions\?.*q=stale-search/;
      await page.route(staleURL, staleRoute);
      const requested = page.waitForRequest(staleURL);
      await search.fill('stale-search');
      await requested;
      await searchFor(`100%_ ${tag}`);
      await page.locator('#topic-' + ids[51]).waitFor();
      const staleResponse = page.waitForResponse(staleURL);
      release();
      await staleResponse;
      await page.unroute(staleURL, staleRoute);
      assert.equal(await page.locator('#topics button').count(), 1);
      await searchFor('no such topic fixture');
      await page.getByRole('status').filter({ hasText: 'No matching topics.' }).waitFor();
      assert.equal(await page.locator('#topics button').count(), 0);
      await page.setViewportSize({ width: 390, height: 844 });
      assert(await search.evaluate((el) => el.getBoundingClientRect().right <= innerWidth));
      await page.screenshot({
        path: path.join(options.output, 'topic-search-mobile.png'), fullPage: true
      });
      await page.setViewportSize({ width: 1440, height: 1000 });
      await page.evaluate(async (ids) => {
        const csrf = decodeURIComponent(
          document.cookie.split('; ').find((part) => part.startsWith('sola_csrf=')).split('=')[1]
        );
        const response = await fetch('/api/v1/sessions/archive', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
          body: JSON.stringify({ session_ids: ids })
        });
        if (!response.ok) throw new Error('Could not archive search fixture');
      }, ids);
      await searchFor(`100%_ ${tag}`);
      await page.getByRole('status').filter({ hasText: 'No matching topics.' }).waitFor();
      await page.locator('#archived-topics').check();
      await page.locator('#topic-' + ids[51]).waitFor();
      assert.equal(await page.locator('#topics button').count(), 1);
      await page.locator('#archived-topics').uncheck();
      await searchFor('');
      await page.locator('#topic-' + fixture.topic + '[aria-current=page]').waitFor();
      assert(await page.locator('#topic-' + fixture.other).isVisible());
      await page.locator('#draft').fill('');
      await audit('topic-search');
    }
  );
  await check('topic manager filters, paged bulk archive and retry after a lost response', async () => {
    const tag = `Bulk archive fixture ${Date.now()}`;
    const ids = await page.evaluate(async (tag) => {
      const { api, uid } = await import('/assets/client.js');
      const ids = [];
      for (let i = 0; i < 102; i++) {
        const topic = await api('/sessions', { method: 'POST', body: { title: `${tag} ${i}` } });
        ids.push(topic.id);
      }
      await api('/runs?wait=5', {
        method: 'POST', headers: { 'Idempotency-Key': uid() },
        body: { session_id: ids.at(-1), content: 'Keep this transcript after archiving.' }
      });
      return ids;
    }, tag);

    const launcher = page.getByRole('button', { name: 'Manage topics' });
    const manager = page.getByRole('dialog', { name: 'Manage topics' });
    const title = manager.getByRole('searchbox', { name: 'Title' });
    const profile = manager.getByRole('combobox', { name: 'Profile' });
    const status = manager.getByRole('combobox', { name: 'Status' });
    const openLatest = manager.getByRole('button', { name: `${tag} 101`, exact: true });
    const selection = (count) => manager.getByRole('status').filter({
      hasText: new RegExp(`^${count} selected\\.`)
    });
    await launcher.focus();
    await page.keyboard.press('Enter');
    await title.waitFor();
    assert(await title.evaluate((input) => input === document.activeElement));
    await title.fill(tag);
    await openLatest.waitFor();
    await profile.selectOption('default');
    const today = await page.evaluate(() => {
      const now = new Date();
      return [now.getFullYear(), now.getMonth() + 1, now.getDate()]
        .map((part) => String(part).padStart(2, '0')).join('-');
    });
    await manager.getByLabel('Created through').fill(today);
    await manager.getByLabel('Last activity from').fill(today);
    await manager.getByRole('status').filter({ hasText: '1 topic loaded.' }).waitFor();
    await page.setViewportSize({ width: 320, height: 760 });
    assert(await manager.evaluate((dialog) => dialog.scrollWidth <= dialog.clientWidth));
    await audit('topic-manager-mobile');
    await page.screenshot({ path: path.join(options.output, 'topic-manager-mobile.png') });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.keyboard.press('Escape');
    await manager.waitFor({ state: 'hidden' });
    assert(await launcher.evaluate((button) => button === document.activeElement));

    await launcher.click();
    await openLatest.click();
    const content = 'Keep this transcript after archiving.';
    await page.locator('#turns').getByText(content, { exact: true }).waitFor();
    const draft = page.locator('#draft');
    await draft.fill('Draft kept through bulk archive');
    await launcher.click();
    await profile.selectOption('');
    await manager.getByRole('status').filter({ hasText: /^50 topics loaded/ }).waitFor();
    const latestCheckbox = manager.getByRole('checkbox', { name: `Select ${tag} 101` });
    await latestCheckbox.focus();
    await page.keyboard.press('Space');
    await selection(1).waitFor();
    await status.selectOption('all');
    await selection(0).waitFor();
    await manager.getByRole('checkbox', { name: 'Select visible active topics' }).check();
    await selection(50).waitFor();
    await manager.getByRole('button', { name: 'Load more topics' }).click();
    await manager.getByRole('status').filter({ hasText: /^100 topics loaded/ }).waitFor();
    await selection(50).waitFor();
    await manager.getByRole('button', { name: 'Select all matching active topics' }).click();
    await selection(102).waitFor();
    await page.screenshot({ path: path.join(options.output, 'topic-manager-selection.png') });

    const submitted = [];
    const archiveRoute = async (route) => {
      submitted.push(route.request().postDataJSON().session_ids);
      const response = await route.fetch();
      assert(response.ok());
      if (submitted.length === 2) await route.abort('failed');
      else await route.fulfill({ response });
    };
    await page.route('**/api/v1/sessions/archive', archiveRoute);
    const archive = manager.getByRole('button', { name: 'Archive selected' });
    let confirmation;
    page.once('dialog', async (dialog) => {
      confirmation = dialog.message();
      await dialog.dismiss();
    });
    await archive.click();
    assert(confirmation.startsWith('Archive 102 topics?'));
    assert.equal(submitted.length, 0);
    const late = await page.evaluate(async (tag) => {
      const { api } = await import('/assets/client.js');
      return api('/sessions', { method: 'POST', body: { title: `${tag} created after selection` } });
    }, tag);
    page.once('dialog', (dialog) => dialog.accept());
    await archive.click();
    await manager.getByRole('alert').filter({ hasText: 'Archive paused.' }).waitFor();
    await manager.getByRole('status').filter({ hasText: '100 of 102 topics confirmed archived.' }).waitFor();
    await manager.getByRole('button', { name: 'Retry archive (2)' }).click();
    await manager.getByRole('status').filter({ hasText: '102 topics archived.' }).waitFor();
    await page.unroute('**/api/v1/sessions/archive', archiveRoute);
    assert.deepEqual(submitted.map((batch) => batch.length), [100, 2, 2]);
    assert.deepEqual(submitted[2], submitted[1]);
    assert.deepEqual(new Set([...submitted[0], ...submitted[1]]), new Set(ids));
    assert(!submitted.flat().includes(late.id));
    await status.selectOption('archived');
    await openLatest.waitFor();
    assert(await latestCheckbox.isDisabled());
    await manager.getByRole('button', { name: 'Close topic manager' }).click();
    await page.locator('#topic-mode').getByText('archived', { exact: false }).waitFor();
    await page.locator('#turns').getByText(content, { exact: true }).waitFor();
    assert.equal(await draft.inputValue(), 'Draft kept through bulk archive');
    assert(await page.locator('#send').isDisabled());
    await page.locator('#topic-' + fixture.topic).click();
    await page.locator('.compaction-summary').first().waitFor();
  });
  await check('Manage topics exports a result and imports ordered files with an idempotent retry', async () => {
    const title = 'Transfer fixture ' + Date.now();
    const transcript = 'Bring this history to the imported topic.';
    const [original, ...earlier] = await page.evaluate(async ({ title, transcript }) => {
      const { api, uid } = await import('/assets/client.js');
      const topic = await api('/sessions', { method: 'POST', body: { title, mode: 'isolated' } });
      await api('/runs?wait=5', {
        method: 'POST', headers: { 'Idempotency-Key': uid() },
        body: { session_id: topic.id, content: transcript }
      });
      const first = await api('/sessions', { method: 'POST', body: { title: `${title} first` } });
      const second = await api('/sessions', { method: 'POST', body: { title: `${title} second` } });
      return [topic.id, first.id, second.id];
    }, { title, transcript });
    await page.setViewportSize({ width: 320, height: 760 });
    await page.getByRole('button', { name: 'Manage topics' }).press('Enter');
    const manager = page.getByRole('dialog', { name: 'Manage topics' });
    await manager.getByRole('combobox', { name: 'Status' }).selectOption('all');
    await manager.getByLabel('Created from').fill('');
    await manager.getByLabel('Created through').fill('');
    await manager.getByLabel('Last activity from').fill('');
    await manager.getByLabel('Last activity through').fill('');
    await manager.getByRole('searchbox', { name: 'Title' }).fill(title);
    const exportButton = manager.getByRole('button', { name: `Export ${title}`, exact: true });
    await exportButton.waitFor();
    await page.route(`**/api/v1/sessions/${original}/export`, (route) => route.fulfill({
      status: 409,
      json: { error: { message: 'Wait for queued and running turns to finish before exporting' } }
    }), { times: 1 });
    await exportButton.press('Enter');
    await manager.getByRole('alert').filter({ hasText: 'Wait for queued and running turns' }).waitFor();

    const downloadEvent = page.waitForEvent('download');
    await exportButton.press('Enter');
    const download = await downloadEvent;
    assert.equal(download.suggestedFilename(), `solakit-session-${original}.zip`);
    const archive = await fs.readFile(await download.path());
    const files = [];
    for (const id of earlier) {
      const result = await page.request.get(new URL(`/api/v1/sessions/${id}/export`, fixture.origin).href);
      assert.equal(result.status(), 200);
      files.push({ name: `solakit-session-${id}.zip`, mimeType: 'application/zip', buffer: await result.body() });
    }
    files.push({ name: download.suggestedFilename(), mimeType: 'application/zip', buffer: archive });
    assert(await manager.isVisible());
    assert.equal(await page.locator('#topics [aria-current=page]').getAttribute('id'),
      'topic-' + fixture.topic);
    await page.screenshot({ path: path.join(options.output, 'topic-manager-transfer.png') });

    const launcher = manager.getByRole('button', { name: 'Import topics', exact: true });
    await launcher.press('Enter');
    const dialog = page.getByRole('dialog', { name: 'Import topics' });
    const file = dialog.getByLabel('Solakit ZIP archives');
    await file.waitFor();
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
    assert(await launcher.evaluate((button) => button === document.activeElement));
    await launcher.press('Enter');
    await file.waitFor();
    assert(await file.evaluate((input) => input === document.activeElement));
    await audit('topic-import-mobile');
    const requests = [];
    const loseSecondReceipt = async (route) => {
      const request = route.request();
      const index = files.findIndex(({ buffer }) => buffer.equals(request.postDataBuffer()));
      const key = request.headers()['idempotency-key'];
      const response = await route.fetch();
      assert.equal(response.status(), 201);
      const { session_id: copied } = await response.json();
      requests.push({ index, key, copied });
      if (requests.length === 2) await route.abort('failed');
      else await route.fulfill({ response });
    };
    await page.route('**/api/v1/sessions/import', loseSecondReceipt);
    await file.setInputFiles(files);
    await dialog.getByRole('button', { name: 'Import topics' }).press('Enter');
    const retry = dialog.getByRole('button', { name: 'Retry import' });
    await retry.waitFor();
    await dialog.getByRole('status').filter({ hasText: '2 of 3 files imported.' }).waitFor();
    const message = await dialog.getByRole('alert').innerText();
    assert(message.includes(files[1].name));
    assert.match(message, /Retry with the same file/);
    await waitFor(() => document.activeElement.id === 'import-submit' && !document.activeElement.disabled);
    await page.screenshot({ path: path.join(options.output, 'topic-import-mobile.png') });
    await retry.press('Enter');
    await dialog.waitFor({ state: 'hidden' });
    await manager.waitFor({ state: 'hidden' });
    await page.unroute('**/api/v1/sessions/import', loseSecondReceipt);
    assert.deepEqual(requests.map(({ index }) => index), [0, 1, 2, 1]);
    assert.equal(new Set(requests.map(({ key }) => key)).size, 3);
    assert.deepEqual(requests[1], requests[3]);
    await page.locator('#turns').getByText(transcript, { exact: true }).waitFor();
    assert.equal(await page.locator('#topic-title').innerText(), title);
    assert.equal(await page.locator('#topics [aria-current=page]').getAttribute('id'),
      'topic-' + requests[2].copied);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'history');
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.locator('#topic-' + fixture.topic).click();
    await page.locator('.compaction-summary').first().waitFor();
  });
  await page.screenshot({
    path: path.join(options.output, 'chat-desktop.png'),
    fullPage: true
  });
  await check(
    'system light/dark themes switch live with readable text and native controls',
    async () => {
      report.themes = {};
      for (const colorScheme of ['dark', 'light']) {
        await page.emulateMedia({ colorScheme });
        const theme = await page.evaluate(() => {
          const rgb = (value) => {
            const context = document.createElement('canvas').getContext('2d');
            context.fillStyle = value;
            context.fillRect(0, 0, 1, 1);
            return [...context.getImageData(0, 0, 1, 1).data].slice(0, 3);
          };
          const luminance = (color) =>
            rgb(color)
              .map((n) => {
                n /= 255;
                return n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4;
              })
              .reduce((sum, n, i) => sum + n * [0.2126, 0.7152, 0.0722][i], 0);
          const contrast = (a, b) =>
            (Math.max(luminance(a), luminance(b)) + 0.05) /
            (Math.min(luminance(a), luminance(b)) + 0.05);
          const root = getComputedStyle(document.documentElement);
          const primary = getComputedStyle(document.querySelector('#send'));
          const muted = getComputedStyle(document.querySelector('#connection')).color;
          const summary = document.querySelector('.compaction-summary');
          const summaryStyle = getComputedStyle(summary);
          return {
            background: root.backgroundColor,
            luminance: luminance(root.backgroundColor),
            scheme: getComputedStyle(document.querySelector('#profile')).colorScheme,
            contrast: {
              text: contrast(root.color, root.backgroundColor),
              muted: contrast(muted, root.backgroundColor),
              sidebar: contrast(
                muted,
                getComputedStyle(document.querySelector('.sidebar')).backgroundColor
              ),
              primary: contrast(primary.color, primary.backgroundColor),
              summary: contrast(summaryStyle.color, summaryStyle.backgroundColor),
              summaryLabel: contrast(
                getComputedStyle(summary.querySelector('.author')).color,
                summaryStyle.backgroundColor
              ),
              error: contrast(
                getComputedStyle(document.querySelector('#topic-error')).color,
                root.backgroundColor
              )
            },
            controlContrast: contrast(
              getComputedStyle(document.querySelector('#profile')).borderTopColor,
              root.backgroundColor
            )
          };
        });
        assert(theme.scheme.includes(colorScheme));
        assert(colorScheme === 'dark' ? theme.luminance < 0.1 : theme.luminance > 0.8);
        for (const [name, ratio] of Object.entries(theme.contrast))
          assert(ratio >= 4.5, `${colorScheme} ${name}: ${ratio}`);
        assert(theme.controlContrast >= 3, `${colorScheme} control: ${theme.controlContrast}`);
        await audit(`chat-${colorScheme}`);
        report.themes[colorScheme] = theme;
        if (colorScheme === 'dark')
          await page.screenshot({
            path: path.join(options.output, 'chat-desktop-dark.png'),
            fullPage: true
          });
      }
    }
  );
  await check('compact desktop controls, keyboard skip link and modal focus', async () => {
    const sendBox = await page.locator('#send').boundingBox();
    assert(sendBox.height >= 24 && sendBox.height < 44);
    await page.locator('.skip').focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'draft');
    const ring = await page.locator('#draft').evaluate((el) => {
      const style = getComputedStyle(el);
      return {
        style: style.outlineStyle,
        width: parseFloat(style.outlineWidth)
      };
    });
    assert(ring.style !== 'none' && ring.width >= 2);
    await page.locator('#settings-open').focus();
    await page.keyboard.press('Enter');
    for (let i = 0; i < 25; i++) {
      await page.keyboard.press('Tab');
      assert(
        await page.evaluate(() =>
          document.querySelector('#settings').contains(document.activeElement) ||
          document.activeElement === document.body
        )
      );
    }
    await audit('settings-light');
    await page.emulateMedia({ colorScheme: 'dark' });
    await audit('settings-dark');
    await page.emulateMedia({ colorScheme: 'light' });
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'settings-open');
    await page.locator('#new-topic').click();
    await audit('topic');
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'new-topic');
  });
  await check('dialog close icons and read-only backdrop dismissal', async () => {
    await page.locator('#settings-open').click();
    await page.mouse.click(4, 4);
    const settingsClose = page.getByRole('button', { name: 'Close Settings', exact: true });
    assert(await settingsClose.locator('sola-icon[name="X"] svg').isVisible());
    await settingsClose.click();
    await page.locator('#settings').waitFor({ state: 'hidden' });

    await page.locator('#new-topic').click();
    await page.locator('#topic-name').fill('Unsaved title');
    await page.mouse.click(4, 4);
    assert(await page.locator('#topic-dialog').isVisible());
    assert.equal(await page.locator('#topic-name').inputValue(), 'Unsaved title');
    await page.locator('#topic-dialog').getByRole('button', { name: 'Cancel' }).click();

    const inspect = page.getByRole('button', { name: 'Inspect evidence', exact: true }).last();
    const evidence = page.locator('#evidence');
    await inspect.click();
    await evidence.waitFor();
    const { x, y } = await evidence.boundingBox();
    await page.mouse.click(x + 4, y + 4);
    await page.mouse.down();
    await page.mouse.move(4, 4);
    await page.mouse.up();
    assert(await evidence.isVisible());
    await page.screenshot({ path: path.join(options.output, 'evidence-close-icon.png') });
    await page.mouse.click(4, 4);
    await evidence.waitFor({ state: 'hidden' });
    assert(await inspect.evaluate((button) => button === document.activeElement));

    await inspect.click();
    const evidenceClose = page.getByRole('button', { name: 'Close evidence', exact: true });
    assert(await evidenceClose.locator('sola-icon[name="X"] svg').isVisible());
    await evidenceClose.click();
    await evidence.waitFor({ state: 'hidden' });
  });
  await check('older history is prepended with a preserved scroll anchor', async () => {
    await page.locator('#history').evaluate((h) => (h.scrollTop = 0));
    const anchor = page.locator('.turn').first();
    const identity = await anchor.getAttribute('data-run-id');
    const before = await anchor.boundingBox();
    report.anchorBefore = {
      box: before,
      ...(await page.locator('#history').evaluate((h) => ({
        top: h.scrollTop,
        height: h.scrollHeight
      })))
    };
    await page.locator('#older').click();
    await page.waitForFunction(
      (count) => document.querySelectorAll('.turn').length > count,
      report.initial.turns
    );
    const after = await page.locator(`[data-run-id="${identity}"]`).boundingBox();
    report.anchorAfter = {
      box: after,
      ...(await page.locator('#history').evaluate((h) => ({
        top: h.scrollTop,
        height: h.scrollHeight
      })))
    };
    assert(Math.abs(before.y - after.y) < 4, `anchor moved ${before.y - after.y}px`);
    report.afterOlder = await page.locator('.turn').count();
    assert.equal(await page.locator('#reply-status').textContent(), '');
    await page.locator('#latest').focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'history');
  });
  await check('hostile strings, tool uncertainty, staged memory and compaction', async () => {
    const body = await page.locator('#turns').textContent();
    assert(body.includes('effect uncertain'));
    assert(body.includes('Memory note staged'));
    assert(body.includes('Memory extraction · ambiguous'));
    assert(body.includes('original turns retained'));
    assert.equal(await page.locator('#turns img, #turns script').count(), 0);
    const summary = page.getByRole('article', { name: 'Context summary', exact: true });
    assert.equal(await summary.count(), 1);
    assert.match(await summary.innerText(), /earlier fixture turns established a working plan/);
    assert.equal(await summary.locator('li').count(), 2);
    const reply = summary.locator('..').getByRole('article', { name: agentName, exact: true });
    assert.match(await reply.innerText(), /Settled answer 500/);
    const safe = await page.evaluate(async () => {
      const { markdown } = await import('/assets/markdown.js');
      const holder = document.createElement('div');
      holder.append(
        markdown(
          '<svg onload=alert(1)>\n\n[bad](data:text/html,evil) [file](file:///etc/passwd) [relative](//evil.invalid) [good](https://example.com)\n\n```html\n<script>alert(1)</script>\n```\n\n- one\n- two'
        )
      );
      return {
        active: holder.querySelectorAll('svg,script,img,iframe').length,
        links: [...holder.querySelectorAll('a')].map((a) => a.protocol),
        code: holder.querySelector('pre code').textContent,
        list: holder.querySelectorAll('li').length
      };
    });
    assert.equal(safe.active, 0);
    assert.deepEqual(safe.links, ['https:']);
    assert.equal(safe.list, 2);
    assert(safe.code.includes('<script>'));
  });
  await check(
    'result dialog keeps focus, reconciliation input and settled messages during updates',
    async () => {
      const launcher = page.getByRole('button', { name: 'View result for Memory extraction' });
      const dialog = page.locator('#result-dialog');
      await launcher.focus();
      const scrollTop = await page.locator('#history').evaluate((el) => el.scrollTop);
      await page.keyboard.press('Enter');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'result-close');
      await page.keyboard.press('Escape');
      assert.equal(await launcher.evaluate((el) => el === document.activeElement), true);
      assert.equal(await page.locator('#history').evaluate((el) => el.scrollTop), scrollTop);
      await page.keyboard.press('Enter');
      const state = await page.evaluate(async () => {
        const { LitElement } = await import('/assets/lit-core.min.js');
        const app = document.querySelector('sola-app');
        const turn = document.querySelector('sola-turn:last-child');
        const input = document.querySelector('#reconcile-provider-task');
        const detail = document.querySelector('#result-body details');
        const message = turn.querySelector('[data-message-id]');
        const content = message.textContent;
        detail.open = true;
        input.value = 'provider-task-in-progress';
        input.focus();
        const selection = getSelection();
        const range = document.createRange();
        range.selectNodeContents(message);
        selection.removeAllRanges();
        selection.addRange(range);
        const selected = selection.toString();
        for (const preview of ['First update', '<img src=x onerror="window.PWNED=true">']) {
          turn.preview = preview;
          turn.requestUpdate();
          await turn.updateComplete;
        }
        const result = {
          components: app instanceof LitElement && turn instanceof LitElement,
          lightDOM: app.renderRoot === app && turn.renderRoot === turn,
          input:
            input.isConnected &&
            document.activeElement === input &&
            input.value === 'provider-task-in-progress',
          disclosure: detail.open,
          message:
            message.isConnected &&
            message.textContent === content &&
            selection.toString() === selected,
          escaped:
            !turn.querySelector('.preview img') &&
            turn.querySelector('.preview').textContent.includes('<img'),
          imports: performance
            .getEntriesByType('resource')
            .some((r) => r.name.endsWith('/assets/lit-core.min.js'))
        };
        turn.preview = '';
        await turn.updateComplete;
        return result;
      });
      assert(Object.values(state).every(Boolean), JSON.stringify(state));
      const input = dialog.getByRole('textbox', { name: 'Verified provider task ID' });
      const reconcile = dialog.getByRole('button', { name: 'Reconcile extraction' });
      const requests = [];
      const recordRequest = (request) => {
        if (request.url().endsWith(`/memory/${fixture.memory_commit_id}/reconcile`))
          requests.push(request.postDataJSON());
      };
      page.on('request', recordRequest);
      assert.deepEqual(requests, []);
      await reconcile.click();
      await dialog.getByRole('alert').waitFor();
      assert.equal(await input.inputValue(), 'provider-task-in-progress');
      await input.fill('fixture-task');
      await reconcile.click();
      await dialog.getByRole('status').filter({ hasText: /accepted|completed/ }).waitFor();
      assert.deepEqual(requests, [
        { provider_task_id: 'provider-task-in-progress' },
        { provider_task_id: 'fixture-task' }
      ]);
      page.off('request', recordRequest);
      await dialog.getByRole('button', { name: 'Close preview' }).click();
      assert.equal(await launcher.evaluate((el) => el === document.activeElement), true);
    }
  );
  await page.locator('#topic-' + fixture.other).click();
  await page.getByText('An interrupted fixture', { exact: true }).waitFor();
  await page.locator('#new-topic').click();
  await page.locator('#topic-name').fill('Browser acceptance conversation');
  await page.locator('#topic-form button.primary').click();
  await page.locator('#topic-dialog').waitFor({ state: 'hidden' });
  await waitFor(() => document.querySelector('#connection').textContent === 'Connected');
  fixture.other = await page.locator('#topics [aria-current=page]').getAttribute('id');
  fixture.other = fixture.other.replace('topic-', '');
  await check('empty composer, keyboard input and topic archive/restore', async () => {
    assert(await page.locator('#send').isDisabled());
    assert(await page.locator('#empty-new-topic').isHidden());
    assert.equal(
      await page.locator('#empty-hint').textContent(),
      'Send a message to start this conversation.'
    );
    await page.locator('#draft').fill('   ');
    assert(await page.locator('#send').isDisabled());
    await page.locator('#draft').fill('Keep this draft');
    assert(await page.locator('#send').isEnabled());
    await page.locator('#draft').press('Shift+Enter');
    assert.equal(await page.locator('#draft').inputValue(), 'Keep this draft\n');
    await page.locator('#draft').dispatchEvent('keydown', { key: 'Enter', isComposing: true });
    assert.equal(await page.locator('.message.user').count(), 0);
    await page.locator('#topic-edit').click();
    assert.equal(await page.evaluate(() => document.activeElement.id), 'topic-name');
    await page.locator('#topic-name').fill('Polished conversation');
    await page.locator('#pinned').check();
    await page.locator('#archived').check();
    await page.locator('#topic-form button.primary').click();
    await page.locator('#topic-dialog').waitFor({ state: 'hidden' });
    assert(await page.locator('#send').isDisabled());
    assert.equal(await page.locator('#draft').inputValue(), 'Keep this draft\n');
    assert.equal(await page.title(), `${agentName} · Polished conversation`);
    await page.locator('#topic-edit').click();
    await page.locator('#archived').uncheck();
    await page.locator('#topic-form button.primary').click();
    await page.locator('#topic-dialog').waitFor({ state: 'hidden' });
    assert(await page.locator('#send').isEnabled());
    await page.locator('#draft').fill('');
    assert(await page.locator('#send').isDisabled());
  });
  await check('two drafts during a tool run, focus and settled text survive previews', async () => {
    await send('tool first submission');
    await page.getByText('fixture.pause · working', { exact: true }).waitFor();
    await send('second queued draft');
    await page.locator('#draft').fill('still editing the next thought');
    await page.locator('#draft').focus();
    await page
      .locator('#turns .message.assistant')
      .first()
      .evaluate((el) => {
        const selection = window.getSelection(),
          range = document.createRange();
        range.selectNodeContents(el);
        selection.removeAllRanges();
        selection.addRange(range);
        window.__settled = el;
        window.__selection = selection.toString();
      });
    await page.waitForTimeout(2500);
    const stability = await page.evaluate(() => ({
      focus: document.activeElement.id,
      draft: document.querySelector('#draft').value,
      same: window.__settled.isConnected,
      selection: window.getSelection().toString() === window.__selection
    }));
    assert.equal(stability.focus, 'draft');
    assert.equal(stability.draft, 'still editing the next thought');
    assert(stability.same);
    assert(stability.selection);
    await page.waitForFunction(
      () =>
        [...document.querySelectorAll('.run-bar')].filter((el) =>
          /queued|running/.test(el.textContent)
        ).length === 0,
      null,
      { timeout: 30000 }
    );
    assert.equal(
      await page
        .locator('#turns .message.user')
        .filter({ hasText: 'tool first submission' })
        .count(),
      1
    );
    const timeline = await page
      .locator('.turn')
      .filter({ hasText: 'tool first submission' })
      .locator('.timeline > *')
      .evaluateAll((nodes) => nodes.map((el) => el.className));
    assert.deepEqual(timeline, ['message user', 'narration', 'work', 'message assistant']);
    assert.equal(
      await page.locator('#turns .message.user').filter({ hasText: 'second queued draft' }).count(),
      1
    );
    await waitFor(() =>
      document.querySelector('#reply-status').textContent.includes('second queued draft')
    );
    const announcement = await page.locator('#reply-status').textContent();
    await page.waitForTimeout(1100);
    assert.equal(await page.locator('#reply-status').textContent(), announcement);
  });
  await check('cancellation and explicit model failure', async () => {
    await send('tool result transition check');
    const working = page.getByRole('button', { name: 'View details for fixture.pause' });
    await working.click();
    const dialog = page.locator('#result-dialog');
    const details = dialog.locator('summary');
    await details.focus();
    assert(await details.evaluate((el) => el.parentElement.open));
    assert.equal(await dialog.locator('details pre').first().textContent(), '{}');
    await dialog.locator('.result-status').filter({ hasText: 'succeeded' }).waitFor();
    assert(await details.evaluate((el) => el.parentElement.open && el === document.activeElement));
    await page.keyboard.press('Escape');
    assert(await page.getByRole('button', { name: 'View result for fixture.pause' }).last()
      .evaluate((el) => el === document.activeElement));
    await send('tool cancellation check');
    await page.getByText('fixture.pause · working', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Cancel run', exact: true }).last().click();
    await page
      .getByText(/cancelled · Run cancelled/)
      .last()
      .waitFor();
    await send('fail synthetic model request');
    await page.locator('.run-bar').filter({ hasText: 'failed' }).last().waitFor({ timeout: 30000 });
  });
  await check('drafts and topic selection survive switch and reload', async () => {
    await page.locator('#draft').fill('A draft to keep across navigation');
    await page.locator('#topic-' + fixture.topic).click();
    await page.getByText('Settled answer 500.', { exact: false }).first().waitFor();
    await page.locator('#draft').fill('The long-topic draft');
    await page.locator('#topic-' + fixture.other).click();
    assert.equal(await page.locator('#draft').inputValue(), 'A draft to keep across navigation');
    await page.reload();
    await page.locator('#topic-' + fixture.other + '[aria-current=page]').waitFor();
    await waitFor(() => document.querySelector('#connection').textContent === 'Connected');
    assert.equal(await page.locator('#draft').inputValue(), 'A draft to keep across navigation');
    report.afterReload = await page.evaluate(() => ({
      turns: document.querySelectorAll('.turn').length,
      nodes: document.querySelectorAll('*').length
    }));
    assert.equal(await page.locator('#reply-status').textContent(), '');
  });
  await check(
    'offline recovery clears previews, preserves timers and avoids duplicate nodes',
    async () => {
      await send('preview reconnect check');
      await page.locator('.preview:not([hidden])').waitFor();
      const nodeCount = await page.locator('*').count();
      await page.waitForTimeout(400);
      assert.equal(await page.locator('*').count(), nodeCount);
      report.previewNodeCount = nodeCount;
      const run = await page.locator('.turn').last().getAttribute('data-run-id');
      const before = await page.evaluate(() => ({
        messages: [...document.querySelectorAll('[data-message-id]')].map(
          (el) => el.dataset.messageId
        ),
        turns: document.querySelectorAll('.turn').length
      }));
      await context.setOffline(true);
      await page.waitForTimeout(1500);
      assert.equal(await page.locator('.preview:not([hidden])').count(), 0);
      await page.locator('#draft').fill('Offline editable draft');
      // One row per page forces the reconnect to cross a page boundary for this cached run.
      const recordsURL = `**/runs/${run}/records?*`;
      let pagedBack = false;
      await page.route(recordsURL, (route) => {
        const url = new URL(route.request().url());
        pagedBack ||= Number(url.searchParams.get('before')) > 0;
        url.searchParams.set('limit', '1');
        return route.continue({ url: url.href });
      });
      await context.setOffline(false);
      await waitFor(() => document.querySelector('#connection').textContent === 'Connected');
      await page
        .locator(`[data-run-id="${run}"] .run-bar`)
        .filter({ hasText: 'completed' })
        .waitFor({ timeout: 30000 });
      assert(pagedBack, 'Catch up on records beyond the newest page');
      await page.unroute(recordsURL);
      assert.equal(await page.locator(`[data-run-id="${run}"]`).count(), 1);
      assert.equal(await page.locator('#draft').inputValue(), 'Offline editable draft');
      const after = await page.evaluate(() =>
        [...document.querySelectorAll('[data-message-id]')].map((el) => el.dataset.messageId)
      );
      assert.equal(new Set(after).size, after.length);
      assert(before.messages.every((id) => after.includes(id)));
      const elapsed = await page.locator(`[data-run-id="${run}"] .run-bar`).textContent();
      assert(!elapsed.includes('0m 0s'));
    }
  );
  await check('polling fallback and retrying a lost receipt reuse the admission key', async () => {
    await page.locator('#settings-open').click();
    await page.getByText('Diagnostics', { exact: true }).click();
    await page.locator('#poll-only').check();
    await page.getByRole('button', { name: 'Close Settings', exact: true }).click();
    const keys = [],
      accepted = [];
    let confirmRetry;
    const retryConfirmed = new Promise((resolve) => (confirmRetry = resolve));
    const intercept = async (route) => {
      keys.push(route.request().headers()['idempotency-key']);
      const response = await route.fetch();
      accepted.push((await response.json()).run.id);
      if (keys.length === 1) await route.abort('failed');
      else {
        await route.fulfill({ response });
        confirmRetry();
      }
    };
    await page.route('**/api/v1/runs', intercept);
    await send('Receipt loss fixture');
    await page.locator('#notice-dismiss').click();
    assert(await page.locator('#notice').isHidden());
    await page
      .getByRole('button', {
        name: 'Retry same submission',
        exact: true
      })
      .click();
    await retryConfirmed;
    await page.unroute('**/api/v1/runs', intercept);
    assert.equal(keys.length, 2);
    assert.equal(keys[0], keys[1]);
    assert.equal(accepted[0], accepted[1]);
    await page
      .locator(`[data-run-id="${accepted[0]}"] .run-bar`)
      .filter({ hasText: 'completed' })
      .waitFor({ timeout: 30000 });
    assert.equal(await page.locator(`[data-run-id="${accepted[0]}"]`).count(), 1);
    assert.equal(
      await page.locator('.message.user').filter({ hasText: 'Receipt loss fixture' }).count(),
      1
    );
  });
  await check('Settings schedule toggle and disabled manual runs', async () => {
    const showCompletedOccurrence = async (job, decision) => {
      const id = await job.getAttribute('data-job-id');
      const deadline = Date.now() + 30000;
      while (Date.now() < deadline) {
        const response = await page.request.get(`${fixture.origin}/api/v1/jobs/${id}/occurrences`);
        assert(response.ok());
        const { items } = await response.json();
        if (items.some((item) => item.state === 'completed' && item.decision === decision)) {
          await job.getByRole('button', { name: 'Occurrences', exact: true }).click();
          return;
        }
        await delay(250);
      }
      assert.fail(`Job ${id} did not complete with decision ${decision}`);
    };
    await page.locator('#settings-open').click();
    await page.locator('#job-new').click();
    await page.locator('#job-task').fill('A scheduled fixture message');
    await page.locator('#job-schedule').fill('cron:0 9 * * *');
    await page.locator('#job-topic').selectOption(fixture.other);
    await page.locator('#job-form button.primary').click();
    const job = page
      .locator('#jobs article')
      .filter({ hasText: 'A scheduled fixture message' })
      .last();
    await job.getByRole('button', { name: 'Enable', exact: true }).click();
    await job.getByRole('button', { name: 'Disable', exact: true }).click();
    await job.getByRole('button', { name: 'Enable', exact: true }).waitFor();
    assert.equal(await job.getByRole('button', { name: /^(Pause|Resume)$/ }).count(), 0);
    const runNow = job.getByRole('button', { name: 'Run now', exact: true });
    await page.getByRole('button', { name: 'Pause all schedules', exact: true }).click();
    await runNow.and(page.locator(':disabled')).waitFor();
    await page.getByRole('button', { name: 'Resume all schedules', exact: true }).click();
    await runNow.click();
    await showCompletedOccurrence(job, 'message');
    await job.getByText(/completed · message/).waitFor();
    assert(await job.getByRole('button', { name: 'Enable', exact: true }).isVisible());
    const jobId = await job.getAttribute('data-job-id');
    await job.getByRole('button', { name: 'Edit', exact: true }).click();
    await page.locator('#job-task').fill('no action');
    await page.locator('#job-form button.primary').click();
    const noAction = page.locator(`[data-job-id="${jobId}"]`);
    await noAction.getByRole('heading', { name: 'no action', exact: true }).waitFor();
    await noAction.getByRole('button', { name: 'Run now', exact: true }).click();
    await showCompletedOccurrence(noAction, 'no_action');
    await noAction.getByText(/completed · No action needed/).waitFor();
    await page.locator('#job-new').click();
    await page.locator('#job-task').fill('no action proactive fixture');
    await page.locator('#job-schedule').fill('cron:0 10 * * *');
    await page.locator('#job-origin').selectOption('proactive');
    await page.locator('#job-form button.primary').click();
    const proactive = page
      .locator('#jobs article')
      .filter({ hasText: 'no action proactive fixture' })
      .last();
    await proactive.getByRole('button', { name: 'Run now', exact: true }).click();
    await showCompletedOccurrence(proactive, 'no_action');
    await proactive.getByText(/completed · No action needed/).waitFor();
    await page.getByRole('button', { name: 'Close Settings', exact: true }).click();
    await page.locator('#new-topic').click();
    await page.locator('#topic-name').fill('Scheduled acceptance topic');
    await page.locator('#topic-form button.primary').click();
    await page.locator('#topic-dialog').waitFor({ state: 'hidden' });
    await waitFor(() =>
      /^(Connected|Polling)/.test(document.querySelector('#connection').textContent)
    );
    await page.locator('#settings-open').click();
    await page.locator('#job-new').click();
    await page.locator('#job-task').fill('Automatic scheduled UI message');
    await page.locator('#job-schedule').fill(`once:${new Date(Date.now() + 2500).toISOString()}`);
    await page.locator('#job-enabled').check();
    await page.locator('#job-form button.primary').click();
    const automatic = page
      .locator('#jobs article')
      .filter({ hasText: 'Automatic scheduled UI message' })
      .last();
    await showCompletedOccurrence(automatic, 'message');
    await automatic.getByText(/completed · message/).waitFor();
    assert((await automatic.textContent()).includes('"trigger": "scheduled"'));
    await page.screenshot({
      path: path.join(options.output, 'settings-desktop.png'),
      fullPage: true
    });
    await page.getByRole('button', { name: 'Close Settings', exact: true }).click();
    await page.getByText(`${agentName} · scheduled message`, { exact: true }).last().waitFor();
    await page
      .locator('.message.assistant')
      .filter({ hasText: 'Automatic scheduled UI message' })
      .waitFor();
    assert.equal(
      await page
        .locator('.message.user')
        .filter({ hasText: 'Automatic scheduled UI message' })
        .count(),
      0
    );
    assert.equal(
      await page.locator('.message.assistant').filter({ hasText: 'NO_ACTION' }).count(),
      0
    );
  });
  await check(
    'Settings tabs, profile tools, prompt inspection, search and memory recovery',
    async () => {
      await page.locator('#settings-open').click();
      await page.getByRole('tab', { name: 'Schedules', exact: true }).focus();
      await page.keyboard.press('End');
      assert.equal(
        await page.evaluate(() => document.activeElement.id),
        'settings-tab-security'
      );
      await page.locator('#login-status').filter({ hasText: 'expires' }).waitFor();
      page.once('dialog', (dialog) => dialog.dismiss());
      await page.locator('#revoke-logins').click();
      assert(await page.locator('#settings').isVisible());
      await page.getByRole('tab', { name: 'Security', exact: true }).focus();
      await page.keyboard.press('Home');
      assert.equal(
        await page
          .getByRole('tab', { name: 'Schedules', exact: true })
          .getAttribute('aria-selected'),
        'true'
      );
      await page.getByRole('tab', { name: 'Profiles & tools', exact: true }).click();
      await page.locator('#tools summary').first().waitFor();
      assert((await page.locator('#profiles').textContent()).includes('Workspace roots'));
      await audit('settings-access');
      await page.getByRole('tab', { name: 'Diagnostics', exact: true }).click();
      await page.locator('#health').filter({ hasText: 'Healthy' }).waitFor();
      await page.getByText('Inspect a draft prompt', { exact: true }).click();
      await page.locator('#prompt-content').fill('Inspect without inference');
      await page.locator('#prompt-hashes').check();
      await page.locator('#prompt-form button').click();
      await page
        .locator('#prompt-result')
        .filter({ hasText: 'Inspect without inference' })
        .waitFor();
      assert((await page.locator('#prompt-result').textContent()).includes('Content hashes'));
      assert((await page.locator('#prompt-result').textContent()).includes('manifest'));
      await audit('settings-diagnostics');
      await page.getByRole('tab', { name: 'Search', exact: true }).click();
      await page.locator('#search-query').fill('Automatic scheduled UI message');
      await page.locator('#search-form button').click();
      await page.locator('#search-results .search-result').first().waitFor();
      await page
        .locator('#search-results')
        .getByRole('button', { name: 'Inspect run evidence', exact: true })
        .first()
        .click();
      await page.locator('#evidence[open]').waitFor();
      await page.keyboard.press('Escape');
      await page.getByRole('tab', { name: 'Memory', exact: true }).click();
      await page.locator('#memory-status').filter({ hasText: 'Extraction observer' }).waitFor();
      await page.getByText('Reconcile failed or ambiguous extraction', { exact: true }).click();
      await page.locator('#memory-commit').fill('0'.repeat(32));
      await page.locator('#memory-task').fill('missing-task');
      await page.locator('#memory-form button').click();
      await page.locator('#settings-error').filter({ hasText: /.+/ }).waitFor();
      assert(await page.locator('#memory-form button').isEnabled());
      await page.getByRole('button', { name: 'Close Settings', exact: true }).click();
    }
  );
  await check(
    'job tool choices follow the profile and archived targets require reselection',
    async () => {
      const jobApi = (path, options) =>
        page.evaluate(async ([path, options]) => {
          const { api } = await import('/assets/client.js');
          return api(path, options);
        }, [path, options]);
      const target = await jobApi('/sessions', {
        method: 'POST',
        body: { title: 'Temporary job target' }
      });
      await page.locator('#settings-open').click();
      await page.locator('#job-new').click();
      await page.locator('#job-task').fill('Restricted one-run fixture');
      await page.locator('#job-schedule').fill('cron:0 11 * * *');
      await page.locator('#job-topic').selectOption(target.id);
      await page.locator('#job-delivery').selectOption('none');
      await page.locator('#job-advanced summary').click();
      const tool = page.getByRole('checkbox', { name: 'fixture.pause', exact: true });
      const toolLimit = page.locator('#job-restrict-tools');
      assert(await page.locator('#job-tools').isHidden());
      await toolLimit.check();
      await tool.check();
      await toolLimit.uncheck();
      assert(await page.locator('#job-tools').isHidden());
      await toolLimit.check();
      assert(await tool.isChecked());
      await page.locator('#job-profile').selectOption('no-tools');
      assert.equal(await page.locator('#job-tools input').count(), 0);
      await page.locator('#job-profile').selectOption('default');
      assert(!(await tool.isChecked()));
      await tool.check();
      const rootLimit = page.locator('#job-restrict-roots');
      const roots = page.locator('#job-roots');
      assert(await page.locator('#job-roots-field').isHidden());
      await rootLimit.check();
      await roots.fill('/example/workspace');
      await rootLimit.uncheck();
      assert(await page.locator('#job-roots-field').isHidden());
      await rootLimit.check();
      assert.equal(await roots.inputValue(), '/example/workspace');
      await roots.clear();
      await page.locator('#job-budget-model_calls').fill('2');
      await page.locator('#job-budget-tool_calls').fill('2');
      await page.locator('#job-budget-active_seconds').fill('30');
      await audit('job-editor');
      await page.screenshot({ path: path.join(options.output, 'job-editor-desktop.png') });
      await page.locator('#job-zone').fill('Not/AZone');
      await page.locator('#job-form button.primary').click();
      await page.locator('#job-error').filter({ hasText: /.+/ }).waitFor();
      assert.equal(await page.locator('#job-task').inputValue(), 'Restricted one-run fixture');
      assert(await page.locator('#job-form button.primary').isEnabled());
      await page.locator('#job-zone').fill('America/Denver');
      await page.locator('#job-form button.primary').click();
      await page.locator('#job-dialog').waitFor({ state: 'hidden' });
      const job = page
        .locator('#jobs article')
        .filter({ hasText: 'Restricted one-run fixture' });
      const jobId = await job.getAttribute('data-job-id');
      assert.deepEqual((await jobApi(`/jobs/${jobId}`)).tools, ['fixture.pause']);
      await jobApi(`/sessions/${target.id}`, { method: 'PATCH', body: { archived: true } });
      await job.getByRole('button', { name: 'Edit', exact: true }).click();
      await page.locator('#job-dialog').waitFor();
      assert.equal(await page.locator('#job-topic').inputValue(), '');
      assert.equal(await page.locator(`#job-topic option[value="${target.id}"]`).count(), 0);
      assert(!(await page.locator('#job-topic').evaluate((el) => el.validity.valid)));
      assert.equal(await page.locator('#job-profile').inputValue(), 'default');
      assert.equal(await page.locator('#job-delivery').inputValue(), 'none');
      assert(await page.locator('#job-restrict-tools').isChecked());
      assert(await tool.isChecked());
      assert(await page.locator('#job-restrict-roots').isChecked());
      assert.equal(await page.locator('#job-budget-model_calls').inputValue(), '2');
      await tool.uncheck();
      await page.locator('#job-topic').selectOption('ephemeral');
      await page.locator('#job-task').fill('Restricted one-run edited');
      await page.locator('#job-form button.primary').click();
      await page.locator('#job-dialog').waitFor({ state: 'hidden' });
      assert.deepEqual((await jobApi(`/jobs/${jobId}`)).tools, []);
      const edited = page
        .locator('#jobs article')
        .filter({ hasText: 'Restricted one-run edited' });
      await edited.getByRole('button', { name: 'Run now', exact: true }).click();
      await page.waitForTimeout(5000);
      await edited.getByRole('button', { name: 'Occurrences', exact: true }).click();
      await edited
        .getByText(/completed/)
        .first()
        .waitFor();
      const occurrence = (await jobApi(`/jobs/${jobId}/occurrences`)).items[0];
      const run = await jobApi(`/runs/${occurrence.run_id}`);
      assert((await jobApi(`/sessions/${run.session_id}`)).archived);
      page.once('dialog', (dialog) => dialog.accept());
      await edited.getByRole('button', { name: 'Delete job', exact: true }).click();
      await edited.waitFor({ state: 'hidden' });
      await page.getByRole('button', { name: 'Close Settings', exact: true }).click();
    }
  );
  await check('authenticated evidence and artifact chunks are inspectable', async () => {
    await page.getByRole('button', { name: 'Inspect evidence', exact: true }).last().click();
    await page.locator('#evidence-records summary').filter({ hasText: 'request' }).first().click();
    await page
      .getByRole('button', { name: /Inspect artifact/ })
      .first()
      .click();
    await page
      .getByText(/Artifact bytes/)
      .first()
      .waitFor();
    await audit('evidence');
    await page.getByRole('tab', { name: 'Model requests', exact: true }).click();
    assert.equal(await page.getByText(/Artifact bytes/).count(), 0);
    await page.locator('#evidence-records summary').first().waitFor();
    assert((await page.locator('#evidence-records').textContent()).includes('request'));
    await page.getByRole('tab', { name: 'All records', exact: true }).click();
    await page.getByRole('button', { name: 'Close evidence', exact: true }).click();
  });
  await check('auth expiry prompts for login and retains the editable draft', async () => {
    await page.locator('#draft').fill('Keep me after sign-in expires');
    await page.evaluate(async () => {
      const csrf = document.cookie
        .split('; ')
        .find((v) => v.startsWith('sola_csrf='))
        .split('=')[1];
      await fetch('/api/v1/auth/sessions', {
        method: 'DELETE',
        headers: { 'X-CSRF-Token': csrf }
      });
    });
    await page.locator('#login-dialog[open]').waitFor({ timeout: 15000 });
    await page.locator('#password').fill('phase11-disposable-fixture');
    await page.locator('#login-form button').click();
    await page.locator('#login-dialog').waitFor({ state: 'hidden' });
    await page.waitForTimeout(1000);
    assert.equal(await page.locator('#draft').inputValue(), 'Keep me after sign-in expires');
  });
  await check('Security signs out every device and preserves the current draft', async () => {
    const draft = await page.locator('#draft').inputValue();
    await page.locator('#settings-open').click();
    await page.getByRole('tab', { name: 'Security', exact: true }).click();
    page.once('dialog', (dialog) => dialog.accept());
    await page.locator('#revoke-logins').click();
    await page.locator('#login-dialog[open]').waitFor();
    const status = await page.evaluate(async () => (await fetch('/api/v1/auth/session')).status);
    assert.equal(status, 401);
    await page.locator('#password').fill('phase11-disposable-fixture');
    await page.locator('#login-form button').click();
    await page.locator('#login-dialog').waitFor({ state: 'hidden' });
    await waitFor(() => /^(Connected|Polling)/.test(document.querySelector('#connection').textContent));
    assert.equal(await page.locator('#draft').inputValue(), draft);
  });
  await check('narrow layout, keyboard focus, labels and reduced motion', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.locator('#draft').focus();
    await page.keyboard.type(' keyboard');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'draft');
    const bounds = await page.evaluate(() => ({
      width: innerWidth,
      scroll: document.documentElement.scrollWidth,
      main: document.querySelector('main').getBoundingClientRect().height,
      composer: document.querySelector('#composer').getBoundingClientRect().bottom,
      buttons: [...document.querySelectorAll('main button')]
        .filter((el) => el.offsetParent !== null)
        .map((el) => el.getBoundingClientRect().height)
    }));
    assert(bounds.scroll <= bounds.width);
    assert(bounds.main > 350);
    assert(bounds.composer <= 844);
    assert(bounds.buttons.every((h) => h >= 36));
    assert.equal(await page.getByRole('textbox', { name: `Message ${agentName}`, exact: true }).count(), 1);
    await page.screenshot({
      path: path.join(options.output, 'chat-mobile.png'),
      fullPage: true
    });
    await page.locator('#settings-open').click();
    await page.screenshot({
      path: path.join(options.output, 'settings-mobile.png'),
      fullPage: true
    });
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'settings-open');
  });
  await check('every Settings tab and the job editor fit a narrow viewport', async () => {
    await page.setViewportSize({ width: 320, height: 568 });
    await page.locator('#settings-open').click();
    for (const name of ['Memory', 'Profiles & tools', 'Search', 'Diagnostics', 'Security', 'Schedules']) {
      await page.getByRole('tab', { name, exact: true }).click();
      assert(await page.locator('#settings').evaluate((el) => el.scrollWidth <= el.clientWidth));
      assert(await page.locator('.settings-content').evaluate((el) => el.scrollWidth <= el.clientWidth));
    }
    await page.locator('#job-new').click();
    await page.locator('#job-advanced summary').click();
    assert(await page.locator('#job-dialog').evaluate((el) => el.scrollWidth <= el.clientWidth));
    await audit('job-editor-mobile');
    await page.screenshot({ path: path.join(options.output, 'job-editor-mobile.png'), fullPage: true });
    await page.keyboard.press('Escape');
    await page.keyboard.press('Escape');
  });
  await check(
    'light/dark mobile layouts, Settings and evidence fit narrow and short viewports',
    async () => {
      for (const colorScheme of ['light', 'dark']) {
        await page.emulateMedia({ colorScheme });
        for (const viewport of [
          { width: 320, height: 568 },
          { width: 390, height: 844 },
          { width: 844, height: 390 }
        ]) {
          await page.setViewportSize(viewport);
          await page.evaluate(() => new Promise(requestAnimationFrame));
          const layout = await page.evaluate(() => {
            const composer = document.querySelector('#composer').getBoundingClientRect();
            return {
              width: innerWidth,
              scrollWidth: document.documentElement.scrollWidth,
              height: innerHeight,
              composerBottom: composer.bottom,
              historyHeight: document.querySelector('#history').clientHeight
            };
          });
          assert(
            layout.scrollWidth <= layout.width &&
              layout.composerBottom <= layout.height &&
              layout.historyHeight > 0,
            `${colorScheme} ${JSON.stringify(layout)}`
          );
          await page.locator('#settings-open').click();
          assert(
            await page.locator('#settings').evaluate((el) => el.scrollWidth <= el.clientWidth)
          );
          assert(
            await page.locator('#topics [aria-current=page]').evaluate((el) => {
              const box = el.getBoundingClientRect(),
                nav = el.parentElement.getBoundingClientRect();
              return box.left < nav.right && box.right > nav.left;
            })
          );
          await page.keyboard.press('Escape');
        }
        await page.setViewportSize({ width: 390, height: 844 });
        await page.evaluate(() => new Promise(requestAnimationFrame));
        await page.screenshot({
          path: path.join(options.output, `chat-mobile-${colorScheme}.png`),
          fullPage: true
        });
        await page.locator('#settings-open').click();
        await page.screenshot({
          path: path.join(options.output, `settings-mobile-${colorScheme}.png`),
          fullPage: true
        });
        await page.keyboard.press('Escape');
        await page
          .getByRole('button', {
            name: 'Inspect evidence',
            exact: true
          })
          .last()
          .click();
        assert(await page.locator('#evidence').evaluate((el) => el.scrollWidth <= el.clientWidth));
        await page.keyboard.press('Escape');
      }
    }
  );
  await check('touch targets, enlarged text, forced colors and short-screen reflow', async () => {
    const touchContext = await browser.newContext({
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      storageState: await context.storageState()
    });
    try {
      const touch = await touchContext.newPage();
      await touch.addInitScript(
        (id) => sessionStorage.setItem('sola.selected', JSON.stringify(id)),
        fixture.topic
      );
      await touch.goto(fixture.origin);
      await touch.locator('#topic-' + fixture.topic + '[aria-current=page]').waitFor();
      const heights = await touch
        .locator('button, select, summary, label:has(input[type=checkbox])')
        .evaluateAll((elements) =>
          elements
            .filter(
              (el) =>
                el.getClientRects().length &&
                !el.closest('dialog:not([open]), details:not([open]) > :not(summary)')
            )
            .map((el) => el.getBoundingClientRect().height)
        );
      assert(heights.length > 0 && heights.every((height) => height >= 44));
      await touch.locator('#topic-' + fixture.image_topic).tap();
      const launcher = touch.getByRole('button', { name: 'Open image from your message' });
      await launcher.tap();
      const preview = touch.getByRole('dialog', { name: 'Attached image' });
      await preview.locator('img').waitFor();
      assert(await preview.evaluate((el) => {
        const box = el.getBoundingClientRect();
        return box.left >= 0 && box.right <= innerWidth &&
          box.top >= 0 && box.bottom <= innerHeight;
      }));
      await preview.getByRole('button', { name: 'Close preview' }).tap();
      assert(await launcher.evaluate((el) => el === document.activeElement));
    } finally {
      await touchContext.close();
    }
    await page.setViewportSize({ width: 320, height: 568 });
    await page.evaluate(() => {
      const sheet = document.styleSheets[0];
      sheet.insertRule(':root { font-size: 200%; }', sheet.cssRules.length);
    });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(options.output, 'chat-large-text.png'), fullPage: true });
    for (const selector of ['#draft', '#settings-open']) {
      await page.locator(selector).focus();
      assert(
        await page.locator(selector).evaluate((el) => {
          const box = el.getBoundingClientRect();
          return box.left >= 0 && box.right <= innerWidth;
        })
      );
    }
    await page.locator('#settings-open').click();
    assert(await page.locator('#settings').evaluate((el) => el.scrollWidth <= el.clientWidth));
    await audit('settings-large-text');
    await page.screenshot({ path: path.join(options.output, 'settings-large-text.png'), fullPage: true });
    await page.keyboard.press('Escape');
    await page.evaluate(() => {
      const sheet = document.styleSheets[0];
      sheet.deleteRule(sheet.cssRules.length - 1);
    });
    await page.emulateMedia({ forcedColors: 'active' });
    const current = page.locator('#topics [aria-current=page]');
    assert.equal(await current.evaluate((el) => getComputedStyle(el).outlineStyle), 'solid');
    await page.emulateMedia({ forcedColors: 'none' });
    await page.setViewportSize({ width: 320, height: 256 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator('#draft').focus();
    await waitFor(() => {
      const { y } = document.getElementById('draft').getBoundingClientRect();
      return y >= 0 && y < innerHeight;
    });
    await page.setViewportSize({ width: 390, height: 844 });
  });
  await check('single-use topics explain why another submission is unavailable', async () => {
    await page.locator('#new-topic').click();
    await page.locator('#topic-name').fill('Ephemeral browser verification');
    await page.locator('#mode').selectOption('ephemeral');
    await page.locator('#topic-form button.primary').click();
    await page.locator('#topic-dialog').waitFor({ state: 'hidden' });
    await send('Single-use conversation');
    await page.locator('.run-bar').filter({ hasText: 'completed' }).waitFor({ timeout: 30000 });
    await page.locator('#draft').fill('Keep the next thought as a draft');
    assert(await page.locator('#send').isDisabled());
    assert((await page.locator('#compose-hint').textContent()).includes('one run'));
    assert(await page.locator('#compose-hint').isVisible());
    await page.reload();
    await waitFor(() => document.querySelector('#connection').textContent === 'Connected');
    assert.equal(await page.locator('#draft').inputValue(), 'Keep the next thought as a draft');
    assert(await page.locator('#send').isDisabled());
    assert.equal(await page.locator('.message.user').count(), 1);
  });
  await check('picker and conversation drop stage source files and a raster image in the same draft', async () => {
    await page.locator('#new-topic').click();
    await page.locator('#topic-name').fill('Attachment verification');
    await page.locator('#mode').selectOption('shared');
    await page.locator('#topic-form button.primary').click();
    await page.locator('#topic-dialog').waitFor({ state: 'hidden' });
    const picker = page.locator('#attachment-files');
    assert.equal(await picker.getAttribute('accept'), null);
    const sourceFiles = [
      { name: 'index.html', mimeType: 'text/html', content: '<main>Moss green</main>' },
      { name: 'build.js', mimeType: 'application/javascript', content: 'export const ready = true;' },
      { name: 'Makefile', mimeType: '', content: 'all:\n\t@echo ready\n' }
    ];
    const svg = '<svg xmlns="http://www.w3.org/2000/svg"><title>Moss green</title></svg>';
    await picker.setInputFiles([
      ...sourceFiles.map(({ name, mimeType, content }) => ({ name, mimeType, buffer: Buffer.from(content) })),
      { name: 'remove.txt', mimeType: 'text/plain', buffer: Buffer.from('Remove me') }
    ]);
    await page.getByRole('button', { name: 'Remove remove.txt', exact: true }).click();
    const transfer = await page.evaluateHandle((svg) => {
      const data = new DataTransfer();
      data.items.add(new File([svg], 'diagram.svg', { type: 'image/svg+xml' }));
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 64;
      const png = atob(canvas.toDataURL().split(',')[1]);
      data.items.add(new File([Uint8Array.from(png, (byte) => byte.charCodeAt(0))], 'pixels.png', { type: 'image/png' }));
      return data;
    }, svg);
    const history = page.locator('#history');
    await history.dispatchEvent('dragenter', { dataTransfer: transfer });
    await page.locator('#drop-files').waitFor({ state: 'visible' });
    await page.screenshot({ path: path.join(options.output, 'drop-files.png') });
    await history.dispatchEvent('drop', { dataTransfer: transfer });
    await page.locator('#drop-files').waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: 'Remove diagram.svg', exact: true }).waitFor();
    await page.locator('#draft-image img').waitFor();
    await page.getByRole('button', { name: 'Open image from your draft' }).click();
    await page.getByRole('dialog', { name: 'Draft image' }).locator('.artifact-image').waitFor();
    await page.keyboard.press('Escape');
    await page.locator('#draft').fill('Read the attached files.');
    await page.reload();
    await waitFor(() => document.querySelector('#connection').textContent === 'Connected');
    assert.equal(await page.locator('#draft').inputValue(), 'Read the attached files.');
    await page.locator('#draft-image img').waitFor();
    assert.equal(await page.locator('#draft-documents .document-attachment').count(), 4);
    await audit('document and image draft');
    await page.screenshot({ path: path.join(options.output, 'attachment-draft.png') });
    await page.locator('#send').click();
    await page.locator('.run-bar').filter({ hasText: 'completed' }).waitFor({ timeout: 30000 });
    await page.locator('.message.user .message-attachment img').waitFor();
    await page.getByRole('button', { name: 'Open image from your message' }).click();
    await page.getByRole('dialog', { name: 'Attached image' }).locator('.artifact-image').waitFor();
    await page.keyboard.press('Escape');
    const resultDialog = page.locator('#result-dialog');
    for (const { name, content } of [...sourceFiles, { name: 'diagram.svg', content: svg }]) {
      const preview = page.getByRole('button', { name: `View attachment ${name}`, exact: true });
      await preview.click();
      await resultDialog.locator('pre').waitFor();
      assert.equal(await resultDialog.locator('pre').textContent(), content);
      assert.equal(await resultDialog.locator('svg:not(sola-icon svg), script, main').count(), 0);
      const downloaded = page.waitForEvent('download');
      await resultDialog.getByRole('button', { name: 'Download text', exact: true }).click();
      const file = await downloaded;
      assert.equal(await fs.readFile(await file.path(), 'utf8'), content);
      await page.keyboard.press('Escape');
    }
    await page.screenshot({ path: path.join(options.output, 'attachment-sent.png') });
    await picker.setInputFiles({
      name: 'brief.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.7\nFixture')
    });
    await page.getByRole('button', { name: 'Remove brief.pdf' }).waitFor();
    await send('Read the converted PDF.');
    await page.getByRole('button', { name: 'View attachment brief.pdf' }).click();
    await resultDialog.locator('pre').waitFor();
    const converted = '# Fixture document\n\nExtracted Markdown.\n';
    assert.equal(await resultDialog.locator('pre').textContent(), converted);
    const downloaded = page.waitForEvent('download');
    await resultDialog.getByRole('button', { name: 'Download text', exact: true }).click();
    assert.equal(await fs.readFile(await (await downloaded).path(), 'utf8'), converted);
    await audit('document preview');
    await page.keyboard.press('Escape');
  });
  await check('authorized PNG preview, download and image attachment survive reload', async () => {
    const preview = page.locator('.message-attachment .image-thumbnail').first();
    let failed = false;
    const imageRequests = '**/api/v1/runs/*/artifacts/*?thumbnail=true';
    await page.route(imageRequests, (route) => {
      if (failed) return route.continue();
      failed = true;
      return route.abort('failed');
    });
    await page.locator('#topic-' + fixture.image_topic).click();
    const thumbnail = page.locator('.message-attachment img').first();
    await preview.getByText('Open image', { exact: true }).waitFor();
    assert(await thumbnail.isHidden());
    await page.unroute(imageRequests);
    await preview.click();
    const dialog = page.getByRole('dialog', { name: 'Attached image' });
    await dialog.locator('img').waitFor();
    await page.waitForFunction(() => document.querySelector('#result-dialog img')?.naturalWidth);
    const dimensions = await dialog.locator('img').evaluate((img) => [img.naturalWidth, img.naturalHeight]);
    assert.deepEqual(dimensions, [fixture.image_width, fixture.image_height]);
    await page.screenshot({ path: path.join(options.output, 'image-preview-mobile.png') });
    const download = page.waitForEvent('download');
    await dialog.getByRole('link', { name: 'Download image', exact: true }).click();
    assert.equal((await download).suggestedFilename(), 'sola-image.png');
    await dialog.getByRole('button', { name: 'Use image in next message', exact: true }).click();
    assert.equal(await page.evaluate(() => document.activeElement.id), 'draft');
    await page.locator('#draft').fill('Describe this attached image.');
    await page.reload();
    await waitFor(() => document.querySelector('#connection').textContent === 'Connected');
    await page.waitForFunction(() => document.querySelector('#draft-image img')?.naturalWidth);
    await page.locator('#send').click();
    await page.locator('.run-bar').filter({ hasText: 'completed' }).waitFor({ timeout: 30000 });
    assert.equal(await page.locator('#draft-image').textContent(), '');
    await page.screenshot({ path: path.join(options.output, 'image-mobile.png'), fullPage: true });
    await audit('image-attachment');
  });
  await check('compact outcomes, chunked text and late image remain inspectable through reconnect', async () => {
    const shell = await page.request.get(fixture.origin);
    assert.match(shell.headers()['content-security-policy'], /img-src 'self' blob:;/);
    await page.evaluate(() => {
      window.previewCspViolations = [];
      document.addEventListener('securitypolicyviolation', (event) =>
        window.previewCspViolations.push(event.violatedDirective));
    });
    const artifactBase = `/api/v1/runs/${fixture.result_run}/artifacts/`;
    const requests = [];
    const recordRequest = (request) => {
      const url = new URL(request.url());
      if (url.pathname.startsWith(artifactBase)) requests.push(url.search);
    };
    page.on('request', recordRequest);
    await page.locator('#topic-' + fixture.result_topic).click();
    const rows = page.locator('.work-row');
    const dialog = page.locator('#result-dialog');
    const generated = rows.filter({ hasText: /images.generate.*succeeded/ });
    const late = rows.filter({ hasText: /images.generate.*uncertain/ });
    await generated.locator('img').scrollIntoViewIfNeeded();
    await page.waitForFunction(() => [...document.querySelectorAll('.work-row img')]
      .some((img) => img.naturalWidth));
    assert.deepEqual(requests, ['?thumbnail=true']);
    for (const label of [
      'files.search · succeeded · 3 matches · truncated',
      'files.write · succeeded · Wrote notes.md',
      'fixture.failed · failed · Could not read fixture output',
      'fixture.denied · denied · Permission denied',
      'fixture.cancelled · cancelled · Tool execution cancelled',
      'fixture.missing · no receipt'
    ]) assert.equal(await rows.getByText(label, { exact: true }).count(), 1, label);
    assert.equal(await generated.locator('img').count(), 1, 'one preview per durable image');
    assert.equal(await late.locator('img').count(), 0);

    await page.getByRole('button', { name: 'View result for files.write' }).click();
    await dialog.getByText('Artifact bytes 0–65536 of 65549').waitFor();
    assert.match(await dialog.locator('sola-artifact pre').textContent(), /^First chunk/);
    await dialog.getByRole('button', { name: 'Next chunk' }).click();
    await dialog.getByText('Second chunk', { exact: true }).waitFor();
    const chunkDownload = page.waitForEvent('download');
    await dialog.getByRole('button', { name: 'Download this chunk' }).click();
    assert.equal(await fs.readFile(await (await chunkDownload).path(), 'utf8'), 'Second chunk\n');
    await dialog.getByRole('button', { name: 'Previous chunk' }).click();
    await dialog.getByText('Artifact bytes 0–65536 of 65549').waitFor();
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: 'View result for fixture.failed' }).click();
    await dialog.getByRole('status').filter({ hasText: 'Could not read fixture output' }).waitFor();
    await page.keyboard.press('Escape');

    const previewUrl = `**${artifactBase}${fixture.generated_image}?preview=true`;
    await page.route(previewUrl, (route) => route.abort('failed'), { times: 1 });
    await generated.getByRole('button', { name: 'Open image from images.generate' }).click();
    await dialog.getByRole('alert').waitFor();
    await dialog.getByRole('button', { name: 'Retry preview' }).click();
    await dialog.locator('img').waitFor();
    await page.waitForFunction(() => document.querySelector('#result-dialog img')?.naturalWidth);
    await audit('generated image result');
    await dialog.getByRole('button', { name: 'Close preview' }).click();
    assert.deepEqual(await page.evaluate(() => window.previewCspViolations), []);

    const lateLauncher = late.getByRole('button', { name: 'View result for images.generate' });
    await lateLauncher.click();
    await dialog.locator('summary').click();
    const csrf = (await context.cookies()).find((cookie) => cookie.name === 'sola_csrf').value;
    await context.setOffline(true);
    await page.getByText(/Offline · drafts|Reconnecting · checking/).waitFor();
    const completed = await page.request.post(`${fixture.origin}/api/v1/fixture/complete-image`, {
      headers: { Origin: fixture.origin, 'X-CSRF-Token': csrf }
    });
    assert.equal(completed.status(), 200);
    await context.setOffline(false);
    await dialog.locator('img').waitFor({ timeout: 20000 });
    assert.match(await dialog.locator('.result-status').textContent(), /uncertain.*PNG image available/);
    assert(await dialog.locator('summary').evaluate((el) =>
      el.parentElement.open && el === document.activeElement
    ));
    await waitFor(() => document.querySelector('#connection').textContent === 'Connected');
    await page.keyboard.press('Escape');
    assert(await lateLauncher.evaluate((el) => el === document.activeElement));
    assert.equal(await late.locator('img').count(), 1);
    await page.reload();
    await late.locator('img').waitFor();
    assert.equal(await late.locator('img').count(), 1);
    await page.screenshot({ path: path.join(options.output, 'tool-results.png') });
    page.off('request', recordRequest);

    let releasePreview;
    const pendingPreview = new Promise((resolve) => { releasePreview = resolve; });
    await page.route(previewUrl, async (route) => {
      await pendingPreview;
      await route.continue().catch(() => {});
    }, { times: 1 });
    await generated.getByRole('button', { name: 'Open image from images.generate' }).click();
    await dialog.getByText('Loading image…', { exact: true }).waitFor();
    await page.locator('#topic-' + fixture.image_topic).evaluate((button) => button.click());
    await dialog.waitFor({ state: 'hidden' });
    releasePreview();
    assert.equal(await page.evaluate(() => document.activeElement.id), 'history');
    await page.getByRole('button', { name: /Open image from your message.*Saved image/ }).click();
    await dialog.locator('img').waitFor();
    const signOut = await page.request.delete(`${fixture.origin}/api/v1/auth/sessions`, {
      headers: { Origin: fixture.origin, 'X-CSRF-Token': csrf }
    });
    assert(signOut.ok());
    await page.locator('#login-dialog[open]').waitFor({ timeout: 15000 });
    await dialog.waitFor({ state: 'hidden' });
    await page.locator('#password').fill('phase11-disposable-fixture');
    await page.locator('#login-form button').click();
    await page.locator('#login-dialog').waitFor({ state: 'hidden' });
    await waitFor(() => document.querySelector('#connection').textContent === 'Connected');
  });
  await check('retry resumes a failed reply in the same turn and keeps the draft', async () => {
    await page.locator('#new-topic').click();
    await page.locator('#topic-name').fill('Retry a failed reply');
    await page.locator('#mode').selectOption('shared');
    await page.locator('#topic-form button.primary').click();
    await page.locator('#topic-dialog').waitFor({ state: 'hidden' });
    await send('tool flaky browser');
    const turn = page.locator('sola-turn');
    const retry = turn.getByRole('button', { name: 'Retry', exact: true });
    await retry.waitFor({ timeout: 20000 });
    await page.locator('#draft').fill('unsent draft');
    await audit('failed reply with retry');
    await retry.click();
    await turn.getByText(/Sola heard: tool flaky browser/).waitFor({ timeout: 20000 });
    for (const reloaded of [false, true]) {
      if (reloaded) {
        await page.reload();
        await turn.getByText(/Sola heard: tool flaky browser/).waitFor();
      }
      await turn.locator('.run-bar [role=status]', { hasText: 'completed' }).waitFor({ timeout: 20000 });
      assert.equal(await turn.count(), 1);
      assert.equal(await turn.locator('.message.assistant').count(), 1);
      assert.equal(await turn.locator('.message.user').count(), 1);
      assert.equal(await turn.getByRole('button', { name: 'View result for fixture.pause' }).count(), 1);
      assert(await retry.isHidden());
      assert(await turn.getByRole('button', { name: 'Attempt 1 evidence' }).isVisible());
    }
    assert.equal(await page.locator('#draft').inputValue(), 'unsent draft');
    await page.screenshot({ path: path.join(options.output, 'retry.png') });
    await page.locator('#draft').fill('');
  });
  await check('delegation links open the authenticated child transcript and run evidence', async () => {
    await page.locator('#new-topic').click();
    await page.locator('#topic-name').fill('Delegation browser verification');
    await page.locator('#mode').selectOption('shared');
    await page.locator('#topic-form button.primary').click();
    await page.locator('#topic-dialog').waitFor({ state: 'hidden' });
    await send('Delegate the browser fixture.');
    const turn = page.locator('sola-turn').filter({ hasText: 'The worker reported a blue fixture.' });
    await turn.getByText('The worker reported a blue fixture.', { exact: true }).waitFor();
    const link = turn.getByRole('link', { name: 'Open delegated topic' });
    const href = await link.getAttribute('href');
    const result = turn.getByRole('button', { name: 'View result for delegate.run' });
    await result.click();
    await page.locator('#result-dialog').getByRole('button', { name: 'Inspect worker run' }).click();
    await page.locator('#evidence-records summary').filter({ hasText: 'model' }).first().waitFor();
    await page.keyboard.press('Escape');
    await page.locator('#result-dialog[open]').waitFor();
    await page.keyboard.press('Escape');
    await turn.getByRole('button', { name: 'Inspect evidence' }).click();
    const linkRecord = page.locator('#evidence-records details').filter({ hasText: 'delegation' });
    await linkRecord.locator('summary').click();
    assert.equal(
      await linkRecord.getByRole('link', { name: 'Open delegated topic' }).getAttribute('href'),
      href
    );
    await page.keyboard.press('Escape');
    await link.click();
    await page.getByText('Report the fixture label.', { exact: true }).waitFor();
    await page.getByText('Fixture label: blue.', { exact: true }).waitFor();
    assert(await page.locator('#draft').isDisabled());
    await page.getByRole('button', { name: 'Inspect evidence' }).click();
    await page.locator('#evidence-records summary').filter({ hasText: 'model' }).first().waitFor();
    await page.keyboard.press('Escape');
  });
  await check('topic follow-up survives a lost receipt and publishes once when due', async () => {
    await page.locator('#new-topic').click();
    await page.locator('#topic-name').fill('Follow-up browser verification');
    await page.locator('#mode').selectOption('isolated');
    await page.locator('#topic-form button.primary').click();
    await page.getByRole('heading', { name: 'Follow-up browser verification' }).waitFor();
    await page.locator('#topic-edit').click();
    await page.locator('#schedule-follow-up').click();
    assert.equal(await page.locator('#follow-up-topic').innerText(), 'Follow-up browser verification');
    assert.equal(await page.locator('#follow-up-timezone').innerText(), 'UTC');
    await audit('schedule follow-up');
    const task = 'Browser fixture follow-up';
    const localAt = new Date(Date.now() + 12_000).toISOString().slice(0, 19);
    const attempts = [];
    const intercept = async (route) => {
      const response = await route.fetch();
      const receipt = await response.json();
      attempts.push([route.request().headers()['idempotency-key'], receipt]);
      if (attempts.length === 1) await route.abort('failed');
      else await route.fulfill({ response });
    };
    await page.route('**/api/v1/jobs', intercept);
    await page.locator('#follow-up-task').fill(task);
    await page.locator('#follow-up-at').fill(localAt);
    await page.locator('#follow-up-submit').click();
    await page.locator('#follow-up-error').filter({ hasText: /./ }).waitFor();
    await page.locator('#follow-up-submit').click();
    const dueAt = localAt.replace('T', ' ') + '+00:00';
    await page.locator('#follow-up-status').filter({ hasText: `Scheduled for ${dueAt}.` }).waitFor();
    await page.unroute('**/api/v1/jobs', intercept);
    assert.equal(attempts.length, 2);
    assert.deepEqual(attempts[0], attempts[1]);
    await page.screenshot({ path: path.join(options.output, 'follow-up.png') });
    await page.getByRole('button', { name: 'View schedules', exact: true }).click();
    const job = page.locator(`[data-job-id="${attempts[0][1].job_id}"]`);
    await job.getByText(`Due · ${dueAt} · UTC`).waitFor();
    await page.getByRole('button', { name: 'Close Settings', exact: true }).click();
    const message = page.locator('.message.assistant').filter({ hasText: `Sola heard: ${task}` });
    await message.waitFor({ timeout: 30000 });
    await page.reload();
    await message.waitFor();
    assert.equal(await message.count(), 1);
    await page.locator('#settings-open').click();
    await job.getByRole('button', { name: 'Occurrences', exact: true }).click();
    await job.getByText(/completed · message · Delivery delivered/).waitFor();
    await page.getByRole('button', { name: 'Close Settings', exact: true }).click();
  });
  await check('Feeds proposal review, keyboard reader and isolated discussion', async () => {
    await send(`Suggest the fixture feed ${Date.now()}`);
    await page.locator('.message.assistant').getByText('Fixture feed proposed.').waitFor();
    await page.locator('#settings-open').click();
    await page.getByRole('tab', { name: 'Security', exact: true }).focus();
    await page.keyboard.press('ArrowLeft');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'settings-tab-feeds');
    const feeds = page.locator('sola-feeds');
    await feeds.getByRole('button', { name: 'Subscriptions · pending' }).click();
    const pending = feeds.locator('.feed-subscription').filter({ hasText: 'Fixture subscription proposal' });
    await pending.getByText(/Agent · pending/).waitFor();
    await pending.getByText(/Found via Fixture discovery topic/).waitFor();
    assert.equal(await pending.getByRole('button', { name: 'Poll now' }).count(), 0);
    const url = await pending.locator('h4').textContent();
    await pending.getByRole('button', { name: 'Approve' }).click();
    const approved = feeds.locator('.feed-subscription').filter({ hasText: url.trim() });
    await approved.getByText(/Agent · active/).waitFor();
    await approved.getByRole('button', { name: 'Poll now' }).click();
    await approved.getByText(/2 unread of 2 items/).waitFor();
    await feeds.getByText('Add a feed', { exact: true }).click();
    await feeds.getByLabel('Feed URL').fill(`https://broken-feed.example/rss/${Date.now()}`);
    await feeds.getByLabel('Title (optional)').fill('Unreachable feed');
    await feeds.getByRole('button', { name: 'Add subscription' }).click();
    const broken = feeds.locator('.feed-subscription').filter({ hasText: 'Unreachable feed' });
    await broken.getByRole('button', { name: 'Poll now' }).click();
    await broken.getByText('Feed returned HTTP 503').waitFor();
    await broken.getByRole('button', { name: 'Pause', exact: true }).click();
    await broken.getByRole('button', { name: 'Resume', exact: true }).waitFor();
    page.once('dialog', (dialog) => dialog.accept());
    await broken.getByRole('button', { name: 'Remove', exact: true }).click();
    await broken.waitFor({ state: 'detached' });
    await feeds.getByRole('button', { name: 'Reader', exact: true }).click();
    assert.equal(await feeds.locator('img').count(), 0);
    const story = feeds.getByRole('button', { name: 'A first story', exact: true });
    await story.focus();
    await page.keyboard.press('Enter');
    await feeds.getByText('First external excerpt.', { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => document.activeElement.id), 'feed-item-heading');
    assert.equal(await story.count(), 0);
    assert.equal(await feeds.getByRole('link', { name: 'Open article' }).getAttribute('href'),
      'https://example.org/articles/first');
    for (const colorScheme of ['light', 'dark']) {
      await page.emulateMedia({ colorScheme });
      await audit(`feeds-${colorScheme}`);
      await page.screenshot({ path: path.join(options.output, `feeds-${colorScheme}.png`) });
    }
    await feeds.getByRole('button', { name: `Ask ${agentName} about this` }).click();
    await feeds.getByLabel('Your question').fill('What is in the fixture excerpt?');
    await feeds.getByRole('button', { name: 'Send to a new isolated topic' }).click();
    await page.getByRole('heading', { name: 'A first story', exact: true }).waitFor();
    await page.locator('.message.user').getByText('What is in the fixture excerpt?').waitFor();
    await page.locator('.message.assistant').getByText('I read the external feed excerpt.').waitFor();
    await page.locator('#settings-open').click();
    await page.getByRole('tab', { name: 'Feeds', exact: true }).click();
    await feeds.getByRole('button', { name: 'Subscriptions', exact: true }).click();
    page.once('dialog', (dialog) => dialog.accept());
    await approved.getByRole('button', { name: 'Remove', exact: true }).click();
    await approved.waitFor({ state: 'detached' });
    await page.getByRole('button', { name: 'Close Settings', exact: true }).click();
    await page.emulateMedia({ colorScheme: 'light' });
  });
  report.errors = errors;
  assert.deepEqual(errors, []);
} catch (error) {
  report.failure = error.message;
  report.errors = errors;
  await page.screenshot({
    path: path.join(options.output, 'failure.png'),
    fullPage: true
  });
  throw error;
} finally {
  await fs.writeFile(
    path.join(options.output, 'report.json'),
    JSON.stringify(report, null, 2) + '\n'
  );
  await browser.close();
}
