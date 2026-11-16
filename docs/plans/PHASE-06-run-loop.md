# Phase 6 — Run loop and tool reliability

Status: implemented; deterministic and eager live pinned-model reliability gates passed.
Deferred disclosure failed its separate paired gate and remains off.
See [execution contract](../contracts/run-loop.md) and [evidence](../validation/phase-06.md). Depends on [Phases 1–5](README.md). Milestone: a reliable headless harness, before live memory or UI.

## Outcome

Sola performs bounded multistep work through one application entry point. Successful completion, failure, cancellation, and uncertainty are visible and durable.

## Implementation slices

1. **Implement durable admission and queues.** Accept session/profile/input and request-idempotency key. Atomically reserve a run and session queue position through Phase 3's protocol. Same key/same input returns the same receipt; same key/different input conflicts. Preserve per-session FIFO and one context mutation owner. On restart, resume untouched queued admissions after revalidation; mark previously running work interrupted, without replaying external calls. Terminal outcomes are immutable.
2. **Implement the model/tool cycle.** Assemble the recorded request, acquire the single inference semaphore, obtain a normalized response, release the permit, validate complete calls, execute authorized tools, append receipts/results, and continue. Start with sequential tool execution for predictable evidence/effects. Check budgets before each model call and tool. No business logic depends on FastAPI or request connections.
3. **Make concurrency bounded.** Keep session ownership while a run is active, but hold the inference permit only around model use. Choose a documented fair arbitration policy for interactive/background callers with bounded pending admissions and measurable starvation checks. Compaction uses the same permit; it cannot secretly double inference concurrency. No network/tool await may retain the permit unnecessarily.
4. **Implement cancellation and recovery.** Queued cancellation removes eligibility; active cancellation signals model/tool tasks and records the outcome plus uncertain effect receipts. Use structured task ownership and finally blocks for permits, locks, and subscriptions. Do not interpret HTTP disconnect as cancellation. Repeated ephemeral sessions must release workers, locks, and context references. Waiting on a job queued into the current session must not deadlock; return admission instead.
5. **Add bounded reliability recovery.** Independently cap argument repair, schema disclosure, narration continuation, and total steps/time. Preserve malformed arguments and original replies. Narration detection distinguishes actual unattempted requested work from quotes, hypothetical discussion, and ordinary conversation. An identifiable authorized tool may be disclosed and receive one continuation opportunity. Unresolved action narration ends failed with evidence; no invented tool execution or success.
6. **Evaluate disclosure; keep delegation deferred.** Eager schemas are default. Compare deferred mode on Phase 1 paired cases and predeclared gates. Phase 0 deferred delegation: add no child-agent executor or delegation framework for v1.

## Verification and exit gate

Fault-test admission acknowledgement, cancellation before/after tool intent, stream interruption, receipt persistence failure, shutdown/restart, same-session concurrency, semaphore leaks, and fairness under sustained load. A completed model response with uncertain external effects must not become an unconditional success claim.

Run the real-model corpus and report success rate alongside silent failure and false continuation rates. Require no known narration case silently counted as success, no incorrect/duplicate test effects, and agreed task-completion thresholds. Keep deferred mode off unless its gate passes. An increased visible-failure rate alone is not a reliability improvement.

Deliver headless runtime, execution/state diagram, receipts, fault report, and eager/deferred evaluation. Exit requires both deterministic concurrency/recovery tests and live pinned-model evidence.
