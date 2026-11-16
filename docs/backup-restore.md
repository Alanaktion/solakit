# Evidence maintenance and recovery

Use a disposable destination first. These commands operate on local files; they
never start the server, model or memory clients. Backup/import/rebuild take the
same exclusive owner lock as `sola serve`: stop the runtime cleanly before using
them. The maintenance script refuses a missing data directory. Search is read-only
and does not take ownership or construct the runtime.

```sh
uv run --locked sola search 'exact phrase' --data-dir /absolute/sola-data
uv run --locked sola search 'exact phrase' --config /absolute/config.toml --limit 20
uv run --locked python scripts/storage.py rebuild --data-dir /absolute/sola-data
uv run --locked python scripts/storage.py backup --data-dir /absolute/sola-data /absolute/new-backup
uv run --locked python scripts/storage.py restore /absolute/new-backup /absolute/new-restored-data
```

`sola search` prints JSON with `available`, `unavailable`, or `corrupt`; errors exit
2. Queries are bound literal phrases, never SQL or raw FTS syntax. Optional
`--session ID` scopes operator search. Each result includes a snippet and a verified
segment/offset/length/hash/entry-ID reference. This is operator access, not a model
tool permission. Missing/stale/corrupt search requires rebuild; source corruption
requires inspecting/restoring evidence, never editing a JSONL source line.
Rebuild replaces only search.db after integrity checks. It works even without
state.db; that does **not** authorize starting the runtime without state.db.

Backup writes a SQLite backup snapshot in standalone DELETE journal mode, copies
all evidence/recovery/artifact/raw-import files, fsyncs them, then publishes a
checksummed manifest with session/evidence high-water marks. Search and the owner
lock are omitted. A directory without a completed manifest is an incomplete backup.
Keep it for diagnosis; choose a new destination for the next attempt. Never copy
only a live main SQLite file while ignoring WAL. Back up operator configuration,
identity fragments and external credentials/service data separately.

Restore requires a **new**, absent destination. It validates path safety, every
checksum, SQLite integrity/version, evidence framing, state/evidence reconciliation
and high-water marks in private staging before publishing the directory. Failed
staging remains beside the destination as `.NAME.restore` for inspection. Choose
a new destination for a subsequent attempt. A successful restore preserves queued
admissions, session metadata, durable operational rows, artifacts and raw imports.
On the first runtime start, running runs become interrupted, sending deliveries
become uncertain, and in-progress memory extraction becomes failed. Do not retry
unreceipted tool effects automatically. Verify the restored tree using disposable
configuration before intentionally switching the operator's data directory.

A torn final line is sealed in place with a checksummed recovery note and the next
segment is used. Interior corruption, changed seals, missing acknowledged evidence
and outbox conflicts block startup. Preserve the whole tree and restore a coherent
checkpoint. Recovery never truncates bytes or fabricates a successful outcome.
State.db contains irreplaceable operational data; evidence/search cannot recover
lost schedules, tombstones or logins on their own.

For the Phase 3 import rehearsal, copy synthetic legacy fixtures first:

```sh
uv run --locked python scripts/storage.py import-fixture --data-dir /absolute/disposable-data /absolute/copied-fixture.jsonl
```

The data directory must already exist. Import preserves raw bytes at
`imports/SHA256`, then wraps each complete JSON object in a versioned legacy entry,
retaining unknown fields, call/result IDs, timestamps and archive source names.
Identity uses the copied source's canonical path, content hash and line number;
retrying the same source is idempotent. Different source paths (including identical
basenames/content) remain separate provenance, while identical raw bytes share a
content-addressed copy. Invalid/torn JSON is rejected with its raw copy retained;
no partial import is acknowledged. No legacy operation is replayed, no job enabled,
and no incomplete historical receipt is converted into a successful v2 receipt.
These searchable envelopes rehearse preservation, not semantic production migration
of legacy sessions/jobs/logins; that mapping remains Phase 13 work.

See [the storage protocol](contracts/storage.md) and
[Phase 3 verification](validation/phase-03.md) for tested boundaries.
