// Real Chromium acceptance against scripts/chat_fixture.py; no production credentials.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.SOLA_PLAYWRIGHT_PATH || 'playwright');
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
try {
  await page.goto(fixture.origin);
  await check('password login and recent 500-turn history', async () => {
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
          const rgb = (value) =>
            value
              .match(/[\d.]+/g)
              .slice(0, 3)
              .map(Number);
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
              error: contrast(
                getComputedStyle(document.querySelector('#topic-error')).color,
                root.backgroundColor
              )
            }
          };
        });
        assert(theme.scheme.includes(colorScheme));
        assert(colorScheme === 'dark' ? theme.luminance < 0.1 : theme.luminance > 0.8);
        for (const [name, ratio] of Object.entries(theme.contrast))
          assert(ratio >= 4.5, `${colorScheme} ${name}: ${ratio}`);
        report.themes[colorScheme] = theme;
        if (colorScheme === 'dark')
          await page.screenshot({
            path: path.join(options.output, 'chat-desktop-dark.png'),
            fullPage: true
          });
      }
    }
  );
  await check('older history is prepended with a preserved scroll anchor', async () => {
    await page.locator('#history').evaluate((h) => (h.scrollTop = 0));
    const anchor = page.locator('.turn').first();
    const identity = await anchor.getAttribute('data-run-id');
    const before = await anchor.boundingBox();
    report.anchorBefore = {
      box: before,
      ...(await page
        .locator('#history')
        .evaluate((h) => ({ top: h.scrollTop, height: h.scrollHeight })))
    };
    await page.locator('#older').click();
    await page.waitForFunction(
      (count) => document.querySelectorAll('.turn').length > count,
      report.initial.turns
    );
    const after = await page.locator(`[data-run-id="${identity}"]`).boundingBox();
    report.anchorAfter = {
      box: after,
      ...(await page
        .locator('#history')
        .evaluate((h) => ({ top: h.scrollTop, height: h.scrollHeight })))
    };
    assert(Math.abs(before.y - after.y) < 4, `anchor moved ${before.y - after.y}px`);
    report.afterOlder = await page.locator('.turn').count();
  });
  await check('hostile strings, tool uncertainty, staged memory and compaction', async () => {
    const body = await page.locator('#turns').textContent();
    assert(body.includes('effect uncertain'));
    assert(body.includes('Memory note staged'));
    assert(body.includes('Memory extraction · ambiguous'));
    assert(body.includes('original turns retained'));
    assert.equal(await page.locator('#turns img, #turns script').count(), 0);
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
    'local Lit components preserve disclosure inputs and settled nodes during updates',
    async () => {
      const state = await page.evaluate(async () => {
        const { LitElement } = await import('/assets/lit-core.min.js');
        const app = document.querySelector('sola-app');
        const turn = [...document.querySelectorAll('sola-turn')].find((el) =>
          el.querySelector('.work input')
        );
        const input = turn.querySelector('.work input');
        const detail = input.closest('details');
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
        detail.open = false;
        return result;
      });
      assert(Object.values(state).every(Boolean), JSON.stringify(state));
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
    assert.equal(await page.title(), 'Sola · Polished conversation');
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
    assert.equal(
      await page.locator('#turns .message.user').filter({ hasText: 'second queued draft' }).count(),
      1
    );
  });
  await check('cancellation and explicit model failure', async () => {
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
      await context.setOffline(false);
      await waitFor(() => document.querySelector('#connection').textContent === 'Connected');
      await page
        .locator(`[data-run-id="${run}"] .run-bar`)
        .filter({ hasText: 'completed' })
        .waitFor({ timeout: 30000 });
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
    await page.getByRole('button', { name: 'Retry same submission', exact: true }).click();
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
  await check('Settings job creation, pause, manual message and no-action occurrence', async () => {
    await page.locator('#settings-open').click();
    await page.locator('#job-task').fill('A scheduled fixture message');
    await page.locator('#job-schedule').fill('cron:0 9 * * *');
    await page.locator('#job-topic').selectOption(fixture.other);
    await page.locator('#job-form button.primary').click();
    const job = page
      .locator('#jobs article')
      .filter({ hasText: 'A scheduled fixture message' })
      .last();
    await job.getByRole('button', { name: 'Pause', exact: true }).click();
    await job.getByRole('button', { name: 'Resume', exact: true }).click();
    await job.getByRole('button', { name: 'Run now', exact: true }).click();
    await page.waitForTimeout(5000);
    await job.getByRole('button', { name: 'Occurrences', exact: true }).click();
    await job.getByText(/completed · message/).waitFor();
    await job.getByRole('button', { name: 'Edit', exact: true }).click();
    await page.locator('#job-task').fill('no action');
    await page.locator('#job-form button.primary').click();
    const noAction = page.locator('#jobs article').filter({ hasText: 'no action' }).last();
    await noAction.getByRole('button', { name: 'Run now', exact: true }).click();
    await page.waitForTimeout(1500);
    await noAction.getByRole('button', { name: 'Occurrences', exact: true }).click();
    await noAction.getByText(/completed · No action needed/).waitFor();
    await page.locator('#job-reset').click();
    await page.locator('#job-task').fill('no action proactive fixture');
    await page.locator('#job-schedule').fill('cron:0 10 * * *');
    await page.locator('#job-origin').selectOption('proactive');
    await page.locator('#job-form button.primary').click();
    const proactive = page
      .locator('#jobs article')
      .filter({ hasText: 'no action proactive fixture' })
      .last();
    await proactive.getByRole('button', { name: 'Run now', exact: true }).click();
    await page.waitForTimeout(1500);
    await proactive.getByRole('button', { name: 'Occurrences', exact: true }).click();
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
    await page.locator('#job-reset').click();
    await page.locator('#job-task').fill('Automatic scheduled UI message');
    await page.locator('#job-schedule').fill(`once:${new Date(Date.now() + 2500).toISOString()}`);
    await page.locator('#job-enabled').check();
    await page.locator('#job-form button.primary').click();
    const automatic = page
      .locator('#jobs article')
      .filter({ hasText: 'Automatic scheduled UI message' })
      .last();
    await page.waitForTimeout(8500);
    await automatic.getByRole('button', { name: 'Occurrences', exact: true }).click();
    await automatic.getByText(/completed · message/).waitFor();
    assert((await automatic.textContent()).includes('"trigger": "scheduled"'));
    await page.screenshot({
      path: path.join(options.output, 'settings-desktop.png'),
      fullPage: true
    });
    await page.getByRole('button', { name: 'Close Settings', exact: true }).click();
    await page.getByText('Sola · scheduled message', { exact: true }).last().waitFor();
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
    await page.getByRole('button', { name: 'Close evidence', exact: true }).click();
  });
  await check('auth expiry prompts for login and retains the editable draft', async () => {
    await page.locator('#draft').fill('Keep me after sign-in expires');
    await page.evaluate(async () => {
      const csrf = document.cookie
        .split('; ')
        .find((v) => v.startsWith('sola_csrf='))
        .split('=')[1];
      await fetch('/api/v1/auth/sessions', { method: 'DELETE', headers: { 'X-CSRF-Token': csrf } });
    });
    await page.locator('#login-dialog[open]').waitFor({ timeout: 15000 });
    await page.locator('#password').fill('phase11-disposable-fixture');
    await page.locator('#login-form button').click();
    await page.locator('#login-dialog').waitFor({ state: 'hidden' });
    await page.waitForTimeout(1000);
    assert.equal(await page.locator('#draft').inputValue(), 'Keep me after sign-in expires');
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
    assert(bounds.buttons.every((h) => h >= 44));
    assert.equal(await page.getByRole('textbox', { name: 'Message Sola', exact: true }).count(), 1);
    await page.screenshot({ path: path.join(options.output, 'chat-mobile.png'), fullPage: true });
    await page.locator('#settings-open').click();
    await page.screenshot({
      path: path.join(options.output, 'settings-mobile.png'),
      fullPage: true
    });
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'settings-open');
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
        await page.getByRole('button', { name: 'Inspect evidence', exact: true }).last().click();
        assert(await page.locator('#evidence').evaluate((el) => el.scrollWidth <= el.clientWidth));
        await page.keyboard.press('Escape');
      }
    }
  );
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
  report.errors = errors;
  assert.deepEqual(errors, []);
} catch (error) {
  report.failure = error.message;
  report.errors = errors;
  await page.screenshot({ path: path.join(options.output, 'failure.png'), fullPage: true });
  throw error;
} finally {
  await fs.writeFile(
    path.join(options.output, 'report.json'),
    JSON.stringify(report, null, 2) + '\n'
  );
  await browser.close();
}
