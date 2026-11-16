# Phase 4 — Model access and deterministic prompt assembly

Status: implemented and locally/live verified (2026-11-16). Depends on [Phases 2](PHASE-02-records-configuration.md) and [3](PHASE-03-evidence-search.md). Unlocks the run loop and live evaluations. See the [model/prompt contract](../contracts/model-prompts.md) and [validation report](../validation/phase-04.md). API identity is verified; weight identity and tokenizer accounting remain explicitly unverified. Production attachment admission stays in Phase 12.

## Outcome

Every Qwen request is attributable to exact messages, parameters, identity fragments, tool schemas, and context selection. Prompt inspection calls the same assembler as execution.

## Implementation slices

1. **Build the model adapter.** Own one application-lifetime AsyncOpenAI client targeting configured oMLX. Normalize responses into v2 records. Select the explicit pin; other models in `/models` are acceptable. Reject missing/wrong identity and unexpected returned identity when available. Record what the server can actually verify; an alias alone cannot establish weight identity.
2. **Handle request lifecycle.** Configure SDK retry behavior explicitly, with every attempt visible. Bound connection/generation timeouts and cancellation cleanup. Normalize content, reasoning, tool calls, finish reason, and usage. Assemble streamed tool-call arguments by index/ID and dispatch only after a complete validated response. A broken stream, token limit, or malformed tool-call response is not an ordinary successful answer.
3. **Freeze prompt placement.** Agree and document exact roles/order for SOUL, curated memory, context summary/history, recorded recall, profile fragments, time/origin, and input. Keep the user's text verbatim with harness metadata in distinct fields. Eager tool schemas live in the request tools field; any deferred catalog is an explicitly versioned prompt component. Never silently add personality or retrieval policy prose to operator fragments.
4. **Capture request evidence.** Before transmission, persist exact serialized request bytes and hashes, fragment versions/hashes, selected message IDs, context boundary, disclosure view, model pin, and sampling. Exclude transport credentials/headers. Use private artifact files for large bodies and reference them from JSONL. Operational logs/presentation are redacted; private input evidence retains user/model content. Document these distinct guarantees so secret masking does not silently destroy transcript fidelity.
5. **Make accounting and inspection honest.** Reserve output and tool-result headroom; label estimates when no verified tokenizer is available. `sola prompt` accepts frozen/recorded recall for offline reproduction, or labels recall omitted. Print inspectable prompt sections and optional hashes without starting the server. Model-specific reasoning-field handling must preserve literal quoted `<think>` examples and code.

## Deliverables, verification, and exit gate

Deliver adapter, prompt assembler, request-evidence contract, golden fixtures, `sola prompt`, and a developer live probe. Test nonstreamed/streamed equivalence of final normalized calls, split UTF-8/arguments, missing usage, malformed responses, wrong model, retry counts, cancellation, interrupted streams, and reasoning-only output.

Golden tests freeze time, recall, fragment bytes, IDs, and schema ordering; identical frozen inputs produce identical request bytes. A changed tool schema or operator fragment must change its recorded hash. A later config edit cannot alter inspection of an earlier request.

Exit requires deterministic fixtures and a live oMLX probe for the chosen Qwen confirming tool-call/stream behavior. If unavailable, keep live verification pending. This phase does not implement narration continuation or dispatch tools; those belong to Phase 6.
