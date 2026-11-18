import { html, nothing } from './lit-core.min.js';
import { action, SolaElement } from './ui.js';

class SolaArtifact extends SolaElement {
  static properties = {
    runId: { attribute: false },
    reference: { attribute: false },
    chunk: { state: true },
    imageUrl: { state: true },
    loading: { state: true }
  };

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this.imageUrl) URL.revokeObjectURL(this.imageUrl);
    this.imageUrl = null;
    this.imageLoad = (this.imageLoad || 0) + 1;
    this.loading = false;
  }

  updated(changed) {
    if (changed.has('reference') && changed.get('reference')?.path !== this.reference?.path) {
      if (this.imageUrl) URL.revokeObjectURL(this.imageUrl);
      this.imageUrl = null;
    }
  }

  async loadImage() {
    if (this.loading) return;
    if (this.reference.media_type !== 'image/png' || this.reference.length > 16 * 1024 * 1024)
      throw new Error('Image exceeds the preview limit.');
    const reference = this.reference,
      runId = this.runId;
    const token = (this.imageLoad = (this.imageLoad || 0) + 1);
    this.loading = true;
    try {
      const parts = [];
      let offset = 0;
      while (offset < reference.length) {
        await this.load(offset);
        if (
          !this.isConnected || this.imageLoad !== token ||
          this.reference.path !== reference.path || this.runId !== runId
        ) return;
        if (
          !this.chunk.bytes.byteLength ||
          this.chunk.next !== offset + this.chunk.bytes.byteLength ||
          this.chunk.length !== reference.length || this.chunk.next > reference.length
        ) throw new Error('Image download has an invalid byte range.');
        parts.push(this.chunk.bytes);
        offset = this.chunk.next;
      }
      if (this.imageUrl) URL.revokeObjectURL(this.imageUrl);
      this.imageUrl = URL.createObjectURL(new Blob(parts, { type: 'image/png' }));
      this.chunk = null;
    } finally {
      if (this.imageLoad === token) this.loading = false;
    }
  }

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
    if (this.reference.media_type === 'image/png') return html`
      ${this.imageUrl ? html`<figure>
        <img class="artifact-image" src=${this.imageUrl} alt="Generated image" />
        <figcaption><a href=${this.imageUrl} download="sola-image.png">Download image</a></figcaption>
      </figure>` : html`<button type="button" ?disabled=${this.loading} @click=${action(() => this.loadImage())}>${this.loading ? 'Loading image…' : `View generated image · ${this.reference.length} bytes`}</button>`}
      <button type="button" @click=${() => this.dispatchEvent(new CustomEvent('sola-image-selected', {
        bubbles: true, composed: true,
        detail: { run_id: this.runId, artifact_id: this.reference.path.split('/').pop() }
      }))}>Use image in next message</button>
    `;
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
