# Phase 13 — Direct legacy import

Status: implemented. The operator has backups and accepts manual cleanup, so this
supersedes the earlier snapshot/rehearsal/checkpoint/verifier/rollback plan.

Use [the direct import guide](../migration/cutover.md):

```sh
uv run --locked python -m scripts.migration "$HOME/.local/share/sola"
```

`scripts/migration.py` reads the stopped `/Users/alan/.config/solakit/` runtime
and imports sessions, archived transcripts, runs, jobs and historical evidence
into a new v2 data directory. It preserves raw data files and rebuilds search.
Imported jobs start disabled/paused; historical effects and memory submissions
are not replayed. Existing identity files and remote memory remain in place.
Configuration migration remains the separate existing script.

No snapshot inventory, resumable checkpoints, independent verifier, rollback
worksheet or formal soak is required for this import. The operator handles
backups, any legacy inconsistencies and the actual client/runtime switch.
The importer leaves its source untouched and refuses an existing destination.

Checks and limitations are recorded in [validation](../validation/phase-13-simple.md).
