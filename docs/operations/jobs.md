# Scheduled job management

Use **Operations → New job** to create a recurring or one-time task. Choose
**Recurring** for cron or **One time** for a date and time. The form includes
timezone, execution profile, task, enabled tools, session/channel binding, delivery and polling
options. Edit or delete a job from its row; deletion asks for confirmation and
keeps past runs and conversations. New jobs default to disabled. Enable one when
ready, or pause/resume future scheduled firings without changing its enabled flag.
Active jobs must finish before editing/deletion. Run now requires an enabled job
and works even while paused. A recurring job keeps its next scheduled fire;
for one-time jobs, running early consumes the scheduled attempt.

Job definitions live in `app.db`'s authoritative `job_definitions` table. No
`[[jobs]]` block or server restart is needed. Legacy TOML jobs import once per ID:
later file edits do not replace managed definitions, and deleted IDs retain a
tombstone so stale TOML cannot resurrect them. Remove imported TOML job blocks
when convenient; keep profiles, channels and tool/capability grants in config.
An explicit new creation may reuse a deleted ID. Its old run evidence stays intact.

## Tools for a job

Choose **Inherit profile tools**, **Choose tools**, or **No tools** in the job
form. Selected names must already be granted by the chosen profile. Changing
the profile revalidates the selection; an invalid selection remains visible so
you can remove it. No tools is valid for a pure announcement, and configured
scheduler delivery still works without a model-callable delivery tool.

The API field `tools` has three states:

| Value | Exposure |
| --- | --- |
| omitted or `null` | Inherit the profile's current effective tools |
| `["runtime.status"]` | Only those listed names that the profile grants |
| `[]` | No tools; model requests use `tool_choice: none` |

For example, `PATCH /api/jobs/{id}` with `{"tools": ["runtime.status"]}` narrows
an already granted tool set. `{"tools": null}` restores inheritance. Names outside
the profile's grants are rejected at save time; loading a module alone is not a
grant, and `"*"` is not a job tool name. Capabilities, filesystem scopes, budgets,
timeouts and prompt fragments remain profile-owned. A job list cannot grant
shell, writing or other authority its profile lacks. Both advertised schemas and
dispatch enforce the job restriction, including when a model guesses an excluded
name. A changed profile can shrink future runs; Operations shows excluded names
and job inspection returns `unavailable_tools` until you repair the selection.

The run's `effective_tools` records its admission ceiling, refreshed against grants
when execution starts. Run evidence links **Inspect recorded prompt and tool
schemas**. This uses saved input evidence, so later job/profile/module edits do
not alter what an earlier run was given. `tool_schema_sha256` verifies the private
`runs/*.tool-schemas.json` file; include these files in evidence backups. Older
runs without a snapshot are marked not recorded and cannot reproduce tool schemas.
Legacy TOML jobs omit `tools` to inherit, or use an explicit array such as
`tools = []`. New imports validate grants; existing imported definitions are
preserved across profile trims. API and chat create/update commands use the same
validation, including profile changes. Old UI edit submissions that omit the new
field preserve the existing policy.

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
| `POST /api/jobs/{id}/run?admission=true` | Return HTTP 202 with the durable run receipt before generation; used by UI Run now |
| `POST /api/jobs/{id}/pause` or `/resume` | Control future firings |
| `GET /api/job-options` | Configured default timezone, schedulable profiles, channels and tool choices |

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

For a one-time task, replace `cron` with `run_at`:

```json
{
  "id": "check-on-request",
  "run_at": "2030-01-02T09:00:00-07:00",
  "timezone": "America/Denver",
  "profile": "interactive",
  "task": "Check on the earlier request in this conversation.",
  "bind_session": "web-main",
  "enabled": true
}
```

Supply exactly one schedule field. `run_at` is an ISO date/time, normalized to
UTC in responses. Without an offset it is interpreted in `timezone`; nonexistent
or ambiguous daylight-saving times require an explicit offset. Creation or a
changed one-time date must be in the future. To change schedule type with PATCH,
clear the old field with `null` and set the other field.

One-time jobs have durable `consumed` state. Admission, including an early manual
run, records this before dispatch and clears next fire. Completion, failure,
interruption, restart and index rebuild do not rearm it. An overdue job that was
never attempted runs once when the app returns (or when resumed/enabled); no
polling or `catch_up` flag is needed. An interrupted/uncertain attempt is recorded
and is not automatically replayed. Run again returns 409 after consumption.
To schedule another attempt, set a **new future `run_at`**. Task edits, changing
timezone without moving the instant, and enable/pause toggles do not repeat it.

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
cron/timezone or enabled state resets a recurring job's next firing. One-time
attempt protection persists until an explicit date change.
Non-overlap, downtime skip/one-time polling catch-up, cancellation and uncertain
delivery safeguards remain in effect. Busy edits/deletions/triggers return 409;
missing jobs return 404; invalid definitions return 422. Already accepted work
finishes publication even if its requesting connection is cancelled.


Set `sub_session = true` (API/chat: `"sub_session": true`) or choose **Run in a
fresh sub-session** in the job form to run each firing in a new child of the
bound session. The child inherits its memory scope, enabling normal shared
memory recall, tools and commits, without copying parent conversation history.
Task, tool calls and intermediate replies remain in the child's evidence.
Existing final/artifact/silent delivery settings still apply; automatic delivery
sends only the final reply for non-polling channel bindings. Session-ID bindings
do not implicitly deliver to a channel. The API channel exposes the stored child
run; it does not append a copy into the parent transcript. Ephemeral bindings
cannot use this option. The default is false, preserving existing inline jobs.

## Chat tools

Load the built-in `jobs` module through `enabled_tools`. Grant the desired names
(`jobs.list`, `jobs.create`, `jobs.update`, `jobs.run`, `jobs.pause`, `jobs.delete`)
or use the profile wildcard. Write tools additionally need `jobs.manage` capability
for create/update/pause/delete and `jobs.run` for manual triggering.

Creation defaults to the current chat/profile, configured timezone and disabled
state. `jobs.create` accepts either `cron` or `run_at`. For a request such as
“check on this tomorrow morning”, the model resolves tomorrow in the current
run's timezone, uses 09:00 if no specific morning time was given, and passes
`run_at` with `enabled: true`. It uses the current conversation so the follow-up
retains the original context. The model should confirm the actual local date/time.
`jobs.update(id, changes)` takes a partial field object; use `enabled: true`
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


Jobs can override schema disclosure with `tool_disclosure` and `tool_resident`,
without changing profile authority. Null residents inherit; an empty list starts
with discovery only in deferred mode. See [tool disclosure](tool-disclosure.md)
for validation, examples and run inspection.
