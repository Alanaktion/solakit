# Legacy migration and cutover

`uv run --locked python -m scripts.migration` supplies offline snapshots, inventory,
resumable import, verification and rollback reports. It never starts/stops a runtime,
loads legacy packs, contacts services, switches clients or enables scheduling.
Production cutover remains an operator decision.

## Consistent snapshot and inventory

Choose a private parent outside both installations, with space for the snapshot,
candidate, backup and restore. The legacy directory includes old database backups;
these are retained too. Stop the old runtime cleanly during snapshotting and keep
other writers out of the checkout and included identity/resource trees. The script
opens the existing `.runtime.lock` read-only and refuses an active owner. SQLite's
backup API includes committed WAL content; copying only `app.db` is insufficient.
WAL recovery runs on private file copies, so SQLite never opens the source files.

```sh
mkdir -m 700 "$HOME/.config/sola-migration"
uv run --locked python -m scripts.migration snapshot \
  --source /Users/alan/.config/solakit \
  --legacy-repo /Users/alan/Projects/solakit \
  --include config=/Users/alan/Projects/solakit/config.toml \
  --include soul=/Users/alan/workspace/SOUL.md \
  --include fragments=/Users/alan/workspace/fragments \
  "$HOME/.config/sola-migration/snapshot-1"
uv run --locked python -m scripts.migration inventory \
  "$HOME/.config/sola-migration/snapshot-1" \
  "$HOME/.config/sola-migration/before-1.json"
```

`--include LABEL=PATH` preserves additional selected pack/resource files or trees.
Data symlinks and special files are rejected. The checkout manifest records commit,
dirty/deleted/untracked file hashes, and symlink targets without following links.
Source hashes/inventories are checked again before publication. Failed snapshots
without `manifest.json` cannot import; preserve them for inspection and choose a
new destination. Snapshot creation does not resume.

Files are private (directories 0700, files 0600) and may contain credentials and
conversations. Keep the bundle out of Git. Reports use IDs/counts/hashes rather
than credential values. External OpenViking data and service-owned resources need
their own consistent backups. A copied config preserves memory roots and secret
references without resolving credentials or backing up remote memory. Record
backup locations and credential-reference availability privately.

## Import and verify

```sh
uv run --locked python -m scripts.migration import \
  "$HOME/.config/sola-migration/snapshot-1" \
  "$HOME/.config/sola-migration/candidate-1"
uv run --locked python -m scripts.migration verify \
  "$HOME/.config/sola-migration/snapshot-1" \
  "$HOME/.config/sola-migration/candidate-1"
```

Import requires a new destination or its own interrupted import of the identical
snapshot. Each batch/checkpoint commits transactionally, with existing outbox
recovery after process death. Rerun the same command to resume; a completed rerun
verifies and adds zero records. New v2 evidence blocks import retries. Different
snapshots cannot be mixed into a candidate; take the final snapshot into a fresh
candidate at cutover. Incremental/reverse import is unsupported.

`imports/phase13-source/` retains the entire snapshot, original filenames and raw
bytes, including unknown fields, prompts, deferred packs and login backup data.
`phase13-report.json` records before/after counts, anomalies, reference checks,
job reviews and session/run/job ID maps. `phase13-checkpoint.json` records evidence
hashes and evidence/session cursors. IDs use UUID5 of original source root,
record namespace and old ID (file/line for messages), avoiding cross-kind collisions.

| Legacy authority | V2 mapping |
| --- | --- |
| Session metadata | Mode/title/time/archive/pin in state.db; other fields preserved raw |
| Numbered/active transcripts | Numeric archive ordering, then active transcript; visible user/assistant text plus assistant model steps for continuing context |
| Tools/images/system/compaction history | Scoped, searchable legacy JSONL plus raw files; no fabricated v2 tool receipts, attachments or active compaction boundaries |
| Runs and journals | Known terminal outcomes retained; queued/running or conflicting outcomes interrupted; original origin/profile/lineage/details retained raw; system origin in normalized historical runs |
| Missing separate run-start time | Legacy created_at is the documented approximation |
| app.db.job_definitions | Authoritative jobs/tombstones extracted separately from search; latest JSONL consumed flags preserved |
| Active/cursorless one-shot attempts | Conservatively consumed; no automatic rearm |
| Imported job authority | All disabled/paused, no next due/tools/roots/delivery; global scheduling paused |
| Login, memory/delivery/effect state | Login backup only, invalid in v2; no memory submission, delivery or uncertain effect replay |
| Derived indexes/caches | Raw backup only; search.db rebuilt exclusively from v2 JSONL |

Review unresolved job bindings, child sessions, task templates, profiles, tools and
delivery before enabling any job. Verification checks complete raw inventory and
hashes, mapped payloads and order, batch checkpoints, SQLite integrity, state/outbox/
evidence reconciliation and checkpoint hashes/cursors. Reports compare call/result
pairing and prompt/turn/catalog/schema hashes. Missing artifacts, torn final records,
missing run snapshots and conflicting journals remain explicit anomalies; malformed
complete JSONL blocks preflight. `verified` establishes preservation, not readiness.

