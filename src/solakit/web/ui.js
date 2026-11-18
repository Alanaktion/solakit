import { LitElement } from './lit-core.min.js';

export const $ = (id) => document.getElementById(id);
export const active = (run) => ['queued', 'running'].includes(run.state);
// Light DOM keeps the shared stylesheet, labels and fragment links working across components.
export class SolaElement extends LitElement {
  createRenderRoot() {
    return this;
  }
}

export const action = (callback) => (event) => {
  Promise.resolve()
    .then(() => callback(event))
    .catch(showError);
};

export const text = (element, value) => {
  if (element.textContent !== value) element.textContent = value;
};
export function showError(error) {
  const target = $('evidence').open
    ? $('evidence-error')
    : $('job-dialog').open
      ? $('job-error')
      : $('settings').open
        ? $('settings-error')
        : $('topic-dialog').open
          ? $('topic-error')
          : $('notice-message');
  text(target, error.message || 'Request failed. Please retry.');
  target.hidden = false;
  if (target.id === 'notice-message') $('notice').hidden = false;
}
