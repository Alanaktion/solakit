# Phase 13 — Migration rehearsal, cutover, and recovery

Status: planned. Depends on all prior phase gates and operator acceptance. Milestone: Sola's recoverable production home.

## Outcome

Historical evidence, identity, semantic-memory roots, schedules, and pack data survive migration. Exactly one runtime owns production scheduling and effects. Cutover is an explicit operator decision after a reviewable rehearsal.

## Implementation slices

1. **Freeze inventory and migration mapping.** Record source commit plus working-tree/data-copy manifests. Include transcripts and archived segments, session metadata, run/model/tool/prompt artifacts, compaction references, jobs/consumed flags/tombstones, pack receipts/data, credential references, identity fragments, and memory roots. Decide whether legacy login state is invalidated rather than copying incompatible tokens. Preserve unknown legacy fields and raw bytes, with explicit ID mapping for collisions.
2. **Build an idempotent importer and verifier.** Read consistent copies only; never mutate the source. Version the import manifest and checkpoints so rerunning adds no duplicate sessions, runs, jobs, or effects. Compare counts, ordering, identifiers, payload/file hashes, tool-call pairing, evidence references, and operational integrity. Copying a legacy combined app.db is not a v2 schema migration: extract authoritative jobs/state separately and rebuild search solely from JSONL. Carry uncertain effects as uncertain.
3. **Run an isolated candidate.** Use another port, separate writable data, test credentials/resources, and disabled production schedules, memory commits, pushes, sends, prints, and proactive delivery. Read-only production recall needs an explicitly safe scope; use test memory roots for write acceptance. Run model, OpenViking, API/browser, basic-file/image, compaction, and restart scenarios. Preserve deferred pack data without activating its tools. No Android gate exists. Shadow mode must enforce disabled effects in services, not merely instruct the model.
4. **Rehearse backup and rollback.** Restore state.db, evidence, identity/config, and pack artifacts from a checkpoint manifest into a clean directory; rebuild search. Demonstrate schedules remain consumed/deleted correctly and ambiguous receipts cannot replay. Fix numerical acceptance thresholds and soak duration before production enablement. Suggested soak is seven days, extended until it covers a complete schedule cycle, extraction, restart, and restore rehearsal.
5. **Prepare the final checkpoint.** Present rehearsal results, unresolved risks, exact source/destination, planned downtime, enabled resources, and rollback procedure for the operator's cutover decision. Then pause/quiesce the old runtime, settle or mark in-flight effects, take final consistent backup/delta, migrate and verify, start v2 with scheduling paused, switch clients, and enable one scheduler. Record the checkpoint and every subsequent write/effect using evidence cursors and receipts.
6. **Observe and close.** During soak track completion/failure rates, duplicate effects, backlog, proactive repetition, memory progress, search health, storage growth, and backup restoration. Stop enabling further work if recovery or authority guarantees fail. Keep the old installation and backups intact until soak acceptance; cleanup is a separate deliberate action.

## Rollback procedure

Before any new writes/effects: stop the candidate, verify scheduler inactivity, return clients to the preserved old runtime, and enable its scheduler once.

After new writes/effects: stop both schedulers and quiesce both runtimes. Preserve candidate evidence/state and checkpoint deltas. Reconcile new conversations, consumed occurrences, job edits/tombstones, memory commits, and external receipts; mark uncertain outcomes rather than retrying. If reverse import is unsupported, produce an explicit transfer/reconciliation report before reopening the old runtime. Switching ports alone is not safe rollback.

## Verification and exit gate

Deliver migration/verifier scripts, before/after reports, consistent backup manifest, restore runbook, cutover checklist, rollback reconciliation worksheet, and release evidence bundle. Rehearse a failed cutover both before and after synthetic new writes, proving no lost evidence or repeated occurrence/effect.

Exit requires all deterministic/live/operator gates, explicit cutover decision, completed soak coverage, and successful recovery rehearsal. A running candidate or a successful import alone does not complete this phase.
