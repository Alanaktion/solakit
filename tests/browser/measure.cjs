/* Reproducible synthetic audit measurements; uses the isolated QA server only. */
const { request } = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const client = await request.newContext({baseURL: 'http://127.0.0.1:18382', timeout: 120000});
  try {
    await client.post('/login', {form: {token: 'scratch-browser-token'}});
    for (const count of [44, 500]) {
      const fixture = await (await client.post('/qa/ux-history?count=' + count)).json();
      const page = await (await client.get('/sessions/' + fixture.session)).text();
      assert.equal((page.match(/data-run-id=/g) || []).length, 20);
      const started = Date.now();
      const response = await client.post(`/sessions/${fixture.session}/messages`, {
        form: {message: 'measure-browser'}, headers: {Accept: 'application/json'},
      });
      assert.equal(response.status(), 202);
      const accepted = await response.json();
      const events = await client.get(`/sessions/${fixture.session}/events?run=${accepted.run_id}`);
      const body = await events.text();
      const payloads = body.split('\n').filter(line => line.startsWith('data: ')).map(line => line.slice(6));
      for (const payload of payloads) {
        const snapshot = JSON.parse(payload);
        assert.equal(snapshot.run_id, accepted.run_id);
        assert.equal((snapshot.html.match(/data-run-id=/g) || []).length, 1);
        assert(!snapshot.html.includes(fixture.last));
      }
      const sizes = payloads.map(payload => Buffer.byteLength(payload));
      assert(Math.max(...sizes) < 3000);
      console.log(JSON.stringify({turns: count, pageBytes: Buffer.byteLength(page), durationMs: Date.now()-started,
        snapshots: sizes.length, minBytes: Math.min(...sizes), maxBytes: Math.max(...sizes),
        totalBytes: sizes.reduce((a,b)=>a+b,0)}));
    }
  } finally { await client.dispose(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
