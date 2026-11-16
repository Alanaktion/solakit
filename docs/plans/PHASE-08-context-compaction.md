# Phase 8 — Context management and compaction

Status: planned. Depends on [Phase 6](PHASE-06-run-loop.md), [Phase 7](PHASE-07-memory-migration.md), and immutable storage. Unlocks long-lived topics and background work using their context.

## Outcome

Long conversations stay usable within the model window. Summaries change future context selection while original messages, tool calls, and receipts remain immutable and searchable.

## Implementation slices

1. **Define context accounting.** Account for identity/fragments, recall, tool schemas, active history, output reserve, and expected tool-result headroom. Prefer verified tokenization; otherwise record conservative estimation and safety margin. Define thresholds from the pinned model's actual window. Handle a single oversized input/schema/result explicitly instead of repeatedly compacting unchanged context.
2. **Choose safe source boundaries.** Compact only under the session mutation owner at a completed conversational boundary. Never split tool-call/result groups or hide unresolved effects. Select an eligible prefix and preserve the recent tail. Keep active commitments, dates, user corrections, and unfinished tasks. Evidence references let later retrieval recover original detail.
3. **Generate a bounded summary.** Use pinned local Qwen through the common adapter and semaphore. Freeze summary instructions and include provenance/source IDs. Treat source text as material to summarize, not executable instructions. Validate that the result is nonempty, bounded, and covers the expected range. Do not submit the summary as a new user statement or silently commit it as semantic memory.
4. **Commit with a manifest.** Persist prepared manifest with prior boundary, source range/hash, summary artifact/hash, model identity, and prompt hash. Append/fsync the summary evidence, then activate the new boundary through the state/evidence protocol and mark committed. Startup can finish a verified prepared commit or retain the previous valid boundary. It must not apply a summary twice. Keep superseded summaries as evidence.
5. **Inspect and fail honestly.** Show context boundary, selected tail, summary provenance, accounting method, and omitted source count in prompt inspection. Summary/model failure leaves the old boundary intact. If the old context cannot fit, end with explicit context-limit failure and retained input; do not discard arbitrary history. OpenViking-backed summaries remain optional until their contract and quality are verified.

## Verification and exit gate

Construct a multi-compaction session containing tool groups, corrections, commitments, and known facts. Assert original byte hashes and receipts remain unchanged; future model context contains exactly one active summary plus the intended tail. Search still finds original text.

Inject crashes before/after manifest creation, summary publication, boundary activation, and completion. Restart must choose the old or new fully valid context with no duplication. Test summary failure, invalid summary, excessive schemas, oversized tool results, and concurrent incoming turns.

Use deterministic fixtures for range/atomicity and live-model cases for summary usefulness. Evaluate preservation of active commitments and operator identity, not exact summary wording. An operator should be able to trace any summarized fact to source evidence.

Deliver compaction service, manifest/boundary specification, inspection output, recovery tests, and quality report. Exit requires multiple successful compactions and fault recovery; archiving sessions remains independent metadata behavior.
