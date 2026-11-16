# Phase 3 — Durable evidence, sessions, and offline search

Status: planned. Depends on [Phase 2](PHASE-02-records-configuration.md). Unlocks evidence-backed model requests, runs, memory tracking, and migration.

## Outcome and authorities

JSONL preserves conversation/execution evidence. `state.db` owns session routing/metadata, admission, jobs/occurrences, delivery, memory tracking, and operational coordination. `search.db` owns nothing: deleting it must leave every authoritative record untouched. It is rebuilt solely from JSONL, not a live model or OpenViking.

## Implementation slices

1. **Define layout and durability.** Use private data directories for transcripts, run/request artifacts, recovery records, and the two databases. Version schemas and migrations. Specify append locking, complete-line framing, fsync before durable acknowledgement, atomic sidecar publication, and directory sync where needed. Never rewrite an existing source line. Historical raw imports remain byte-preserved alongside normalized v2 views.
2. **Implement the cross-store protocol.** SQLite and JSONL have no shared transaction. Proposed protocol: commit a state mutation plus uniquely identified evidence-outbox entry in one SQLite transaction; append/fsync that evidence; mark the outbox delivered. Acknowledge admission only after its required evidence is durable. A crash after append but before marking delivered is reconciled by stable evidence ID and verified payload, not another logical event. Same ID with different content is corruption. Consequential effects cannot begin before durable intent. Publish the operation-by-operation protocol before coding callers.
3. **Recover deliberately.** Drain/reconcile pending outbox entries before dispatch. Compare durable execution facts with nonterminal operational rows; interrupted or uncertain work never becomes successful just because state is missing. Preserve torn bytes and recovery metadata; seal a damaged segment and continue in a new one rather than truncating evidence. Interior corruption blocks affected reads/execution and remains visible. State backup remains required: the evidence tree is not a substitute for lost schedules or credentials/login metadata.
4. **Build session services.** Create, list, title, archive/restore, and paginate sessions/messages with stable cursors. Archive is metadata, compaction is context selection. Use indexed run/message reads and bounded pages, with validated scan fallback where appropriate. Avoid full-history scans on every reply or delivery.
5. **Build independent FTS projection.** Store source fingerprints, byte offsets, record IDs, projection version, and checkpoints. Incremental indexing follows durable append; index failure marks search degraded without negating a successful write. Rebuild into a temporary database and atomically replace after validation. Provide a developer rebuild script and offline `sola search` with escaped/bound queries, snippets, evidence references, and explicit unavailable/corrupt status.
6. **Back up and rehearse import.** Use SQLite backup facilities or quiesced copies, plus evidence high-water marks and checksummed manifests. Keep state and evidence from a coherent checkpoint; do not copy only a live main DB while ignoring WAL state. Import small copied legacy fixtures idempotently, including archives, tool calls, unknown fields, and duplicate source names.

## Verification and exit gate

Fault-inject at state commit, append, fsync, outbox completion, and acknowledgement. Every acknowledged record survives restart; each logical event appears once to readers. An unacknowledged admission may exist and must be discoverable by idempotency key.

Test trailing/interior corruption, invalid path IDs, disk-full/index failure, stale offsets, Unicode, archive pagination, and deterministic index rebuild. Delete `search.db`, rebuild, and compare search results while asserting unchanged state.db jobs and tombstones. Run search with server/model/memory offline.

Deliver the store, session/search services, authority/reconciliation specification, rebuild script, backup/restore runbook, and fixture import report. Exit requires a restored backup recovering operational state and evidence, not merely matching message counts.
