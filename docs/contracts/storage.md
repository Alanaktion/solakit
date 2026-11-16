# Evidence, operational state, and search

Phase 3 protocol, selected before implementing storage callers. Local filesystem,
one cooperating writer under the existing data-directory ownership lock. Store
methods are synchronous, bounded local operations called on the owning event
loop; there is no extra worker or independent SQLite writer.

## Layout and framing

`state.db` (SQLite schema/user_version 1, WAL, synchronous FULL) owns operational
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
| Search projection | No authoritative mutation | Best effort after append; failure marks search degraded and does not undo a successful write |
| Backup | Writer held; drain outbox; SQLite backup API | Evidence high-water sizes + hashes + state backup + atomic manifest; restore verifies all files before publication |
| Legacy fixture import | Source digest ledger and deterministic import identities | Preserve raw source first; retain unknown fields in normalized legacy entries; retry is idempotent |

Each mutation first drains older outbox work. Commit state and exact serialized
outbox bytes in one transaction, append and fsync, then mark delivered. No caller
may dispatch from a committed-but-undelivered row. Admission acceptance is the
return from `admit`, not the SQLite commit. An unacknowledged request is discoverable
by its request ID after recovery. The admission key covers session, profile,
origin, lineage, input and frozen admission details, excluding generated IDs/time.
Phase 3 admits interactive/system work only. Scheduled/proactive admissions are
rejected until Phase 9 can verify authoritative job/occurrence claiming.

On restart validate evidence, reconcile stable IDs and exact payloads, then drain
the outbox before allowing new mutations. Append-before-delivered reuses the
existing entry after an fsync; it never emits a second logical event. A duplicate
ID with different content, invalid complete line, sequence gap, stale offset, or
state/evidence mismatch is corruption and blocks the store. Missing operational
state is never silently reconstructed from evidence. A running run becomes
interrupted; sending delivery becomes uncertain; running memory work becomes
failed. Queued runs remain queued for the Phase 6 policy. Unreceipted tool intents
remain visible and must not be automatically retried.

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

Search schema/projection version 1 uses FTS5 with bound, literal phrase queries,
source hashes, byte offsets, record IDs, entry IDs, checkpoints and snippets.
Incremental projection follows durable writes. Offline search opens only search
and evidence read-only, reports missing/stale/corrupt projections explicitly, and
imports neither server nor model nor memory clients. Rebuild validates all JSONL,
builds a temporary database, checks integrity and atomically replaces search.db;
no state.db access or model/memory service is needed. Legacy entries are searchable
but do not fabricate valid v2 tool receipts or successful runs.

## Boundary

State is backed up, never rebuilt from search. Future jobs, tombstones, logins and
occurrences in state.db are included by the SQLite backup API without needing
Phase 3 scheduler schemas. Restoring requires both state and evidence, into a new
empty directory. Backups do not contain operator configuration or external
credentials; back those up separately. Raw fixture import is a rehearsal, not a
production migration or proof of the actual legacy export format (Phase 13).
Normalized legacy envelopes preserve source canonical path, hash, line number and
all original fields; raw bytes are content-addressed separately. They do not
fabricate v2 operational sessions/jobs or successful historical outcomes.
See the [maintenance runbook](../backup-restore.md).
