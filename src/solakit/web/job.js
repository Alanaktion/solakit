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
        <button type="button" @click=${() => this.editJob(job)}>Edit</button>
        <button
          type="button"
          @click=${action(async () => {
            await api(`/jobs/${job.id}`, { method: 'PATCH', body: { paused: !job.paused } });
            await this.refreshJobs();
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
            await this.refreshJobs();
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

customElements.define('sola-job', SolaJob);
