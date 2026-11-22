import { LitElement } from './lit-core.min.js';
// The local UMD bundle assigns globalThis.lucide when evaluated as a module.
import './lucide.min.js';

// Decorative Lucide icon by PascalCase name; the SVG is rebuilt only when the name changes.
class SolaIcon extends HTMLElement {
  static observedAttributes = ['name'];
  // Lit walks cloned templates by node index before insertion; adding children then breaks bindings.
  connectedCallback() {
    this.draw();
  }
  attributeChangedCallback() {
    if (this.isConnected) this.draw();
  }
  draw() {
    const name = this.getAttribute('name');
    if (this.drawn === name) return;
    this.drawn = name;
    const icon = globalThis.lucide.icons[name];
    this.setAttribute('aria-hidden', 'true');
    this.replaceChildren(icon ? globalThis.lucide.createElement(icon) : '');
  }
}
customElements.define('sola-icon', SolaIcon);

export const $ = (id) => document.getElementById(id);
export const agentName = document.querySelector('meta[name="agent-name"]').content;
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
