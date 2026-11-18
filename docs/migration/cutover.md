# Direct legacy import

Stop the old runtime, then import directly into a new v2 data directory:

```sh
uv run --locked python -m scripts.migration "$HOME/.local/share/sola"
```

The source defaults to `/Users/alan/.config/solakit/` on this machine. Use
`--source /another/legacy-directory` to override it. The destination must not
already exist. Existing backups and manual cleanup replace the earlier snapshot,
checkpoint, verification and rollback workflow.

The importer brings over sessions, numbered archives, conversation text, historical
runs and disabled jobs. Original history, prompts, receipts and other data files
are retained under `imports/legacy/`; legacy tool records remain searchable within
their topics. Search is rebuilt from JSONL. It skips old databases/backups,
login sessions and disposable caches. Identity files and OpenViking memory stay
in their existing locations; use the existing [config migration](../memory.md)
script separately if needed.

Imported work never runs automatically: running/queued historical runs become
interrupted, jobs/global scheduling start paused, and old memory submissions and
effects are not replayed. Review job profiles/bindings before enabling them.

Run snapshots supply historical outcomes; the importer does not reconcile them
against every journal. Inverted timestamps are clamped, missing run snapshots
become interrupted history, and torn final JSON fragments remain in the raw copy.
Tool/image/compaction records are preserved without reconstructing active v2
provider operations or attachments. Clean up legacy inconsistencies manually.

There is no resume, merge, audit report, or automatic rollback. If an import fails,
fix the source issue and use a fresh destination (or remove the failed import).
The source is never rewritten, and an active legacy runtime or uncheckpointed
SQLite WAL causes the command to stop. Starting v2 and switching clients remain
separate operator actions.

## One-time OpenViking history import

After migrating sessions, stop Sola and select the v2 config with memory enabled,
the intended account/user/root, and a shared default profile with memory mode
`optional` or `required` (see [memory setup](../memory.md)). Preview locally, then
submit explicitly:

```sh
uv run --locked python -m scripts.import_memory_history \
  --config ~/.config/sola/config.toml --dry-run
uv run --locked python -m scripts.import_memory_history \
  --config ~/.config/sola/config.toml
```

This imports every original user/assistant message in shared sessions, including
archived and pre-compaction history and messages from unsuccessful runs. It reads
the migrated v2 records, preserving their order within each session. Isolated and
ephemeral sessions, tool output, reasoning, compaction summaries and image bytes
are excluded. This is conversation-text extraction, not an OpenViking backup of
all local evidence. Local history remains intact.

Messages are submitted in batches of at most 100 parts and 64 KiB, with separate
provider sessions for each batch. Long messages are split into consecutive parts
without truncation; each part retains its source message ID. The JSON output counts
parts and batches. Acceptance means a provider task was returned; extraction and
later retrieval are separate checks. Task IDs are retained in private
`imports/openviking-history/*.accepted.json` files. Inspect them through OpenViking
in the same account/user. The runtime memory observer does not manage this manual
import's tasks.

Keep Sola stopped until submission finishes. Rerunning against the same history
and destination skips accepted batches. History or scope changes are refused.
Submission attempts are durably recorded before remote writes: a started batch
without an acceptance receipt is uncertain and stops a rerun. Inspect its recorded
`sola-...` provider session manually; do not delete receipts to force a retry.
There is no automatic rollback or reconciliation with pre-existing OpenViking
memory, so this can re-extract facts already present from legacy operation.
