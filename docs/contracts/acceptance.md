# Phase 0 acceptance journeys and traceability

These are executable acceptance specifications to implement in later phases, not claims that the v2 runtime already passes them. Use deterministic fixtures for persistence/authority and real services for model/memory capability. Operator review evaluates Sola's continuity without exact-answer assertions.

## J1 — Interactive conversation and identity

Input: create a shared topic and ask an ordinary conversational question. Repeat with OpenViking unavailable. Admission produces stable topic/message/run IDs; prompt assembly uses unchanged SOUL/profile fragments and explicitly records recall or recall failure; pinned Qwen produces a response; no tools or effects are needed; the final answer and terminal event persist and render once.

Success: user text is verbatim, identity fragment hashes match operator files, answer completes without unnecessary continuation/tool calls, and degraded recall is visible without a false memory claim. Operator reviews continuity. Disconnect during generation and reconnect: the accepted run continues and the current answer replaces lost previews.

Evidence: durable admission, request bytes/hashes, recall status, final message/outcome. Primary gates: Phases 2–4/6/7/10/11; baseline no-tool cases in Phase 1.

## J2 — Reliable tool-assisted file and image work

Input: in a disposable granted workspace, read a brief, create a requested text artifact, then generate an image with one configured workflow and save/refer to its output. The core services authorize each file/workflow resource and effect, record exact tool arguments, persist intent before consequential execution, and associate results with call IDs. Generation shares normal budgets, cancellation, and inference coordination where applicable.

Success: text artifact has expected content, image output is accessible via a stable authorized reference, and each reported action has a receipt. A path escape, guessed tool, malformed argument, or unavailable workflow fails clearly before an unauthorized effect. An announcement without execution is not task success. Stop after an uncertain image submission and expose its reconciliation reference instead of submitting again.

Evidence: input/request/schema snapshots, guarded path decision, tool intents/receipts, output hashes/provider IDs, final outcome. Phases 1/4/5/6/12; browser artifact handling in Phase 11. Domain packs and shell are unnecessary.

## J3 — Cross-session memory without evidence confusion

Input: in a test shared memory root, tell Sola a distinctive fact. Finish the interaction, await accepted commit and extraction, then ask a fresh shared topic a question requiring that fact without repeating it. Recall records actual sources. Separately try the same query in isolated and ephemeral modes.

Success: the fresh shared topic retrieves the fact with recorded evidence; provider extraction completion alone is insufficient. Isolated/ephemeral runs make zero remote calls and cannot bypass isolation via transcript tools. A commit timeout becomes failed/ambiguous with durable intent and no blind resubmission. Reply delivery need not wait for extraction.

Evidence: recall query/content/root, note staging if used, payload hash, provider reference, extraction observations, fresh-session retrieval. Phases 4/6/7, scope tests in Phase 5. Test facts do not enter production memory.

## J4 — Scheduled and proactive work, including no action

Input: operator enables a wake bound to a topic with an outstanding commitment and a restricted profile. Scheduler claims a unique due occurrence, durably admits a run, supplies bounded context/recall, then Sola either prepares permitted file/image work, leaves one useful message, or does nothing. Repeat a wake with no outstanding work.

Success: task/artifact/message has a complete run trace; no-action is recorded without an empty chat message. Quiet-hour notifications/outbound effects are deferred/coalesced, local preparation may proceed, and cooldown/global/run budgets survive restart. Autonomous attempts to schedule/enable/rearm another wake are denied even if job tools are otherwise granted. A blocked archived target remains visible rather than being rerouted. Jobs work without an open browser.

Evidence: definition version, due instant, occurrence ID, admission/run, origin lineage, policy decision, delivery/dedupe state, memory status. Phases 2/5–10/11/12. Use fake clocks and destinations before live scheduled acceptance.

## J5 — Restart recovery and historical preservation

Input: admit work; stop the process after an acknowledged transcript append, after tool intent, and after a simulated external acceptance but before receipt. Restart with a consistent copied state/evidence set. Delete only search.db and rebuild it. Rehearse import of archived, unknown-field, and torn legacy-shaped examples.

Success: acknowledged evidence survives; untouched queued work is recoverable; running work is interrupted; uncertain effects never replay automatically; consumed one-time jobs and tombstones persist; search results rebuild without touching state.db. Original imported bytes remain verifiable and unknown fields survive in raw evidence. Torn fragments are retained and reported. A complete backup restores coordination as well as history.

Evidence: crash-point logs, JSONL hashes, state checkpoint, outbox/receipt reconciliation, index rebuild report, backup manifest/import report. Phases 2/3/6/8/9/12/13; acceptance uses test effect destinations.

## Outline-to-gate matrix

| Requirement | Journey | Deterministic / live / operator gate |
| --- | --- | --- |
| Identity and ordinary conversation | J1 | Prompt fixtures / pinned Qwen / continuity review |
| Reliable basic tools | J2 | Grants and receipts / model + image workflow / artifact usability |
| OpenViking memory and isolation | J3 | Scope/replay tests / fresh-session recall / memory usefulness |
| Background/proactive behavior | J4 | Fake-clock and lineage tests / scheduled run + extraction / quiet-hour UX |
| Immutable evidence and separate databases | J5 | Crash/rebuild/restore/import / service interruption probes / recovery walkthrough |
| Thin HTTP and bounded UI | J1/J2/J4 | API/reconnect tests / browser flow / keyboard/mobile acceptance |

No journey requires Android, delegation, shell, browser control, or deferred domain packs. Phase 1 adds numeric task/reliability criteria before candidate scoring. Missing source D1–D6 do not override these explicit journeys.
