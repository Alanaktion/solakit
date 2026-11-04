# Phase 8: Persistent scheduling and job operations

Status: planned

Depends on: phase 7

- Integrate a compatible APScheduler version with persistent SQLite job state.
- Resolve channel/session/ephemeral bindings into the existing run queue.
- Implement timezone-aware schedules, bounded task substitution, pause/resume, manual runs, and job status endpoints.
- Record missed firings across downtime. Enforce skip versus one-time catch-up semantics and prevent overlapping executions of a job.
- Preserve task/result messages in the bound session with job origin metadata.

**Exit check:** Controlled-clock restart tests prove misfire behavior. A job run delivered to a channel appears in the next conversational turn’s model context.

## Completion record

Record implementation, validation, and remaining limitations here before marking complete.
