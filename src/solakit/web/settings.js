import { html, render as renderTemplate } from './lit-core.min.js';
import { api } from './client.js';
import { $, text, showError } from './ui.js';
import './job.js';

export function createSettings({ topics, selected, refreshRuns }) {
  let jobCursor = null,
    editingJob = null,
    schedulerPaused = false;
  async function loadTools() {
    text(
      $('tools'),
      JSON.stringify(
        await api(`/tools?profile=${encodeURIComponent($('tool-profile').value)}`),
        null,
        2
      )
    );
  }
  async function loadProfiles() {
    const result = await api('/profiles');
    for (const id of ['profile', 'tool-profile']) {
      const value = $(id).value;
      renderTemplate(
        result.items.map((p) => html`<option value=${p.name}>${p.name}</option>`),
        $(id)
      );
      if ([...$(id).options].some((option) => option.value === value)) $(id).value = value;
    }
    renderTemplate(
      result.items.map(
        (p) => html`
          <details>
            <summary>${p.name}</summary>
            <pre tabindex="0">${JSON.stringify(p, null, 2)}</pre>
          </details>
        `
      ),
      $('profiles')
    );
    await loadTools();
  }
  async function refreshMemory() {
    text($('memory-status'), JSON.stringify(await api('/memory'), null, 2));
    refreshRuns();
  }
  async function refreshHealth() {
    text($('health'), JSON.stringify(await api('/health'), null, 2));
    text($('transport-detail'), $('connection').textContent);
  }
  const jobs = new Map();
  async function loadJobs(more = false) {
    const [result, control] = await Promise.all([
      api(`/jobs?limit=50${more && jobCursor ? `&after=${encodeURIComponent(jobCursor)}` : ''}`),
      api('/jobs/control')
    ]);
    if (!more) jobs.clear();
    for (const job of result.items) jobs.set(job.id, job);
    jobCursor = result.next;
    $('more-jobs').hidden = result.items.length < 50;
    schedulerPaused = control.paused;
    $('jobs-empty').hidden = jobs.size > 0;
    text($('scheduler-toggle'), control.paused ? 'Resume all schedules' : 'Pause all schedules');
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

  function fillJob(job = null) {
    editingJob = job;
    text($('job-form-title'), job ? 'Edit job' : 'Create a job');
    $('job-task').value = job?.task || '';
    $('job-schedule').value = job?.schedule || '';
    $('job-zone').value = job?.timezone || 'America/Denver';
    $('job-topic').value = job?.session_id || selected() || '';
    $('job-origin').value = job?.origin || 'scheduled';
    $('job-origin').disabled = !!job;
    $('job-enabled').checked = job?.enabled || false;
  }
  async function openSettings() {
    $('settings').showModal();
    text($('settings-error'), '');
    text($('settings-status'), '');
    renderTemplate(
      [...topics.values()]
        .filter((topic) => topic.mode !== 'ephemeral' && !topic.archived)
        .map(
          (topic) => html`<option value=${topic.id}>${topic.title || 'Untitled topic'}</option>`
        ),
      $('job-topic')
    );
    $('job-topic-hint').hidden = $('job-topic').options.length > 0;
    $('job-form').querySelector('button.primary').disabled = !$('job-topic').options.length;
    fillJob();
    await Promise.all([loadJobs(), refreshMemory(), refreshHealth()]);
  }
  async function saveJob(event) {
    event.preventDefault();
    try {
      const topic = topics.get($('job-topic').value);
      const body = {
        task: $('job-task').value,
        schedule: $('job-schedule').value,
        timezone: $('job-zone').value,
        session_id: topic.id,
        mode: topic.mode,
        enabled: $('job-enabled').checked
      };
      if (!editingJob) {
        body.origin = $('job-origin').value;
        if ($('profile').value) body.profile = $('profile').value;
      }
      await api(editingJob ? `/jobs/${editingJob.id}` : '/jobs', {
        method: editingJob ? 'PATCH' : 'POST',
        body
      });
      fillJob();
      await loadJobs();
    } catch (error) {
      showError(error);
    }
  }

  async function search(event) {
    event.preventDefault();
    try {
      const result = await api(
        `/search?q=${encodeURIComponent($('search-query').value)}${$('search-topic').checked && selected() ? `&session_id=${selected()}` : ''}`
      );
      renderTemplate(
        html`<pre tabindex="0">${JSON.stringify(result.items, null, 2)}</pre>`,
        $('search-results')
      );
    } catch (error) {
      showError(error);
    }
  }

  async function toggleScheduler() {
    await api('/jobs/control', { method: 'PUT', body: { paused: !schedulerPaused } });
    await loadJobs();
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
    toggleScheduler
  };
}
