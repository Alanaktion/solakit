# Phase 6: OpenViking integration and context compaction

Status: planned

Depends on: phase 5

- Add pre-turn recall and post-turn session commits through the existing OpenViking API.
- Route all memory access through a scope-aware client in `ToolContext`; MVP sessions use `shared`.
- Construct and persist the final prompt after recall, before inference.
- Add between-run compaction with archived transcript rotation, boundary records, and summary references.
- Make memory failures and commit failures visible; prevent retries from duplicating committed messages.

**Exit check:** A committed fact is recalled in a later run. Compaction preserves archived history and valid tool-call groups. Dry-run inspection remains free of external calls.

## Completion record

Record implementation, validation, and remaining limitations here before marking complete.
