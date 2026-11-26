import { html } from './lit-core.min.js';
import { api, downloadTopic } from './client.js';
import { action, SolaElement } from './ui.js';

const batchSize = 100;
const requestTimeout = () => AbortSignal.timeout(30000);
const modes = { shared: 'Shared', isolated: 'Isolated', ephemeral: 'One run' };
const titleOf = (topic) => topic.title || 'Untitled topic';
const plural = (count) => count === 1 ? 'topic' : 'topics';
const localDate = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' });

function dateBoundary(value, nextDay) {
  if (!value) return '';
  const date = new Date(`${value}T00:00:00`);
  if (nextDay) date.setDate(date.getDate() + 1);
  const year = date.getUTCFullYear();
  return year >= 1 && year <= 9999 ? date.toISOString() : null;
}

async function getPage(filters, signal, after = null, limit = 50) {
  const query = new URLSearchParams(filters);
  query.set('limit', limit);
  if (after) query.set('after', after);
  return api(`/sessions/catalog?${query}`, { signal });
}

export class SolaTopicManager extends SolaElement {
  static properties = {
    rows: { state: true },
    next: { state: true },
    selected: { state: true },
    pending: { state: true },
    busy: { state: true },
    message: { state: true },
    listError: { state: true },
    archiveError: { state: true },
    exporting: { state: true },
    exportError: { state: true },
    profiles: { state: true },
    profileError: { state: true },
    openTopic: { attribute: false },
    openImport: { attribute: false },
    markArchived: { attribute: false },
    refreshSidebar: { attribute: false }
  };

  constructor() {
    super();
    this.rows = [];
    this.selected = new Set();
    this.profiles = [];
    this.exporting = new Set();
  }

  get dialog() {
    return this.querySelector('dialog');
  }

  async show() {
    await this.updateComplete;
    if (this.dialog.open) return;
    this.dialog.showModal();
    void this.loadProfiles();
    this.applyFilters();
  }

  close() {
    if (this.busy !== 'archiving') this.dialog.close();
  }

  reset() {
    if (this.dialog.open) return;
    this.read?.abort();
    this.profileRead?.abort();
    this.read = null;
    this.busy = null;
    this.pending = null;
    this.archiveError = '';
    this.exportError = null;
  }

  async loadProfiles() {
    this.profileRead?.abort();
    const read = new AbortController();
    this.profileRead = read;
    this.profileError = '';
    try {
      const signal = AbortSignal.any([read.signal, requestTimeout()]);
      const { items } = await api('/sessions/catalog/profiles', { signal });
      if (!read.signal.aborted) this.profiles = items;
    } catch (error) {
      if (!read.signal.aborted) this.profileError = error.message;
    }
  }

  applyFilters() {
    if (this.pending) return;
    this.read?.abort();
    this.read = null;
    this.busy = null;
    this.rows = [];
    this.next = null;
    this.selected = new Set();
    this.message = this.archiveError = this.listError = '';
    this.exportError = null;

    const form = new FormData(this.querySelector('form'));
    const filters = new URLSearchParams();
    for (const name of ['q', 'profile', 'mode', 'status']) {
      if (form.get(name)) filters.set(name, form.get(name));
    }
    this.filters = null;
    for (const [prefix, label] of [['created', 'Created'], ['activity', 'Last activity']]) {
      const from = dateBoundary(form.get(`${prefix}_from`), false);
      const before = dateBoundary(form.get(`${prefix}_through`), true);
      if (from === null || before === null) {
        this.listError = `Enter a valid ${label.toLowerCase()} date.`;
        return;
      }
      if (from && before && from >= before) {
        this.listError = `${label} start must be on or before the end date.`;
        return;
      }
      if (from) filters.set(`${prefix}_from`, from);
      if (before) filters.set(`${prefix}_before`, before);
    }
    this.filters = filters;
    void this.loadPage();
  }

  async readTopics(phase, work) {
    const read = new AbortController();
    this.read = read;
    this.busy = phase;
    this.listError = '';
    try {
      await work(read.signal);
    } catch (error) {
      if (!read.signal.aborted) this.listError = error.message;
    } finally {
      if (this.read === read) {
        this.read = null;
        this.busy = null;
      }
    }
  }

  async loadPage(more = false) {
    if (!this.filters || this.busy || (more && !this.next)) return;
    this.listRetry = () => this.loadPage(more);
    await this.readTopics('loading', async (read) => {
      const signal = AbortSignal.any([read, requestTimeout()]);
      const page = await getPage(this.filters, signal, more ? this.next : null);
      if (read.aborted) return;
      this.rows = more ? [...this.rows, ...page.items] : page.items;
      this.next = page.next;
    });
  }

