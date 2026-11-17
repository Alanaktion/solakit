# Context selection and compaction

Phase 8 keeps immutable conversation and execution evidence while selecting a
bounded context for future requests. The run loop owns the session throughout
selection, summary generation and activation. There is no second worker or model
client. Archive/pin metadata remains independent.

## Accounting and limits

`model.context_tokens` is the operator's window ceiling (32768 by default).
The live pinned `Qwen3.6-35B-A3B-mxfp4` advertises `max_model_len=262144` through
oMLX `/models`; the default ceiling is deliberately smaller. The live quality probe
uses an even smaller 18000 ceiling to exercise repeated compaction economically.
These observations verify the advertised window, not weights or a tokenizer.

Until a tokenizer is verified, count serialized UTF-8 bytes plus 32 per message.
Reserve the admitted output tokens, 10% of the configured window as safety margin,
and expected tool-result headroom of `min(tool_result_bytes, window/8)` when tools
are offered. The headroom is an expectation, not permission to silently trim a
large result. Every actual continuation is checked again. Manifests break down
identity/fragments, summary, history, recall, metadata, input, continuation and
schemas, and record all reserves and their total. This supersedes Phase 4's
bytes/4 accounting; bodies without compaction retain the original placement.

Before its first conversational model step, a run tries compaction when total
reserved context exceeds 80% of the ceiling. The hard refusal is 100%. First check
whether identity, recall, schemas and the current input alone can fit; an oversized
fixed component fails without attempting to summarize unchanged history. An
oversized continuation/tool result fails explicitly rather than compacting during
an active exchange. Inputs and full receipts/artifacts remain retained.

## Safe selection and summary requests

Select only started turns preceding the owning run. Future queued inputs never
enter its history. Ephemeral runs never select continuing context or compact.
Preserve the two most recent started turns. The eligible prefix ends at a whole
terminal run; no tool-call/result group is split. Stop before a run with an
unreceipted call or uncertain effect, retaining it and all later history.

Take the largest oldest prefix that fits a summary request, then repeat if the
remaining context still needs compaction. A single oldest turn too large for that
request is explicitly uncompactable. Summary requests use the same pinned adapter,
inference semaphore, active deadline and admitted/current model-call budget;
reserve at least one call for the conversation. Current budget reductions apply
between summary calls. There are no model retries or unbounded attempts.

The operator's identity/fragments remain system messages. Frozen summary instructions
in `compaction.py` treat source JSON as material, preserve corrections, commitments,
dates, unfinished work and failures, and request source IDs. Include the prior
summary and boundary ID when extending a compacted range. Summary output must be
an exact JSON envelope echoing the final source run and source hash, with nonempty
text of at most `min(4096, output_tokens/2)` UTF-8 bytes. Tool proposals, malformed
JSON, wrong ranges/hashes, blank text and oversized summaries fail validation.
Structural validation cannot prove factual coverage; the live quality gate is
separate.

`ModelStep.purpose` defaults to `conversation` for old evidence. A summary step has
purpose `compaction`, is never dispatched, and is excluded from ordinary tool-history
projection. Summary text is an assistant context message, never an admitted user
statement. Memory extraction excludes summary message IDs, including prepared and
superseded summaries; no summary is automatically committed to OpenViking.

## Manifest and activation protocol

The existing `CompactionBoundary` gains optional `manifest` artifact and `state`
(`prepared`/`committed`, default `committed` for older records). Only committed,
manifest-backed boundaries participate in automatic selection. Older stub records
remain readable. No SQLite schema migration or source rewrite is required.

The immutable manifest includes boundary/prior-boundary IDs, ordered source run IDs,
exact evidence references, a canonical source-reference hash, the summary Message
artifact and byte limit, model-step/model identity, and exact request-body hash.
Each reference includes the original JSONL entry, offset, length and byte hash.
The model request's private manifest also retains the frozen instruction hash and
source range. Prior manifests preserve cumulative provenance across compactions.

1. Publish/fsync summary and manifest artifacts privately, then durably append a
   prepared boundary through the store's transactional outbox. It is not active.
2. Verify the complete source prefix, terminal/effect safety, hashes, request,
   response envelope, summary contents and prior boundary. Append/fsync the exact
   summary Message. Reuse it if recovery finds the same ID and bytes.
3. Commit the boundary's `committed` snapshot and corresponding outbox entry in
   one state transaction; flush/fsync the evidence before acknowledgement.
   Activation and completion are the same durable transition. There is no
   independently mutable active pointer to diverge from the boundary state.
4. Startup reconciles/drains the ordinary outbox first, then verifies boundaries
   and finishes valid preparations without inference. An invalid preparation stays
   inactive and inspectable; the previous valid boundary remains selected.
   Corruption of a committed boundary blocks startup. Recovery never duplicates
   the summary or reapplies an already committed boundary.

The latest committed boundary selects exactly one summary and the later started
turns. Superseded summaries, their manifests and every original message/tool
receipt remain evidence and independently searchable. There is no transcript
deletion, history rewrite, search replacement or semantic-memory dependency.

## Failure and inspection

Summary/model failure leaves the previously valid boundary selected. If that
context still fits, the conversation continues with it and the failed summary
attempt remains evidence. Otherwise the run terminates with `context_limit` and
retained input. Context-limit failures do not masquerade as malformed model output.

Recorded `sola prompt --request-body BODY --manifest MANIFEST --hashes` inspection
shows the exact selected summary, tail record IDs, boundary and manifest reference,
prior boundary, source/summary/prompt hashes, model, omitted run/source counts, and
accounting method/components/reserves. Inspect the referenced compaction manifest
to trace summarized facts to original JSONL references. Offline inspection never
regenerates a summary or contacts Qwen/OpenViking. Fresh `--frozen` inspection can
also carry a recorded compaction selection.

See [validation](../validation/phase-08.md) and the
[model request contract](model-prompts.md).
