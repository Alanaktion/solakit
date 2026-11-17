import { LitElement, html, nothing, render as renderTemplate } from './lit-core.min.js';
import { markdown } from './markdown.js';

const $ = (id) => document.getElementById(id);
const active = (run) => ['queued', 'running'].includes(run.state);
const uid = () =>
  Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) =>
    b.toString(16).padStart(2, '0')
  ).join('');

// Light DOM keeps the shared stylesheet, labels and fragment links working across components.
class SolaElement extends LitElement {
  createRenderRoot() {
    return this;
  }
}

class SolaApp extends SolaElement {
  render() {
    return html`
      <a class="skip" href="#draft">Skip to message</a>
      <aside class="sidebar" aria-label="Topics">
        <div class="brand">Sola <span>Your ongoing conversation</span></div>
        <button @click=${newTopic} id="new-topic">＋ New topic</button>
        <nav id="topics" aria-label="Choose a topic"></nav>
        <button @click=${action(() => loadTopics(true))} id="more-topics" hidden>
          More topics
        </button>
        <label class="archive-toggle"
          ><input @change=${action(() => loadTopics())} id="archived-topics" type="checkbox" />
          Archived topics</label
        >
        <div class="sidebar-footer">
          <button @click=${action(openSettings)} id="settings-open">Settings</button
          ><button
            @click=${action(async () => {
              await api('/auth/logout', { method: 'POST' });
              requireLogin();
            })}
            id="logout"
          >
            Sign out
          </button>
        </div>
      </aside>
      <main>
        <header class="topbar">
          <div>
            <h1 id="topic-title">Conversation</h1>
            <p id="topic-mode">A little room to think.</p>
          </div>
          <button @click=${editTopic} id="topic-edit" disabled>Topic details</button>
        </header>
        <p id="connection" role="status" aria-live="polite">Connecting…</p>
        <div id="notice" role="alert" hidden>
          <span id="notice-message"></span
          ><button
            @click=${() => {
              $('notice').hidden = true;
            }}
            id="notice-dismiss"
          >
            Dismiss
          </button>
        </div>
        <section @scroll=${historyScroll} id="history" aria-label="Conversation" tabindex="0">
          <button @click=${action(older)} id="older" hidden>Load older conversation</button>
          <div id="empty">
            <h2>What’s on your mind?</h2>
            <p id="empty-hint">Start a topic, then send Sola a message.</p>
            <button @click=${newTopic} id="empty-new-topic" class="primary">Start a topic</button>
          </div>
          <div id="turns"></div>
        </section>
        <button @click=${jumpLatest} id="latest" hidden>Jump to latest ↓</button>
        <div id="outbox" aria-label="Submissions awaiting confirmation"></div>
        <form @submit=${submit} id="composer">
          <label class="sr-only" for="draft">Message Sola</label>
          <textarea
            @keydown=${draftKeydown}
            @input=${saveDraft}
            id="draft"
            rows="3"
            maxlength="32768"
            placeholder="Message Sola…"
            aria-describedby="compose-hint"
            required
          ></textarea>
          <div class="composer-actions">
            <span id="compose-hint">Enter to send · Shift + Enter for a new line</span>
            <label
              >Profile
              <select @change=${saveDraft} id="profile">
                <option value="">Server default</option>
              </select></label
            >
            <button class="primary" id="send" type="submit" aria-label="Send message" disabled>
              Send ↑
            </button>
          </div>
        </form>
      </main>
      <dialog
        @cancel=${(event) => event.preventDefault()}
        id="login-dialog"
        aria-labelledby="login-title"
      >
        <form @submit=${login} id="login-form">
          <h2 id="login-title">Welcome to Sola</h2>
          <p id="login-reason">Sign in with your operator password.</p>
          <label for="password">Password</label
          ><input id="password" type="password" autocomplete="current-password" required />
          <p id="login-error" role="alert"></p>
          <button class="primary">Sign in</button>
        </form>
      </dialog>
      <dialog id="topic-dialog" aria-labelledby="topic-dialog-title">
        <form @submit=${saveTopic} id="topic-form">
          <h2 id="topic-dialog-title">New topic</h2>
          <p id="topic-error" role="alert"></p>
          <label for="topic-name">Title</label
          ><input id="topic-name" maxlength="256" autocomplete="off" autofocus />
          <label for="mode">Memory mode</label
          ><select id="mode">
            <option value="shared">Shared memory</option>
            <option value="isolated">Isolated — no shared memory</option>
            <option value="ephemeral">Ephemeral — one run, evidence retained</option>
          </select>
          <label><input type="checkbox" id="pinned" /> Pin topic</label
          ><label><input type="checkbox" id="archived" /> Archive topic</label>
          <div class="actions">
            <button
              type="button"
              data-close="topic-dialog"
              @click=${() => $('topic-dialog').close()}
            >
              Cancel</button
            ><button class="primary">Save topic</button>
          </div>
        </form>
      </dialog>
      <dialog id="settings" class="wide" aria-labelledby="settings-title">
        <header>
          <h2 id="settings-title">Settings</h2>
          <button data-close="settings" @click=${() => $('settings').close()}>
            Close Settings
          </button>
        </header>
        <p>
          Authority and credentials are managed in your operator configuration. Changes below use
          your signed-in authority.
        </p>
        <details open>
          <summary>Scheduled &amp; proactive work</summary>
          <div class="actions">
            <button @click=${action(() => loadJobs())} id="jobs-refresh">Refresh jobs</button
            ><button
              @click=${action(async () => {
                await api('/jobs/control', { method: 'PUT', body: { paused: !schedulerPaused } });
                await loadJobs();
              })}
              id="scheduler-toggle"
            >
              Pause all schedules
            </button>
          </div>
          <p id="jobs-empty" class="meta" hidden>No jobs yet. Create one below when you need it.</p>
          <div id="jobs"></div>
          <button @click=${action(() => loadJobs(true))} id="more-jobs" hidden>More jobs</button>
          <form @submit=${saveJob} id="job-form">
            <h3 id="job-form-title">Create a job</h3>
            <p id="job-topic-hint" class="meta" hidden>
              Create a shared or isolated topic before scheduling work.
            </p>
            <label for="job-task">Task</label
            ><textarea id="job-task" required maxlength="32768" rows="3"></textarea>
            <div class="fields">
              <div>
                <label for="job-schedule">Schedule (cron: or once:)</label
                ><input id="job-schedule" required placeholder="cron:0 9 * * *" />
              </div>
              <div>
                <label for="job-zone">Time zone</label
                ><input id="job-zone" value="America/Denver" required />
              </div>
            </div>
            <label for="job-topic">Topic</label
            ><select id="job-topic" required></select>
            <label for="job-origin">Kind</label
            ><select id="job-origin">
              <option value="scheduled">Scheduled task</option>
              <option value="proactive">Proactive wake-up</option>
            </select>
            <label><input id="job-enabled" type="checkbox" /> Enable wake-ups for this job</label>
            <p>
              Saving an enabled job authorizes automatic work in its topic, within the selected
              profile’s permissions.
            </p>
            <div class="actions">
              <button type="button" @click=${() => fillJob()} id="job-reset">Clear</button
              ><button class="primary">Save job</button>
            </div>
          </form>
        </details>
        <details>
          <summary>Memory</summary>
          <button @click=${action(refreshMemory)} id="memory-refresh">Refresh memory status</button>
          <pre id="memory-status"></pre>
          <p>
            Staged notes are waiting for extraction. Only completed extraction is reported as saved
            memory. Inspect a run to reconcile failed or ambiguous extraction with a verified
            provider task ID.
          </p>
        </details>
        <details>
          <summary>Profiles, tools &amp; workspace roots</summary>
          <div id="profiles"></div>
          <label for="tool-profile">Tools for profile</label
          ><select @change=${action(loadTools)} id="tool-profile"></select>
          <pre id="tools"></pre>
        </details>
        <details>
          <summary>Credential references</summary>
          <p>
            Credential values are never loaded into this page. References in profile diagnostics are
            redacted; configure their environment variables outside the browser.
          </p>
        </details>
        <details>
          <summary>Search conversation evidence</summary>
          <form @submit=${search} id="search-form">
            <label for="search-query">Search</label
            ><input id="search-query" required maxlength="4096" /><label
              ><input type="checkbox" id="search-topic" checked /> Current topic only</label
            ><button>Search</button>
          </form>
          <div id="search-results"></div>
        </details>
        <details>
          <summary>Diagnostics</summary>
          <label
            ><input @change=${connect} id="poll-only" type="checkbox" /> Use polling instead of
            streaming</label
          ><button @click=${action(refreshHealth)} id="health-refresh">Refresh health</button>
          <pre id="health"></pre>
          <p id="transport-detail"></p>
        </details>
        <p id="settings-status" role="status"></p>
        <p id="settings-error" role="alert"></p>
      </dialog>
      <dialog id="evidence" class="wide" aria-labelledby="evidence-title">
        <header>
          <h2 id="evidence-title">Run evidence</h2>
          <button data-close="evidence" @click=${() => $('evidence').close()}>
            Close evidence
          </button>
        </header>
        <div id="evidence-records"></div>
        <button @click=${action(moreEvidence)} id="more-evidence">Load more records</button>
        <p id="evidence-error" role="alert"></p>
      </dialog>
    `;
  }

