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

// Direct HTTP on a private IP has no Async Clipboard API.
function copyOnHttp(source) {
  const active = document.activeElement;
  const selection = document.getSelection();
  const { anchorNode, anchorOffset, focusNode, focusOffset } = selection;
  const field = document.createElement('textarea');
  field.className = 'clipboard-input';
  field.readOnly = true;
  field.tabIndex = -1;
  field.value = ' ';
  let copied = false;
  field.addEventListener('copy', (event) => {
    event.preventDefault();
    if (event.clipboardData) {
      event.clipboardData.setData('text/plain', source);
      copied = true;
    }
  });
  document.body.append(field);
  try {
    field.focus({ preventScroll: true });
    field.select();
    return document.execCommand('copy') && copied;
  } finally {
    field.remove();
    active?.focus({ preventScroll: true });
    if (anchorNode && focusNode) {
      selection.setBaseAndExtent(anchorNode, anchorOffset, focusNode, focusOffset);
    } else selection.removeAllRanges();
  }
}

export function copyButton(source, kind) {
  const label = kind === 'code' ? 'Copy' : 'Copy message';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'copy-button';
  const setLabel = (copied) => {
    button.textContent = copied ? 'Copied' : label;
    button.setAttribute('aria-label', `${copied ? 'Copied' : 'Copy'} ${kind}`);
  };
  setLabel(false);
  let reset;
  button.addEventListener('click', async () => {
    const status = $('copy-status');
    status.textContent = '';
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(source);
      else if (!copyOnHttp(source)) throw new Error();
    } catch {
      showError(new Error(`Could not copy ${kind} to the clipboard.`));
      return;
    }
    setLabel(true);
    status.textContent = button.getAttribute('aria-label');
    clearTimeout(reset);
    reset = setTimeout(() => setLabel(false), 1500);
  });
  return button;
}

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
