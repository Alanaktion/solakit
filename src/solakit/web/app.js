import { html } from './lit-core.min.js';
import { $, action, SolaElement, agentName } from './ui.js';
import { settingsTemplate } from './settings.js';
import { closeResult } from './result.js';
import './topics.js';

export class SolaApp extends SolaElement {
  static properties = { actions: { attribute: false } };

  render() {
    const {
      newTopic,
      loadTopics,
      openManagedTopic,
      markManagedTopicsArchived,
      refreshManagedTopics,
      openSettings,
      logout,
      editTopic,
      historyScroll,
      older,
      jumpLatest,
      submit,
      draftKeydown,
      saveDraft,
      pickFiles,
      dragFiles,
      dropFiles,
      login,
      saveTopic,
      exportTopic,
      openFollowUp,
      saveFollowUp,
      openImport,
      chooseImport,
      importTopic,
      moreEvidence,
      switchEvidence,
      settingsKeydown
    } = this.actions;
    return html`
      <a class="skip" href="#draft">Skip to message</a>
      <aside class="sidebar" aria-label="Topics">
        <div class="brand">🌙 ${agentName}</div>
        <button @click=${newTopic} id="new-topic">
          <sola-icon name="Plus"></sola-icon> New topic
        </button>
        <div class="topic-search-tools">
          <input
            id="topic-search"
            type="search"
            aria-label="Search topics by title"
            placeholder="Search topics…"
            maxlength="256"
            @input=${action(() => loadTopics())}
          />
          <button type="button" id="manage-topics" aria-haspopup="dialog"
            @click=${action(() => $('topic-manager-component').show())}>
            <sola-icon name="ListChecks"></sola-icon> Manage topics
          </button>
        </div>
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
            <sola-icon name="Settings"></sola-icon> Settings</button
          ><button @click=${action(logout)} id="logout">
            <sola-icon name="LogOut"></sola-icon> Sign out
          </button>
        </div>
      </aside>
      <main @dragenter=${dragFiles} @dragover=${dragFiles} @dragleave=${dragFiles} @drop=${dropFiles}>
        <header class="topbar">
          <div>
            <h1 id="topic-title">Conversation</h1>
            <p id="topic-mode">A little room to think.</p>
          </div>
          <button @click=${editTopic} id="topic-edit" aria-haspopup="dialog" disabled>
            <sola-icon name="SlidersHorizontal"></sola-icon> Topic details
          </button>
        </header>
        <p id="connection" role="status" aria-live="polite">Connecting…</p>
        <p id="reply-status" class="sr-only" role="status" aria-atomic="true"></p>
        <p id="copy-status" class="sr-only" role="status"></p>
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
        <div class="history-wrap">
          <section @scroll=${historyScroll} id="history" aria-label="Conversation" tabindex="0">
            <button @click=${action(older)} id="older" hidden>Load older conversation</button>
            <div id="empty">
              <h2>What’s on your mind?</h2>
              <p id="empty-hint">Start a topic, then send ${agentName} a message.</p>
              <button @click=${newTopic} id="empty-new-topic" class="primary">
                Start a topic
              </button>
            </div>
            <div id="turns"></div>
          </section>
          <button @click=${jumpLatest} id="latest" hidden>
            Jump to latest <sola-icon name="ArrowDown"></sola-icon>
          </button>
        </div>
        <div id="outbox" role="group" aria-label="Submissions awaiting confirmation"></div>
        <form @submit=${submit} id="composer">
          <div id="draft-image"></div>
          <div id="draft-documents"></div>
          <input id="attachment-files" type="file" multiple @change=${pickFiles} hidden />
          <label class="sr-only" for="draft">Message ${agentName}</label>
          <textarea
            @keydown=${draftKeydown}
            @input=${saveDraft}
            id="draft"
            rows="2"
            maxlength="32768"
            placeholder="Message ${agentName}…"
            aria-describedby="compose-hint"
            required
          ></textarea>
          <div class="composer-actions">
            <button id="attach-files" type="button" disabled
              @click=${() => $('attachment-files').click()}>
              <sola-icon name="Paperclip"></sola-icon> Attach files
            </button>
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
              Send <sola-icon name="ArrowUp"></sola-icon>
            </button>
          </div>
        </form>
        <div id="drop-files" aria-hidden="true" hidden><span>Drop files to attach</span></div>
      </main>
      <dialog
        @cancel=${(event) => event.preventDefault()}
        id="login-dialog"
        aria-labelledby="login-title"
      >
        <form @submit=${login} id="login-form">
          <h2 id="login-title">Welcome to ${agentName}</h2>
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
            <button type="button" id="export-topic" @click=${exportTopic} hidden>Export topic</button>
            <button type="button" id="schedule-follow-up" @click=${openFollowUp} hidden>
              Schedule follow-up
            </button>
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
      <dialog id="follow-up-dialog" aria-labelledby="follow-up-title"
        @cancel=${(event) => { if ($('follow-up-submit').disabled) event.preventDefault(); }}>
        <form @submit=${saveFollowUp} id="follow-up-form">
          <h2 id="follow-up-title">Schedule follow-up</h2>
          <p class="meta" id="follow-up-topic"></p>
          <div id="follow-up-fields">
            <label for="follow-up-task">Task</label>
            <textarea id="follow-up-task" rows="3" maxlength="32768" required autofocus></textarea>
            <label for="follow-up-at">Date and time</label>
            <input id="follow-up-at" type="datetime-local" step="1" required
              aria-describedby="follow-up-timezone" />
            <p class="meta" id="follow-up-timezone"></p>
            <label for="follow-up-offset">UTC offset (optional)</label>
            <input id="follow-up-offset" type="text" pattern="(?:[+]|-)[0-9]{2}:[0-9]{2}"
              maxlength="6" placeholder="-07:00" aria-describedby="follow-up-offset-hint" />
            <p class="meta" id="follow-up-offset-hint">Use an offset to specify a time in a repeated hour or another time zone.</p>
          </div>
          <p id="follow-up-error" role="alert"></p>
          <p id="follow-up-status" role="status"></p>
          <div class="actions">
            <button id="follow-up-close" type="button" @click=${() => $('follow-up-dialog').close()}>
              Cancel
            </button>
            <button class="primary" id="follow-up-submit">Schedule</button>
            <button type="button" id="follow-up-schedules" hidden @click=${action(async () => {
              $('follow-up-dialog').close();
              await openSettings();
            })}>View schedules</button>
          </div>
        </form>
      </dialog>
      <dialog id="import-dialog" aria-labelledby="import-title"
        @cancel=${(event) => { if ($('import-file').disabled) event.preventDefault(); }}>
        <form @submit=${importTopic} id="import-form">
          <h2 id="import-title">Import topics</h2>
          <label for="import-file">Solakit ZIP archives</label>
          <input id="import-file" type="file" accept=".zip,application/zip" multiple
            @change=${chooseImport} autofocus />
          <p id="import-error" role="alert"></p>
          <p id="import-status" role="status"></p>
          <div class="actions">
            <button type="button" id="import-cancel" @click=${() => $('import-dialog').close()}>Cancel</button>
            <button class="primary" id="import-submit" disabled>Import topics</button>
          </div>
        </form>
      </dialog>
      <sola-topic-manager id="topic-manager-component" .openTopic=${openManagedTopic}
        .markArchived=${markManagedTopicsArchived} .refreshSidebar=${refreshManagedTopics}
        .openImport=${openImport}>
      </sola-topic-manager>
      ${settingsTemplate(this.actions)}
      <dialog id="result-dialog" class="wide" aria-labelledby="result-title"
        @close=${() => { if (!$('result-dialog').open) closeResult(); }}
        @cancel=${(event) => { event.preventDefault(); closeResult(); }}>
        <header>
          <h2 id="result-title"></h2>
          <button type="button" id="result-close" class="dialog-close"
            aria-label="Close preview" autofocus @click=${() => closeResult()}>
            <sola-icon name="X"></sola-icon>
          </button>
        </header>
        <div id="result-body"></div>
      </dialog>
      <dialog id="evidence" class="wide" aria-labelledby="evidence-title" closedby="any">
        <header>
          <h2 id="evidence-title">Run evidence</h2>
          <button
            type="button"
            class="dialog-close"
            aria-label="Close evidence"
            @click=${() => $('evidence').close()}
          >
            <sola-icon name="X"></sola-icon>
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
    const evidence = $('evidence');
    if (!('closedBy' in evidence)) {
      // The backdrop and the dialog padding both target the dialog itself.
      const onBackdrop = (event) => {
        if (event.target !== evidence) return false;
        const { left, right, top, bottom } = evidence.getBoundingClientRect();
        return (
          event.clientX < left || event.clientX >= right ||
          event.clientY < top || event.clientY >= bottom
        );
      };
      let pressedBackdrop = false;
      evidence.addEventListener('pointerdown', (event) => {
        pressedBackdrop = onBackdrop(event);
      });
      evidence.addEventListener('click', (event) => {
        if (pressedBackdrop && onBackdrop(event)) evidence.close();
      });
    }
    this.actions.initialize();
  }
}
