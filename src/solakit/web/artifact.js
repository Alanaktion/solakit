import { html, nothing } from './lit-core.min.js';
import { api } from './client.js';
import { SolaElement } from './ui.js';

export const artifactPath = (runId, reference) => `/runs/${runId}/${reference.path}`;

export function imageThumbnail(src, label, open) {
  return html`<button type="button" class="image-thumbnail" aria-haspopup="dialog"
    aria-label=${`Open image from ${label}`} @click=${open}>
    <img src=${src} alt="" loading="lazy"
      @error=${({ currentTarget }) => currentTarget.setAttribute('data-failed', '')}
      @load=${({ currentTarget }) => currentTarget.removeAttribute('data-failed')} />
    <span>Open image</span>
  </button>`;
}

class SolaArtifact extends SolaElement {
  static properties = {
    runId: { attribute: false },
    reference: { attribute: false },
    previewPath: { attribute: false },
    automatic: { attribute: false },
    documentName: { attribute: false },
    imageLabel: { attribute: false },
    canReuse: { attribute: false },
    chunk: { state: true },
    imageUrl: { state: true },
    loading: { state: true },
    error: { state: true }
  };

  get isImage() {
    return this.previewPath || this.reference?.media_type === 'image/png';
  }

  reset() {
    this.request?.abort();
    this.request = null;
    if (this.imageUrl) URL.revokeObjectURL(this.imageUrl);
    this.imageUrl = null;
    this.chunk = null;
    this.error = '';
    this.loading = false;
    this.offset = 0;
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this.reset();
  }

  updated(changed) {
    const previous = changed.get('reference');
    if (
      changed.has('runId') || changed.has('previewPath') ||
      (changed.has('reference') && (previous?.path !== this.reference?.path ||
        previous?.sha256 !== this.reference?.sha256))
    ) {
      this.reset();
      if (this.automatic && this.isConnected) this.load();
    }
  }

  async load(offset = 0) {
    if (this.loading) return;
    const request = new AbortController();
    this.request = request;
    this.offset = offset;
    this.loading = true;
    this.error = '';
    const { signal } = request;
    const path = this.reference && artifactPath(this.runId, this.reference);
    try {
      if (this.isImage) {
        const image = await api(this.previewPath || `${path}?preview=true`, {
          responseType: 'blob', signal
        });
        if (signal.aborted) return;
        if (image.type !== 'image/png' || !image.size ||
            (this.reference && image.size !== this.reference.length))
          throw new Error('The PNG preview did not match the recorded image.');
        this.imageUrl = URL.createObjectURL(image);
      } else {
        const response = await fetch(`/api/v1${path}?offset=${offset}&limit=65536`, {
          credentials: 'same-origin', signal
        });
        if (signal.aborted) return;
        if (response.status === 401) {
          document.dispatchEvent(new Event('sola-auth-required'));
          throw new Error('Sign in to inspect artifacts.');
        }
        if (!response.ok) throw new Error('Artifact could not be authorized or verified.');
        const bytes = await response.arrayBuffer();
        if (signal.aborted) return;
        const next = Number(response.headers.get('X-Next-Offset'));
        const length = Number(response.headers.get('X-Artifact-Length'));
        if (length !== this.reference.length || next !== offset + bytes.byteLength ||
            next > length || (next === offset && offset < length) ||
            (this.documentName && next !== length))
          throw new Error('The artifact preview has an invalid byte range.');
        this.chunk = { bytes, offset, next, length };
      }
    } catch (error) {
      if (!signal.aborted) this.error = error.message || 'Preview could not be loaded.';
    } finally {
      if (this.request === request) this.loading = false;
    }
  }

  download() {
    const { bytes, offset } = this.chunk;
    const type = this.documentName ? 'text/plain;charset=utf-8' : 'application/octet-stream';
    const url = URL.createObjectURL(new Blob([bytes], { type }));
    const link = document.createElement('a');
    link.href = url;
    link.download = this.documentName
      ? `${this.documentName}.txt`
      : `${this.reference.path.split('/').pop()}-${offset}.bin`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  render() {
    const { chunk, imageUrl, loading, error } = this;
    const status = html`
      ${loading ? html`<p role="status">Loading ${this.isImage ? 'image' : 'text'}…</p>` : nothing}
      ${error ? html`<p class="preview-error" role="alert">${error}</p>
        <button type="button" @click=${() => this.load(this.offset)}>Retry preview</button>` : nothing}
    `;
    if (this.isImage) return html`
      ${!this.automatic && !imageUrl && !loading && !error
        ? html`<button type="button" @click=${() => this.load()}>View generated image · ${this.reference.length} bytes</button>`
        : nothing}
      ${imageUrl ? html`<figure class="artifact-figure">
        <img class="artifact-image" src=${imageUrl} alt=${this.imageLabel || 'Generated image'}
          @error=${() => {
            URL.revokeObjectURL(this.imageUrl);
            this.imageUrl = null;
            this.error = 'The image preview could not be displayed.';
          }} />
        <figcaption class="actions">
          <a href=${imageUrl} download="sola-image.png">Download image</a>
          ${this.reference && this.canReuse !== false ? this.attachButton() : nothing}
        </figcaption>
      </figure>` : nothing}
      ${status}
    `;
    return html`
      ${!this.automatic ? html`<button type="button" ?disabled=${loading}
        @click=${() => this.load()}>Inspect artifact · ${this.reference.length} bytes</button>` : nothing}
      ${chunk ? html`
        ${!this.documentName ? html`<p>Artifact bytes ${chunk.offset}–${chunk.next} of ${chunk.length}</p>` : nothing}
        <pre tabindex="0">${new TextDecoder().decode(chunk.bytes)}</pre>
        <div class="actions">
          <button type="button" @click=${() => this.download()}>
            ${this.documentName ? 'Download text' : 'Download this chunk'}
          </button>
          ${chunk.offset > 0 ? html`<button type="button" ?disabled=${loading}
            @click=${() => this.load(Math.max(0, chunk.offset - 65536))}>Previous chunk</button>` : nothing}
          ${chunk.next < chunk.length ? html`<button type="button" ?disabled=${loading}
            @click=${() => this.load(chunk.next)}>Next chunk</button>` : nothing}
        </div>` : nothing}
      ${status}
    `;
  }

  attachButton() {
    return html`<button type="button" @click=${() => this.dispatchEvent(new CustomEvent('sola-image-selected', {
      bubbles: true, composed: true,
      detail: { run_id: this.runId, artifact_id: this.reference.path.split('/').pop() }
    }))}>Use image in next message</button>`;
  }
}

customElements.define('sola-artifact', SolaArtifact);
