import { html, nothing, render as renderTemplate } from './lit-core.min.js';
import { api } from './client.js';
import { $, action, text } from './ui.js';
import './artifact.js';

let evidenceRun = null,
  view = 'records';
let pages = {};
export function childTopicLink(record) {
  return html`<a href="/?session=${record.child_session_id}">Open delegated topic</a>`;
}
export function workerEvidence(record) {
  return record.continued_from_run_id ? html`
      <button type="button" class="quiet" @click=${action(() => openEvidence(record.continued_from_run_id))}>
        Previous worker run
      </button>
      <button type="button" class="quiet" @click=${action(() => openEvidence(record.child_run_id))}>
        Follow-up worker run
      </button>
    ` : html`<button type="button" class="quiet" @click=${action(() => openEvidence(record.child_run_id))}>
      Inspect worker run
    </button>`;
}
export async function openEvidence(id) {
  evidenceRun = id;
  pages = { records: { rows: [], cursor: 0 }, prompts: { rows: [], cursor: 0 } };
  text($('evidence-error'), '');
  if (!$('evidence').open) $('evidence').showModal();
  await switchEvidence('records');
}
export async function switchEvidence(next) {
  view = next;
  renderTemplate(nothing, $('evidence-records'));
  for (const key of ['records', 'prompts']) {
    $(`evidence-tab-${key}`).setAttribute('aria-selected', String(key === view));
    $(`evidence-tab-${key}`).tabIndex = key === view ? 0 : -1;
  }
  $('evidence-records').setAttribute('aria-labelledby', `evidence-tab-${view}`);
  renderEvidence();
  if (!pages[view].rows.length) await moreEvidence();
}
export async function moreEvidence() {
  const id = evidenceRun,
    selectedView = view,
    page = pages[view];
  const button = $('more-evidence');
  if (page.loading) return;
  page.loading = true;
  button.disabled = true;
  try {
    const result = await api(`/runs/${id}/${selectedView}?after=${page.cursor}&limit=50`);
    if (id !== evidenceRun || page !== pages[selectedView]) return;
    page.rows.push(...result.items);
    page.cursor = result.next;
    page.done = !result.items.length;
    if (view === selectedView) renderEvidence();
  } finally {
    page.loading = false;
    if (id === evidenceRun && page === pages[view]) button.disabled = false;
  }
}
function renderEvidence() {
  const id = evidenceRun,
    page = pages[view];
  const evidenceRows = page.rows;
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
          ${row.kind === 'delegation' ? html`${childTopicLink(row.record)} ${workerEvidence(row.record)}` : nothing}
          <pre tabindex="0">${JSON.stringify(row.record, null, 2)}</pre>
          ${artifacts.map((reference) => html`<sola-artifact .runId=${id} .reference=${reference}></sola-artifact>`)}
        </details>
      `;
    }),
    $('evidence-records')
  );
  $('more-evidence').hidden = !!page.done;
  $('more-evidence').disabled = !!page.loading;
  if (page.done && !page.rows.length)
    renderTemplate(
      html`<p class="empty-state">
        No ${view === 'prompts' ? 'model requests' : 'records'} for this run.
      </p>`,
      $('evidence-records')
    );
}