  firstUpdated() {
    initialize();
  }
}

const action = (callback) => (event) => {
  Promise.resolve()
    .then(() => callback(event))
    .catch(showError);
};

const text = (element, value) => {
  if (element.textContent !== value) element.textContent = value;
};
const read = (key, fallback) => {
  try {
    return JSON.parse(sessionStorage.getItem(key)) ?? fallback;
  } catch {
    return fallback;
  }
};
const write = (key, value) => {
  try {
    sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    showError(
      new Error(
        'Browser storage is full. Keep this tab open to preserve drafts and pending submissions.'
      )
    );
  }
};
const outbox = read('sola.outbox', []);
const topics = new Map(),
  messages = new Map(),
  runs = new Map(),
  records = new Map(),
  turns = new Map();
let selected = null,
  generation = 0,
  source = null,
  polling = null,
  retryStream = null;
let loggedIn = false,
  topicCursor = null,
  jobCursor = null,
  olderMessages = 0,
  olderRuns = 0;
let messagesDone = false,
  runsDone = false,
  following = true,
  refreshTimer = null;
let editingTopic = false,
  editingJob = null,
  schedulerPaused = false,
  evidenceRun = null,
  evidenceCursor = 0;
let refreshChain = Promise.resolve(),
  cursor = null;
const pendingRuns = new Set();

