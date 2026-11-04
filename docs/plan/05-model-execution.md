# Phase 5: Model client and queued agent execution

Status: complete

Depends on: phase 4

- Implement the async oMLX client with exact pinned-model verification and no fallback.
- Add FIFO execution per session and a global inference semaphore.
- Implement bounded tool-call loops, assistant-before-tool ordering, argument repair limits, token usage, and finalization.
- Add queued/running cancellation, explicit failure statuses, and clean shutdown.
- Connect authenticated API messages to the same session/run path used by future channels and jobs.
- Decide how to retire or adapt the scaffold’s `/v1/runs` route; avoid maintaining a second execution path.

**Exit check:** Mock endpoint tests prove exact request context, tool ordering, gating, budgets, cancellation, and concurrency. Unavailable or incorrect models produce run records without adding session messages.

## Completion record

Implemented async pinned oMLX requests, FIFO session workers, global inference
semaphore, bounded tool loop, repair budgets, token accounting, persistent API
execution, queued/running cancellation and clean shutdown. Removed both scaffold
execution routes; all messages use /api/sessions/{id}/messages.
First-response model failures leave transcripts untouched. Incomplete tool groups
are retained as evidence and explicitly counted when excluded from model context.
A process ownership lock prevents concurrent recovery of active runs.
Validated with 23 passing tests, including mock exact-context, ordering, model
identity/down cases, capabilities, successful FIFO, concurrency, cancellation,
shutdown and recovery; Ruff lint and format pass. Live oMLX validation remains
part of phase 9. Memory/compaction and delivery remain later phases.