  async selectMatching() {
    if (this.busy || this.pending) return;
    const filters = new URLSearchParams(this.filters);
    filters.set('status', 'active');
    this.message = '';
    this.listRetry = () => this.selectMatching();
    await this.readTopics('selecting', async (read) => {
      const ids = new Set();
      let next = null;
      do {
        const signal = AbortSignal.any([read, requestTimeout()]);
        const page = await getPage(filters, signal, next, batchSize);
        if (read.aborted) return;
        for (const { id } of page.items) ids.add(id);
        next = page.next;
      } while (next);
      this.selected = ids;
    });
  }

  async exportTopic(topic) {
    if (this.exporting.has(topic.id)) return;
    this.exporting = new Set([...this.exporting, topic.id]);
    if (this.exportError?.id === topic.id) this.exportError = null;
    try {
      await downloadTopic(topic.id);
    } catch (error) {
      if (this.dialog.open) this.exportError = { id: topic.id, message: error.message };
    } finally {
      const exporting = new Set(this.exporting);
      exporting.delete(topic.id);
      this.exporting = exporting;
    }
  }

  check(ids, checked) {
    const selected = new Set(this.selected);
    for (const id of ids) {
      if (checked) selected.add(id);
      else selected.delete(id);
    }
    this.message = '';
    this.selected = selected;
  }

  clearSelection() {
    this.pending = null;
    this.selected = new Set();
    this.archiveError = this.message = '';
  }

  async archive() {
    if (this.busy || !this.selected.size) return;
    if (!this.pending) {
      const count = this.selected.size;
      if (!window.confirm(
        `Archive ${count} ${plural(count)}? Archived topics stay readable. ` +
        'New messages and scheduled runs require restoring the topic first.'
      )) return;
      this.pending = { ids: [...this.selected], confirmed: 0 };
    }
    const pending = this.pending;
    const total = pending.ids.length;
    this.busy = 'archiving';
    this.archiveError = '';
    this.message = '';
    const updateProgress = () => {
      this.message = `${pending.confirmed} of ${total} ${plural(total)} confirmed archived.`;
    };
    updateProgress();
    try {
      while (pending.confirmed < total) {
        const ids = pending.ids.slice(pending.confirmed, pending.confirmed + batchSize);
        await api('/sessions/archive', {
          method: 'POST', body: { session_ids: ids }, signal: requestTimeout()
        });
        pending.confirmed += ids.length;
        this.check(ids, false);
        updateProgress();
        this.markArchived(ids);
      }
    } catch (error) {
      this.archiveError = `Archive paused. ${error.message}`;
    }
    if (pending.confirmed === total) {
      this.pending = null;
      this.message = `${total} ${plural(total)} archived.`;
    }
    // A request can commit before its response is lost. Refresh both views and keep those IDs retryable.
    try {
      await this.refreshSidebar(pending.ids, requestTimeout());
    } catch (error) {
      this.archiveError = [this.archiveError, `Could not refresh the sidebar. ${error.message}`]
        .filter(Boolean).join(' ');
    } finally {
      this.busy = null;
      await this.loadPage();
    }
  }

  dateFilter(prefix, label) {
    return html`
      <fieldset class="manager-dates">
        <legend>${label}</legend>
        <label>From <input type="date" name=${`${prefix}_from`} aria-label=${`${label} from`} /></label>
        <label>Through <input type="date" name=${`${prefix}_through`} aria-label=${`${label} through`} /></label>
      </fieldset>
    `;
  }

