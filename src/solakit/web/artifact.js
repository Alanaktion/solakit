import { html, nothing } from './lit-core.min.js';
import { action, SolaElement } from './ui.js';

class SolaArtifact extends SolaElement {
  static properties = {
    runId: { attribute: false },
    reference: { attribute: false },
    chunk: { state: true }
  };

  async load(offset = 0) {
    const identity = this.reference.path.split('/').pop();
    const response = await fetch(
      `/api/v1/runs/${this.runId}/artifacts/${identity}?offset=${offset}&limit=65536`,
      { credentials: 'same-origin' }
    );
    if (response.status === 401) {
      document.dispatchEvent(new Event('sola-auth-required'));
      throw new Error('Sign in to inspect artifacts.');
    }
    if (!response.ok) throw new Error('Artifact could not be authorized or verified.');
    this.chunk = {
      bytes: await response.arrayBuffer(),
      offset,
      next: Number(response.headers.get('X-Next-Offset')),
      length: Number(response.headers.get('X-Artifact-Length'))
    };
  }

  download() {
    const { bytes, offset } = this.chunk;
    const url = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `${this.reference.path.split('/').pop()}-${offset}.bin`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  render() {
    const chunk = this.chunk;
    return html`
      <button type="button" @click=${action(() => this.load())}>
        Inspect artifact · ${this.reference.length} bytes
      </button>
      ${
        chunk
          ? html`
              <div>
                <p>
                  Artifact bytes ${chunk.offset}–${chunk.offset + chunk.bytes.byteLength} of
                  ${chunk.length}
                </p>
                <pre tabindex="0">${new TextDecoder().decode(chunk.bytes)}</pre>
                <button type="button" @click=${() => this.download()}>Download this chunk</button>
                ${
                  chunk.next < chunk.length
                    ? html`
                        <button type="button" @click=${action(() => this.load(chunk.next))}>
                          Next chunk
                        </button>
                      `
                    : nothing
                }
              </div>
            `
          : nothing
      }
    `;
  }
}

customElements.define('sola-artifact', SolaArtifact);
