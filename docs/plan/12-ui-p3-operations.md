# P3 — operations UI

Status: partial — implementation complete; targeted browser verification remains.

## Audit checklist

- [x] Operations views and job controls implemented; HTTP/action checks pass.
- [x] Operator reports general functionality looks good.
- [ ] Check light/dark and narrow-screen tables, timezone display and run/pause/resume
  feedback in a real browser.

General approval is not recorded as completion of the specific browser matrix.

`/operations` displays existing API job/channel state: enabled, paused/active,
cron/timezone, next fire, last run/outcome, adapter status, connected-since and last
accepted message ID. Missing data remains unknown/empty, never invented health.
Displayed times use the browser timezone; schedule zones remain explicit.

POST controls use existing job run/pause/resume APIs. Run-now waits for the actual
run and opens its evidence page; failures retain actionable API errors. Pause
changes future schedule behavior and does not cancel an active run. Disabled or
active jobs do not offer an enabled Run-now button; server checks stay authoritative.
Channel display is read-only. No config creation/editing or activation is added.

Mock integration checks verify pause/resume, run evidence, disabled-job rejection,
unknown actions/jobs, read-only channel state and authentication. Existing live
memory/integration acceptance gates remain unchanged; no live jobs were enabled.

Still deferred: markdown, per-message profile selection, streaming and Discord
archival exposure. API/UI archival and read-only archive browsing are implemented.
