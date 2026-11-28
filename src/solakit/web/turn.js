import { html, nothing, render as renderTemplate } from './lit-core.min.js';
import { markdown } from './markdown.js';
import { childTopicLink, openEvidence } from './evidence.js';
import { artifactPath, imageThumbnail } from './artifact.js';
import { openResult, updateResult } from './result.js';
import { active, action, copyButton, SolaElement, agentName } from './ui.js';

// Visual tone for a record state; the visible label always carries the state in words.
const tone = (state) =>
  ({
    queued: 'pending',
    running: 'working',
    working: 'working',
    succeeded: 'ok',
    completed: 'ok',
    downloaded: 'ok',
    delivered: 'ok',
    failed: 'error',
    denied: 'error',
    uncertain: 'warn',
    ambiguous: 'warn',
    cancelled: 'muted',
    interrupted: 'muted',
    skipped: 'muted'
  })[state] || 'pending';
const toneIcons = {
  ok: 'CircleCheck',
  error: 'CircleX',
  warn: 'CircleAlert',
  muted: 'CircleMinus',
  info: 'Info',
  working: 'LoaderCircle',
  pending: 'CircleDashed'
};

const short = (value, limit = 160) => {
  if (typeof value !== 'string') return '';
  const line = value.replace(/[\p{Cc}\p{Cf}\s]+/gu, ' ').trim();
  return line.length > limit ? `${line.slice(0, limit - 1)}…` : line;
};
const artifactLabel = (reference) =>
  reference?.media_type === 'image/png' ? 'PNG image available'
    : reference ? 'Artifact available' : '';
const count = (number, singular, plural = `${singular}s`) =>
  `${number} ${number === 1 ? singular : plural}`;
const matchTools = ['files.search', 'files.glob', 'files.grep'];
const fileListTools = ['files.list', ...matchTools];
const structuredResults = new Set([
  ...fileListTools, 'files.write', 'web.search', 'transcripts.search'
]);

