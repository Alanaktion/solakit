# Phase 2: Append-only storage and session routing

Status: complete

Depends on: phase 1

- Implement JSONL transcripts, run records, exact prompt files, tool logs, and the rebuildable SQLite index outside the code tree.
- Route configured channel identities to pinned sessions; create sessions on first use.
- Record origin metadata on every transcript entry.
- Define crash recovery: retain partial run evidence, identify interrupted runs, and rebuild indexes without rewriting history.
- Add session and run inspection endpoints.

**Exit check:** Sessions survive restart, channel histories remain independent, and deleting/rebuilding the index loses no conversation or run evidence.

## Completion record

Implemented JSONL transcripts and run event journals, immutable exact prompt files,
redacted tool logs, immutable session metadata, channel pinning, recovery and
rebuildable SQLite indexes. Run .json files are disposable snapshots backed by
append-only .events.jsonl evidence. Inspection endpoints are authenticated.
Validated with 10 passing tests including restart, index deletion, independent
channel histories, torn-write preservation, and interrupted-run recovery; Ruff passes.
The store is a single-process service, consistent with the target architecture.
