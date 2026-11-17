import { html, nothing, render as renderTemplate } from './lit-core.min.js';
import { $, active, action, text, showError } from './ui.js';
import { api, read, write, uid } from './client.js';
import { openEvidence, moreEvidence } from './evidence.js';
import { createSettings } from './settings.js';
import { SolaApp } from './app.js';
import './turn.js';

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
  olderMessages = 0,
  olderRuns = 0;
let messagesDone = false,
  runsDone = false,
  following = true,
  refreshTimer = null;
let editingTopic = false;
let refreshChain = Promise.resolve(),
  cursor = null;
const pendingRuns = new Set();

const {
  loadTools,
  loadProfiles,
  refreshMemory,
  refreshHealth,
  loadJobs,
  fillJob,
  openSettings,
  saveJob,
  search,
  toggleScheduler
} = createSettings({
  topics,
  selected: () => selected,
  refreshRuns: () => queueRefresh([...runs.keys()])
});

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
function turnFor(id) {
  if (!turns.has(id)) {
    const turn = document.createElement('sola-turn');
    turn.className = 'turn';
    turn.dataset.runId = id;
    turn.cancelRun = async () => {
      const result = await api(`/runs/${id}/cancel`, { method: 'POST' });
      merge('run', [{ ...runs.get(id), record: result }]);
      render();
    };
    turn.refreshRun = () => queueRefresh(id);
    turns.set(id, turn);
  }
  return turns.get(id);
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
      turn.setRecords(records.get(id));
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

async function logout() {
  await api('/auth/logout', { method: 'POST' });
  requireLogin();
}

document.addEventListener('sola-auth-required', requireLogin);
customElements.define('sola-app', SolaApp);
document.querySelector('sola-app').actions = {
  initialize,
  newTopic,
  loadTopics,
  openSettings,
  logout,
  editTopic,
  historyScroll,
  older,
  jumpLatest,
  submit,
  draftKeydown,
  saveDraft,
  login,
  saveTopic,
  loadJobs,
  toggleScheduler,
  saveJob,
  fillJob,
  refreshMemory,
  loadTools,
  search,
  connect,
  refreshHealth,
  moreEvidence
};