function showError(error) {
  const target = $('evidence').open
    ? $('evidence-error')
    : $('settings').open
      ? $('settings-error')
      : $('topic-dialog').open
        ? $('topic-error')
        : $('notice-message');
  text(target, error.message || 'Request failed. Please retry.');
  target.hidden = false;
  if (target.id === 'notice-message') $('notice').hidden = false;
}
function requireLogin() {
  loggedIn = false;
  stopTransport();
  if (!$('login-dialog').open) {
    $('login-dialog').showModal();
    $('password').focus();
  }
  text(
    $('connection'),
    'Sign in to reconnect. Drafts and pending submissions are retained in this tab.'
  );
}
async function api(path, options = {}) {
  const headers = new Headers(options.headers);
  if (options.body !== undefined) {
    headers.set('Content-Type', 'application/json');
    options = { ...options, body: JSON.stringify(options.body) };
  }
  if (options.method && !['GET', 'HEAD'].includes(options.method)) {
    const csrf = document.cookie
      .split('; ')
      .find((value) => value.startsWith('sola_csrf='))
      ?.split('=')[1];
    if (csrf) headers.set('X-CSRF-Token', csrf);
  }
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers,
    credentials: 'same-origin'
  });
  if (response.status === 401) {
    requireLogin();
    throw new Error('Your sign-in expired. Sign in to continue.');
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error?.message || `Request failed (${response.status}).`);
  }
  return response.status === 204 ? null : response.json();
}
function preserveScroll(action, prepend = false) {
  const history = $('history');
  const anchor = prepend
    ? [...$('turns').children].find(
        (el) => el.getBoundingClientRect().bottom > history.getBoundingClientRect().top
      )
    : null;
  const top = anchor?.getBoundingClientRect().top;
  action();
  if (anchor?.isConnected) history.scrollTop += anchor.getBoundingClientRect().top - top;
  else if (following) history.scrollTop = history.scrollHeight;
  $('latest').hidden = following;
}
function merge(kind, rows) {
  const map = kind === 'message' ? messages : runs;
  for (const row of rows) {
    const old = map.get(row.record.id);
    if (JSON.stringify(old) !== JSON.stringify(row)) map.set(row.record.id, row);
    if (kind === 'run') {
      const index = outbox.findIndex((item) => item.key === row.record.request_id);
      if (index !== -1) {
        outbox.splice(index, 1);
        write('sola.outbox', outbox);
        renderOutbox();
      }
    }
  }
}
class SolaTurn extends SolaElement {
  static properties = {
    run: { attribute: false },
    messageRows: { attribute: false },
    workEntries: { attribute: false },
    preview: { state: true },
    elapsed: { state: true }
  };

  constructor() {
    super();
    this.messageRows = [];
    this.workEntries = new Map();
    this.messageNodes = new Map();
    this.preview = '';
    this.elapsed = '';
  }

  renderMessage(row) {
    const m = row.record;
    if (!this.messageNodes.has(m.id)) {
      const author =
        m.role === 'user'
          ? 'You'
          : m.origin === 'proactive'
            ? 'Sola · proactive message'
            : m.origin === 'scheduled'
              ? 'Sola · scheduled message'
              : 'Sola';
      const message = document.createElement('div');
      message.className = `message ${m.role}`;
      message.dataset.messageId = m.id;
      renderTemplate(
        html`
          <div class="author">${author}</div>
          <div>
            ${markdown(m.content || (row.truncated ? 'Large message — inspect evidence.' : ''))}
          </div>
        `,
        message
      );
      this.messageNodes.set(m.id, message);
    }
    return this.messageNodes.get(m.id);
  }

  render() {
    const run = this.run;
    const origin = ['proactive', 'scheduled'].includes(run?.origin)
      ? `${run.origin === 'proactive' ? 'Proactive wake-up' : 'Scheduled task'} · `
      : '';
    return html`
      <div>${this.messageRows.map((row) => this.renderMessage(row))}</div>
      <div class="work">
        ${[...this.workEntries.values()].map(
          (entry) => html`
            <details>
              <summary>${entry.label}</summary>
              <pre>${JSON.stringify(entry.body, null, 2)}</pre>
              ${
                entry.reconcile
                  ? html`
                      <form
                        @submit=${(event) => {
                          event.preventDefault();
                          const providerTask = event.currentTarget.querySelector('input').value;
                          action(async () => {
                            await api(`/memory/${entry.id}/reconcile`, {
                              method: 'POST',
                              body: { provider_task_id: providerTask }
                            });
                            queueRefresh(this.dataset.runId);
                          })();
                        }}
                      >
                        <label for=${`reconcile-${entry.id}`}>Verified provider task ID</label>
                        <input id=${`reconcile-${entry.id}`} required maxlength="256" />
                        <button>Reconcile extraction</button>
                      </form>
                    `
                  : nothing
              }
            </details>
          `
        )}
      </div>
      <div class="preview" ?hidden=${!this.preview}>${this.preview}</div>
      <div class="run-bar">
        <span role="status" aria-live="polite"
          >${run ? `${origin}${run.state}${run.reason ? ` · ${run.reason.message}` : ''}` : ''}</span
        >
        <span>${this.elapsed}</span>
        <button
          type="button"
          ?hidden=${!run || !active(run)}
          @click=${action(async () => {
            const id = this.dataset.runId;
            const result = await api(`/runs/${id}/cancel`, { method: 'POST' });
            merge('run', [{ ...runs.get(id), record: result }]);
            render();
          })}
        >
          Cancel run
        </button>
        <button type="button" @click=${action(() => openEvidence(this.dataset.runId))}>
          Inspect evidence
        </button>
      </div>
    `;
  }
}

function turnFor(id) {
  if (!turns.has(id)) {
    const turn = document.createElement('sola-turn');
    turn.className = 'turn';
    turn.dataset.runId = id;
    turns.set(id, turn);
  }
  return turns.get(id);
}
function renderWork(turn, id) {
  const rows = [...(records.get(id)?.values() || [])].sort((a, b) => a.sequence - b.sequence);
  const calls = new Map(
    rows.filter((row) => row.kind === 'tool').map((row) => [row.record.id, row.record])
  );
  const receipts = new Map(
    rows.filter((row) => row.kind === 'receipt').map((row) => [row.record.tool_call_id, row.record])
  );
  const entries = [];
  for (const [callId, call] of calls) {
    const receipt = receipts.get(callId);
    entries.push({
      id: callId,
      label: `${call.name} · ${receipt?.status || (active(runs.get(id)?.record || {}) ? 'working' : 'no receipt — inspect evidence')}${receipt?.effect === 'uncertain' ? ' · effect uncertain' : ''}`,
      body: { arguments: call.arguments, ...(receipt || {}) }
    });
  }
  for (const [callId, receipt] of receipts)
    if (!calls.has(callId))
      entries.push({
        id: callId,
        label: `Tool · ${receipt.status}${receipt.effect === 'uncertain' ? ' · effect uncertain' : ''}`,
        body: receipt
      });
  for (const row of rows) {
    const r = row.record;
    if (row.kind === 'memory_note')
      entries.push({ id: r.id, label: 'Memory note staged — awaiting extraction', body: r });
    if (row.kind === 'memory')
      entries.push({
        id: r.id,
        label: `Memory extraction · ${r.state === 'completed' ? 'completed — saved' : r.state}`,
        body: r,
        reconcile: ['ambiguous', 'failed'].includes(r.state)
      });
    if (row.kind === 'compaction')
      entries.push({
        id: r.id,
        label: 'Earlier context summarized · original turns retained',
        body: r
      });
    if (row.kind === 'delivery')
      entries.push({ id: r.id, label: `Scheduled message · ${r.state}`, body: r });
    if (row.kind === 'recovery') entries.push({ id: r.id, label: 'Recovery note', body: r });
  }
  for (const entry of entries) turn.workEntries.set(entry.id, entry);
  turn.requestUpdate();
}

