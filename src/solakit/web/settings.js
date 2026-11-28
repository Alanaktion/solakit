import { html, nothing, render as renderTemplate } from './lit-core.min.js';
import { api } from './client.js';
import { $, text, action, showError, agentName } from './ui.js';
import { openEvidence } from './evidence.js';
import './job.js';
import './feeds.js';

const tabs = [
  ['jobs', 'Schedules'],
  ['memory', 'Memory'],
  ['access', 'Profiles & tools'],
  ['search', 'Search'],
  ['diagnostics', 'Diagnostics'],
  ['feeds', 'Feeds'],
  ['security', 'Security']
];
const budgets = [
  ['model_calls', 'Model calls'],
  ['tool_calls', 'Tool calls'],
  ['active_seconds', 'Active seconds'],
  ['output_tokens', 'Output tokens'],
  ['tool_result_bytes', 'Tool result bytes'],
  ['tool_timeout_seconds', 'Tool timeout (seconds)']
];
const json = (value) => html`<pre tabindex="0">${JSON.stringify(value, null, 2)}</pre>`;

export function settingsTemplate(actions) {
  const {
    switchSettingsTab,
    openFeedDiscussion,
    settingsKeydown,
    loadJobs,
    toggleScheduler,
    fillJob,
    saveJob,
    refreshMemory,
    reconcileMemory,
    loadTools,
    search,
    connect,
    refreshHealth,
    inspectPrompt,
    revokeLogins,
    changeJobRestriction,
    changeJobProfile
  } = actions;
  return html`
    <dialog id="settings" class="wide" aria-labelledby="settings-title">
      <header>
        <h2 id="settings-title">Settings</h2>
        <button
          type="button"
          class="dialog-close"
          aria-label="Close Settings"
          @click=${() => $('settings').close()}
        >
          <sola-icon name="X"></sola-icon>
        </button>
      </header>
      <div class="settings-layout">
        <div
          class="settings-nav"
          role="tablist"
          aria-label="Settings sections"
          @keydown=${settingsKeydown}
        >
          ${tabs.map(
            ([id, label], index) => html`
              <button
                id=${`settings-tab-${id}`}
                role="tab"
                aria-controls=${`settings-panel-${id}`}
                aria-selected=${String(index === 0)}
                tabindex=${index === 0 ? 0 : -1}
                @click=${action(() => switchSettingsTab(id))}
              >
                ${label}
              </button>
            `
          )}
        </div>
        <div class="settings-content">
          <section
            id="settings-panel-jobs"
            role="tabpanel"
            aria-labelledby="settings-tab-jobs"
            tabindex="0"
          >
            <div class="section-heading">
              <div>
                <h3>Scheduled &amp; proactive work</h3>
                <p class="meta">
                  Give ${agentName} a task to revisit, or a bounded wake-up to decide what needs
                  attention.
                </p>
              </div>
              <button class="primary" id="job-new" @click=${action(() => fillJob())}>
                New job
              </button>
            </div>
            <div class="actions">
              <button @click=${action(() => loadJobs())} id="jobs-refresh">Refresh jobs</button>
              <button @click=${action(toggleScheduler)} id="scheduler-toggle">
                Pause all schedules
              </button>
            </div>
            <p id="scheduler-status" class="meta" role="status"></p>
            <p id="jobs-empty" class="empty-state" hidden>
              No jobs yet. Create a job when you want ${agentName} to follow up.
            </p>
            <div id="jobs"></div>
            <button @click=${action(() => loadJobs(true))} id="more-jobs" hidden>
              More jobs
            </button>
          </section>
          <section
            id="settings-panel-memory"
            role="tabpanel"
            aria-labelledby="settings-tab-memory"
            tabindex="0"
            hidden
          >
            <h3>Memory</h3>
            <p class="meta">
              Staged notes become saved memory only after extraction completes.
            </p>
            <button @click=${action(refreshMemory)} id="memory-refresh">
              Refresh memory status
            </button>
            <div id="memory-status"></div>
            <details class="settings-section">
              <summary>Reconcile failed or ambiguous extraction</summary>
              <p class="meta">
                Find the commit ID in a run’s memory evidence and verify its existing task in
                OpenViking. This checks that task without resubmitting content.
              </p>
              <form @submit=${reconcileMemory} id="memory-form">
                <label for="memory-commit">Memory commit ID</label>
                <input id="memory-commit" required pattern="[a-f0-9]{32}" maxlength="32" />
                <label for="memory-task">Verified provider task ID</label>
                <input id="memory-task" required maxlength="256" />
                <div class="actions"><button>Reconcile extraction</button></div>
              </form>
              <div id="memory-result"></div>
            </details>
          </section>
          <section
            id="settings-panel-access"
            role="tabpanel"
            aria-labelledby="settings-tab-access"
            tabindex="0"
            hidden
          >
            <h3>Profiles &amp; tools</h3>
            <p class="meta">
              Permissions, workspace roots and credentials are managed in your operator
              configuration.
            </p>
            <label for="tool-profile">Profile</label>
            <select @change=${action(loadTools)} id="tool-profile"></select>
            <div id="profiles"></div>
            <h3 class="settings-section">Available tools</h3>
            <div id="tools"></div>
            <p class="meta settings-section">
              Credential values never enter this page. Environment references are redacted in
              configuration details.
            </p>
          </section>
          <section
            id="settings-panel-search"
            role="tabpanel"
            aria-labelledby="settings-tab-search"
            tabindex="0"
            hidden
          >
            <h3>Search evidence</h3>
            <p class="meta">
              Find conversation and execution records, including retained evidence from archived
              topics.
            </p>
            <form @submit=${search} id="search-form">
              <label for="search-query">Search phrase</label
              ><input id="search-query" required maxlength="4096" />
              <div class="fields">
                <label
                  >Scope<select id="search-scope">
                    <option value="current">Current topic</option>
                    <option value="all">All topics</option>
                  </select></label
                >
                <label
                  >Results<select id="search-limit">
                    <option>20</option>
                    <option>50</option>
                    <option>100</option>
                  </select></label
                >
              </div>
              <div class="actions"><button class="primary">Search</button></div>
            </form>
            <p id="search-status" role="status" class="meta"></p>
            <div id="search-results"></div>
          </section>
          <section
            id="settings-panel-diagnostics"
            role="tabpanel"
            aria-labelledby="settings-tab-diagnostics"
            tabindex="0"
            hidden
          >
            <h3>Runtime health</h3>
            <p id="transport-detail" class="meta"></p>
            <label
              ><input @change=${connect} id="poll-only" type="checkbox" /> Use polling instead
              of streaming</label
            >
            <div class="actions">
              <button @click=${action(refreshHealth)} id="health-refresh">
                Refresh health
              </button>
            </div>
            <div id="health"></div>
            <details class="settings-section">
              <summary>Inspect a draft prompt</summary>
              <p class="meta">
                Preview prompt assembly without running the model. Includes operator prompt
                content; excludes topic history and memory recall.
              </p>
              <form @submit=${inspectPrompt} id="prompt-form">
                <label for="prompt-profile">Profile</label
                ><select id="prompt-profile"></select>
                <label for="prompt-content">Draft message</label
                ><textarea id="prompt-content" rows="3" required maxlength="32768"></textarea>
                <label
                  ><input type="checkbox" id="prompt-hashes" /> Include content hashes</label
                >
                <div class="actions"><button>Inspect prompt</button></div>
              </form>
              <div id="prompt-result"></div>
            </details>
          </section>
          <section
            id="settings-panel-feeds"
            role="tabpanel"
            aria-labelledby="settings-tab-feeds"
            tabindex="0"
            hidden
          >
            <sola-feeds id="feed-settings" @feed-discussion=${action(openFeedDiscussion)}></sola-feeds>
          </section>
          <section
            id="settings-panel-security"
            role="tabpanel"
            aria-labelledby="settings-tab-security"
            tabindex="0"
            hidden
          >
            <h3>Sign-in &amp; security</h3>
            <p id="login-status" class="meta"></p>
            <div class="settings-section">
              <h3>Sign out everywhere</h3>
              <p class="meta">
                Revoke every browser and API login, including this one. You’ll need your
                operator password to sign in again.
              </p>
              <button @click=${action(revokeLogins)} id="revoke-logins">
                Sign out all devices
              </button>
            </div>
            <p class="meta settings-section">
              Password and credential changes belong in your operator configuration and
              environment.
            </p>
          </section>
        </div>
      </div>
      <footer class="settings-feedback">
        <p id="settings-status" role="status"></p>
        <p id="settings-error" role="alert"></p>
      </footer>
    </dialog>
    <dialog id="job-dialog" aria-labelledby="job-form-title">
      <form @submit=${saveJob} id="job-form">
        <h2 id="job-form-title">Create a job</h2>
        <p id="job-error" role="alert"></p>
        <label for="job-task">Task</label
        ><textarea id="job-task" required maxlength="32768" rows="3" autofocus></textarea>
        <label for="job-schedule">Schedule</label
        ><input
          id="job-schedule"
          required
          maxlength="256"
          placeholder="cron:0 9 * * *"
          aria-describedby="job-schedule-hint"
        />
        <p id="job-schedule-hint" class="meta">
          Use cron:0 9 * * * for daily at 9 am, or once:2026-12-01T09:00:00-07:00 for a single
          date.
        </p>
        <label for="job-zone">Time zone</label
        ><input id="job-zone" value="America/Denver" required maxlength="128" />
        <label for="job-topic">Topic</label
        ><select id="job-topic" required @change=${changeJobProfile}></select>
        <p class="meta">
          One-run topics are archived after each run. Execution evidence is retained.
        </p>
        <div class="fields">
          <label
            >Kind<select id="job-origin">
              <option value="scheduled">Scheduled task</option>
              <option value="proactive">Proactive wake-up</option>
            </select></label
          >
          <label>Profile<select id="job-profile" @change=${changeJobProfile}></select></label>
        </div>
        <label for="job-delivery">Results</label
        ><select id="job-delivery" @change=${changeJobProfile}>
          <option value="session">Publish to the target topic</option>
          <option value="none">Keep in execution evidence only</option>
        </select>
        <details id="job-advanced" class="settings-section">
          <summary>Permissions &amp; limits</summary>
          <p class="meta">
            Overrides can only narrow the selected profile. Leave limits blank to inherit its
            scheduled-work limits.
          </p>
          <label
            ><input id="job-restrict-tools" type="checkbox" @change=${changeJobRestriction} />
            Limit tools for this job</label
          >
          <fieldset id="job-tools" hidden>
            <legend>Tool names</legend>
            <div id="job-tool-options"></div>
          </fieldset>
          <label
            ><input id="job-restrict-roots" type="checkbox" @change=${changeJobRestriction} />
            Limit workspace roots for this job</label
          >
          <div id="job-roots-field" hidden>
            <label for="job-roots">Absolute paths, one per line (empty allows none)</label
            ><textarea id="job-roots" rows="3"></textarea>
          </div>
          <div class="fields">
            ${budgets.map(([key, label]) => html`<label for=${`job-budget-${key}`}>${label}<input id=${`job-budget-${key}`} type="number" min="1" step="1" placeholder="Inherit" /></label>`)}
          </div>
        </details>
        <label
          ><input id="job-enabled" type="checkbox" aria-describedby="job-enabled-hint" /> Enable
          automatic wake-ups</label
        >
        <p id="job-enabled-hint" class="meta">
          Enabling authorizes automatic work within this job’s permissions. New jobs start
          disabled.
        </p>
        <div class="actions">
          <button type="button" @click=${() => $('job-dialog').close()}>Cancel</button
          ><button class="primary">Save job</button>
        </div>
      </form>
    </dialog>
  `;
}

