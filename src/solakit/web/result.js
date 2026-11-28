import { html, nothing, render } from './lit-core.min.js';
import { api } from './client.js';
import { childTopicLink, openEvidence, workerEvidence } from './evidence.js';
import { $, action, text } from './ui.js';

let view = null;
let canReuseImage = false;

export function openResult(launcher, content) {
  const dialog = $('result-dialog');
  if (dialog.open) closeResult(false);
  view = { launcher, ...content, showDetails: !!content.entry?.working };
  renderResult();
  dialog.showModal();
  $('result-close').focus({ preventScroll: true });
}

export function closeResult(restoreFocus = true) {
  const current = view;
  if (!current) return;
  view = null;
  if ($('result-dialog').open) $('result-dialog').close();
  render(nothing, $('result-body'));
  if (restoreFocus) {
    const target = current.launcher.isConnected ? current.launcher : $('history');
    target.focus({ preventScroll: true });
  }
  return true;
}

export function updateResult(entry) {
  if (view?.entry?.id !== entry.id || view.entry.runId !== entry.runId) return;
  view.entry = entry;
  renderResult();
}

export function setImageReuse(allowed) {
  if (canReuseImage === allowed) return;
  canReuseImage = allowed;
  if (view) renderResult();
}

async function reconcile(event, current) {
  event.preventDefault();
  if (current.saving) return;
  const providerTask = new FormData(event.currentTarget).get('provider-task');
  const { id, runId } = current.entry;
  current.saving = true;
  current.error = '';
  renderResult();
  try {
    await api(`/memory/${id}/reconcile`, {
      method: 'POST',
      body: { provider_task_id: providerTask }
    });
    current.refreshRun(runId);
  } catch (error) {
    current.error = error.message;
  } finally {
    current.saving = false;
    if (view === current) renderResult();
  }
}

function resultContent(entry) {
  const current = view;
  const { name, state, summary, truncated, body, runId, references, related } = entry;
  const selected = references?.find((reference) => reference.path === current.imagePath);
  const previews = selected
    ? [selected, ...references.filter((reference) => reference !== selected)]
    : references;
  return html`
    ${state || summary ? html`<p class="result-status" role="status">
      ${[state, summary].filter(Boolean).join(' · ')}
    </p>` : nothing}
    ${previews?.map((reference) => html`<sola-artifact .runId=${runId}
      .reference=${reference} .automatic=${true} .canReuse=${canReuseImage}
      .imageLabel=${`Generated image from ${name}`}></sola-artifact>`)}
    ${entry.output !== undefined ? html`<pre tabindex="0">${entry.output}</pre>` : nothing}
    ${truncated ? html`<p class="meta">Result truncated.</p>` : nothing}
    ${entry.delegation ? html`<div class="actions">
      ${childTopicLink(entry.delegation)} ${workerEvidence(entry.delegation)}
    </div>` : nothing}
    ${entry.reconcile ? html`<form @submit=${(event) => reconcile(event, current)}>
      <label for="reconcile-provider-task">Verified provider task ID</label>
      <input id="reconcile-provider-task" name="provider-task" required maxlength="256" />
      <button type="submit" ?disabled=${current.saving}>Reconcile extraction</button>
      <p class="preview-error" role="alert" ?hidden=${!current.error}>${current.error}</p>
    </form>` : nothing}
    <details ?open=${current.showDetails}>
      <summary>Recorded details</summary>
      ${entry.arguments !== undefined ? html`<h3>Arguments</h3>
        <pre tabindex="0">${JSON.stringify(entry.arguments, null, 2)}</pre>` : nothing}
      ${body ? html`<h3>${entry.call ? 'Tool receipt' : 'Record'}</h3>
        <pre tabindex="0">${JSON.stringify(body, null, 2)}</pre>` : nothing}
      ${related ? html`<h3>Image generation</h3>
        <pre tabindex="0">${JSON.stringify(related, null, 2)}</pre>` : nothing}
      ${entry.delegation ? html`<h3>Delegation</h3>
        <pre tabindex="0">${JSON.stringify(entry.delegation, null, 2)}</pre>` : nothing}
    </details>
    <div class="actions">
      <button type="button" aria-haspopup="dialog" @click=${action(() => openEvidence(runId))}>
        <sola-icon name="FileSearch"></sola-icon> Inspect evidence
      </button>
    </div>`;
}

function renderResult() {
  const { entry, title, runId, reference, previewPath, documentName, imageLabel } = view;
  text($('result-title'), entry?.name || title);
  render(entry ? resultContent(entry) : html`
    <sola-artifact .runId=${runId} .reference=${reference} .previewPath=${previewPath}
      .automatic=${true} .documentName=${documentName} .imageLabel=${imageLabel}
      .canReuse=${canReuseImage}></sola-artifact>`, $('result-body'));
}
