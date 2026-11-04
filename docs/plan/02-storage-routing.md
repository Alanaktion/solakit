# Phase 2: Append-only storage and session routing

Status: planned

Depends on: phase 1

- Implement JSONL transcripts, run records, exact prompt files, tool logs, and the rebuildable SQLite index outside the code tree.
- Route configured channel identities to pinned sessions; create sessions on first use.
- Record origin metadata on every transcript entry.
- Define crash recovery: retain partial run evidence, identify interrupted runs, and rebuild indexes without rewriting history.
- Add session and run inspection endpoints.

**Exit check:** Sessions survive restart, channel histories remain independent, and deleting/rebuilding the index loses no conversation or run evidence.

## Completion record

Record implementation, validation, and remaining limitations here before marking complete.
