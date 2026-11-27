# Evidence, operational state, and search

One cooperating writer owns the data directory under a local filesystem lock. Store
methods are synchronous, bounded local operations called on the owning event
loop; there is no extra worker or independent SQLite writer.

## Layout and framing

`state.db` (SQLite schema/user_version 2, WAL, synchronous FULL) owns operational
records, admission keys, per-session sequences and the evidence outbox.
`evidence/00000001.jsonl` and subsequent numbered segments contain versioned
entries with stable IDs, a typed record, optional per-session durable event, and
operation details. UTF-8 JSON plus one newline frames each entry. No source line
is rewritten. An entry's canonical JSON SHA-256 identifies its payload. Evidence
files and new directories are private (0600/0700). The persistent owner lock also
serializes append, recovery, rebuild and backup. Existing permissions are retained.
`artifacts/` contains atomically published immutable request/run sidecars;
`recovery/` records sealed segment tails and reasons. `imports/` preserves copied
legacy files byte for byte. `search.db` is disposable and contains no authority.

## Operation protocol

| Operation | State transaction | Required durable evidence / acknowledgement |
| --- | --- | --- |
| Create/title/archive/restore session | Latest metadata + outbox | Session snapshot; return after append/fsync and delivered mark |
| Admit run | Unique request key + queued run + frozen admission details + input message + allocated sequences + outbox | Run admission and input entries drained together before returning; retry same key/payload returns same run, different payload fails |
| Start/terminal run | Validate prior lifecycle + latest snapshot + sequence + outbox | Started/terminal event durable before return; immutable terminal state |
| Message/model/tool/receipt/compaction | Check parent identities + insert immutable record + sequence + outbox | Record/event durable before return; tool intent return precedes any future effect |
| Delivery/memory transition | Validate lifecycle + latest state + sequence + outbox | New snapshot/event durable before return; no success inferred from missing state |
| Letter publication | Reserve request or reply target + latest phase + outbox | Prepared allocation precedes file creation. Confirmed result follows verified push or operator reconciliation. Global ledger entries retain the original tool intent ID across session archival. |
| Search projection | No authoritative mutation | Best effort after append; failure marks search degraded and does not undo a successful write |
| Backup | Writer held; drain outbox; SQLite backup API | Evidence high-water sizes + hashes + state backup + atomic manifest; restore verifies all files before publication |
| Legacy fixture import | Source digest ledger and deterministic import identities | Preserve raw source first; retain unknown fields in normalized legacy entries; retry is idempotent |

Each mutation first drains older outbox work. Commit state and exact serialized
outbox bytes in one transaction, append and fsync, then mark delivered. No caller
may dispatch from a committed-but-undelivered row. Admission acceptance is the
return from `admit`, not the SQLite commit. An unacknowledged request is discoverable
by its request ID after recovery. The admission key covers session, profile,
origin, lineage, input and frozen admission details, excluding generated IDs/time.
Scheduled and proactive admissions require an atomic occurrence claim. [Job contract](jobs.md)

On restart validate active evidence, reconcile stable IDs and exact payloads, then drain
the outbox before allowing new mutations. Append-before-delivered reuses the
existing entry after an fsync; it never emits a second logical event. A duplicate
ID with different content, invalid complete line, sequence gap, stale offset, or
state/evidence mismatch is corruption and blocks the store. Missing operational
state is never silently reconstructed from evidence. A running run becomes
interrupted; sending delivery becomes uncertain; running memory work becomes
failed. Queued runs remain eligible for revalidation. Unreceipted tool intents
remain visible and must not be automatically retried.

Segments mix sessions. For delivered execution and legacy entries belonging to
archived sessions, startup reuses references and event sequences from the SQLite
outbox, seeks past their payloads, and omits their historical snapshot/admission
reconciliation. Session metadata, global records and undelivered entries still
receive full validation. Segment layout, cached offset boundaries and sealed tails
remain checked, and whole-segment hashes are still computed for search and backup.
Unfinished runs, deliveries, memory work and prepared compactions recover even in
archived sessions; completed archived compactions skip startup revalidation.
Archived payloads are verified when read, restored, or scanned for an offline
search rebuild. Restore always reconciles all sessions. Restoring a topic makes
its history eligible for full startup validation again.