function resultValue(result) {
  if (typeof result === 'string' && /^\s*[\[{]/.test(result)) {
    try { return JSON.parse(result); }
    catch { return result; }
  }
  return result;
}

function toolSummary(name, receipt, value, references) {
  const failure = short(receipt?.failure?.message) ||
    short(receipt?.failure?.code?.replaceAll('_', ' '));
  if (failure) return [failure, artifactLabel(references[0])].filter(Boolean).join(' · ');
  if (references.some((reference) => reference.media_type === 'image/png'))
    return 'PNG image available';
  if (receipt?.status === 'succeeded') {
    if (matchTools.includes(name) && Array.isArray(value?.matches))
      return count(value.matches.length, 'match', 'matches');
    if (name === 'files.list' && Array.isArray(value?.entries))
      return count(value.entries.length, 'entry', 'entries');
    if (['web.search', 'transcripts.search'].includes(name) && Array.isArray(value))
      return count(value.length, 'result');
    if (name === 'files.write' && typeof value?.path === 'string') {
      const filename = short(value.path.split('/').pop());
      if (filename) return `Wrote ${filename}`;
    }
  }
  return artifactLabel(references[0]);
}

function toolEntry({ call, receipt, image, delegation }, running) {
  const r = receipt?.record;
  const generation = image?.record;
  const first = call || receipt || image || delegation;
  const name = short(call?.record.name, 256) ||
    (image ? 'Image generation' : delegation ? 'Delegation' : 'Tool');
  const state = r?.status ?? (running ? 'working' : 'no receipt');
  const references = [...new Map(
    [generation?.artifact ?? generation?.artifacts?.artifact, r?.artifact ?? r?.artifacts?.artifact]
      .filter(Boolean).map((reference) => [reference.path, reference])
  ).values()];
  const value = structuredResults.has(name) ? resultValue(r?.result) : r?.result;
  const truncated = r?.truncated || receipt?.truncated || call?.truncated || image?.truncated ||
    (fileListTools.includes(name) && value?.truncated === true);
  return {
    id: call?.record.id || r?.tool_call_id || generation?.tool_call_id || delegation.record.id,
    runId: first.record.run_id,
    sequence: first.sequence,
    name,
    state: state + (r?.effect === 'uncertain' ? ' · effect uncertain' : ''),
    tone: state === 'no receipt' ? 'warn' : tone(state),
    working: state === 'working',
    summary: toolSummary(name, r, value, references) ||
      (generation?.state ? `Image generation ${generation.state}` : ''),
    truncated,
    call: true,
    arguments: call?.record.arguments,
    body: r,
    related: generation,
    delegation: delegation?.record,
    references,
    output: value != null && !(references.length && (r?.truncated || name === 'images.generate'))
      ? (typeof value === 'string' ? value : JSON.stringify(value, null, 2))
      : undefined
  };
}

class SolaTurn extends SolaElement {
  static properties = {
    run: { attribute: false },
    messageRows: { attribute: false },
    workEntries: { attribute: false },
    retryable: { state: true },
    retrying: { state: true },
    preview: { state: true },
    elapsed: { state: true }
  };

  constructor() {
    super();
    this.messageRows = [];
    this.workEntries = new Map();
    this.messageNodes = new Map();
    this.entryNodes = new Map();
    this.summaryIds = new Set();
    this.preview = '';
    this.elapsed = '';
    this.attempts = [];
  }

  renderMessage(row) {
    const m = row.record;
    if (!this.messageNodes.has(m.id)) {
      const image = m.image || m.artifacts?.image;
      const author =
        m.role === 'user'
          ? (m.origin === 'delegated' ? 'Delegation brief' : 'You')
          : m.origin === 'delegated'
            ? 'Delegated worker'
          : m.origin === 'proactive'
            ? `${agentName} · proactive message`
            : m.origin === 'scheduled'
              ? `${agentName} · scheduled message`
              : agentName;
      const attachmentOrigin = author === 'You' ? 'your message' : author;
      const messageExcerpt = short(m.content, 60);
      const message = document.createElement('article');
      message.className = `message ${m.role}`;
      message.dataset.messageId = m.id;
      message.setAttribute('aria-labelledby', `author-${m.id}`);
      renderTemplate(
        html`
          <div class="message-header">
            <span class="author" id=${`author-${m.id}`}>${author}</span>
            ${m.content?.trim() ? copyButton(m.content, 'message') : nothing}
          </div>
          <div>
            ${markdown(m.content || (row.truncated ? 'Large message — inspect evidence.' : ''))}
            ${image || m.documents?.length ? html`<div class="message-attachment">
              ${image ? imageThumbnail(`/api/v1${artifactPath(m.run_id, image)}?thumbnail=true`,
                messageExcerpt ? `${attachmentOrigin} “${messageExcerpt}”` : attachmentOrigin,
                ({ currentTarget }) =>
                  openResult(currentTarget, {
                    runId: m.run_id, reference: image,
                    title: 'Attached image', imageLabel: 'Image attached to the message'
                  })) : nothing}
              ${m.documents?.map((file) => html`<div class="document-attachment">
                <sola-icon name="FileText"></sola-icon>
                <span>${file.name}</span>
                <button type="button" aria-haspopup="dialog" aria-label=${`View attachment ${file.name}`}
                  @click=${({ currentTarget }) => openResult(currentTarget, {
                    runId: m.run_id, reference: file, title: file.name, documentName: file.name
                  })}>View attachment</button>
              </div>`)}
            </div>` : nothing}
          </div>
        `,
        message
      );
      this.messageNodes.set(m.id, message);
    }
    const message = this.messageNodes.get(m.id);
    // A boundary can arrive after its message has already been rendered.
    if (this.summaryIds.has(m.id) && !message.classList.contains('compaction-summary')) {
      message.classList.add('compaction-summary');
      message.querySelector('.author').textContent = 'Context summary';
    }
    return message;
  }

  renderEntry(entry) {
    if (!this.entryNodes.has(entry.id)) {
      const node = document.createElement('div');
      node.className = entry.narration ? 'narration' : 'work';
      this.entryNodes.set(entry.id, node);
    }
    const node = this.entryNodes.get(entry.id);
    if (entry.narration) {
      if (node.source !== entry.body.content) {
        node.source = entry.body.content;
        node.replaceChildren(markdown(entry.body.content));
      }
      return node;
    }
    const open = (event, imagePath) =>
      openResult(event.currentTarget, { entry, imagePath, refreshRun: this.refreshRun });
    // Keep the launchers in place as new receipts and image records arrive.
    renderTemplate(
      html`<div class="work-row" data-tone=${entry.tone}>
        <sola-icon class="status-icon" name=${toneIcons[entry.tone]}></sola-icon>
        <span><strong>${entry.name}</strong>${entry.state ? ` · ${entry.state}` : ''}
          ${entry.summary ? ` · ${entry.summary}` : ''}
          ${entry.truncated ? ' · truncated' : ''}</span>
        ${entry.references?.filter((reference) => reference.media_type === 'image/png')
          .map((reference) => imageThumbnail(
            `/api/v1${artifactPath(entry.runId, reference)}?thumbnail=true`, entry.name,
            (event) => open(event, reference.path)
          ))}
        <button type="button" aria-haspopup="dialog" aria-label=${`View ${entry.working ? 'details' : 'result'} for ${entry.name}`}
          @click=${open}>${entry.working ? 'View details' : 'View result'}</button>
        ${entry.delegation ? childTopicLink(entry.delegation) : nothing}
      </div>`,
      node
    );
    return node;
  }

  render() {
    const run = this.run;
    const origin = ['proactive', 'scheduled'].includes(run?.origin)
      ? `${run.origin === 'proactive' ? 'Proactive wake-up' : 'Scheduled task'} · `
      : '';
    const entries = [...this.workEntries.values()];
    const analyzing = run?.state === 'running' &&
      this.messageRows.some(({ record }) => record.image_route === 'vision') &&
      !entries.some(({ body }) =>
        body?.purpose === 'vision' && (body.run_id === run.id || !body.failure));
    // One sequence-ordered timeline: request, tool work, then the reply it produced.
    const timeline = [
      ...this.messageRows.map((row) => [row.sequence, this.renderMessage(row)]),
      ...entries.map((entry) => [entry.sequence, this.renderEntry(entry)])
    ].sort((a, b) => a[0] - b[0]);
    const thinking =
      run && active(run) && !analyzing && !this.preview &&
      !entries.some((entry) => entry.tone === 'working');
    return html`
      <div class="timeline">${timeline.map(([, node]) => node)}</div>
      <div class="thinking" aria-hidden="true" ?hidden=${!thinking}><span></span><span></span><span></span></div>
      <div class="preview" ?hidden=${!this.preview}>${this.preview}</div>
      <div class="run-bar" data-tone=${run ? tone(run.state) : nothing}>
        <sola-icon class="status-icon" name=${toneIcons[run ? tone(run.state) : 'pending']}></sola-icon>
        <span role="status" aria-live="polite"
          >${analyzing ? 'Analyzing image…'
            : run ? `${origin}${run.state}${run.reason ? ` · ${run.reason.message}` : ''}` : ''}</span
        >
        <span>${this.elapsed}</span>
        <button
          type="button"
          ?hidden=${!run || !active(run)}
          @click=${action(() => this.cancelRun())}
        >
          <sola-icon name="CircleStop"></sola-icon> Cancel run
        </button>
        <button
          type="button"
          ?hidden=${!this.retryable}
          ?disabled=${this.retrying}
          @click=${action(() => this.retryRun())}
        >
          <sola-icon name="RotateCcw"></sola-icon> Retry
        </button>
        ${this.attempts.slice(0, -1).map(
          (id, index) => html`
            <button type="button" aria-haspopup="dialog" @click=${action(() => openEvidence(id))}>
              <sola-icon name="FileSearch"></sola-icon> Attempt ${index + 1} evidence
            </button>
          `
        )}
        <button
          type="button"
          aria-haspopup="dialog"
          @click=${action(() => openEvidence(run?.id ?? this.dataset.runId))}
        >
          <sola-icon name="FileSearch"></sola-icon> Inspect evidence
        </button>
      </div>
    `;
  }
  setRecords(records, runs) {
    const rows = [...(records?.values() || [])].sort((a, b) => a.sequence - b.sequence);
    const latestRequests = new Map();
    const steeredRequests = new Set();
    const dispatchedSteps = new Set();
    for (const { kind, record } of rows) {
      if (kind === 'request' && record.purpose === 'conversation')
        latestRequests.set(record.run_id, record.id);
      if (kind === 'message' && record.steering_request_id && latestRequests.has(record.run_id))
        steeredRequests.add(latestRequests.get(record.run_id));
      if (kind === 'tool') dispatchedSteps.add(record.model_step_id);
    }
    const tools = new Map();
    const entries = [];
    for (const row of rows) {
      const { kind, sequence, record: r, truncated } = row;
      if (['tool', 'receipt', 'image', 'delegation'].includes(kind) &&
          (r.tool_call_id || kind !== 'image')) {
        const key = `${r.run_id}:${r.tool_call_id || r.id}`;
        if (!tools.has(key)) tools.set(key, {});
        tools.get(key)[kind === 'tool' ? 'call' : kind] = row;
        continue;
      }
      const entry = { id: r.id, runId: r.run_id, sequence, body: r, truncated };
      // Text the model wrote alongside tool calls; the final reply arrives as a message.
      if (kind === 'model' && r.purpose !== 'compaction' && r.tool_calls?.length && r.content?.trim() &&
          (!steeredRequests.has(r.request_id) || dispatchedSteps.has(r.id)))
        entries.push({ ...entry, narration: true });
      if (kind === 'memory_note')
        entries.push({ ...entry, tone: 'pending', name: 'Memory note staged',
          state: 'awaiting extraction', output: r.content });
      if (kind === 'memory')
        entries.push({
          ...entry,
          tone: tone(r.state),
          name: 'Memory extraction',
          state: r.state === 'completed' ? 'completed · saved' : r.state,
          summary: short(r.failure?.message),
          reconcile: ['ambiguous', 'failed'].includes(r.state)
        });
      if (kind === 'compaction') {
        this.summaryIds.add(r.summary_message_id);
        entries.push({
          ...entry,
          tone: 'info',
          name: 'Earlier context summarized',
          state: 'original turns retained'
        });
      }
      if (kind === 'delivery')
        entries.push({ ...entry, tone: tone(r.state), name: 'Scheduled message', state: r.state });
      if (kind === 'recovery')
        entries.push({ ...entry, tone: 'info', name: 'Recovery note', output: r.content });
      if (kind === 'model' && r.purpose === 'vision')
        entries.push({
          ...entry,
          tone: r.failure ? tone(r.failure.code === 'cancelled' ? 'cancelled' : 'failed') : 'ok',
          name: 'Image analysis',
          state: r.failure ? (r.failure.code === 'cancelled' ? 'cancelled' : 'failed') : 'completed',
          summary: short(r.failure?.message)
        });
      if (kind === 'image')
        entries.push({
          ...entry,
          tone: tone(r.state),
          name: 'Image generation',
          state: r.state,
          references: [r.artifact ?? r.artifacts?.artifact].filter(Boolean)
        });
    }
    for (const tool of tools.values()) {
      const { record } = tool.call || tool.receipt || tool.image || tool.delegation;
      entries.push(toolEntry(tool, active(runs.get(record.run_id)?.record || {})));
    }
    this.workEntries = new Map(entries.map((entry) => [entry.id, entry]));
    for (const entry of entries) updateResult(entry);
  }
}

customElements.define('sola-turn', SolaTurn);