  render() {
    const archiving = this.busy === 'archiving';
    const locked = archiving || !!this.pending;
    const visible = this.rows.filter((topic) => !topic.archived).map((topic) => topic.id);
    const checked = visible.filter((id) => this.selected.has(id)).length;
    const count = this.selected.size;
    const status = this.busy === 'loading' ? 'Loading topics…'
      : this.busy === 'selecting' ? 'Finding all matching active topics…'
        : this.rows.length ? `${this.rows.length} ${plural(this.rows.length)} loaded.`
          : this.listError ? '' : 'No matching topics.';
    return html`
      <dialog id="topic-manager" class="wide" aria-labelledby="topic-manager-title"
        @close=${this.reset} @cancel=${(event) => { if (archiving) event.preventDefault(); }}>
        <header>
          <h2 id="topic-manager-title">Manage topics</h2>
          <div class="manager-actions">
            <button type="button" id="import-topic" aria-haspopup="dialog"
              ?disabled=${archiving} @click=${() => this.openImport()}>
              <sola-icon name="Upload"></sola-icon> Import topics
            </button>
            <button class="dialog-close" type="button" aria-label="Close topic manager"
              ?disabled=${archiving} @click=${this.close}><sola-icon name="X"></sola-icon></button>
          </div>
        </header>
        <form @submit=${(event) => { event.preventDefault(); this.applyFilters(); }}
          @change=${this.applyFilters}>
          <fieldset class="manager-filters" ?disabled=${locked}>
            <legend class="sr-only">Filter topics</legend>
            <div class="manager-filter-options">
              <label>Title <input type="search" name="q" maxlength="256" autofocus
                @input=${this.applyFilters} @change=${(event) => event.stopPropagation()} /></label>
              <label>Profile
                <select name="profile" aria-describedby="topic-profile-hint">
                  <option value="">Any profile</option>
                  ${this.profiles.map((name) => html`<option value=${name}>${name}</option>`)}
                </select>
              </label>
              <label>Memory mode
                <select name="mode">
                  <option value="">Any mode</option>
                  ${Object.entries(modes).map(([value, label]) => html`<option value=${value}>${label}</option>`)}
                </select>
              </label>
              <label>Status
                <select name="status">
                  <option value="active">Active</option>
                  <option value="archived">Archived</option>
                  <option value="all">All</option>
                </select>
              </label>
            </div>
            <p id="topic-profile-hint" class="sr-only">Matches any run that used this profile.</p>
            <div class="manager-date-options">
              ${this.dateFilter('created', 'Created')}
              ${this.dateFilter('activity', 'Last activity')}
            </div>
          </fieldset>
        </form>
        <p class="meta" role="alert" ?hidden=${!this.profileError}>
          ${this.profileError} <button type="button" @click=${this.loadProfiles}>Reload profiles</button>
        </p>
        <div class="manager-toolbar">
          <label>
            <input type="checkbox" .checked=${visible.length > 0 && checked === visible.length}
              .indeterminate=${checked > 0 && checked < visible.length}
              ?disabled=${locked || !!this.busy || !visible.length}
              @change=${(event) => this.check(visible, event.target.checked)} /> Select visible active topics
          </label>
          <button type="button" ?hidden=${this.filters?.get('status') === 'archived'}
            ?disabled=${locked || !!this.busy || !this.rows.length || !!this.listError}
            @click=${this.selectMatching}>Select all matching active topics</button>
        </div>
        <div class="manager-toolbar manager-selection">
          <p class="meta" role="status" aria-atomic="true">${count} selected. ${this.message}</p>
          <div class="manager-actions">
            <button type="button" ?disabled=${!!this.busy || !count}
              @click=${this.clearSelection}>Clear selection</button>
            <button type="button" class="primary" ?disabled=${!!this.busy || !count}
              @click=${this.archive}>${archiving ? 'Archiving…' : this.pending ? `Retry archive (${count})` : 'Archive selected'}</button>
          </div>
        </div>
        <p class="manager-error" role="alert" ?hidden=${!this.archiveError}>${this.archiveError}</p>
        <p class="manager-error" role="alert" ?hidden=${!this.listError}>${this.listError}</p>
        <button type="button" ?hidden=${!this.listError || !this.filters} ?disabled=${!!this.busy}
          @click=${() => this.listRetry()}>Retry topic lookup</button>
        <p class="meta manager-status" role="status" aria-atomic="true">${status}</p>
        <ul class="manager-rows" aria-label="Matching topics">
          ${this.rows.map((topic) => html`
            <li>
              <label class="manager-checkbox">
                <input type="checkbox" aria-label=${`Select ${titleOf(topic)}`}
                  .checked=${this.selected.has(topic.id)} ?disabled=${locked || !!this.busy || topic.archived}
                  @change=${(event) => this.check([topic.id], event.target.checked)} />
              </label>
              <div class="manager-row-details">
                <div class="manager-row-heading">
                  <button class="manager-topic-title" type="button" ?disabled=${archiving}
                    @click=${action(() => { this.close(); return this.openTopic(topic.id); })}>${titleOf(topic)}</button>
                  <button type="button"
                    aria-label=${`${this.exporting.has(topic.id) ? 'Exporting' : 'Export'} ${titleOf(topic)}`}
                    aria-disabled=${this.exporting.has(topic.id)}
                    @click=${() => this.exportTopic(topic)}>
                    ${this.exporting.has(topic.id) ? 'Exporting…' : 'Export'}
                  </button>
                </div>
                <div class="meta manager-topic-meta">
                  <span>${modes[topic.mode]} · ${topic.archived ? 'Archived' : 'Active'}</span>
                  <span>Created ${this.renderDate(topic.created_at)}</span>
                  <span>Last activity ${this.renderDate(topic.last_activity_at)}</span>
                </div>
                ${this.exportError?.id === topic.id
                  ? html`<p class="manager-error" role="alert">${this.exportError.message}</p>` : ''}
              </div>
            </li>
          `)}
        </ul>
        <button type="button" ?hidden=${!this.next} ?disabled=${!!this.busy}
          @click=${() => this.loadPage(true)}>Load more topics</button>
      </dialog>
    `;
  }

  renderDate(at) {
    return html`<time datetime=${at}>${localDate.format(new Date(at))}</time>`;
  }
}

customElements.define('sola-topic-manager', SolaTopicManager);