function render() {
  preserveScroll(() => {
    const ids = new Set([
      ...runs.keys(),
      ...[...messages.values()].map((row) => row.record.run_id)
    ]);
    const sorted = [...ids].sort((a, b) => {
      const position = (id) =>
        runs.get(id)?.sequence ??
        Math.min(
          ...[...messages.values()]
            .filter((row) => row.record.run_id === id)
            .map((row) => row.sequence)
        );
      return position(a) - position(b);
    });
    for (let index = 0; index < sorted.length; index++) {
      const id = sorted[index],
        turn = turnFor(id),
        run = runs.get(id)?.record;
      // Keep keyed turn elements in place so prepending history preserves selections.
      if (!turn.isConnected) {
        const next = sorted
          .slice(index + 1)
          .map((key) => turns.get(key))
          .find((root) => root?.isConnected);
        $('turns').insertBefore(turn, next || null);
      }
      turn.messageRows = [...messages.values()]
        .filter((row) => row.record.run_id === id && row.record.role !== 'tool')
        .sort((a, b) => a.sequence - b.sequence);
      turn.run = run;
      if (run && !active(run)) turn.preview = '';
      renderWork(turn, id);
      // Flush Lit before measuring scroll anchors and following the latest content.
      turn.performUpdate();
    }
    $('empty').hidden = ids.size > 0;
  });
  updateTimers();
  updateComposer();
}
function updateTimers() {
  for (const [id, turn] of turns) {
    const r = runs.get(id)?.record;
    if (!r?.started_at) {
      turn.elapsed = '';
      turn.performUpdate();
      continue;
    }
    const seconds = Math.max(
      0,
      Math.floor(
        ((r.ended_at ? Date.parse(r.ended_at) : Date.now()) - Date.parse(r.started_at)) / 1000
      )
    );
    turn.elapsed = `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    turn.performUpdate();
  }
}
async function loadRecords(id, token) {
  const result = await api(`/runs/${id}/records?before=0&limit=50`);
  if (token !== generation) return;
  if (!records.has(id)) records.set(id, new Map());
  for (const row of result.items) {
    records.get(id).set(`${row.kind}:${row.record.id}`, row);
    if (row.kind === 'run') merge('run', [row]);
  }
  const delivered = new Set(
    [...records.get(id).values()]
      .filter((row) => row.kind === 'delivery' && row.record.state === 'delivered')
      .map((row) => row.record.message_id)
  );
  for (const row of records.get(id).values()) {
    if (
      row.kind === 'message' &&
      (!['scheduled', 'proactive'].includes(row.record.origin) ||
        (row.record.role === 'assistant' && delivered.has(row.record.id)))
    )
      merge('message', [row]);
  }
}
async function refresh(ids = []) {
  if (!selected || !loggedIn) return;
  const token = generation,
    topic = selected;
  const [messagePage, runPage] = await Promise.all([
    api(`/sessions/${topic}/messages?before=0&limit=25`),
    api(`/sessions/${topic}/runs?before=0&limit=25`)
  ]);
  if (token !== generation) return;
  const changed = runPage.items
    .filter((row) => JSON.stringify(row.record) !== JSON.stringify(runs.get(row.record.id)?.record))
    .map((row) => row.record.id);
  merge('message', messagePage.items);
  merge('run', runPage.items);
  const missing = new Set(
    messagePage.items.map((row) => row.record.run_id).filter((id) => !runs.has(id))
  );
  const inspect = new Set([
    ...ids,
    ...changed,
    ...runPage.items
      .filter((row) => !records.has(row.record.id) || active(row.record))
      .map((row) => row.record.id),
    ...missing
  ]);
  await Promise.all(
    [...inspect].map(async (id) => {
      const run = await api(`/runs/${id}`);
      if (token !== generation || run.session_id !== topic) return;
      const old = runs.get(id);
      merge('run', [
        {
          sequence:
            old?.sequence ??
            messagePage.items.find((row) => row.record.run_id === id)?.sequence ??
            Number.MAX_SAFE_INTEGER,
          record: run,
          kind: 'run',
          truncated: false
        }
      ]);
      await loadRecords(id, token);
    })
  );
  if (token !== generation) return;
  render();
}
function queueRefresh(id) {
  for (const identity of Array.isArray(id) ? id : id ? [id] : []) pendingRuns.add(identity);
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    const ids = [...pendingRuns];
    pendingRuns.clear();
    const token = generation;
    refreshChain = refreshChain
      .catch(() => {})
      .then(() => (token === generation ? refresh(ids) : null))
      .catch(showError);
  }, 100);
}
async function older() {
  const token = generation,
    topic = selected;
  const [mp, rp] = await Promise.all([
    messagesDone
      ? { items: [] }
      : api(`/sessions/${topic}/messages?before=${olderMessages}&limit=25`),
    runsDone ? { items: [] } : api(`/sessions/${topic}/runs?before=${olderRuns}&limit=25`)
  ]);
  if (token !== generation) return;
  messagesDone ||= !mp.items.length;
  runsDone ||= !rp.items.length;
  if (mp.items.length) olderMessages = mp.next;
  if (rp.items.length) olderRuns = rp.next;
  merge('message', mp.items);
  merge('run', rp.items);
  const inspect = new Set([
    ...rp.items.map((row) => row.record.id),
    ...mp.items.map((row) => row.record.run_id)
  ]);
  await Promise.all(
    [...inspect].map(async (id) => {
      if (!runs.has(id)) {
        const run = await api(`/runs/${id}`);
        if (token !== generation) return;
        merge('run', [
          { sequence: mp.items.find((row) => row.record.run_id === id).sequence, record: run }
        ]);
      }
      await loadRecords(id, token);
    })
  );
  if (token !== generation) return;
  const follow = following;
  following = false;
  preserveScroll(render, true);
  following = follow;
  $('older').hidden = messagesDone && runsDone;
}
function stopTransport() {
  source?.close();
  source = null;
  clearInterval(polling);
  polling = null;
  clearTimeout(retryStream);
}
function startPolling() {
  if (polling) return;
  for (const turn of turns.values()) {
    turn.preview = '';
    turn.performUpdate();
  }
  text(
    $('connection'),
    navigator.onLine
      ? 'Reconnecting · checking for updates every 5 seconds'
      : 'Offline · drafts are kept in this tab'
  );
  polling = setInterval(() => queueRefresh([...runs.keys()]), 5000);
}
function connect() {
  stopTransport();
  if (!selected || !loggedIn) return;
  if ($('poll-only').checked) {
    startPolling();
    text($('connection'), 'Polling · checking for updates every 5 seconds');
    return;
  }
  const token = generation;
  const stream = new EventSource(
    `/api/v1/sessions/${selected}/events${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`
  );
  source = stream;
  stream.onopen = () => {
    if (token !== generation) return;
    clearInterval(polling);
    polling = null;
    text($('connection'), 'Connected');
  };
  const snapshot = (event) => {
    if (token !== generation) return;
    const data = JSON.parse(event.data);
    cursor = data.cursor;
    for (const turn of turns.values()) {
      turn.preview = '';
      turn.performUpdate();
    }
    topics.set(data.session.id, data.session);
    renderTopics();
    setTopicHeading();
    queueRefresh(data.runs.filter((run) => runs.has(run.id) || active(run)).map((run) => run.id));
  };
  stream.addEventListener('snapshot', snapshot);
  stream.addEventListener('reset', snapshot);
  for (const type of [
    'run.admitted',
    'run.started',
    'run.terminal',
    'message.appended',
    'model.requested',
    'model.recorded',
    'tool.intent',
    'tool.receipt',
    'memory.changed',
    'delivery.changed',
    'compaction.recorded',
    'recovery.noted'
  ]) {
    stream.addEventListener(type, (event) => {
      if (token !== generation) return;
      const data = JSON.parse(event.data);
      if (cursor && data.sequence <= Number(cursor.split(':')[1])) return;
      cursor = event.lastEventId;
      if (['model.recorded', 'run.terminal'].includes(type)) {
        const turn = turns.get(data.run_id);
        if (turn) {
          turn.preview = '';
          turn.performUpdate();
        }
      }
      queueRefresh(data.run_id);
    });
  }
  stream.addEventListener('preview', (event) => {
    if (token !== generation) return;
    const p = JSON.parse(event.data),
      run = runs.get(p.run_id)?.record;
    if (!run || !active(run)) return;
    const turn = turnFor(p.run_id);
    preserveScroll(() => {
      turn.preview = `${p.truncated ? '…' : ''}${p.text}`;
      turn.performUpdate();
    });
  });
  stream.addEventListener('auth.expired', requireLogin);
  stream.onerror = () => {
    if (token !== generation || !loggedIn) return;
    stream.close();
    startPolling();
    api('/auth/session')
      .then(() => {
        retryStream = setTimeout(connect, 10000);
      })
      .catch((error) => {
        if (loggedIn) {
          showError(error);
          retryStream = setTimeout(connect, 10000);
        }
      });
  };
}
function saveDraft() {
  if (selected)
    write(`sola.draft.${selected}`, {
      content: $('draft').value,
      profile: $('profile').value
    });
  updateComposer();
}
function updateComposer() {
  const topic = topics.get(selected);
  const used =
    topic?.mode === 'ephemeral' &&
    (runs.size > 0 || outbox.some((item) => item.session_id === selected));
  $('draft').disabled = !topic;
  $('send').disabled = !topic || topic.archived || used || !$('draft').value.trim();
  $('compose-hint').classList.toggle('explanation', !!(topic?.archived || used));
  text(
    $('compose-hint'),
    topic?.archived
      ? 'Restore this topic in Topic details to send a message.'
      : used
        ? 'This topic allows one run. Start a new topic to continue.'
        : 'Enter to send · Shift + Enter for a new line'
  );
}
function setTopicHeading() {
  const topic = topics.get(selected);
  const title = topic ? topic.title || 'Untitled topic' : 'Conversation';
  text($('topic-title'), title);
  document.title = `Sola · ${title}`;
  const modes = {
    shared: 'Shared memory',
    isolated: 'Isolated memory',
    ephemeral: 'One run · evidence retained'
  };
  text(
    $('topic-mode'),
    topic ? `${modes[topic.mode]}${topic.archived ? ' · archived' : ''}` : 'A little room to think.'
  );
  text(
    $('empty-hint'),
    topic
      ? 'Send a message to start this conversation.'
      : 'Start a topic, then send Sola a message.'
  );
  $('empty-new-topic').hidden = !!topic;
  $('topic-edit').disabled = !topic;
  updateComposer();
}
async function selectTopic(id) {
  saveDraft();
  stopTransport();
  generation++;
  const token = generation;
  selected = id;
  cursor = null;
  clearTimeout(refreshTimer);
  refreshTimer = null;
  pendingRuns.clear();
  write('sola.selected', id);
  messages.clear();
  runs.clear();
  records.clear();
  turns.clear();
  $('turns').replaceChildren();
  olderMessages = olderRuns = 0;
  messagesDone = runsDone = false;
  following = true;
  const draft = read(`sola.draft.${id}`, {});
  $('draft').value = draft.content || '';
  $('profile').value = draft.profile || '';
  setTopicHeading();
  renderTopics();
  renderOutbox();
  await older();
  if (token !== generation) return;
  $('history').scrollTop = $('history').scrollHeight;
  connect();
}
function renderTopics() {
  renderTemplate(
    [...topics.values()].map(
      (topic) => html`
        <button
          type="button"
          id=${`topic-${topic.id}`}
          ?hidden=${topic.archived !== $('archived-topics').checked && topic.id !== selected}
          aria-current=${topic.id === selected ? 'page' : nothing}
          @click=${action(() => selectTopic(topic.id))}
        >
          ${`${topic.pinned ? '⌑ ' : ''}${topic.title || 'Untitled topic'}${topic.archived ? ' (archived)' : ''}`}
        </button>
      `
    ),
    $('topics')
  );
  revealTopic();
}
function revealTopic() {
  $(`topic-${selected}`)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}
async function loadTopics(more = false) {
  const result = await api(
    `/sessions?limit=50&include_ephemeral=true&archived=${$('archived-topics').checked}${more && topicCursor ? `&after=${encodeURIComponent(topicCursor)}` : ''}`
  );
  for (const topic of result.items) topics.set(topic.id, topic);
  topicCursor = result.next;
  $('more-topics').hidden = result.items.length < 50;
  renderTopics();
}
function renderOutbox() {
  renderTemplate(
    outbox
      .filter((item) => item.session_id === selected)
      .map(
        (item) => html`
          <div class="pending" data-request-id=${item.key}>
            <span
              >${`${item.sending ? 'Submitting' : 'Awaiting confirmation'} · ${item.content.slice(0, 80)}`}</span
            >
            ${item.sending ? nothing : html`<button type="button" @click=${action(() => deliver(item))}>Retry same submission</button>`}
          </div>
        `
      ),
    $('outbox')
  );
}

async function deliver(item) {
  if (item.sending) return;
  item.sending = true;
  renderOutbox();
  try {
    const result = await api('/runs', {
      method: 'POST',
      headers: { 'Idempotency-Key': item.key },
      body: {
        session_id: item.session_id,
        content: item.content,
        ...(item.profile ? { profile: item.profile } : {})
      }
    });
    const index = outbox.findIndex((value) => value.key === item.key);
    if (index !== -1) outbox.splice(index, 1);
    write('sola.outbox', outbox);
    $('notice').hidden = true;
    if (item.session_id === selected) {
      merge('run', [{ record: result.run, sequence: Number.MAX_SAFE_INTEGER }]);
      queueRefresh(result.run.id);
    }
  } catch (error) {
    showError(error);
  } finally {
    item.sending = false;
    write('sola.outbox', outbox);
    renderOutbox();
  }
}
async function submit(event) {
  event.preventDefault();
  if ($('send').disabled) return;
  if (new TextEncoder().encode($('draft').value).length > 32768) {
    showError(new Error('Please keep messages under 32 KiB.'));
    return;
  }
  const item = {
    key: uid(),
    session_id: selected,
    profile: $('profile').value,
    content: $('draft').value,
    sending: false
  };
  outbox.push(item);
  write('sola.outbox', outbox);
  $('draft').value = '';
  saveDraft();
  $('draft').focus();
  await deliver(item);
}
async function openEvidence(id) {
  evidenceRun = id;
  evidenceCursor = 0;
  evidenceRows = [];
  renderTemplate(nothing, $('evidence-records'));
  text($('evidence-error'), '');
  if (!$('evidence').open) $('evidence').showModal();
  await moreEvidence();
}
class SolaArtifact extends SolaElement {
  static properties = {
    runId: { attribute: false },
    reference: { attribute: false },
    chunk: { state: true }
  };

  async load(offset = 0) {
    const identity = this.reference.path.split('/').pop();
    const response = await fetch(
      `/api/v1/runs/${this.runId}/artifacts/${identity}?offset=${offset}&limit=65536`,
      { credentials: 'same-origin' }
    );
    if (response.status === 401) {
      requireLogin();
      throw new Error('Sign in to inspect artifacts.');
    }
    if (!response.ok) throw new Error('Artifact could not be authorized or verified.');
    this.chunk = {
      bytes: await response.arrayBuffer(),
      offset,
      next: Number(response.headers.get('X-Next-Offset')),
      length: Number(response.headers.get('X-Artifact-Length'))
    };
  }

  download() {
    const { bytes, offset } = this.chunk;
    const url = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `${this.reference.path.split('/').pop()}-${offset}.bin`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  render() {
    const chunk = this.chunk;
    return html`
      <button type="button" @click=${action(() => this.load())}>
        Inspect artifact · ${this.reference.length} bytes
      </button>
      ${
        chunk
          ? html`
              <div>
                <p>
                  Artifact bytes ${chunk.offset}–${chunk.offset + chunk.bytes.byteLength} of
                  ${chunk.length}
                </p>
                <pre>${new TextDecoder().decode(chunk.bytes)}</pre>
                <button type="button" @click=${() => this.download()}>Download this chunk</button>
                ${
                  chunk.next < chunk.length
                    ? html`
                        <button type="button" @click=${action(() => this.load(chunk.next))}>
                          Next chunk
                        </button>
                      `
                    : nothing
                }
              </div>
            `
          : nothing
      }
    `;
  }
}

let evidenceRows = [];
async function moreEvidence() {
  const id = evidenceRun,
    result = await api(`/runs/${id}/records?after=${evidenceCursor}&limit=50`);
  if (id !== evidenceRun) return;
  evidenceRows.push(...result.items);
  renderTemplate(
    evidenceRows.map((row) => {
      const artifacts = row.truncated
        ? Object.values(row.record.artifacts || {})
        : Object.values(row.record).filter(
            (value) =>
              value && typeof value === 'object' && /^artifacts\/[a-f0-9]{32}$/.test(value.path)
          );
      return html`
        <details>
          <summary>
            ${`${row.sequence} · ${row.kind}${row.truncated ? ' · large record' : ''}`}
          </summary>
          <pre>${JSON.stringify(row.record, null, 2)}</pre>
          ${artifacts.map((reference) => html`<sola-artifact .runId=${id} .reference=${reference}></sola-artifact>`)}
        </details>
      `;
    }),
    $('evidence-records')
  );
  evidenceCursor = result.next;
  $('more-evidence').hidden = !result.items.length;
}
async function loadTools() {
  text(
    $('tools'),
    JSON.stringify(
      await api(`/tools?profile=${encodeURIComponent($('tool-profile').value)}`),
      null,
      2
    )
  );
}
async function loadProfiles() {
  const result = await api('/profiles');
  for (const id of ['profile', 'tool-profile']) {
    const value = $(id).value;
    renderTemplate(
      result.items.map((p) => html`<option value=${p.name}>${p.name}</option>`),
      $(id)
    );
    if ([...$(id).options].some((option) => option.value === value)) $(id).value = value;
  }
  renderTemplate(
    result.items.map(
      (p) => html`
        <details>
          <summary>${p.name}</summary>
          <pre>${JSON.stringify(p, null, 2)}</pre>
        </details>
      `
    ),
    $('profiles')
  );
  await loadTools();
}
async function refreshMemory() {
  text($('memory-status'), JSON.stringify(await api('/memory'), null, 2));
  queueRefresh([...runs.keys()]);
}
async function refreshHealth() {
  text($('health'), JSON.stringify(await api('/health'), null, 2));
  text($('transport-detail'), $('connection').textContent);
}
const jobs = new Map();
async function loadJobs(more = false) {
  const [result, control] = await Promise.all([
    api(`/jobs?limit=50${more && jobCursor ? `&after=${encodeURIComponent(jobCursor)}` : ''}`),
    api('/jobs/control')
  ]);
  if (!more) jobs.clear();
  for (const job of result.items) jobs.set(job.id, job);
  jobCursor = result.next;
  $('more-jobs').hidden = result.items.length < 50;
  schedulerPaused = control.paused;
  $('jobs-empty').hidden = jobs.size > 0;
  text($('scheduler-toggle'), control.paused ? 'Resume all schedules' : 'Pause all schedules');
  renderTemplate(
    [...jobs.values()].map(
      (job) => html` <sola-job .job=${job} .schedulerPaused=${schedulerPaused}></sola-job> `
    ),
    $('jobs')
  );
}

class SolaJob extends SolaElement {
  static properties = {
    job: { attribute: false },
    schedulerPaused: { attribute: false },
    occurrences: { state: true },
    occurrenceCursor: { state: true },
    occurrencesDone: { state: true }
  };

  constructor() {
    super();
    this.occurrences = null;
    this.occurrenceCursor = null;
    this.occurrencesDone = false;
  }

  willUpdate(changed) {
    if (changed.has('job') && changed.get('job')?.id !== this.job.id) {
      this.occurrences = null;
      this.occurrenceCursor = null;
      this.occurrencesDone = false;
    }
  }

  async loadOccurrences(reset = false) {
    const id = this.job.id;
    const after = reset ? null : this.occurrenceCursor;
    const page = await api(
      `/jobs/${id}/occurrences?limit=25${after ? `&after=${encodeURIComponent(after)}` : ''}`
    );
    if (id !== this.job.id) return;
    this.occurrences = [...(reset ? [] : this.occurrences || []), ...page.items];
    this.occurrenceCursor = page.next;
    this.occurrencesDone = !page.items.length;
  }

  async runNow() {
    const runKey = `sola.job-run.${this.job.id}`;
    const key = read(runKey, null) || uid();
    write(runKey, key);
    const occurrence = await api(`/jobs/${this.job.id}/run`, {
      method: 'POST',
      headers: { 'Idempotency-Key': key }
    });
    write(runKey, null);
    text(
      $('settings-status'),
      `Manual run accepted · ${occurrence.state}. Inspect Occurrences for its outcome.`
    );
    this.requestUpdate();
  }

  render() {
    const job = this.job;
    return html`
      <article>
        <h3>${job.task.slice(0, 128)}</h3>
        <p class="meta">
          ${`${job.enabled ? (job.paused ? 'Paused' : 'Enabled') : 'Disabled'} · ${job.schedule} · ${job.timezone}`}
        </p>
        <button type="button" @click=${() => fillJob(job)}>Edit</button>
        <button
          type="button"
          @click=${action(async () => {
            await api(`/jobs/${job.id}`, { method: 'PATCH', body: { paused: !job.paused } });
            await loadJobs();
          })}
        >
          ${job.paused ? 'Resume' : 'Pause'}
        </button>
        <button
          type="button"
          ?disabled=${job.paused || this.schedulerPaused}
          @click=${action(() => this.runNow())}
        >
          ${read(`sola.job-run.${job.id}`, null) ? 'Retry run now' : 'Run now'}
        </button>
        <button
          type="button"
          @click=${action(async () => {
            if (
              !window.confirm(
                'Delete this job and stop future wake-ups? Existing evidence is retained.'
              )
            )
              return;
            await api(`/jobs/${job.id}`, { method: 'DELETE' });
            await loadJobs();
          })}
        >
          Delete job
        </button>
        <button type="button" @click=${action(() => this.loadOccurrences(true))}>
          Occurrences
        </button>
        ${
          this.occurrences
            ? html`
                <div>
                  <button
                    type="button"
                    ?hidden=${this.occurrencesDone}
                    @click=${action(() => this.loadOccurrences())}
                  >
                    Older / more occurrences
                  </button>
                  ${this.occurrences.map(
                    (occurrence) => html`
                      <details>
                        <summary>
                          ${`${occurrence.state} · ${occurrence.decision === 'no_action' ? 'No action needed' : occurrence.decision} · ${occurrence.due_at}`}
                        </summary>
                        <pre>${JSON.stringify(occurrence, null, 2)}</pre>
                        ${
                          occurrence.run_id
                            ? html`<button
                                type="button"
                                @click=${action(() => openEvidence(occurrence.run_id))}
                              >
                                Inspect run evidence
                              </button>`
                            : nothing
                        }
                      </details>
                    `
                  )}
                </div>
              `
            : nothing
        }
      </article>
    `;
  }
}

function fillJob(job = null) {
  editingJob = job;
  text($('job-form-title'), job ? 'Edit job' : 'Create a job');
  $('job-task').value = job?.task || '';
  $('job-schedule').value = job?.schedule || '';
  $('job-zone').value = job?.timezone || 'America/Denver';
  $('job-topic').value = job?.session_id || selected || '';
  $('job-origin').value = job?.origin || 'scheduled';
  $('job-origin').disabled = !!job;
  $('job-enabled').checked = job?.enabled || false;
}
async function openSettings() {
  $('settings').showModal();
  text($('settings-error'), '');
  text($('settings-status'), '');
  renderTemplate(
    [...topics.values()]
      .filter((topic) => topic.mode !== 'ephemeral' && !topic.archived)
      .map((topic) => html`<option value=${topic.id}>${topic.title || 'Untitled topic'}</option>`),
    $('job-topic')
  );
  $('job-topic-hint').hidden = $('job-topic').options.length > 0;
  $('job-form').querySelector('button.primary').disabled = !$('job-topic').options.length;
  fillJob();
  await Promise.all([loadJobs(), refreshMemory(), refreshHealth()]);
}
async function boot() {
  await api('/auth/session');
  loggedIn = true;
  $('notice').hidden = true;
  await Promise.all([loadTopics(), loadProfiles()]);
  const saved = read('sola.selected', null);
  if (saved && !topics.has(saved)) {
    try {
      topics.set(saved, await api(`/sessions/${saved}`));
    } catch {
      /* topic may have been removed */
    }
  }
  if (topics.size)
    await selectTopic(
      topics.has(selected) ? selected : topics.has(saved) ? saved : topics.keys().next().value
    );
  else {
    text($('connection'), 'Connected · create a topic to begin');
    setTopicHeading();
  }
}

async function login(event) {
  event.preventDefault();
  const submit = $('login-form').querySelector('button');
  if (submit.disabled) return;
  submit.disabled = true;
  try {
    await api('/auth/login', {
      method: 'POST',
      body: { password: $('password').value, transport: 'cookie' }
    });
    $('password').value = '';
    text($('login-error'), '');
    $('login-dialog').close();
    await boot();
  } catch (error) {
    text($('login-error'), error.message);
    $('password').value = '';
    $('password').focus();
  } finally {
    submit.disabled = false;
  }
}

function draftKeydown(event) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$('send').disabled) $('composer').requestSubmit();
  }
}

function historyScroll() {
  const h = $('history');
  following = h.scrollHeight - h.scrollTop - h.clientHeight < 64;
  $('latest').hidden = following;
}

function jumpLatest() {
  following = true;
  $('history').scrollTop = $('history').scrollHeight;
  $('latest').hidden = true;
}

function newTopic() {
  editingTopic = false;
  text($('topic-error'), '');
  text($('topic-dialog-title'), 'New topic');
  $('topic-form').reset();
  $('mode').disabled = false;
  $('pinned').disabled = $('archived').disabled = true;
  $('topic-dialog').showModal();
}

function editTopic() {
  editingTopic = true;
  text($('topic-error'), '');
  const topic = topics.get(selected);
  text($('topic-dialog-title'), 'Topic details');
  $('topic-name').value = topic.title;
  $('mode').value = topic.mode;
  $('mode').disabled = true;
  $('pinned').disabled = $('archived').disabled = false;
  $('pinned').checked = topic.pinned;
  $('archived').checked = topic.archived;
  $('topic-dialog').showModal();
}

async function saveTopic(event) {
  event.preventDefault();
  const submit = $('topic-form').querySelector('button.primary');
  if (submit.disabled) return;
  submit.disabled = true;
  try {
    const topic = await api(editingTopic ? `/sessions/${selected}` : '/sessions', {
      method: editingTopic ? 'PATCH' : 'POST',
      body: editingTopic
        ? {
            title: $('topic-name').value,
            pinned: $('pinned').checked,
            archived: $('archived').checked
          }
        : { title: $('topic-name').value, mode: $('mode').value }
    });
    topics.set(topic.id, topic);
    $('topic-dialog').close();
    await selectTopic(topic.id);
    $('draft').focus();
  } catch (error) {
    showError(error);
  } finally {
    submit.disabled = false;
  }
}

async function saveJob(event) {
  event.preventDefault();
  try {
    const topic = topics.get($('job-topic').value);
    const body = {
      task: $('job-task').value,
      schedule: $('job-schedule').value,
      timezone: $('job-zone').value,
      session_id: topic.id,
      mode: topic.mode,
      enabled: $('job-enabled').checked
    };
    if (!editingJob) {
      body.origin = $('job-origin').value;
      if ($('profile').value) body.profile = $('profile').value;
    }
    await api(editingJob ? `/jobs/${editingJob.id}` : '/jobs', {
      method: editingJob ? 'PATCH' : 'POST',
      body
    });
    fillJob();
    await loadJobs();
  } catch (error) {
    showError(error);
  }
}

async function search(event) {
  event.preventDefault();
  try {
    const result = await api(
      `/search?q=${encodeURIComponent($('search-query').value)}${$('search-topic').checked && selected ? `&session_id=${selected}` : ''}`
    );
    renderTemplate(html`<pre>${JSON.stringify(result.items, null, 2)}</pre>`, $('search-results'));
  } catch (error) {
    showError(error);
  }
}

function initialize() {
  for (const item of outbox) item.sending = false;
  window.addEventListener('online', connect);
  window.addEventListener('resize', revealTopic);
  window.addEventListener('offline', () => {
    stopTransport();
    startPolling();
  });
  window.addEventListener('pagehide', saveDraft);
  setInterval(updateTimers, 1000);
  boot().catch((error) => {
    if (loggedIn) showError(error);
    else requireLogin();
  });
}

customElements.define('sola-artifact', SolaArtifact);
customElements.define('sola-job', SolaJob);
customElements.define('sola-turn', SolaTurn);
customElements.define('sola-app', SolaApp);
