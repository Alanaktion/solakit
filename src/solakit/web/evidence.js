import { html, nothing, render as renderTemplate } from './lit-core.min.js';
import { api } from './client.js';
import { $, text } from './ui.js';
import './artifact.js';

let evidenceRun = null,
  evidenceCursor = 0;
export async function openEvidence(id) {
  evidenceRun = id;
  evidenceCursor = 0;
  evidenceRows = [];
  renderTemplate(nothing, $('evidence-records'));
  text($('evidence-error'), '');
  if (!$('evidence').open) $('evidence').showModal();
  await moreEvidence();
}
let evidenceRows = [];
export async function moreEvidence() {
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
          <pre tabindex="0">${JSON.stringify(row.record, null, 2)}</pre>
          ${artifacts.map((reference) => html`<sola-artifact .runId=${id} .reference=${reference}></sola-artifact>`)}
        </details>
      `;
    }),
    $('evidence-records')
  );
  evidenceCursor = result.next;
  $('more-evidence').hidden = !result.items.length;
}
