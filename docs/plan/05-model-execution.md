# Phase 5: Model client and queued agent execution

Status: planned

Depends on: phase 4

- Implement the async oMLX client with exact pinned-model verification and no fallback.
- Add FIFO execution per session and a global inference semaphore.
- Implement bounded tool-call loops, assistant-before-tool ordering, argument repair limits, token usage, and finalization.
- Add queued/running cancellation, explicit failure statuses, and clean shutdown.
- Connect authenticated API messages to the same session/run path used by future channels and jobs.
- Decide how to retire or adapt the scaffold’s `/v1/runs` route; avoid maintaining a second execution path.

**Exit check:** Mock endpoint tests prove exact request context, tool ordering, gating, budgets, cancellation, and concurrency. Unavailable or incorrect models produce run records without adding session messages.

## Completion record

Record implementation, validation, and remaining limitations here before marking complete.
