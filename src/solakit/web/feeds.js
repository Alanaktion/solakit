import { html, nothing } from './lit-core.min.js';
import { api, uid } from './client.js';
import { $, action, agentName, SolaElement, text } from './ui.js';

const date = (value) => new Date(value).toLocaleString();
const age = (value) => {
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 60000));
  const unit = minutes >= 1440 ? 'day' : minutes >= 60 ? 'hour' : 'minute';
  const divisor = { day: 1440, hour: 60, minute: 1 }[unit];
  return new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' }).format(
    -Math.floor(minutes / divisor),
    unit
  );
};
const label = (feed) => feed?.title || feed?.url || 'Feed';

class SolaFeeds extends SolaElement {
  static properties = {
    view: {},
    feeds: { state: true },
    items: { state: true },
    enabled: { state: true },
    filter: { state: true },
    unread: { state: true },
    next: { state: true },
    opened: { state: true },
    asking: { state: true },
    editing: { state: true }
  };

  constructor() {
    super();
    this.view = 'reader';
    this.feeds = [];
    this.items = [];
    this.enabled = false;
    this.filter = '';
    this.unread = true;
    this.next = null;
    this.itemRequest = 0;
    this.opened = null;
    this.asking = false;
    this.editing = null;
  }

  async load() {
    const result = await api('/feeds');
    this.feeds = result.feeds;
    this.enabled = result.enabled;
    if (!this.feeds.some((feed) => feed.id === this.filter)) this.filter = '';
    await this.loadItems();
  }

  async loadItems(more = false) {
    const request = ++this.itemRequest;
    const params = new URLSearchParams({ unread_only: this.unread });
    if (this.filter) params.set('feed_id', this.filter);
    if (more && this.next !== null) params.set('offset', this.next);
    const result = await api(`/feed-items?${params}`);
    if (request !== this.itemRequest) return;
    this.items = more ? [...this.items, ...result.items] : result.items;
    this.next = result.next;
  }

  async updateFilter(event) {
    if (event.target.id === 'feed-filter') this.filter = event.target.value;
    else this.unread = event.target.checked;
    this.opened = null;
    await this.loadItems();
  }

  async readItem(id, open) {
    const item = await api(`/feed-items/${id}/read`, { method: 'POST' });
    if (open) {
      this.opened = item;
      this.asking = false;
      this.questionKey = uid();
    }
    await this.load();
    if (open) {
      await this.updateComplete;
      $('feed-item-heading').focus();
    } else if (this.unread) {
      $('feed-filter').focus();
    }
  }

  async ask(event) {
    const form = event.target;
    const button = form.querySelector('button[type=submit]');
    if (button.disabled) return;
    button.disabled = true;
    try {
      const run = await api(`/feed-items/${this.opened.id}/ask`, {
        method: 'POST',
        headers: { 'Idempotency-Key': this.questionKey },
        body: { content: $('feed-question').value, profile: $('profile').value || null }
      });
      this.asking = false;
      this.dispatchEvent(new CustomEvent('feed-discussion', { detail: run.session_id }));
    } finally {
      button.disabled = false;
    }
  }

  async edit(feed) {
    this.editing = feed;
    await this.updateComplete;
    const form = $('feed-form');
    form.open = true;
    $(feed ? 'feed-title' : 'feed-url').focus();
  }

  async save(event) {
    const form = event.target;
    const button = form.querySelector('button[type=submit]');
    if (button.disabled) return;
    button.disabled = true;
    const keywords = (id) => $(id).value.split('\n').map((word) => word.trim()).filter(Boolean);
    const body = {
      title: $('feed-title').value,
      keywords: keywords('feed-keywords'),
      exclude_keywords: keywords('feed-exclude')
    };
    const edited = this.editing;
    if (!edited) body.url = $('feed-url').value;
    try {
      await api(edited ? `/feeds/${edited.id}` : '/feeds', {
        method: edited ? 'PATCH' : 'POST',
        body
      });
      form.reset();
      this.editing = null;
      $('feed-form').open = false;
      await this.load();
      $('feed-form').querySelector('summary').focus();
      text($('settings-status'), edited ? 'Subscription saved.' : 'Subscription added.');
    } finally {
      button.disabled = false;
    }
  }

