import { markdown } from './markdown.js';

const $ = (id) => document.getElementById(id);
const active = (run) => ['queued', 'running'].includes(run.state);
const uid = () => crypto.randomUUID().replaceAll('-', '');
const node = (tag, text, className) => {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
};
const button = (text, action) => {
  const b = node('button', text);
  b.type = 'button';
  b.addEventListener('click', () => action().catch(showError));
  return b;
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
        : $('notice');
  text(target, error.message || 'Request failed. Please retry.');
  target.hidden = false;
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
function turnFor(id) {
  if (turns.has(id)) return turns.get(id);
  const root = node('article', undefined, 'turn');
  root.dataset.runId = id;
  const content = node('div'),
    work = node('div', undefined, 'work');
  const preview = node('div', undefined, 'preview');
  preview.hidden = true;
  const bar = node('div', undefined, 'run-bar'),
    state = node('span'),
    elapsed = node('span');
  const cancel = button('Cancel run', async () => {
    const run = await api(`/runs/${id}/cancel`, { method: 'POST' });
    merge('run', [{ ...runs.get(id), record: run }]);
    render();
  });
  const inspect = button('Inspect evidence', async () => openEvidence(id));
  state.setAttribute('role', 'status');
  state.setAttribute('aria-live', 'polite');
  bar.append(state, elapsed, cancel, inspect);
  root.append(content, work, preview, bar);
  const turn = {
    root,
    content,
    work,
    preview,
    state,
    elapsed,
    cancel,
    messageNodes: new Map(),
    workNodes: new Map()
  };
  turns.set(id, turn);
  return turn;
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
  for (const entry of entries) {
    let view = turn.workNodes.get(entry.id);
    if (!view) {
      const detail = node('details'),
        summary = node('summary'),
        pre = node('pre');
      detail.append(summary, pre);
      view = { detail, summary, pre, signature: '' };
      turn.workNodes.set(entry.id, view);
      turn.work.append(detail);
    }
    text(view.summary, entry.label);
    const signature = JSON.stringify(entry.body);
    if (view.signature !== signature) {
      text(view.pre, JSON.stringify(entry.body, null, 2));
      view.signature = signature;
    }
    if (entry.reconcile && !view.form) {
      const form = node('form'),
        label = node('label', 'Verified provider task ID'),
        input = node('input');
      input.required = true;
      input.maxLength = 256;
      input.id = `reconcile-${entry.id}`;
      label.htmlFor = input.id;
      const submit = node('button', 'Reconcile extraction');
      form.append(label, input, submit);
      view.detail.append(form);
      view.form = form;
      form.addEventListener('submit', (event) => {
        event.preventDefault();
        api(`/memory/${entry.id}/reconcile`, {
          method: 'POST',
          body: { provider_task_id: input.value }
        })
          .then(() => queueRefresh(id))
          .catch(showError);
      });
    }
    if (!entry.reconcile && view.form) {
      view.form.remove();
      view.form = null;
    }
  }
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
      // Insert only new turns; settled DOM, selections and open disclosures stay in place.
      if (!turn.root.isConnected) {
        const next = sorted
          .slice(index + 1)
          .map((key) => turns.get(key)?.root)
          .find((root) => root?.isConnected);
        $('turns').insertBefore(turn.root, next || null);
      }
      const rows = [...messages.values()]
        .filter((row) => row.record.run_id === id && row.record.role !== 'tool')
        .sort((a, b) => a.sequence - b.sequence);
      for (let i = 0; i < rows.length; i++) {
        const row = rows[i],
          m = row.record;
        if (!turn.messageNodes.has(m.id)) {
          const article = node('div', undefined, `message ${m.role}`);
          article.dataset.messageId = m.id;
          const author = node(
            'div',
            m.role === 'user'
              ? 'You'
              : m.origin === 'proactive'
                ? 'Sola · proactive message'
                : m.origin === 'scheduled'
                  ? 'Sola · scheduled message'
                  : 'Sola',
            'author'
          );
          const body = node('div');
          body.append(
            markdown(m.content || (row.truncated ? 'Large message — inspect evidence.' : ''))
          );
          article.append(author, body);
          turn.messageNodes.set(m.id, article);
          const next = rows
            .slice(i + 1)
            .map((item) => turn.messageNodes.get(item.record.id))
            .find(Boolean);
          turn.content.insertBefore(article, next || null);
        }
      }
      if (run) {
        const origin = ['proactive', 'scheduled'].includes(run.origin)
          ? `${run.origin === 'proactive' ? 'Proactive wake-up' : 'Scheduled task'} · `
          : '';
        text(turn.state, `${origin}${run.state}${run.reason ? ` · ${run.reason.message}` : ''}`);
        turn.cancel.hidden = !active(run);
        if (!active(run)) {
          turn.preview.hidden = true;
          turn.preview.textContent = '';
        }
      }
      renderWork(turn, id);
    }
    $('empty').hidden = ids.size > 0;
  });
  updateTimers();
}
function updateTimers() {
  for (const [id, turn] of turns) {
    const r = runs.get(id)?.record;
    if (!r?.started_at) {
      text(turn.elapsed, '');
      continue;
    }
    const seconds = Math.max(
      0,
      Math.floor(
        ((r.ended_at ? Date.parse(r.ended_at) : Date.now()) - Date.parse(r.started_at)) / 1000
      )
    );
    text(turn.elapsed, `${Math.floor(seconds / 60)}m ${seconds % 60}s`);
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
    turn.preview.textContent = '';
    turn.preview.hidden = true;
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
      turn.preview.textContent = '';
      turn.preview.hidden = true;
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
          turn.preview.textContent = '';
          turn.preview.hidden = true;
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
      text(turn.preview, `${p.truncated ? '…' : ''}${p.text}`);
      turn.preview.hidden = false;
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
    write(`sola.draft.${selected}`, { content: $('draft').value, profile: $('profile').value });
}
function setTopicHeading() {
  const topic = topics.get(selected);
  text($('topic-title'), topic?.title || 'Untitled topic');
  text($('topic-mode'), `${topic?.mode || 'shared'}${topic?.archived ? ' · archived' : ''}`);
  $('topic-edit').disabled = !topic;
  $('send').disabled = !topic || topic.archived;
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
  for (const topic of topics.values()) {
    let b = $(`topic-${topic.id}`);
    if (!b) {
      b = button('', async () => selectTopic(topic.id));
      b.id = `topic-${topic.id}`;
      $('topics').append(b);
    }
    text(
      b,
      `${topic.pinned ? '⌑ ' : ''}${topic.title || 'Untitled topic'}${topic.archived ? ' (archived)' : ''}`
    );
    b.hidden = topic.archived !== $('archived-topics').checked && topic.id !== selected;
    if (topic.id === selected) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  }
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
  $('outbox').replaceChildren();
  for (const item of outbox.filter((item) => item.session_id === selected)) {
    const row = node('div', undefined, 'pending');
    row.dataset.requestId = item.key;
    row.append(
      node(
        'span',
        `${item.sending ? 'Submitting' : 'Awaiting confirmation'} · ${item.content.slice(0, 80)}`
      )
    );
    if (!item.sending) row.append(button('Retry same submission', async () => deliver(item)));
    $('outbox').append(row);
  }
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
  if (!selected || !$('draft').value.trim()) return;
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
  $('evidence-records').replaceChildren();
  text($('evidence-error'), '');
  if (!$('evidence').open) $('evidence').showModal();
  await moreEvidence();
}
async function artifactChunks(run, reference, parent, offset = 0) {
  const identity = reference.path.split('/').pop();
  const response = await fetch(
    `/api/v1/runs/${run}/artifacts/${identity}?offset=${offset}&limit=65536`,
    { credentials: 'same-origin' }
  );
  if (response.status === 401) {
    requireLogin();
    throw new Error('Sign in to inspect artifacts.');
  }
  if (!response.ok) throw new Error('Artifact could not be authorized or verified.');
  const bytes = await response.arrayBuffer(),
    pre = node('pre', new TextDecoder().decode(bytes));
  parent.replaceChildren(
    node(
      'p',
      `Artifact bytes ${offset}–${offset + bytes.byteLength} of ${response.headers.get('X-Artifact-Length')}`
    ),
    pre
  );
  const download = button('Download this chunk', async () => {
    const url = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }));
    const link = node('a');
    link.href = url;
    link.download = `${identity}-${offset}.bin`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  parent.append(download);
  const next = Number(response.headers.get('X-Next-Offset'));
  if (next < Number(response.headers.get('X-Artifact-Length')))
    parent.append(button('Next chunk', async () => artifactChunks(run, reference, parent, next)));
}
async function moreEvidence() {
  const id = evidenceRun,
    result = await api(`/runs/${id}/records?after=${evidenceCursor}&limit=50`);
  if (id !== evidenceRun) return;
  for (const row of result.items) {
    const detail = node('details'),
      summary = node(
        'summary',
        `${row.sequence} · ${row.kind}${row.truncated ? ' · large record' : ''}`
      );
    detail.append(summary, node('pre', JSON.stringify(row.record, null, 2)));
    const artifacts = row.truncated
      ? Object.values(row.record.artifacts || {})
      : Object.values(row.record).filter(
          (value) =>
            value && typeof value === 'object' && /^artifacts\/[a-f0-9]{32}$/.test(value.path)
        );
    for (const reference of artifacts) {
      const panel = node('div');
      detail.append(
        button(`Inspect artifact · ${reference.length} bytes`, async () =>
          artifactChunks(id, reference, panel)
        ),
        panel
      );
    }
    $('evidence-records').append(detail);
  }
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
  for (const p of result.items) {
    for (const id of ['profile', 'tool-profile']) {
      if (![...$(id).options].some((option) => option.value === p.name)) {
        const option = node('option', p.name);
        option.value = p.name;
        $(id).append(option);
      }
    }
    const detail = node('details');
    detail.append(node('summary', p.name), node('pre', JSON.stringify(p, null, 2)));
    $('profiles').append(detail);
  }
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
  text($('scheduler-toggle'), control.paused ? 'Resume all schedules' : 'Pause all schedules');
  $('jobs').replaceChildren();
  for (const job of jobs.values()) {
    const article = node('article');
    article.append(
      node('h3', job.task.slice(0, 128)),
      node(
        'p',
        `${job.enabled ? (job.paused ? 'Paused' : 'Enabled') : 'Disabled'} · ${job.schedule} · ${job.timezone}`,
        'meta'
      )
    );
    article.append(
      button('Edit', async () => fillJob(job)),
      button(job.paused ? 'Resume' : 'Pause', async () => {
        await api(`/jobs/${job.id}`, { method: 'PATCH', body: { paused: !job.paused } });
        await loadJobs();
      })
    );
    const runKey = `sola.job-run.${job.id}`;
    const runButton = button(read(runKey, null) ? 'Retry run now' : 'Run now', async () => {
      const key = read(runKey, null) || uid();
      write(runKey, key);
      const occurrence = await api(`/jobs/${job.id}/run`, {
        method: 'POST',
        headers: { 'Idempotency-Key': key }
      });
      write(runKey, null);
      text(
        $('settings-status'),
        `Manual run accepted · ${occurrence.state}. Inspect Occurrences for its outcome.`
      );
    });
    runButton.disabled = job.paused || schedulerPaused;
    article.append(runButton);
    article.append(
      button('Delete job', async () => {
        if (
          !window.confirm(
            'Delete this job and stop future wake-ups? Existing evidence is retained.'
          )
        )
          return;
        await api(`/jobs/${job.id}`, { method: 'DELETE' });
        await loadJobs();
      })
    );
    const occurrences = node('div');
    article.append(
      button('Occurrences', async () => {
        occurrences.replaceChildren();
        let after = null;
        const more = async () => {
          const page = await api(
            `/jobs/${job.id}/occurrences?limit=25${after ? `&after=${encodeURIComponent(after)}` : ''}`
          );
          for (const occurrence of page.items) {
            const detail = node('details');
            detail.append(
              node(
                'summary',
                `${occurrence.state} · ${occurrence.decision === 'no_action' ? 'No action needed' : occurrence.decision} · ${occurrence.due_at}`
              ),
              node('pre', JSON.stringify(occurrence, null, 2))
            );
            if (occurrence.run_id)
              detail.append(
                button('Inspect run evidence', async () => openEvidence(occurrence.run_id))
              );
            occurrences.append(detail);
          }
          after = page.next;
          moreButton.hidden = !page.items.length;
        };
        const moreButton = button('Older / more occurrences', more);
        occurrences.append(moreButton);
        await more();
      }),
      occurrences
    );
    $('jobs').append(article);
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
  $('job-topic').replaceChildren();
  for (const topic of topics.values())
    if (topic.mode !== 'ephemeral' && !topic.archived) {
      const option = node('option', topic.title || 'Untitled topic');
      option.value = topic.id;
      $('job-topic').append(option);
    }
  fillJob();
  await Promise.all([loadJobs(), refreshMemory(), refreshHealth()]);
}
async function boot() {
  await api('/auth/session');
  loggedIn = true;
  $('notice').hidden = true;
  $('profiles').replaceChildren();
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
    $('send').disabled = true;
  }
}

