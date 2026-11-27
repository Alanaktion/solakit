import { html, nothing } from './lit-core.min.js';
import { api, read, write, uid } from './client.js';
import { openEvidence } from './evidence.js';
import { $, text, action, SolaElement } from './ui.js';

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
    if (this.occurrences) await this.loadOccurrences(true);
    this.requestUpdate();
  }

  render() {
    const job = this.job;
    return html`
      <article data-job-id=${job.id}>
        <h3>${job.task.slice(0, 128)}</h3>
        <p class="meta">
          ${job.follow_up
            ? `${job.consumed ? 'See occurrence' : job.enabled && !this.schedulerPaused ? 'Due' : 'Paused'} · ${job.schedule.startsWith('once:') ? job.schedule.slice(5).replace('T', ' ') : job.schedule} · ${job.timezone}`
            : `${job.enabled ? 'Enabled' : 'Disabled'}${job.consumed ? ' · Finished' : ''} · ${job.schedule} · ${job.timezone}`}
        </p>
        <p class="meta">
          ${job.follow_up ? 'Follow-up' : job.origin === 'proactive' ? 'Proactive wake-up' : 'Scheduled task'} ·
          ${job.profile} ·
          ${job.mode === 'ephemeral' ? 'New one-run topic' : 'Bound topic'}${job.delivery === 'none' ? ' · Evidence only' : ''}${job.enabled && !this.schedulerPaused && job.next_due ? ` · Next: ${new Date(job.next_due).toLocaleString()}` : ''}
        </p>
        <button type="button" @click=${action(() => this.editJob(job))}>Edit</button>
        <button
          class="quiet"
          type="button"
          @click=${action(async () => {
            await api(`/jobs/${job.id}`, { method: 'PATCH', body: { enabled: !job.enabled } });
            await this.refreshJobs();
          })}
        >
          ${job.enabled ? 'Disable' : 'Enable'}
        </button>
        <button
          type="button"
          ?disabled=${this.schedulerPaused}
          @click=${action(() => this.runNow())}
        >
          ${read(`sola.job-run.${job.id}`, null) ? 'Retry run now' : 'Run now'}
        </button>
        <button
          class="quiet"
          type="button"
          @click=${action(async () => {
            if (
              !window.confirm(
                'Delete this job and stop future wake-ups? Existing evidence is retained.'
              )
            )
              return;
            await api(`/jobs/${job.id}`, { method: 'DELETE' });
            await this.refreshJobs();
          })}
        >
          Delete job
        </button>
        <button class="quiet" type="button" @click=${action(() => this.loadOccurrences(true))}>
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
                          ${`${occurrence.state} · ${occurrence.decision === 'no_action' ? 'No action needed' : occurrence.decision}${occurrence.delivery_state ? ` · Delivery ${occurrence.delivery_state}` : ''} · ${occurrence.local_slot || occurrence.due_at}`}
                        </summary>
                        <pre tabindex="0">${JSON.stringify(occurrence, null, 2)}</pre>
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

customElements.define('sola-job', SolaJob);