## Backup, restore and isolated acceptance

```sh
uv run --locked python scripts/storage.py backup \
  --data-dir "$HOME/.config/sola-migration/candidate-1" \
  "$HOME/.config/sola-migration/backup-1"
uv run --locked python scripts/storage.py restore \
  "$HOME/.config/sola-migration/backup-1" \
  "$HOME/.config/sola-migration/restored-1"
uv run --locked python scripts/storage.py rebuild \
  --data-dir "$HOME/.config/sola-migration/restored-1"
uv run --locked python -m scripts.migration verify \
  "$HOME/.config/sola-migration/snapshot-1" \
  "$HOME/.config/sola-migration/restored-1"
```

See [backup/restore](../backup-restore.md) and [config migration](../memory.md).
Generate a private draft and authority diff from the copied configuration:

```sh
uv run --locked python -m scripts.migrate_config \
  --source "$HOME/.config/sola-migration/snapshot-1/extras/config/config.toml" \
  --profile interactive \
  --data-dir "$HOME/.config/sola-migration/candidate-1" \
  --output "$HOME/.config/sola-migration/config-draft-1"
```

Legacy `path:`, `repo:` and `collection:` workspace entries resolve through the
copied declarations, including containment checks for collection directories.
An empty legacy workspace defaults to declared repositories. Split read/write
authority remains conservatively narrowed by the existing config migrator.
Copied relative config paths originally resolved against the old config directory;
review them before drafting. Use a separate port, fresh credentials and writable
test resources for an isolated candidate. Keep memory disabled, effect grants empty
and deferred packs unloaded. Scheduler pause does not disable interactive effects;
grants/roots must enforce isolation in services. For live write acceptance, grant
only sandbox resources and the approved `sola-test` memory account. Production
read-only recall needs a separately reviewed safe scope. Run model/memory/API/
browser/file/image/compaction/restart acceptance against that configuration.

## Cutover checklist

- [ ] Review source commit/dirty/data manifests, exact paths, counts, mappings,
  artifact/pairing anomalies and every imported job's semantics.
- [ ] Record external backups, enabled resources, credential references,
  configuration authority diff, preserved identity and invalidated old logins.
- [ ] Pass deterministic/live/operator gates, including outstanding release
  reliability and browser gates. Fix numerical soak thresholds/duration before
  enabling production. Suggested duration is seven days, extended through a
  complete schedule cycle, extraction, restart and verified restoration.
- [ ] Restore into a new directory and verify consumed/deleted jobs and uncertain
  effects. Rehearse rollback before and after synthetic new writes.
- [ ] Present results, unresolved risks, exact source/destination, downtime and
  rollback worksheet for an explicit operator cutover decision.
- [ ] After approval: quiesce legacy work, stop its runtime/scheduler, settle or
  mark in-flight effects, snapshot again, import into a fresh candidate, verify
  and take a coherent checkpoint backup.
- [ ] Start v2 paused, switch clients, review/enable jobs and enable one scheduler.
  Record subsequent evidence cursors and external receipts.
- [ ] During soak track completion/failure, duplicates, backlog, proactive
  repetition, memory progress, search health, storage growth and restore success.
  Stop enabling work if authority/recovery fails. Preserve the old install and
  backups through explicit soak acceptance; cleanup is a separate action.

## Rollback worksheet

Stop both runtimes/schedulers, durably pause v2 scheduling, quiesce accepted runs
and preserve a coherent v2 backup before generating a new private report:

```sh
uv run --locked python -m scripts.migration reconcile \
  "$HOME/.config/sola-migration/candidate-1" \
  "$HOME/.config/sola-migration/rollback-1.json"
```

The command takes the v2 owner lock, verifies the checkpoint evidence prefix and
lists every subsequent entry with verified byte references, plus current artifacts.
With no new evidence it says `before_new_writes`: check for external work outside
the runtime, return clients to the old installation and enable its scheduler once.
With new evidence it says `reconciliation_required`; reverse import is unsupported.
Switching ports alone is insufficient. Complete the following before reopening:

| Item | Evidence/reference | Transfer/reconciliation and owner | Outcome |
| --- | --- | --- | --- |
| Conversations/artifacts | Report entries/hashes | Preserve and transfer explicitly | Pending |
| Occurrences/consumption | Occurrence/run records | Prevent repeated occurrence/effect | Pending |
| Job edits/tombstones | Job records | Apply reviewed changes | Pending |
| Memory/extraction | Commit/provider task | Reconcile without resubmission | Pending |
| External effects/receipts | Tool/image/delivery/pack evidence | Confirm or mark uncertain; never blind retry | Pending |
| Clients/authentication/scheduler | Operator checkpoint | Reopen one runtime after reconciliation | Pending |

The scripts never reverse-import, reopen a runtime or replay effects.
