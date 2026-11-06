# Scheduled job tool policies

Implemented the requested narrowing-only override: Job.tools=None inherits,
a list intersects the profile's effective tool grants, and [] exposes none.
Profiles retain all capability/scope checks, execution budgets and prompt behavior.
There is no job-specific profile clone or capability override.

## Evidence and lifecycle

The generic execution seam captures a per-run name ceiling at durable admission,
re-intersects it when work starts, filters every schema request, and constrains
registry dispatch. A queued run cannot gain newly granted names. Scheduler cron,
one-time, manual, API, UI and chat paths share these rules. Explicit job tool names
are validated on create/update and new TOML import, including profile changes.
Persisted profile drift is allowed to restart safely: exposure shrinks, while
Operations/API inspection names the unavailable selections. Job updates do not
rearm consumed one-time jobs or modify admitted runs.

Runs retain effective_tools, tool_schema_sha256 and a private, immutable
*.tool-schemas.json input snapshot. Historical /api/prompt?run_id uses that file
and the stored prompt rather than current modules/configuration. Missing legacy
snapshots return 409. Schema hashes are verified on full run reads; schema inputs
preserve property names such as api_key. Presentation masks resolved credential
values before encoding the schema display, preserving schema structure.

## Validation

- 480 core and pack tests passed, including 17 new job-tool regressions.
- Explicit no-tool jobs, profile wildcard expansion, forged excluded calls,
  capability and filesystem-scope denial, queued grant changes, cron/one-time
  dispatch, profile-change save rejection, persisted drift/restart, API null/empty
  round-trips, old-form omission compatibility, and historical/schema evidence
  were exercised using isolated fixtures. Snapshot integrity/immutability and
  schema presentation redaction are also checked.
- Chromium: inherit/selected/none, profile-change errors, drift warnings, narrowed
  runs and historical inspection passed in both themes at 320/390/768/1440px.
- Existing job-management browser regression passed in both themes at desktop/mobile.
- Ruff, frozen workspace sync and core/all-pack builds passed. Packaged UI assets
  checked against source. No live model, external delivery, schedules or data used.
