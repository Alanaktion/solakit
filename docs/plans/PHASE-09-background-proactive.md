# Phase 9 — Durable jobs, background work, and proactive actions

Status: implemented; deterministic checks and the live scheduled-run/extraction gate passed.
See the [job contract](../contracts/jobs.md) and [evidence](../validation/phase-09.md).
Operator acceptance and production migration/soak remain later gates.
Depends on [Phases 6–8](README.md). Milestone: Sola continues bounded personal-agent work without an open client.

## Outcome

Scheduled occurrences survive restarts, use the normal run loop, and produce traceable delivery or no-action outcomes. Proactivity has the same capability boundaries as interactive work.

## Implementation slices

1. **Own jobs in state.db.** Store versioned job definitions, enabled/pause/deletion state, schedule/timezone, selected profile, narrowed tools/budgets, session binding, and delivery policy. New definitions default disabled unless explicitly enabled. Validate schedule, root/profile references, and budgets in application services. Deletion tombstones prevent stale imports resurrecting jobs.
2. **Use APScheduler as a wake mechanism.** Reconcile definitions at startup and wake to evaluate due occurrences. Do not use a second scheduler database as authority. Compute occurrence identity from job generation and scheduled UTC instant; persist unique claim plus queued run in one state transaction with evidence outbox. Include definition snapshot/version. Dispatch only after durable admission. Distinguish scheduled/manual occurrence IDs.
3. **Freeze temporal behavior.** Persist IANA timezone and scheduled instant. Document DST policy: proposed cron skips nonexistent times and runs each repeated local slot once, with exact selected offset recorded; test the chosen APScheduler integration rather than assuming it behaves this way. Define missed-run/backlog caps, overlap policy, and pause/resume behavior. Proposed default: no overlap, bounded skip/coalesce for cron, one overdue unattempted one-shot. A consumed one-shot needs explicit new future schedule/rearm; task edits and restart cannot rearm it.
4. **Separate execution from delivery.** Track occurrence claimed, run admitted, run terminal, and delivery pending/accepted/completed/uncertain. Use provider idempotency keys where supported. A restart may resume untouched queued work but cannot redispatch an interrupted running occurrence or blindly repeat an uncertain send. Retain evidence for operator reconciliation.
5. **Add proactive wake-ups.** Operator-enabled wakes inspect bounded context/commitments and either do nothing, initiate permitted work, or leave one attributed message. Persist wake reason and decision, including no-action. Enforce quiet hours at delivery/effect dispatch, notification deduplication/cooldown, daily/run budgets, maximum consecutive autonomous runs, and the no-autonomous-successor rule in code. Descendants inherit autonomous lineage; writing another schedule cannot reset the limit. Phase 0 forbids autonomous creation/rearming/enabling/rescheduling of wake-ups, regardless of ordinary job grants. Use the confirmed quiet-hour/cooldown settings and initial durable wake budgets in the [product contract](../product.md).
6. **Make operations controllable.** Expose list/create/update/pause/resume/run-now/delete through shared services and narrowly granted tools. Revalidate profile restrictions at execution. Bound active/pending wakes, support a global pause, and preserve interactive fairness. Local final messages use stable IDs rather than appending copies on every refresh.

## Verification and exit gate

Use a fake clock for DST, timezone changes, overdue one-shots, downtime, overlapping ticks, concurrent manual/scheduled claims, definition edits, and tombstones. Inject crashes around claim/admission/effect/receipt. Assert one logical run per occurrence and no automatic replay of ambiguous effects.

Test quiet-hour boundaries, delayed notifications, repeated no-op wakes, deduplication, self-scheduling attempts, lineage limits, disabled grants, and interactive starvation. Restart with queued work and a consumed one-shot, then rebuild search: neither may repeat.

Deliver scheduler/occurrence ledger, proactive policy, delivery tracker, service APIs, and recovery evidence. Exit requires a real scheduled run reaching its intended session and memory extraction trace. Universal exactly-once external effects are not promised.
