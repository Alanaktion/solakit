import { html, nothing, render as renderTemplate } from './lit-core.min.js';
import { $, active, action, text, showError, agentName } from './ui.js';
import { api, downloadTopic, read, write, uid } from './client.js';
import { openEvidence, moreEvidence, switchEvidence } from './evidence.js';
import { createSettings } from './settings.js';
import { SolaApp } from './app.js';
import './turn.js';

const outbox = read('sola.outbox', []);
const topics = new Map(),
  topicIds = new Set(),
  pinnedIds = new Set(),
  messages = new Map(),
  runs = new Map(),
  records = new Map(),
  turns = new Map();
let selected = null,
  generation = 0,
  source = null,
  polling = null,
  retryable = null,
  retryStream = null;
let loggedIn = false,
  topicGeneration = 0,
  topicCursor = null,
  olderMessages = 0,
  olderRuns = 0;
let messagesDone = false,
  runsDone = false,
  following = true,
  refreshTimer = null;
let editingTopic = false;
let importBatch = [];
let draftImage = null;
let draftDocuments = [];
let imageInputs = false, documentConversion = false, attachmentUpload = null, dragDepth = 0;
const imageTypes = { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp' };
const documentTypes = ['.pdf', '.docx', '.pptx'];
const textTypes = 'text/*,.md,.markdown,.txt,.csv,.tsv,.json,.jsonl,.yaml,.yml,.toml,.xml,.log';
let refreshChain = Promise.resolve(),
  cursor = null;
const pendingRuns = new Set();

const settings = createSettings({
  topics,
  selected: () => selected,
  refreshRuns: () => queueRefresh([...runs.keys()]),
  selectTopic: async (id) => {
    if (!topics.has(id)) topics.set(id, await api(`/sessions/${id}`));
    await selectTopic(id);
  }
});
const { loadProfiles, openSettings } = settings;

function requireLogin() {
  loggedIn = false;
  attachmentUpload = null;
  resetFileDrop();
  updateComposer();
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
  const height = history.scrollHeight;
  action();
  if (anchor?.isConnected) history.scrollTop += anchor.getBoundingClientRect().top - top;
  else if (following) history.scrollTop = history.scrollHeight;
  else if (history.scrollHeight > height) $('latest').classList.add('unseen');
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
// Linked attempts share one turn, keyed by the oldest loaded one.
function rootOf(id) {
  let previous;
  while ((previous = runs.get(id)?.record.retry_of) && runs.has(previous)) id = previous;
  return id;
}
async function loadAttempts(token) {
  const tried = new Set();
  while (token === generation) {
    const missing = [...runs.values()]
      .map((row) => row.record.retry_of)
      .filter((id) => id && !runs.has(id) && !tried.has(id));
    if (!missing.length) return;
    for (const id of missing) tried.add(id);
    await Promise.all(missing.map((id) => loadRecords(id, token)));
  }
}
function turnFor(id) {
  if (!turns.has(id)) {
    const turn = document.createElement('sola-turn');
    turn.className = 'turn';
    turn.dataset.runId = id;
    turn.cancelRun = async () => {
      const latest = turn.run.id;
      const result = await api(`/runs/${latest}/cancel`, { method: 'POST' });
      merge('run', [{ ...runs.get(latest), record: result }]);
      render();
    };
    turn.retryRun = async () => {
      turn.retrying = true;
      try {
        const result = await api(`/runs/${turn.run.id}/retry`, { method: 'POST' });
        if (!turn.isConnected) return;
        merge('run', [{ record: result.run, sequence: Number.MAX_SAFE_INTEGER }]);
        render();
        queueRefresh(result.run.id);
      } catch (error) {
        // The server may have accepted it even though the response was lost.
        queueRefresh();
        throw error;
      } finally {
        turn.retrying = false;
      }
    };
    turn.refreshRun = () => queueRefresh(turn.run?.id ?? id);
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
    const position = new Map();
    for (const row of messages.values())
      position.set(
        row.record.run_id,
        Math.min(position.get(row.record.run_id) ?? Infinity, row.sequence)
      );
    for (const [id, row] of runs) position.set(id, row.sequence ?? position.get(id));
    const attempts = new Map();
    for (const id of [...ids].sort((a, b) => position.get(a) - position.get(b))) {
      const root = rootOf(id);
      attempts.set(root, [...(attempts.get(root) || []), id]);
    }
    for (const [id, turn] of turns)
      if (!attempts.has(id)) {
        turn.remove();
        turns.delete(id);
      }
    const sorted = [...attempts.keys()];
    let next = null;
    for (let index = sorted.length - 1; index >= 0; index--) {
      const id = sorted[index],
        turn = turnFor(id),
        linked = attempts.get(id),
        run = runs.get(linked.at(-1))?.record;
      // Move only misplaced turns so settled ones keep focus and selections.
      if (!turn.isConnected || turn.nextElementSibling !== next)
        $('turns').insertBefore(turn, next);
      next = turn;
      turn.messageRows = [...messages.values()]
        .filter((row) => linked.includes(row.record.run_id) && row.record.role !== 'tool')
        .sort((a, b) => a.sequence - b.sequence);
      turn.run = run;
      if (run && !active(run)) turn.preview = '';
      turn.attempts = linked;
      turn.setRecords(new Map(linked.flatMap((attempt) => [...(records.get(attempt) || [])])));
      // Flush Lit before measuring scroll anchors and following the latest content.
      turn.performUpdate();
    }
    $('empty').hidden = ids.size > 0;
  });
  updateTimers();
  updateComposer();
}
function updateTimers() {
  for (const turn of turns.values()) {
    const r = runs.get(turn.run?.id)?.record;
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
  const items = [];
  const cached = records.get(id);
  let before = 0;
  // Catch up across as many pages as arrived while the tab was disconnected.
  while (true) {
    const result = await api(`/runs/${id}/records?before=${before}&limit=50`);
    if (token !== generation) return;
    items.push(...result.items);
    if (!result.items.length || result.items.some((row) =>
      row.kind === 'run' || cached?.has(`${row.kind}:${row.record.id}`)
    )) break;
    before = result.next;
  }
  if (!records.has(id)) records.set(id, new Map());
  for (const row of items) {
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
    topic = selected,
    lastSequence = [...messages.values()].reduce((last, row) => Math.max(last, row.sequence), 0);
  const [messagePage, runPage] = await Promise.all([
    api(`/sessions/${topic}/messages?before=0&limit=25`),
    api(`/sessions/${topic}/runs?before=0&limit=25`)
  ]);
  if (token !== generation) return;
  const latestRuns = new Set(runPage.items.map((row) => row.record.id));
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
  retryable = runPage.retryable;
  await Promise.all(
    [...inspect].map(async (id) => {
      if (!latestRuns.has(id)) {
        const run = await api(`/runs/${id}`);
        if (token !== generation || run.session_id !== topic) return;
        // A first records load supplies the run's original sequence.
        if (runs.has(id)) merge('run', [{ ...runs.get(id), record: run }]);
      }
      await loadRecords(id, token);
    })
  );
  await loadAttempts(token);
  if (token !== generation) return;
  render();
  const replies = [...messages.values()]
    .filter(({ sequence, record }) =>
      sequence > lastSequence && record.role === 'assistant' &&
      !turns.get(rootOf(record.run_id))?.summaryIds.has(record.id)
    )
    .sort((a, b) => a.sequence - b.sequence);
  // Replacing the text also announces a new reply identical to the previous one.
  if (replies.length)
    $('reply-status').textContent = replies.map((row) => `${agentName}: ${row.record.content}`).join('\n\n');
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
  await Promise.all([...inspect].map((id) => loadRecords(id, token)));
  await loadAttempts(token);
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
    'recovery.noted',
    'image.changed',
    'delegation.admitted'
  ]) {
    stream.addEventListener(type, (event) => {
      if (token !== generation) return;
      const data = JSON.parse(event.data);
      if (cursor && data.sequence <= Number(cursor.split(':')[1])) return;
      cursor = event.lastEventId;
      if (['model.recorded', 'run.terminal'].includes(type)) {
        const turn = turns.get(rootOf(data.run_id));
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
    const turn = turnFor(rootOf(p.run_id));
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
      profile: $('profile').value,
      image: draftImage,
      documents: draftDocuments
    });
  updateComposer();
}
function updateComposer() {
  const uploading = attachmentUpload?.generation === generation;
  const imageUrl = draftImage?.upload_id
    ? `/api/v1/sessions/${selected}/image-uploads/${draftImage.upload_id}`
    : draftImage && `/api/v1/runs/${draftImage.run_id}/artifacts/${draftImage.artifact_id}?preview=true`;
  renderTemplate(imageUrl ? html`<img class="image-thumbnail" src=${imageUrl} alt="Attached image">
      <button type="button" @click=${() => { draftImage = null; saveDraft(); }}>
        <sola-icon name="X"></sola-icon> Remove image
      </button>` : nothing, $('draft-image'));
  renderTemplate(html`
    ${draftDocuments.map((file) => html`<div class="document-attachment">
      <sola-icon name="FileText"></sola-icon>
      <span>${file.name}</span>
      <button type="button" aria-label=${`Remove ${file.name}`}
        @click=${() => {
          draftDocuments = draftDocuments.filter((item) => item !== file);
          saveDraft();
        }}><sola-icon name="X"></sola-icon></button>
    </div>`)}
    ${uploading ? html`<span role="status">Preparing attachments…</span>` : nothing}
  `, $('draft-documents'));
  const topic = topics.get(selected);
  for (const turn of turns.values())
    turn.retryable = !topic?.archived && !!turn.run && turn.run.id === retryable;
  const delegated = !!topic?.parent_session_id;
  const used =
    topic?.mode === 'ephemeral' &&
    (runs.size > 0 || outbox.some((item) => item.session_id === selected));
  $('draft').disabled = !topic || delegated;
  const unavailable = !loggedIn || !topic || delegated || topic.archived || used || uploading;
  $('attach-files').disabled = unavailable;
  $('attachment-files').accept = [
    textTypes,
    ...(imageInputs ? [...new Set(Object.values(imageTypes))] : []),
    ...(documentConversion ? documentTypes : [])
  ].join(',');
  $('send').disabled = unavailable || !$('draft').value.trim();
  $('compose-hint').classList.toggle('explanation', !!(delegated || topic?.archived || used));
  text(
    $('compose-hint'),
    delegated
      ? 'This topic contains the delegated worker’s transcript.'
      : topic?.archived
      ? 'Restore this topic in Topic details to send a message.'
      : used
        ? 'This topic allows one run. Start a new topic to continue.'
        : 'Drop files here or use Attach files · Enter to send'
  );
}
function pickFiles(event) {
  const input = event.target;
  const files = Array.from(input.files);
  input.value = '';
  return attachFiles(files);
}
const isFileDrag = (event) => event.dataTransfer?.types.includes('Files');

function resetFileDrop() {
  dragDepth = 0;
  $('drop-files').hidden = true;
}

function dragFiles(event) {
  if (event.type === 'dragleave') {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) resetFileDrop();
    return;
  }
  if (!isFileDrag(event)) return;
  event.preventDefault();
  const enabled = !$('attach-files').disabled;
  if (event.type === 'dragenter') dragDepth++;
  event.dataTransfer.dropEffect = enabled ? 'copy' : 'none';
  $('drop-files').hidden = !enabled;
}

function dropFiles(event) {
  resetFileDrop();
  if (!isFileDrag(event)) return;
  event.preventDefault();
  return attachFiles(Array.from(event.dataTransfer.files));
}

async function attachFiles(files) {
  if (!files.length || $('attach-files').disabled) return;
  const pending = { generation };
  attachmentUpload = pending;
  updateComposer();
  const current = () => attachmentUpload === pending && generation === pending.generation;
  const path = `/sessions/${selected}`;
  const errors = [];
  let attachedImage = false;
  try {
    for (const file of files) {
      if (!current()) return;
      const extension = file.name.split('.').pop().toLowerCase();
      const imageType = Object.hasOwn(imageTypes, extension) ? imageTypes[extension] : file.type;
      try {
        if (imageType.startsWith('image/')) {
          if (!imageInputs) throw new Error('Image inputs are disabled for this model.');
          if (!Object.values(imageTypes).includes(imageType))
            throw new Error('Choose a PNG, JPEG, or WebP image.');
          if (attachedImage) throw new Error('Attach one image per message.');
          const result = await api(`${path}/image-uploads`, {
            method: 'POST', body: file.slice(0, file.size, imageType)
          });
          if (!current()) return;
          draftImage = result;
          attachedImage = true;
        } else {
          if (!documentConversion && documentTypes.includes(`.${extension}`))
            throw new Error('Document conversion is disabled on the model API.');
          if (draftDocuments.length >= 4) throw new Error('Attach up to four documents.');
          const query = new URLSearchParams({ filename: file.name });
          const result = await api(`${path}/document-uploads?${query}`, {
            method: 'POST', body: file.slice(0, file.size, 'application/octet-stream')
          });
          if (!current()) return;
          const size = draftDocuments.reduce((bytes, item) => bytes + item.text_bytes, 0);
          if (size + result.text_bytes > 24 * 1024)
            throw new Error('Attached document text must fit within 24 KiB per message.');
          draftDocuments = [...draftDocuments, { ...result, name: file.name }];
        }
        saveDraft();
      } catch (error) {
        errors.push(`${file.name}: ${error.message}`);
      }
    }
    if (!current()) return;
    if (errors.length) showError(new Error(errors.join('\n')));
    else $('draft').focus({ preventScroll: true });
  } finally {
    if (attachmentUpload === pending) {
      attachmentUpload = null;
      updateComposer();
    }
  }
}
function setTopicHeading() {
  const topic = topics.get(selected);
  const title = topic ? topic.title || 'Untitled topic' : 'Conversation';
  text($('topic-title'), title);
  document.title = `${agentName} · ${title}`;
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
      : `Start a topic, then send ${agentName} a message.`
  );
  $('empty-new-topic').hidden = !!topic;
  $('topic-edit').disabled = !topic;
  updateComposer();
}
async function selectTopic(id) {
  saveDraft();
  stopTransport();
  const url = new URL(window.location.href);
  if (url.searchParams.has('session') && url.searchParams.get('session') !== id) {
    url.searchParams.delete('session');
    history.replaceState(null, '', url);
  }
  generation++;
  resetFileDrop();
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
  retryable = null;
  $('turns').replaceChildren();
  text($('reply-status'), '');
  olderMessages = olderRuns = 0;
  messagesDone = runsDone = false;
  following = true;
  $('latest').hidden = true;
  $('latest').classList.remove('unseen');
  const draft = read(`sola.draft.${id}`, {});
  $('draft').value = draft.content || '';
  $('profile').value = draft.profile || '';
  draftImage = draft.image || null;
  draftDocuments = draft.documents || [];
  setTopicHeading();
  renderTopics();
  renderOutbox();
  await older();
  if (token !== generation) return;
  $('history').scrollTop = $('history').scrollHeight;
  connect();
}
function renderTopics() {
  const ids = new Set([...pinnedIds, ...topicIds]);
  const visible = [...ids].map((id) => topics.get(id));
  if (topics.has(selected) && !ids.has(selected) && !$('topic-search').value.trim())
    visible.push(topics.get(selected));
  visible.sort((a, b) => Number(b.pinned) - Number(a.pinned));
  renderTemplate(
    html`${visible.length ? nothing : html`<p class="meta" role="status">${$('topic-search').value.trim() ? 'No matching topics.' : 'No topics yet.'}</p>`}
    ${visible.map(
      (topic) => html`
        <button
          type="button"
          id=${`topic-${topic.id}`}
          ?hidden=${topic.archived !== $('archived-topics').checked && topic.id !== selected}
          aria-current=${topic.id === selected ? 'page' : nothing}
          @click=${action(() => selectTopic(topic.id))}
        >
          ${topic.pinned ? html`<sola-icon name="Pin"></sola-icon><span class="sr-only">Pinned: </span>` : nothing}${`${topic.title || 'Untitled topic'}${topic.archived ? ' (archived)' : ''}`}
        </button>
      `
    )}`,
    $('topics')
  );
  revealTopic();
}
function revealTopic() {
  $(`topic-${selected}`)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}
async function loadPinnedTopics(archived, signal) {
  const items = [];
  let after = null;
  do {
    const result = await api(
      `/sessions?limit=100&include_ephemeral=true&pinned=true&archived=${archived}${after ? `&after=${encodeURIComponent(after)}` : ''}`,
      { signal }
    );
    items.push(...result.items);
    after = result.items.length === 100 ? result.next : null;
  } while (after);
  return items;
}
async function loadTopics(more = false, signal) {
  const token = ++topicGeneration;
  const archived = $('archived-topics').checked;
  const [result, pinned] = await Promise.all([
    api(`/sessions?limit=50&include_ephemeral=true&pinned=false&archived=${archived}&q=${encodeURIComponent($('topic-search').value)}${more && topicCursor ? `&after=${encodeURIComponent(topicCursor)}` : ''}`, { signal }),
    more ? [] : loadPinnedTopics(archived, signal)
  ]);
  if (token !== topicGeneration) return;
  if (!more) {
    topicIds.clear();
    pinnedIds.clear();
    for (const topic of pinned) {
      topics.set(topic.id, topic);
      pinnedIds.add(topic.id);
    }
  }
  for (const topic of result.items) {
    topics.set(topic.id, topic);
    topicIds.add(topic.id);
  }
  topicCursor = result.next;
  $('more-topics').hidden = result.items.length < 50;
  renderTopics();
}

async function openManagedTopic(id) {
  topics.set(id, await api(`/sessions/${id}`));
  await selectTopic(id);
}

function markManagedTopicsArchived(ids) {
  ++topicGeneration;
  for (const id of ids) {
    const topic = topics.get(id);
    if (topic) topics.set(id, { ...topic, archived: true });
  }
  renderTopics();
  setTopicHeading();
}

async function refreshManagedTopics(ids, signal) {
  const id = selected;
  const refreshSelected = async () => {
    if (!ids.includes(id)) return;
    topics.set(id, await api(`/sessions/${id}`, { signal }));
    renderTopics();
    if (id === selected) setTopicHeading();
  };
  const results = await Promise.allSettled([loadTopics(false, signal), refreshSelected()]);
  for (const result of results) {
    if (result.status === 'rejected') throw result.reason;
  }
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
            ${item.sending ? nothing : html`<button type="button" @click=${action(() => deliver(item))}><sola-icon name="RotateCcw"></sola-icon> Retry same submission</button>`}
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
        ...(item.image ? { image: item.image } : {}),
        ...(item.documents?.length ? { document_upload_ids: item.documents } : {}),
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
    image: draftImage,
    documents: draftDocuments.map((file) => file.upload_id),
    sending: false
  };
  outbox.push(item);
  write('sola.outbox', outbox);
  $('draft').value = '';
  draftImage = null;
  draftDocuments = [];
  saveDraft();
  $('draft').focus();
  await deliver(item);
}
async function boot() {
  await api('/auth/session');
  loggedIn = true;
  $('notice').hidden = true;
  await Promise.all([loadTopics(), loadProfiles(), api('/health').then((health) => {
    imageInputs = health.image_inputs;
    documentConversion = health.document_conversion;
    updateComposer();
  })]);
  const linked = new URLSearchParams(window.location.search).get('session');
  const saved = /^[a-f0-9]{32}$/.test(linked || '') ? linked : read('sola.selected', null);
  if (saved && !topics.has(saved)) {
    try {
      topics.set(saved, await api(`/sessions/${saved}`));
    } catch {
      /* topic may have been removed */
    }
  }
  if (topics.size)
    await selectTopic(
      linked && topics.has(linked) ? linked
        : topics.has(selected) ? selected : topics.has(saved) ? saved : topics.keys().next().value
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
  if (following) $('latest').classList.remove('unseen');
}

function jumpLatest() {
  following = true;
  $('history').scrollTop = $('history').scrollHeight;
  if (document.activeElement === $('latest')) $('history').focus({ preventScroll: true });
  $('latest').hidden = true;
  $('latest').classList.remove('unseen');
}

function newTopic() {
  editingTopic = false;
  text($('topic-error'), '');
  text($('topic-dialog-title'), 'New topic');
  $('topic-form').reset();
  $('mode').disabled = false;
  $('pinned').disabled = $('archived').disabled = true;
  $('export-topic').hidden = true;
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
  $('export-topic').hidden = false;
  $('topic-dialog').showModal();
}

async function exportTopic() {
  const id = selected;
  const button = $('export-topic');
  if (!id || button.disabled) return;
  button.disabled = true;
  text(button, 'Exporting…');
  text($('topic-error'), '');
  try {
    await downloadTopic(id);
  } catch (error) {
    text($('topic-error'), error.message);
  } finally {
    button.disabled = false;
    text(button, 'Export topic');
  }
}

function openImport() {
  $('import-form').reset();
  chooseImport();
  $('import-dialog').showModal();
}

function chooseImport() {
  importBatch = Array.from($('import-file').files, (file) => ({
    file, key: uid(), sessionId: null, error: null
  }));
  $('import-submit').disabled = !importBatch.length;
  text($('import-submit'), 'Import topics');
  text($('import-error'), '');
  text($('import-status'), '');
}

async function importTopic(event) {
  event.preventDefault();
  const picker = $('import-file');
  const submit = $('import-submit');
  const cancel = $('import-cancel');
  if (!importBatch.length || submit.disabled) return;
  picker.disabled = submit.disabled = cancel.disabled = true;
  const alert = $('import-error');
  const status = $('import-status');
  text(alert, '');
  try {
    for (const [index, item] of importBatch.entries()) {
      if (item.sessionId) continue;
      text(status, `Importing file ${index + 1} of ${importBatch.length}…`);
      try {
        const { file, key } = item;
        const { session_id } = await api('/sessions/import', {
          method: 'POST',
          headers: { 'Idempotency-Key': key },
          body: file.slice(0, file.size, 'application/zip')
        });
        item.sessionId = session_id;
        item.error = null;
      } catch (error) {
        item.error = error instanceof TypeError
          ? 'Could not reach Solakit. Retry with the same file.'
          : error.message;
        if (!loggedIn) break;
      }
    }
    const imported = importBatch.filter((item) => item.sessionId).length;
    text(status, `${imported} of ${importBatch.length} files imported.`);
    if (imported < importBatch.length) {
      text(alert, importBatch.filter((item) => item.error)
        .map((item) => `${item.file.name}: ${item.error}`).join('\n'));
      text(submit, 'Retry import');
      if (imported && loggedIn) {
        void $('topic-manager-component').loadPage();
        await loadTopics();
      }
      return;
    }
    const topic = await api(`/sessions/${importBatch.at(-1).sessionId}`);
    topics.set(topic.id, topic);
    $('topic-search').value = '';
    $('archived-topics').checked = topic.archived;
    await selectTopic(topic.id);
    await loadTopics();
    $('import-dialog').close();
    $('topic-manager-component').close();
    $('history').focus({ preventScroll: true });
  } catch (error) {
    text(alert, [alert.textContent, error.message].filter(Boolean).join('\n'));
    text(submit, 'Retry import');
  } finally {
    picker.disabled = submit.disabled = cancel.disabled = false;
    if (loggedIn && $('import-dialog').open) submit.focus();
  }
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
    await loadTopics();
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
  for (const type of ['dragover', 'drop']) window.addEventListener(type, (event) => {
    if (isFileDrag(event)) event.preventDefault();
  });
  // Images, disclosures and composer resizes change height outside render(); stay pinned.
  const pin = new ResizeObserver(() => {
    if (following) $('history').scrollTop = $('history').scrollHeight;
  });
  pin.observe($('turns'));
  pin.observe($('history'));
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
document.addEventListener('sola-image-selected', (event) => {
  if (!selected || topics.get(selected)?.archived) return;
  if ($('attach-files').disabled) return;
  draftImage = event.detail;
  saveDraft();
  if ($('evidence').open) $('evidence').close();
  $('draft').focus();
});
customElements.define('sola-app', SolaApp);
document.querySelector('sola-app').actions = {
  initialize,
  newTopic,
  loadTopics,
  openManagedTopic,
  markManagedTopicsArchived,
  refreshManagedTopics,
  openSettings,
  logout,
  editTopic,
  historyScroll,
  older,
  jumpLatest,
  submit,
  draftKeydown,
  saveDraft,
  pickFiles: action(pickFiles),
  dragFiles,
  dropFiles: action(dropFiles),
  login,
  saveTopic,
  exportTopic,
  openImport,
  chooseImport,
  importTopic,
  ...settings,
  connect,
  moreEvidence,
  switchEvidence
};
