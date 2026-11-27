import { html, nothing, render as renderTemplate } from './lit-core.min.js';
import { markdown } from './markdown.js';
import { api } from './client.js';
import { childTopicLink, openEvidence } from './evidence.js';
import { active, action, SolaElement, agentName } from './ui.js';

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
      const message = document.createElement('article');
      message.className = `message ${m.role}`;
      message.dataset.messageId = m.id;
      message.setAttribute('aria-labelledby', `author-${m.id}`);
      renderTemplate(
        html`
          <div class="author" id=${`author-${m.id}`}>${author}</div>
          <div>
            ${markdown(m.content || (row.truncated ? 'Large message — inspect evidence.' : ''))}
            ${m.image ? html`<sola-artifact .runId=${m.run_id} .reference=${m.image} .thumbnail=${true}></sola-artifact>` : nothing}
            ${m.documents?.map((file) => html`<div class="document-attachment">
              <sola-icon name="FileText"></sola-icon>
              <a href=${`/api/v1/runs/${m.run_id}/${file.path}`}
                download=${`${file.name}.txt`} aria-label=${`Download text from ${file.name}`}>
                ${file.name}
              </a>
            </div>`)}
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
      message.firstElementChild.textContent = 'Context summary';
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
    if (entry.delegation) {
      renderTemplate(childTopicLink(entry.body), node);
      return node;
    }
    if (entry.narration) {
      if (node.source !== entry.body.content) {
        node.source = entry.body.content;
        node.replaceChildren(markdown(entry.body.content));
      }
      return node;
    }
    // Rendering into a keyed node keeps disclosure state and form input across updates.
    renderTemplate(
      html`
        <details data-tone=${entry.tone}>
          <summary>
            <sola-icon class="status-icon" name=${toneIcons[entry.tone]}></sola-icon>${entry.label}<sola-icon
              class="chevron"
              name="ChevronRight"
            ></sola-icon>
          </summary>
          <pre tabindex="0">${JSON.stringify(entry.body, null, 2)}</pre>
          ${entry.body.artifact ? html`<sola-artifact .runId=${entry.body.run_id || this.dataset.runId} .reference=${entry.body.artifact}></sola-artifact>` : nothing}
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
                        this.refreshRun();
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
      `,
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
        body.purpose === 'vision' && (body.run_id === run.id || !body.failure));
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
  setRecords(records) {
    const rows = [...(records?.values() || [])].sort((a, b) => a.sequence - b.sequence);
    const calls = new Map(rows.filter((row) => row.kind === 'tool').map((row) => [row.record.id, row]));
    const receipts = new Map(
      rows.filter((row) => row.kind === 'receipt').map((row) => [row.record.tool_call_id, row])
    );
    const running = active(this.run || {});
    const entries = [];
    for (const [callId, { sequence, record: call }] of calls) {
      const receipt = receipts.get(callId)?.record;
      const state = receipt?.status || (running ? 'working' : 'no receipt — inspect evidence');
      entries.push({
        id: callId,
        sequence,
        tone: receipt ? tone(receipt.status) : running ? 'working' : 'warn',
        label: `${call.name} · ${state}${receipt?.effect === 'uncertain' ? ' · effect uncertain' : ''}`,
        body: { arguments: call.arguments, ...(receipt || {}) }
      });
    }
    for (const [callId, { sequence, record: receipt }] of receipts)
      if (!calls.has(callId))
        entries.push({
          id: callId,
          sequence,
          tone: tone(receipt.status),
          label: `Tool · ${receipt.status}${receipt.effect === 'uncertain' ? ' · effect uncertain' : ''}`,
          body: receipt
        });
    for (const { kind, sequence, record: r } of rows) {
      const entry = { id: r.id, sequence, body: r };
      if (kind === 'delegation')
        entries.push({ ...entry, id: `delegation:${r.id}`, delegation: true });
      // Text the model wrote alongside tool calls; the final reply arrives as a message.
      if (kind === 'model' && r.purpose !== 'compaction' && r.tool_calls?.length && r.content?.trim())
        entries.push({ ...entry, narration: true });
      if (kind === 'memory_note')
        entries.push({ ...entry, tone: 'pending', label: 'Memory note staged — awaiting extraction' });
      if (kind === 'memory')
        entries.push({
          ...entry,
          tone: tone(r.state),
          label: `Memory extraction · ${r.state === 'completed' ? 'completed — saved' : r.state}`,
          reconcile: ['ambiguous', 'failed'].includes(r.state)
        });
      if (kind === 'compaction') {
        this.summaryIds.add(r.summary_message_id);
        entries.push({
          ...entry,
          tone: 'info',
          label: 'Earlier context summarized · original turns retained'
        });
      }
      if (kind === 'delivery')
        entries.push({ ...entry, tone: tone(r.state), label: `Scheduled message · ${r.state}` });
      if (kind === 'recovery') entries.push({ ...entry, tone: 'info', label: 'Recovery note' });
      if (kind === 'model' && r.purpose === 'vision')
        entries.push({
          ...entry,
          tone: r.failure ? tone(r.failure.code === 'cancelled' ? 'cancelled' : 'failed') : 'ok',
          label: r.failure
            ? (r.failure.code === 'cancelled' ? 'Image analysis cancelled' : 'Image analysis failed')
            : 'Image analyzed'
        });
      if (kind === 'image')
        entries.push({
          ...entry,
          tone: ['intent', 'submitted'].includes(r.state) && running ? 'working' : tone(r.state),
          label: `Image generation · ${r.state}`
        });
    }
    for (const entry of entries) this.workEntries.set(entry.id, entry);
    this.requestUpdate();
  }
}

customElements.define('sola-turn', SolaTurn);