Bulk archive uses one state/outbox transaction for up to 100 requested session
IDs, then drains and indexes the batch once. Missing IDs reject the entire batch;
duplicates and already archived sessions make no additional evidence. As with
other mutations, interrupted evidence publication finishes during recovery.

A final incomplete line is preserved in place. Publish a recovery sidecar with
segment, byte offset, tail hash, size and reason, seal that segment and append to
a new segment. A sealed tail must still match its sidecar on every read. Interior
corruption or a changed sealed tail blocks reads. No truncation or silent repair.

## Reads and projection

Session listing uses `(created_at,id)` exclusive-after cursors; message and run
reads use indexed session sequence numbers. Pages are bounded to 1–100 rows.
Archives are metadata and remain readable/searchable. Ephemeral sessions are
hidden by default and admit only one run. A session's mode is fixed at creation.
Snapshots pair records and the session high-water sequence in one SQLite read
transaction. Evidence references contain segment, byte offset, length, SHA-256
and entry ID; reads verify bytes and identity rather than trusting cached offsets.
These operator services do not grant model-callable cross-session access.

Opening schema 1 upgrades it transactionally by adding hashed login sessions,
expiry/revocation and the persistent login throttle. These are operational state,
outside JSONL/search. Existing evidence and admission keys are unchanged. The backup and restore tools support schemas 1 and 2.

Search schema/projection version 1 uses FTS5 with bound, literal phrase queries,
source hashes, byte offsets, record IDs, entry IDs, checkpoints and snippets.
Incremental projection follows durable writes. Offline search opens only search
and evidence read-only, reports missing/stale/corrupt projections explicitly, and
imports neither server nor model nor memory clients. Rebuild validates all JSONL,
builds a temporary database, checks integrity and atomically replaces search.db;
no state.db access or model/memory service is needed. Legacy entries are searchable
but do not fabricate valid v2 tool receipts or successful runs.

## Boundary

The SQLite backup includes jobs, tombstones, logins and occurrences in state.db.
Restoring requires state and evidence and a fresh destination. Back up operator
configuration and external credentials separately. The
[direct legacy importer](../migration/cutover.md) handles actual legacy sessions and jobs.
Normalized legacy envelopes preserve source canonical path, hash, line number and
all original fields; raw bytes are content-addressed separately. They do not
fabricate v2 operational sessions/jobs or successful historical outcomes.
See the [maintenance runbook](../backup-restore.md).

## Session transfer

The Solakit session ZIP consists of stored members with versioned
`manifest.json`, ordered `evidence.jsonl`, `provenance.jsonl`, and
`artifacts/<sha256>`. The manifest records the root session ID and the length
and SHA-256 of each other member. Each JSONL row holds the scoped session ID,
original evidence reference, and complete entry. The bundle includes the selected
topic and its delegated descendants. Linked occurrences, letter publications,
and topic-scoped legacy entries are carried in provenance. Each direction
accepts up to 128 MiB and 50,000 evidence rows and artifact members combined.

Import verifies the bundle and creates fresh local identities. Typed
conversation and execution references point into the copy. Exact source entries
and artifact payloads retain their original IDs and checksums for inspection and
re-export. A standalone delegated topic retains its external lineage in the
source evidence. A job occurrence or tool publication remains evidence of what
happened on the source. Import creates no runnable jobs, admission authority,
deliveries, memory submissions, or provider recovery work from those records.

The store publishes verified immutable artifacts and provenance before committing
the imported session projection and retry receipt through the existing outbox.
Imported history carries the last verified committed summary into continuation.
Subsequent compactions and new runs follow the destination’s normal execution
and authority rules. Filesystem backups include imported provenance alongside
the rest of the evidence and operational state.
