# Scheduled job management

Use **Operations → New job** to create a recurring task. The form includes cron,
timezone, execution profile, task, session/channel binding, delivery and polling
options. Edit or delete a job from its row; deletion asks for confirmation and
keeps past runs and conversations. New jobs default to disabled. Enable one when
ready, or pause/resume future scheduled firings without changing its enabled flag.
Active jobs must finish before editing/deletion. Run now requires an enabled job
and works even while paused; it does not move the scheduled next fire.

Job definitions live in `app.db`'s authoritative `job_definitions` table. No
`[[jobs]]` block or server restart is needed. Legacy TOML jobs import once per ID:
later file edits do not replace managed definitions, and deleted IDs retain a
tombstone so stale TOML cannot resurrect them. Remove imported TOML job blocks
when convenient; keep profiles, channels and tool/capability grants in config.
An explicit new creation may reuse a deleted ID. Its old run evidence stays intact.

## Operator API

All routes use the existing Cookie or Bearer operator authentication.

| Route | Result |
| --- | --- |
| `GET /api/jobs` | Full definitions plus next fire, pause/active and last-run state |
| `POST /api/jobs` | Create a job; HTTP 201, duplicate ID 409 |
| `GET /api/jobs/{id}` | Inspect one definition and its state |
| `PATCH /api/jobs/{id}` | Partial edit; ID is immutable, `deliver: null` clears delivery |
| `DELETE /api/jobs/{id}` | Stop future firings; retain run evidence |
| `POST /api/jobs/{id}/run` | Run once and wait for its run record |
| `POST /api/jobs/{id}/run?wait=false` | Queue once and return HTTP 202 with accepted job state |
| `POST /api/jobs/{id}/pause` or `/resume` | Control future firings |
| `GET /api/job-options` | Configured default timezone, schedulable profiles and channels |

Example create body:

```json
{
  "id": "evening-reflection",
  "cron": "0 19 * * *",
  "timezone": "America/Denver",
  "profile": "interactive",
  "task": "Reflect on {date}.",
  "bind_session": "web-main",
  "enabled": false
}
```

Tasks accept `{date}`, `{time}`, `{timezone}` and `{job_id}`; double literal
braces as `{{` / `}}`. The cron has five fields; use weekday names to avoid
numbering ambiguity. Times use the job's IANA timezone. `bind_session` can name a
configured channel, a session ID (created if absent), or `ephemeral` for a fresh
session without memory. Named channel bindings take precedence over session IDs.
Optional `deliver` uses `{ "to": "channel-name", "mode": "final" }`, `artifact`
or `silent`. Without explicit delivery, non-polling jobs bound to channels deliver
there. `catch_up` requires `polling: true` and must be used for idempotent work.

Definitions validate cron, timezone, placeholders, profiles, binding IDs and
delivery channels. Maintenance profiles cannot be scheduled. New jobs remain
disabled unless requested otherwise. Edits take effect immediately; changing
cron/timezone or enabled state resets the next firing to a future time.
Non-overlap, downtime skip/one-time polling catch-up, cancellation and uncertain
delivery safeguards remain in effect. Busy edits/deletions/triggers return 409;
missing jobs return 404; invalid definitions return 422. Already accepted work
finishes publication even if its requesting connection is cancelled.

## Chat tools

Load the built-in `jobs` module through `enabled_tools`. Grant the desired names
(`jobs.list`, `jobs.create`, `jobs.update`, `jobs.run`, `jobs.pause`, `jobs.delete`)
or use the profile wildcard. Write tools additionally need `jobs.manage` capability
for create/update/pause/delete and `jobs.run` for manual triggering.

Creation defaults to the current chat/profile, configured timezone and disabled
state. `jobs.update(id, changes)` takes a partial field object; use `enabled: true`
to activate a job and `deliver: null` to clear delivery. `jobs.pause(id, paused=false)`
resumes. `jobs.run(id)` queues without waiting, so a current-session job can run
after the calling chat turn instead of deadlocking. Its acknowledgement is job
state, not a newly completed run or a promised new run ID. Run evidence/status
become available through normal operations once dispatch progresses.

Without `jobs.manage.all`, chat tools list/manage/trigger only jobs for their own
profile and session with no external delivery. This extra capability allows
other profiles, sessions, channel bindings, ephemeral sessions and delivery
destinations; it does not replace `jobs.manage`/`jobs.run` or tool grants. Scope is
checked again under the scheduler's mutation lock. Result text is marked as
external data and masks resolved credentials. `runtime.status` reports schedule
state without exposing tasks from other conversations.

## Persistence and backups

Back up **`app.db` as well as the evidence tree**. It is no longer wholly
disposable: conversation/FTS indexes are rebuildable, but managed job definitions
and deletion tombstones exist only in SQLite. Use SQLite's backup facility while
running, or copy the database with the server stopped. `Store.rebuild()` refreshes
only derived tables and preserves `job_definitions`; do not delete `app.db` to
repair an index. Pause/next-fire/active/last-run cursors retain their append-only
`jobs/<id>.jsonl` evidence. Interrupted dispatches remain visible gaps and are not
silently replayed. Keep referenced profiles and channels available across restarts.

Tests and browser checks use isolated scratch data and mocked services. They do
not activate real channels/jobs or complete the live rollout checks in
[operator acceptance](acceptance.md).
