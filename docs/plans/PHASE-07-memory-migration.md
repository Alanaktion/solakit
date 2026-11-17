# Phase 7 — Semantic memory and configuration migration

Status: implemented and locally/live verified; production migration remains Phase 13. Depends on [Phase 6](PHASE-06-run-loop.md) and Phase 0 session-mode decisions. Unlocks persistent recall and memory-backed workflows.

## Outcome

OpenViking enriches Sola's context and receives eligible completed interactions without becoming the authority for conversation history. A fast reply does not wait for extraction.

## Implementation slices

1. **Verify and adapt the installed service.** Record installed OpenViking version and live API capabilities using a developer probe. Build an application-lifetime adapter that normalizes recall, commit acceptance, and extraction status. Canonicalize configured roots and enforce scope for reads and writes. Do not copy legacy endpoint assumptions without verification.
2. **Recall at a defined boundary.** Recall once when an eligible run begins, using a documented bounded query derived from eligible conversation and input. Store query, scope, returned source references/content, timing, and recall status as request evidence. Tool output is external data, not identity policy. A trivial-message skip is optional and measurable; memory-disabled/isolated/ephemeral modes make no remote calls under the selected contract.
3. **Separate write operations.** `remember` stages an exact local note with a note/run ID and a receipt saying staged, not remembered. Normal interaction extraction submits the selected eligible batch. Phase 0 deferred a dedicated curated-memory write tool: operator edits supply curated files. Do not expose arbitrary writes into managed OpenViking memory internals.
4. **Track commits durably.** Persist commit ID, payload hash, source IDs, notes, and intent before external submission. Distinguish pending, accepted with provider reference, completed extraction, failed, and ambiguous. An observer polls with persisted next-check/backoff and resumes after restart. Never blindly replay an ambiguous non-idempotent submission; provider-supported idempotency may allow safe retry. A completed extraction does not itself prove the fact is retrievable.
5. **Handle failure and scope.** Proposed default: commit completed shared runs only; retain failed/cancelled staged notes for inspection, not automatic draining. Recall outage can continue with an explicit degraded status unless the profile requires memory. Track recall and commit/extraction separately. Reply success and memory failure can coexist. Define backlog caps and visible stuck/uncertain work rather than indefinite hidden retries.
6. **Migrate configuration once.** Translate copied legacy configuration into strict v2 config, list unsupported keys, preserve source, and generate an effective-authority diff. Do not carry removed Android/channel/shell settings or resource/effect permissions into active grants. Compare roots, tools, fragments, and budgets; require intentional resolution for any broadened permission. No credentials in reports.

## Verification and exit gate

Test scope boundaries, staged-note exactness, commit deduplication, crash before/after acceptance, restart observation, outage/backoff, isolated sessions, and asynchronous reply delivery. Offline prompt inspection must use recorded recall or identify omission.

Run a live test in an isolated test memory root: recall, state a distinctive fact, commit, wait for extraction, then recall from a fresh session without repeating the fact. Record actual retrieval separately from provider completion. Verify no live test fact enters production memory.

Deliver adapter, memory tools/tracker, migration script and authority diff, outage/reconciliation runbook, and live evidence. Exit requires the complete live path; mocks cannot satisfy it.

## Implementation evidence

The 0.5.5 adapter, recorded recall, exact staged notes, durable extraction tracker,
restart observer, copied-config migration/diff and reconciliation scripts are
implemented. Read the [memory contract](../contracts/memory.md),
[runbook](../memory.md), and [Phase 7 verification](../validation/phase-07.md).
The live gate uses account `sola-test`, user `default`, isolated from the default
account. Arbitrary extraction subdirectories are not supported by 0.5.5; the
operator approved the dedicated test account instead. Production config/data and
semantic memory were not migrated.

The version check now accepts stable `>=0.5.5,<0.6.0`, including four-part
hotfixes, instead of requiring exactly 0.5.5. Version 0.5.5 remains the live-tested
baseline. See decision V2-28 and the [version-policy follow-up](../validation/phase-07-version-policy.md).
