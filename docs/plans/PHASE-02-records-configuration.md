# Phase 2 — Runtime records, lifecycle, and effective configuration

Status: implemented and locally verified, 2026-11-16. See the [runtime contract](../contracts/runtime.md) and [validation evidence](../validation/phase-02.md). Depends on [Phase 0](PHASE-00-product-contracts.md) and [Phase 1](PHASE-01-skeleton-evaluation.md). Unlocks storage, adapters, tools, and transport. Live clients/stores and durable enforcement retain their later-phase gates.

## Outcome

One set of owned records and lifecycle rules serves the core, storage, API, and tools. Configuration explains effective authority without turning the HTTP framework into the application container.

## Implementation slices

1. **Define versioned records.** Specify Session, Message, Run, ModelStep, ToolCall, ToolReceipt, Job, Occurrence, Delivery, MemoryCommit, and CompactionBoundary. Include stable IDs, UTC timestamps, schema version, origin, and explicit foreign-key relationships. Record lineage for scheduled/proactive work. Keep request idempotency IDs distinct from run and provider-call IDs. Tool results retain call IDs even on failure.
2. **Freeze transitions.** Implement transition validation using the table below. Store outcome reason separately from state (for example `budget_exceeded`, `model_unavailable`, or `tool_denied`). Operational activity such as waiting for inference is progress, not a new terminal status. Memory extraction and delivery have separate lifecycles so a completed reply can have pending extraction or uncertain delivery.
3. **Define events.** Durable envelope: version, event ID, run/session IDs, sequence, timestamp, type, payload. Define ordering scope and replay cursor now. Separate durable state-change events from ephemeral token previews. Specify terminal-event idempotency and how consumers obtain a current snapshot. Never require durable persistence of every token.
4. **Load strict configuration.** Parse operator TOML into validated configuration, then resolve an immutable effective profile: model pin, workspace roots, loaded/granted tools, resources, effects, memory mode, fragments, and budgets. Reject unknown settings and invalid types. Empty initialization config can parse, but cannot validate as a runnable profile missing a workspace. Explain every inherited value with its source and redact secret references/values appropriately.
5. **Own resources explicitly.** Build a small application context in a composition function; FastAPI lifespan later calls its start/stop. Own clients, state/search stores, workers, model semaphore, memory observers, subscriptions, and scheduler. Define partial-startup cleanup and bounded shutdown. A data-directory ownership lock rejects a second worker.

| From | Allowed next states | Constraint |
| --- | --- | --- |
| queued | running, cancelled, interrupted, failed | Admission durable before visibility |
| running | completed, failed, cancelled, interrupted | One terminal transition |
| terminal | none | Retry creates a new linked run |

A recovery event may explain a terminal record; it does not rewrite the historical outcome. Queued recovery policy is finalized with admission in Phase 6.

## Deliverables, verification, and exit gate

Deliver `docs/contracts/runtime.md`, event examples, schema fixtures, effective-config output, error taxonomy, and explicit startup/shutdown ownership notes. Validate IDs before they form paths. Keep SDK objects and FastAPI imports out of these records.

Tests cover invalid transitions, duplicate terminal attempts, config inheritance, narrowing, unknown settings, secret-safe explain output, missing roots, second-owner rejection, startup failure cleanup, and repeated start/stop. Use fixed clocks/IDs in fixtures.

Exit when later components can consume these contracts without importing HTTP or external SDK types, and default budgets have documented units, limits, and precedence.