$('login-dialog').addEventListener('cancel', (event) => event.preventDefault());
$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
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
  }
});
for (const item of outbox) item.sending = false;
for (const b of document.querySelectorAll('[data-close]'))
  b.addEventListener('click', () => $(b.dataset.close).close());
$('composer').addEventListener('submit', submit);
$('draft').addEventListener('input', saveDraft);
$('profile').addEventListener('change', saveDraft);
$('draft').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$('send').disabled) $('composer').requestSubmit();
  }
});
$('history').addEventListener('scroll', () => {
  const h = $('history');
  following = h.scrollHeight - h.scrollTop - h.clientHeight < 64;
  $('latest').hidden = following;
});
$('latest').addEventListener('click', () => {
  following = true;
  $('history').scrollTop = $('history').scrollHeight;
  $('latest').hidden = true;
});
$('older').addEventListener('click', () => older().catch(showError));
$('archived-topics').addEventListener('change', () => loadTopics().catch(showError));
$('more-topics').addEventListener('click', () => loadTopics(true).catch(showError));
$('new-topic').addEventListener('click', () => {
  editingTopic = false;
  text($('topic-dialog-title'), 'New topic');
  $('topic-form').reset();
  $('mode').disabled = false;
  $('pinned').disabled = $('archived').disabled = true;
  $('topic-dialog').showModal();
});
$('topic-edit').addEventListener('click', () => {
  editingTopic = true;
  const topic = topics.get(selected);
  text($('topic-dialog-title'), 'Topic details');
  $('topic-name').value = topic.title;
  $('mode').value = topic.mode;
  $('mode').disabled = true;
  $('pinned').disabled = $('archived').disabled = false;
  $('pinned').checked = topic.pinned;
  $('archived').checked = topic.archived;
  $('topic-dialog').showModal();
});
$('topic-form').addEventListener('submit', async (event) => {
  event.preventDefault();
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
  }
});
$('logout').addEventListener('click', () =>
  api('/auth/logout', { method: 'POST' }).then(requireLogin).catch(showError)
);
$('settings-open').addEventListener('click', () => openSettings().catch(showError));
$('poll-only').addEventListener('change', connect);
$('tool-profile').addEventListener('change', () => loadTools().catch(showError));
$('memory-refresh').addEventListener('click', () => refreshMemory().catch(showError));
$('health-refresh').addEventListener('click', () => refreshHealth().catch(showError));
$('jobs-refresh').addEventListener('click', () => loadJobs().catch(showError));
$('more-jobs').addEventListener('click', () => loadJobs(true).catch(showError));
$('scheduler-toggle').addEventListener('click', () =>
  api('/jobs/control', { method: 'PUT', body: { paused: !schedulerPaused } })
    .then(() => loadJobs())
    .catch(showError)
);
$('job-reset').addEventListener('click', () => fillJob());
$('job-form').addEventListener('submit', async (event) => {
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
});
$('search-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const result = await api(
      `/search?q=${encodeURIComponent($('search-query').value)}${$('search-topic').checked && selected ? `&session_id=${selected}` : ''}`
    );
    $('search-results').replaceChildren(node('pre', JSON.stringify(result.items, null, 2)));
  } catch (error) {
    showError(error);
  }
});
$('more-evidence').addEventListener('click', () => moreEvidence().catch(showError));
window.addEventListener('online', connect);
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
