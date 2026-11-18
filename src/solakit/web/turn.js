import { html, nothing, render as renderTemplate } from './lit-core.min.js';
import { markdown } from './markdown.js';
import { api } from './client.js';
import { openEvidence } from './evidence.js';
import { active, action, SolaElement } from './ui.js';

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
      const message = document.createElement('article');
      message.className = `message ${m.role}`;
      message.dataset.messageId = m.id;
      message.setAttribute('aria-labelledby', `author-${m.id}`);
      renderTemplate(
        html`
          <div class="author" id=${`author-${m.id}`}>${author}</div>
          <div>
            ${markdown(m.content || (row.truncated ? 'Large message — inspect evidence.' : ''))}
            ${m.image ? html`<sola-artifact .runId=${m.run_id} .reference=${m.image}></sola-artifact>` : nothing}
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
              <pre tabindex="0">${JSON.stringify(entry.body, null, 2)}</pre>
              ${entry.body.artifact ? html`<sola-artifact .runId=${this.dataset.runId} .reference=${entry.body.artifact}></sola-artifact>` : nothing}
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
          @click=${action(() => this.cancelRun())}
        >
          Cancel run
        </button>
        <button
          type="button"
          aria-haspopup="dialog"
          @click=${action(() => openEvidence(this.dataset.runId))}
        >
          Inspect evidence
        </button>
      </div>
    `;
  }
  setRecords(records) {
    const rows = [...(records?.values() || [])].sort((a, b) => a.sequence - b.sequence);
    const calls = new Map(
      rows.filter((row) => row.kind === 'tool').map((row) => [row.record.id, row.record])
    );
    const receipts = new Map(
      rows
        .filter((row) => row.kind === 'receipt')
        .map((row) => [row.record.tool_call_id, row.record])
    );
    const entries = [];
    for (const [callId, call] of calls) {
      const receipt = receipts.get(callId);
      entries.push({
        id: callId,
        label: `${call.name} · ${receipt?.status || (active(this.run || {}) ? 'working' : 'no receipt — inspect evidence')}${receipt?.effect === 'uncertain' ? ' · effect uncertain' : ''}`,
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
      if (row.kind === 'image') entries.push({ id: r.id, label: `Image generation · ${r.state}`, body: r });
    }
    for (const entry of entries) this.workEntries.set(entry.id, entry);
    this.requestUpdate();
  }
}

customElements.define('sola-turn', SolaTurn);