  async manage(feed, operation, event) {
    const button = event.currentTarget || event.target.closest('button');
    if (button.disabled) return;
    if (operation === 'remove' && !window.confirm('Remove this feed and its reader items?')) return;
    if (operation === 'reject' && !window.confirm('Reject this proposal?')) return;
    button.disabled = true;
    const remove = ['remove', 'reject'].includes(operation);
    try {
      const result = await api(`/feeds/${feed.id}${remove ? '' : `/${operation}`}`, {
        method: remove ? 'DELETE' : 'POST'
      });
      if (remove && this.opened?.feed_id === feed.id) this.opened = null;
      if (remove && this.editing?.id === feed.id) this.editing = null;
      await this.load();
      text($('settings-status'), operation === 'poll'
        ? (result.last_failure_reason || 'Poll complete.')
        : 'Subscription updated.');
      if (remove || ['approve', 'pause', 'resume'].includes(operation)) {
        $('feed-form').querySelector('summary').focus();
      }
    } finally {
      button.disabled = false;
    }
  }

  reader() {
    const item = this.opened;
    return html`
      <div class="fields">
        <label>Show feed
          <select id="feed-filter" .value=${this.filter} @change=${action((e) => this.updateFilter(e))}>
            <option value="">All feeds</option>
            ${this.feeds.map((feed) => html`<option value=${feed.id} ?selected=${this.filter === feed.id}>${label(feed)}</option>`)}
          </select>
        </label>
        <label><input id="feeds-unread" type="checkbox" .checked=${this.unread}
          @change=${action((e) => this.updateFilter(e))} /> Unread only</label>
      </div>
      ${this.filter ? html`
        <button @click=${action(async () => {
          const { marked } = await api(`/feeds/${this.filter}/read`, { method: 'POST' });
          await this.load();
          text($('settings-status'), `Marked ${marked} items read.`);
        })}>Mark all read</button>` : nothing}
      ${item ? html`
        <article class="feed-excerpt" aria-labelledby="feed-item-heading">
          <h4 id="feed-item-heading" tabindex="-1">${item.title || 'Untitled item'}</h4>
          <p class="meta">External excerpt from ${label(this.feeds.find((feed) => feed.id === item.feed_id))}</p>
          <p>${item.summary || 'No summary available.'}</p>
          <div class="actions">
            ${item.link ? html`<a href=${item.link} target="_blank" rel="noopener noreferrer">Open article</a>` : nothing}
            ${this.asking ? nothing : html`
              <button class="primary" @click=${action(async () => {
                this.asking = true;
                await this.updateComplete;
                $('feed-question').focus();
              })}>Ask ${agentName} about this</button>`}
            <button @click=${() => { this.opened = null; $('feed-filter').focus(); }}>Close excerpt</button>
          </div>
          ${this.asking ? html`
            <form @submit=${(event) => { event.preventDefault(); action((e) => this.ask(e))(event); }}>
              <label for="feed-question">Your question</label>
              <textarea id="feed-question" rows="3" required maxlength="32768"></textarea>
              <div class="actions"><button class="primary" type="submit">Send to a new isolated topic</button></div>
            </form>` : nothing}
        </article>` : nothing}
      ${this.items.length ? html`
        <ul class="feed-items">
          ${this.items.map((entry) => html`
            <li>
              <div>
                <button class="feed-item-title" @click=${action(() => this.readItem(entry.id, true))}>
                  ${entry.title || 'Untitled item'}
                </button>
                <p class="meta">${label(this.feeds.find((feed) => feed.id === entry.feed_id))} ·
                  <time datetime=${entry.published_at || entry.fetched_at}>${date(entry.published_at || entry.fetched_at)}</time>
                  ${this.unread ? nothing : entry.read ? ' · Read' : ' · Unread'}
                </p>
              </div>
              ${entry.read ? nothing : html`<button aria-label=${`Mark read for ${entry.title || 'untitled item'}`}
                @click=${action(() => this.readItem(entry.id, false))}>Mark read</button>`}
            </li>`)}
        </ul>` : html`<p class="empty-state">${this.unread ? 'No unread feed items.' : 'No feed items.'}</p>`}
      ${this.next !== null ? html`<button @click=${action(() => this.loadItems(true))}>More items</button>` : nothing}
    `;
  }

