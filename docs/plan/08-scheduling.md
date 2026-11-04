# Phase 8: Persistent scheduling and job operations

Status: local implementation complete — live work gated

Depends on: phase 7

- Integrate a compatible APScheduler version with persistent SQLite job state.
- Resolve channel/session/ephemeral bindings into the existing run queue.
- Implement timezone-aware schedules, bounded task substitution, pause/resume, manual runs, and job status endpoints.
- Record missed firings across downtime. Enforce skip versus one-time catch-up semantics and prevent overlapping executions of a job.
- Preserve task/result messages in the bound session with job origin metadata.

**Exit check:** Controlled-clock restart tests prove misfire behavior. A job run delivered to a channel appears in the next conversational turn’s model context.

## Completion record

Implemented APScheduler 3 wake-ups/CronTrigger, durable JSONL job cursors indexed
in SQLite, channel/session/ephemeral binding into the existing execution queue,
timezone-aware bounded task placeholders, pause/resume/manual/status operations,
missed firing ranges, one-time polling catch-up, and queued/running non-overlap.
Controlled-clock restart tests verify misfires, index rebuild, pause persistence,
interrupted dispatch and cron-to-channel-to-conversation continuity.

The scheduler intentionally restores declarative jobs from TOML plus owned cursor
state rather than pickling runtime callbacks into a library job store. This keeps
state rebuildable, avoids global callback registries, and makes downtime decisions
explicit. Live jobs remain disabled pending live_ready and acceptance gates.

## Revised release gates

Local scheduler tests may use mock adapters after phase 7 implementation. Live
jobs remain disabled until the phase 6 live probe and phase 9 acceptance gates.
Persist scheduling cursors and pause/last-run evidence outside the rebuildable
index. Controlled-clock restart tests must distinguish skip from exactly one
catch-up, and a job must remain non-overlapping while queued as well as running.
