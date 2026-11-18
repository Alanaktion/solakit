import { html } from './lit-core.min.js';
import { $, action, SolaElement } from './ui.js';
import { settingsTemplate } from './settings.js';

export class SolaApp extends SolaElement {
  static properties = { actions: { attribute: false } };

  render() {
    const {
      newTopic,
      loadTopics,
      openSettings,
      logout,
      editTopic,
      historyScroll,
      older,
      jumpLatest,
      submit,
      draftKeydown,
      saveDraft,
      login,
      saveTopic,
      moreEvidence,
      switchEvidence,
      settingsKeydown
    } = this.actions;
    return html`
      <a class="skip" href="#draft">Skip to message</a>
      <aside class="sidebar" aria-label="Topics">
        <div class="brand">🌙 Sola</div>
        <button @click=${newTopic} id="new-topic">
          <span aria-hidden="true">＋</span> New topic
        </button>
        <nav id="topics" aria-label="Choose a topic"></nav>
        <button @click=${action(() => loadTopics(true))} id="more-topics" hidden>
          More topics
        </button>
        <label class="archive-toggle"
          ><input @change=${action(() => loadTopics())} id="archived-topics" type="checkbox" />
          Archived topics</label
        >
        <div class="sidebar-footer">
          <button @click=${action(openSettings)} id="settings-open" aria-haspopup="dialog">
            Settings</button
          ><button @click=${action(logout)} id="logout">Sign out</button>
        </div>
      </aside>
      <main>
        <header class="topbar">
          <div>
            <h1 id="topic-title">Conversation</h1>
            <p id="topic-mode">A little room to think.</p>
          </div>
          <button @click=${editTopic} id="topic-edit" aria-haspopup="dialog" disabled>
            Topic details
          </button>
        </header>
        <p id="connection" role="status" aria-live="polite">Connecting…</p>
        <p id="reply-status" class="sr-only" role="status" aria-atomic="true"></p>
        <div id="notice" role="alert" hidden>
          <span id="notice-message"></span
          ><button
            @click=${() => {
              $('notice').hidden = true;
            }}
            id="notice-dismiss"
          >
            Dismiss
          </button>
        </div>
        <section @scroll=${historyScroll} id="history" aria-label="Conversation" tabindex="0">
          <button @click=${action(older)} id="older" hidden>Load older conversation</button>
          <div id="empty">
            <h2>What’s on your mind?</h2>
            <p id="empty-hint">Start a topic, then send Sola a message.</p>
            <button @click=${newTopic} id="empty-new-topic" class="primary">
              Start a topic
            </button>
          </div>
          <div id="turns"></div>
        </section>
        <button @click=${jumpLatest} id="latest" hidden>
          Jump to latest <span aria-hidden="true">↓</span>
        </button>
        <div id="outbox" role="group" aria-label="Submissions awaiting confirmation"></div>
        <form @submit=${submit} id="composer">
          <div id="draft-image" role="status"></div>
          <label class="sr-only" for="draft">Message Sola</label>
          <textarea
            @keydown=${draftKeydown}
            @input=${saveDraft}
            id="draft"
            rows="2"
            maxlength="32768"
            placeholder="Message Sola…"
            aria-describedby="compose-hint"
            required
          ></textarea>
          <div class="composer-actions">
            <span id="compose-hint" role="status"
              >Enter to send · Shift + Enter for a new line</span
            >
            <label
              >Profile
              <select @change=${saveDraft} id="profile">
                <option value="">Server default</option>
              </select></label
            >
            <button class="primary" id="send" type="submit" aria-label="Send message" disabled>
              Send <span aria-hidden="true">↑</span>
            </button>
          </div>
        </form>
      </main>
      <dialog
        @cancel=${(event) => event.preventDefault()}
        id="login-dialog"
        aria-labelledby="login-title"
      >
        <form @submit=${login} id="login-form">
          <h2 id="login-title">Welcome to Sola</h2>
          <p id="login-reason">Sign in with your operator password.</p>
          <label for="password">Password</label
          ><input
            id="password"
            type="password"
            autocomplete="current-password"
            aria-describedby="login-reason login-error"
            required
          />
          <p id="login-error" role="alert"></p>
          <button class="primary">Sign in</button>
        </form>
      </dialog>
      <dialog id="topic-dialog" aria-labelledby="topic-dialog-title">
        <form @submit=${saveTopic} id="topic-form">
          <h2 id="topic-dialog-title">New topic</h2>
          <p id="topic-error" role="alert"></p>
          <label for="topic-name">Title</label
          ><input id="topic-name" maxlength="256" autocomplete="off" autofocus />
          <label for="mode">Memory mode</label
          ><select id="mode">
            <option value="shared">Shared memory</option>
            <option value="isolated">Isolated — no shared memory</option>
            <option value="ephemeral">Ephemeral — one run, evidence retained</option>
          </select>
          <label><input type="checkbox" id="pinned" /> Pin topic</label
          ><label><input type="checkbox" id="archived" /> Archive topic</label>
          <div class="actions">
            <button
              type="button"
              data-close="topic-dialog"
              @click=${() => $('topic-dialog').close()}
            >
              Cancel</button
            ><button class="primary">Save topic</button>
          </div>
        </form>
      </dialog>
      ${settingsTemplate(this.actions)}
      <dialog id="evidence" class="wide" aria-labelledby="evidence-title">
        <header>
          <h2 id="evidence-title">Run evidence</h2>
          <button data-close="evidence" @click=${() => $('evidence').close()}>
            Close evidence
          </button>
        </header>
        <div
          class="evidence-tabs"
          role="tablist"
          aria-label="Evidence view"
          @keydown=${settingsKeydown}
        >
          <button
            id="evidence-tab-records"
            role="tab"
            aria-selected="true"
            aria-controls="evidence-records"
            @click=${action(() => switchEvidence('records'))}
          >
            All records
          </button>
          <button
            id="evidence-tab-prompts"
            role="tab"
            aria-selected="false"
            aria-controls="evidence-records"
            tabindex="-1"
            @click=${action(() => switchEvidence('prompts'))}
          >
            Model requests
          </button>
        </div>
        <div
          id="evidence-records"
          role="tabpanel"
          aria-labelledby="evidence-tab-records"
          tabindex="0"
        ></div>
        <button @click=${action(moreEvidence)} id="more-evidence">Load more records</button>
        <p id="evidence-error" role="alert"></p>
      </dialog>
    `;
  }

  firstUpdated() {
    this.actions.initialize();
  }
}