  subscriptions() {
    const edit = this.editing;
    const control = (feed, name, caption) => html`
      <button @click=${action((event) => this.manage(feed, name, event))}>${caption}</button>`;
    return html`
      <details id="feed-form">
        <summary>${edit ? 'Edit subscription' : 'Add a feed'}</summary>
        <form @submit=${(event) => { event.preventDefault(); action((e) => this.save(e))(event); }}>
          ${edit ? html`<p class="meta">${edit.url}</p>` : html`
            <label for="feed-url">Feed URL</label><input id="feed-url" type="url" maxlength="2048" required />`}
          <label for="feed-title">Title (optional)</label>
          <input id="feed-title" maxlength="200" .value=${edit?.title || ''} />
          <label for="feed-keywords">Include keywords, one per line</label>
          <textarea id="feed-keywords" rows="2" .value=${edit?.keywords.join('\n') || ''}></textarea>
          <label for="feed-exclude">Exclude keywords, one per line</label>
          <textarea id="feed-exclude" rows="2" .value=${edit?.exclude_keywords.join('\n') || ''}></textarea>
          <div class="actions">
            <button class="primary" type="submit">${edit ? 'Save subscription' : 'Add subscription'}</button>
            ${edit ? html`<button type="button" @click=${action(() => this.edit(null))}>Cancel edit</button>` : nothing}
          </div>
        </form>
      </details>
      ${this.feeds.length ? this.feeds.map((feed) => html`
        <article class="feed-subscription">
          <h4>${label(feed)}</h4>
          ${feed.title ? html`<p class="meta">${feed.url}</p>` : nothing}
          <p class="meta">
            ${feed.source === 'agent' ? 'Agent' : 'Operator'} · ${feed.status}
            · ${feed.unread_count} unread of ${feed.item_count} items
            · ${feed.consecutive_failures} consecutive failures
          </p>
          <p class="meta">${feed.last_fetched_at ? `Last fetched ${date(feed.last_fetched_at)}` : 'Never fetched'}</p>
          ${feed.status === 'pending' ? html`
            <p class="meta">Proposed ${age(feed.created_at)}
              ${feed.discovered_from ? ` · Found via ${feed.discovered_from}` : nothing}</p>
            <p>${feed.discovery_reason}</p>` : nothing}
          ${feed.last_failure_reason ? html`<p class="feed-failure">${feed.last_failure_reason}</p>` : nothing}
          <div class="actions">
            ${feed.status === 'pending' ? [
              control(feed, 'approve', 'Approve'), control(feed, 'reject', 'Reject')
            ] : [
              control(feed, feed.status === 'active' ? 'pause' : 'resume', feed.status === 'active' ? 'Pause' : 'Resume'),
              feed.status === 'active' && this.enabled ? control(feed, 'poll', 'Poll now') : nothing,
              control(feed, 'remove', 'Remove')
            ]}
            <button @click=${action(() => this.edit(feed))}>Edit</button>
          </div>
        </article>`) : html`<p class="empty-state">No feed subscriptions yet.</p>`}
    `;
  }

  render() {
    return html`
      <h3>Feeds</h3>
      <div class="actions">
        <button aria-pressed=${this.view === 'reader'} @click=${() => { this.view = 'reader'; }}>Reader</button>
        <button aria-pressed=${this.view === 'subscriptions'} @click=${() => { this.view = 'subscriptions'; }}>
          Subscriptions${this.feeds.some((feed) => feed.status === 'pending') ? ' · pending' : ''}
        </button>
        <button @click=${action(() => this.load())}>Refresh</button>
      </div>
      ${this.enabled ? nothing : html`<p class="meta">Feed polling is disabled in operator configuration.</p>`}
      ${this.view === 'reader' ? this.reader() : this.subscriptions()}
    `;
  }
}

customElements.define('sola-feeds', SolaFeeds);