export function createSettings({ topics, selected, refreshRuns, selectTopic }) {
  let jobCursor = null,
    editingJob = null,
    schedulerPaused = false,
    profiles = [];
  const jobs = new Map();
  async function loadTools() {
    const name = $('tool-profile').value;
    const result = await api(`/tools?profile=${encodeURIComponent(name)}`);
    if ($('tool-profile').value !== name) return;
    const profile = profiles.find((p) => p.name === name);
    const values = profile?.values;
    renderTemplate(
      values
        ? html`
            <dl class="summary-list">
              <dt>Model</dt>
              <dd>${values.model_pin || 'Not configured'}</dd>
              <dt>Memory</dt>
              <dd>${values.memory_mode}</dd>
              <dt>Workspace roots</dt>
              <dd>
                ${values.workspace_roots.length ? values.workspace_roots.map((root) => html`<div>${root}</div>`) : 'No file access'}
              </dd>
            </dl>
            <details>
              <summary>Effective configuration &amp; sources</summary>
              ${json(profile)}
            </details>
          `
        : nothing,
      $('profiles')
    );
    renderTemplate(
      result.items.length
        ? result.items.map(
            (tool) => html`
              <details class="catalog-item">
                <summary>${tool.function.name}</summary>
                <p class="meta">${tool.function.description}</p>
                ${json(tool.function.parameters)}
              </details>
            `
          )
        : html`<p class="empty-state">This profile has no available tools.</p>`,
      $('tools')
    );
  }
  async function loadProfiles() {
    profiles = (await api('/profiles')).items;
    for (const id of ['profile', 'tool-profile', 'job-profile', 'prompt-profile']) {
      const value = $(id).value;
      renderTemplate(
        html`${id === 'tool-profile' ? nothing : html`<option value="">Server default</option>`}${profiles.map((p) => html`<option value=${p.name}>${p.name}</option>`)}`,
        $(id)
      );
      if ([...$(id).options].some((option) => option.value === value)) $(id).value = value;
    }
  }
  async function refreshMemory() {
    const status = await api('/memory');
    renderTemplate(
      html`
        <dl class="summary-list">
          <dt>Extraction observer</dt>
          <dd>${status.observer}</dd>
          ${Object.entries(status.commits).map(
            ([state, count]) =>
              html`<dt>${state}</dt>
                <dd>${count}</dd>`
          )}
        </dl>
        ${Object.keys(status.commits).length ? nothing : html`<p class="meta">No memory extractions yet.</p>`}
        <details>
          <summary>Raw status</summary>
          ${json(status)}
        </details>
      `,
      $('memory-status')
    );
    refreshRuns();
  }
  async function refreshHealth() {
    const status = await api('/health');
    renderTemplate(
      html`<dl class="summary-list">
          <dt>Runtime</dt>
          <dd>${status.status}</dd>
          <dt>New work</dt>
          <dd>${status.accepting ? 'Accepting' : 'Not accepting'}</dd>
          <dt>Scheduler</dt>
          <dd>${status.scheduler_failed ? 'Failed — inspect local state' : 'Healthy'}</dd>
          <dt>Memory observer</dt>
          <dd>${status.memory.observer}</dd>
        </dl>
        <details>
          <summary>Raw health</summary>
          ${json(status)}
        </details>`,
      $('health')
    );
    text($('transport-detail'), $('connection').textContent);
  }
  async function loadJobs(more = false) {
    const [result, control] = await Promise.all([
      api(
        `/jobs?limit=50${more && jobCursor ? `&after=${encodeURIComponent(jobCursor)}` : ''}`
      ),
      api('/jobs/control')
    ]);
    if (!more) jobs.clear();
    for (const job of result.items) jobs.set(job.id, job);
    jobCursor = result.next;
    $('more-jobs').hidden = !result.items.length;
    schedulerPaused = control.paused;
    $('jobs-empty').hidden = jobs.size > 0;
    text(
      $('scheduler-toggle'),
      control.paused ? 'Resume all schedules' : 'Pause all schedules'
    );
    text(
      $('scheduler-status'),
      control.paused
        ? 'All schedules and manual runs are paused.'
        : 'Scheduler active · only enabled jobs wake automatically.'
    );
    renderTemplate(
      [...jobs.values()].map(
        (job) => html`
          <sola-job
            .job=${job}
            .schedulerPaused=${schedulerPaused}
            .editJob=${fillJob}
            .refreshJobs=${loadJobs}
          ></sola-job>
        `
      ),
      $('jobs')
    );
  }
  function changeJobRestriction() {
    $('job-tools').hidden = !$('job-restrict-tools').checked;
    $('job-roots-field').hidden = !$('job-restrict-roots').checked;
  }
  function checkedJobTools() {
    return [...$('job-tool-options').querySelectorAll('input:checked')].map((input) => input.value);
  }
  function renderJobTools(checked) {
    const name = $('job-profile').value || (editingJob ? null : profiles[0]?.name);
    const profile = profiles.find((profile) => profile.name === name);
    const mode = topics.get($('job-topic').value)?.mode || 'ephemeral';
    const names = (profile?.values.tools ?? []).filter((tool) => {
      if (tool === 'feeds.items') return mode !== 'shared' || profile.values.memory_mode === 'off';
      if (tool === 'feeds.acknowledge') return mode === 'isolated' && $('job-delivery').value === 'session';
      return true;
    });
    const options = $('job-tool-options');
    renderTemplate(
      names.length
        ? names.map((name) => html`<label><input type="checkbox" value=${name} />${name}</label>`)
        : html`<p class="meta">This profile has no available tools.</p>`,
      options
    );
    for (const input of options.querySelectorAll('input')) {
      input.checked = checked.includes(input.value);
    }
  }
  function changeJobProfile() {
    renderJobTools(checkedJobTools());
  }
  async function fillJob(job = null) {
    // Fetch the selected definition so editing never silently drops fields from an older list.
    if (job) job = await api(`/jobs/${job.id}`);
    // Load every eligible topic, independent of sidebar pagination and archive filtering.
    const targets = new Map();
    let after = null;
    do {
      const page = await api(
        `/sessions?limit=100&archived=false${after ? `&after=${encodeURIComponent(after)}` : ''}`
      );
      for (const topic of page.items)
        if (!topic.archived && !topic.parent_session_id && topic.mode !== 'ephemeral')
          targets.set(topic.id, topic);
      after = page.items.length ? page.next : null;
    } while (after);
    for (const [id, topic] of targets) topics.set(id, topic);
    const unavailable = job?.session_id && !targets.has(job.session_id);
    renderTemplate(
      html`${unavailable ? html`<option value="" disabled>Current topic unavailable. Choose a target.</option>` : nothing}
        <option value="ephemeral">New one-run topic each time</option>
        ${[...targets.values()].map((topic) => html`<option value=${topic.id}>${topic.title || 'Untitled topic'}</option>`)}`,
      $('job-topic')
    );
    editingJob = job;
    $('job-form').reset();
    text($('job-error'), '');
    text($('job-form-title'), job ? 'Edit job' : 'Create a job');
    $('job-task').value = job?.task || '';
    $('job-schedule').value = job?.schedule || '';
    $('job-zone').value = job?.timezone || 'America/Denver';
    const target = job ? job.session_id : selected();
    $('job-topic').value = unavailable ? '' : targets.has(target) ? target : 'ephemeral';
    $('job-origin').value = job?.origin || 'scheduled';
    $('job-origin').disabled = !!job;
    $('job-profile').value = job?.profile || '';
    $('job-profile').required = !!job;
    $('job-profile').querySelector('[value=""]').disabled = !!job;
    $('job-delivery').value = job?.delivery || 'session';
    $('job-enabled').checked = job?.enabled || false;
    $('job-restrict-tools').checked = job?.tools != null;
    renderJobTools(job?.tools ?? []);
    $('job-restrict-roots').checked = job?.workspace_roots != null;
    $('job-roots').value = (job?.workspace_roots || []).join('\n');
    for (const [key] of budgets) $(`job-budget-${key}`).value = job?.budgets[key] ?? '';
    changeJobRestriction();
    $('job-advanced').open = !!(
      job?.tools ||
      job?.workspace_roots ||
      Object.keys(job?.budgets || {}).length
    );
    $('job-dialog').showModal();
  }
  async function switchSettingsTab(id) {
    for (const [key] of tabs) {
      $(`settings-tab-${key}`).setAttribute('aria-selected', String(key === id));
      $(`settings-tab-${key}`).tabIndex = key === id ? 0 : -1;
      $(`settings-panel-${key}`).hidden = key !== id;
    }
    $('settings').querySelector('.settings-content').scrollTop = 0;
    text($('settings-error'), '');
    text($('settings-status'), '');
    const loaders = {
      jobs: loadJobs,
      memory: refreshMemory,
      access: loadTools,
      diagnostics: refreshHealth,
      feeds: () => $('feed-settings').load(),
      security: refreshLogin
    };
    if (loaders[id]) await loaders[id]();
  }
  function settingsKeydown(event) {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key))
      return;
    const buttons = [...event.currentTarget.querySelectorAll('[role=tab]')];
    const current = buttons.indexOf(document.activeElement);
    const next =
      event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? buttons.length - 1
          : (current +
              (['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : -1) +
              buttons.length) %
            buttons.length;
    event.preventDefault();
    buttons[next].focus();
    buttons[next].click();
  }
  async function openSettings() {
    $('settings').showModal();
    $('search-scope').querySelector('[value=current]').disabled = !selected();
    if (!selected()) $('search-scope').value = 'all';
    await switchSettingsTab('jobs');
  }
  async function saveJob(event) {
    event.preventDefault();
    const submit = $('job-form').querySelector('button.primary');
    if (submit.disabled || !$('job-topic').value) return;
    submit.disabled = true;
    text($('job-error'), '');
    try {
      const topic = topics.get($('job-topic').value);
      const lines = (id) =>
        $(id)
          .value.split('\n')
          .map((line) => line.trim())
          .filter(Boolean);
      const body = {
        task: $('job-task').value,
        schedule: $('job-schedule').value,
        timezone: $('job-zone').value,
        session_id: topic?.id || null,
        mode: topic?.mode || 'ephemeral',
        enabled: $('job-enabled').checked,
        delivery: $('job-delivery').value,
        tools: $('job-restrict-tools').checked ? checkedJobTools() : null,
        workspace_roots: $('job-restrict-roots').checked ? lines('job-roots') : null,
        budgets: Object.fromEntries(
          budgets
            .filter(([key]) => $(`job-budget-${key}`).value !== '')
            .map(([key]) => [key, Number($(`job-budget-${key}`).value)])
        )
      };
      if ($('job-profile').value) body.profile = $('job-profile').value;
      if (!editingJob) body.origin = $('job-origin').value;
      await api(editingJob ? `/jobs/${editingJob.id}` : '/jobs', {
        method: editingJob ? 'PATCH' : 'POST',
        body
      });
      $('job-dialog').close();
      text($('settings-status'), 'Job saved.');
      await loadJobs().catch(showError);
    } catch (error) {
      text($('job-error'), error.message);
    } finally {
      submit.disabled = false;
    }
  }
  async function search(event) {
    event.preventDefault();
    const submit = event.currentTarget.querySelector('button');
    submit.disabled = true;
    text($('settings-error'), '');
    text($('search-status'), 'Searching…');
    renderTemplate(nothing, $('search-results'));
    try {
      const params = new URLSearchParams({
        q: $('search-query').value,
        limit: $('search-limit').value
      });
      if ($('search-scope').value === 'current' && selected())
        params.set('session_id', selected());
      const result = await api(`/search?${params}`);
      text(
        $('search-status'),
        result.items.length
          ? `${result.items.length} results`
          : 'No results. Try another phrase or search all topics.'
      );
      renderTemplate(
        result.items.map(
          (item) => html`
            <article class="search-result">
              <p class="meta">
                ${item.kind} ·
                ${topics.get(item.session_id)?.title || item.session_id || 'Retained evidence'}
              </p>
              <p>${item.snippet}</p>
              <div class="actions">
                ${
                  item.session_id
                    ? html`<button
                        @click=${action(async () => {
                          await selectTopic(item.session_id);
                          $('settings').close();
                        })}
                      >
                        Open topic
                      </button>`
                    : nothing
                }
                ${item.run_id ? html`<button @click=${action(() => openEvidence(item.run_id))}>Inspect run evidence</button>` : nothing}
              </div>
              <details>
                <summary>Evidence reference</summary>
                ${json(item.evidence)}
              </details>
            </article>
          `
        ),
        $('search-results')
      );
    } catch (error) {
      text($('search-status'), 'Search failed.');
      text($('settings-error'), error.message);
    } finally {
      submit.disabled = false;
    }
  }
  async function inspectPrompt(event) {
    event.preventDefault();
    const submit = event.currentTarget.querySelector('button');
    submit.disabled = true;
    renderTemplate(nothing, $('prompt-result'));
    text($('settings-error'), '');
    try {
      const result = await api('/prompt', {
        method: 'POST',
        body: {
          ...($('prompt-profile').value ? { profile: $('prompt-profile').value } : {}),
          content: $('prompt-content').value,
          hashes: $('prompt-hashes').checked
        }
      });
      renderTemplate(
        html`<details open>
            <summary>Assembled request</summary>
            ${json(result.request)}
          </details>
          <details>
            <summary>Prompt manifest</summary>
            ${json(result.manifest)}
          </details>
          ${result.hashes ? html`<h3>Content hashes</h3>${json(result.hashes)}` : nothing}`,
        $('prompt-result')
      );
    } catch (error) {
      text($('settings-error'), error.message);
    } finally {
      submit.disabled = false;
    }
  }
  async function reconcileMemory(event) {
    event.preventDefault();
    const submit = event.currentTarget.querySelector('button');
    submit.disabled = true;
    text($('settings-error'), '');
    renderTemplate(nothing, $('memory-result'));
    try {
      const result = await api(`/memory/${$('memory-commit').value}/reconcile`, {
        method: 'POST',
        body: { provider_task_id: $('memory-task').value }
      });
      renderTemplate(
        html`<p role="status">Task verified. Extraction state: ${result.state}.</p>
          ${json(result)}`,
        $('memory-result')
      );
      await refreshMemory();
    } catch (error) {
      text($('settings-error'), error.message);
    } finally {
      submit.disabled = false;
    }
  }
  async function refreshLogin() {
    const login = await api('/auth/session');
    text(
      $('login-status'),
      `Signed in with this browser · expires ${new Date(login.expires_at).toLocaleString()}`
    );
  }
  async function revokeLogins() {
    if (!window.confirm('Sign out every browser and API client, including this one?')) return;
    await api('/auth/sessions', { method: 'DELETE' });
    $('settings').close();
    document.dispatchEvent(new Event('sola-auth-required'));
  }
  async function toggleScheduler() {
    await api('/jobs/control', { method: 'PUT', body: { paused: !schedulerPaused } });
    await loadJobs();
  }
  async function openFeedDiscussion(event) {
    $('settings').close();
    await selectTopic(event.detail);
  }
  return {
    loadTools,
    loadProfiles,
    refreshMemory,
    refreshHealth,
    loadJobs,
    fillJob,
    openSettings,
    saveJob,
    search,
    toggleScheduler,
    switchSettingsTab,
    openFeedDiscussion,
    settingsKeydown,
    inspectPrompt,
    reconcileMemory,
    revokeLogins,
    changeJobRestriction,
    changeJobProfile
  };
}
