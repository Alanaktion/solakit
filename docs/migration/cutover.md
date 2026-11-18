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
