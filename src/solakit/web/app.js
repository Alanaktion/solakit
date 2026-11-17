import { html } from './lit-core.min.js';
import { $, action, SolaElement } from './ui.js';

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
      loadJobs,
      toggleScheduler,
      saveJob,
      fillJob,
      refreshMemory,
      loadTools,
      search,
      connect,
      refreshHealth,
      moreEvidence
    } = this.actions;
    return html`
      <a class="skip" href="#draft">Skip to message</a>
      <aside class="sidebar" aria-label="Topics">
        <div class="brand">Sola <span>Your ongoing conversation</span></div>
        <button @click=${newTopic} id="new-topic">＋ New topic</button>
        <nav id="topics" aria-label="Choose a topic"></nav>
        <button @click=${action(() => loadTopics(true))} id="more-topics" hidden>
          More topics
        </button>
        <label class="archive-toggle"
          ><input @change=${action(() => loadTopics())} id="archived-topics" type="checkbox" />
          Archived topics</label
        >
        <div class="sidebar-footer">
          <button @click=${action(openSettings)} id="settings-open">Settings</button
          ><button @click=${action(logout)} id="logout">Sign out</button>
        </div>
      </aside>
      <main>
        <header class="topbar">
          <div>
            <h1 id="topic-title">Conversation</h1>
            <p id="topic-mode">A little room to think.</p>
          </div>
          <button @click=${editTopic} id="topic-edit" disabled>Topic details</button>
        </header>
        <p id="connection" role="status" aria-live="polite">Connecting…</p>
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
            <button @click=${newTopic} id="empty-new-topic" class="primary">Start a topic</button>
          </div>
          <div id="turns"></div>
        </section>
        <button @click=${jumpLatest} id="latest" hidden>Jump to latest ↓</button>
        <div id="outbox" aria-label="Submissions awaiting confirmation"></div>
        <form @submit=${submit} id="composer">
          <label class="sr-only" for="draft">Message Sola</label>
          <textarea
            @keydown=${draftKeydown}
            @input=${saveDraft}
            id="draft"
            rows="3"
            maxlength="32768"
            placeholder="Message Sola…"
            aria-describedby="compose-hint"
            required
          ></textarea>
          <div class="composer-actions">
            <span id="compose-hint">Enter to send · Shift + Enter for a new line</span>
            <label
              >Profile
              <select @change=${saveDraft} id="profile">
                <option value="">Server default</option>
              </select></label
            >
            <button class="primary" id="send" type="submit" aria-label="Send message" disabled>
              Send ↑
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
          ><input id="password" type="password" autocomplete="current-password" required />
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
      <dialog id="settings" class="wide" aria-labelledby="settings-title">
        <header>
          <h2 id="settings-title">Settings</h2>
          <button data-close="settings" @click=${() => $('settings').close()}>
            Close Settings
          </button>
        </header>
        <p>
          Authority and credentials are managed in your operator configuration. Changes below use
          your signed-in authority.
        </p>
        <details open>
          <summary>Scheduled &amp; proactive work</summary>
          <div class="actions">
            <button @click=${action(() => loadJobs())} id="jobs-refresh">Refresh jobs</button
            ><button @click=${action(toggleScheduler)} id="scheduler-toggle">
              Pause all schedules
            </button>
          </div>
          <p id="jobs-empty" class="meta" hidden>No jobs yet. Create one below when you need it.</p>
          <div id="jobs"></div>
          <button @click=${action(() => loadJobs(true))} id="more-jobs" hidden>More jobs</button>
          <form @submit=${saveJob} id="job-form">
            <h3 id="job-form-title">Create a job</h3>
            <p id="job-topic-hint" class="meta" hidden>
              Create a shared or isolated topic before scheduling work.
            </p>
            <label for="job-task">Task</label
            ><textarea id="job-task" required maxlength="32768" rows="3"></textarea>
            <div class="fields">
              <div>
                <label for="job-schedule">Schedule (cron: or once:)</label
                ><input id="job-schedule" required placeholder="cron:0 9 * * *" />
              </div>
              <div>
                <label for="job-zone">Time zone</label
                ><input id="job-zone" value="America/Denver" required />
              </div>
            </div>
            <label for="job-topic">Topic</label
            ><select id="job-topic" required></select>
            <label for="job-origin">Kind</label
            ><select id="job-origin">
              <option value="scheduled">Scheduled task</option>
              <option value="proactive">Proactive wake-up</option>
            </select>
            <label><input id="job-enabled" type="checkbox" /> Enable wake-ups for this job</label>
            <p>
              Saving an enabled job authorizes automatic work in its topic, within the selected
              profile’s permissions.
            </p>
            <div class="actions">
              <button type="button" @click=${() => fillJob()} id="job-reset">Clear</button
              ><button class="primary">Save job</button>
            </div>
          </form>
        </details>
        <details>
          <summary>Memory</summary>
          <button @click=${action(refreshMemory)} id="memory-refresh">Refresh memory status</button>
          <pre id="memory-status"></pre>
          <p>
            Staged notes are waiting for extraction. Only completed extraction is reported as saved
            memory. Inspect a run to reconcile failed or ambiguous extraction with a verified
            provider task ID.
          </p>
        </details>
        <details>
          <summary>Profiles, tools &amp; workspace roots</summary>
          <div id="profiles"></div>
          <label for="tool-profile">Tools for profile</label
          ><select @change=${action(loadTools)} id="tool-profile"></select>
          <pre id="tools"></pre>
        </details>
        <details>
          <summary>Credential references</summary>
          <p>
            Credential values are never loaded into this page. References in profile diagnostics are
            redacted; configure their environment variables outside the browser.
          </p>
        </details>
        <details>
          <summary>Search conversation evidence</summary>
          <form @submit=${search} id="search-form">
            <label for="search-query">Search</label
            ><input id="search-query" required maxlength="4096" /><label
              ><input type="checkbox" id="search-topic" checked /> Current topic only</label
            ><button>Search</button>
          </form>
          <div id="search-results"></div>
        </details>
        <details>
          <summary>Diagnostics</summary>
          <label
            ><input @change=${connect} id="poll-only" type="checkbox" /> Use polling instead of
            streaming</label
          ><button @click=${action(refreshHealth)} id="health-refresh">Refresh health</button>
          <pre id="health"></pre>
          <p id="transport-detail"></p>
        </details>
        <p id="settings-status" role="status"></p>
        <p id="settings-error" role="alert"></p>
      </dialog>
      <dialog id="evidence" class="wide" aria-labelledby="evidence-title">
        <header>
          <h2 id="evidence-title">Run evidence</h2>
          <button data-close="evidence" @click=${() => $('evidence').close()}>
            Close evidence
          </button>
        </header>
        <div id="evidence-records"></div>
        <button @click=${action(moreEvidence)} id="more-evidence">Load more records</button>
        <p id="evidence-error" role="alert"></p>
      </dialog>
    `;
  }

  firstUpdated() {
    this.actions.initialize();
  }
}
