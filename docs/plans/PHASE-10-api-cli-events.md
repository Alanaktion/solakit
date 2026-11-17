# Phase 10 — API, authentication, CLI, and event transport

Status: implemented. Deterministic checks and the live pinned-Qwen HTTP gate are
recorded in [Phase 10 evidence](../validation/phase-10.md). Operator acceptance,
remote TLS deployment and browser usability remain pending. Depends on
[Phases 2–9](README.md). Unlocks the web client. No Android route/payload
compatibility work is required. Read the [API contract](../contracts/api.md),
[event protocol](../contracts/events.md), [OpenAPI fixture](../contracts/examples/openapi.json)
and [CLI documentation](../cli.md).

## Outcome

A clean versioned HTTP interface exposes core services without owning runtime behavior. Disconnects and wait timeouts do not stop durable work.

## Implementation slices

1. **Publish the API contract.** Define `/api/v1` resources for sessions/messages, run submission/inspection/cancellation, jobs, memory status/reconciliation, tools/profiles, health, search, and prompt inspection. Specify pagination, limits, UTC timestamps, structured errors, validation, conflicts, and idempotency. Large prompt/tool artifacts use authorized bounded retrieval. Route handlers validate, call one application service, and map its result; no duplicate permission or queue logic.
2. **Compose the app explicitly.** `create_app` wires the small context and lifespan; routers receive that context without hidden dependency graphs. API requests never instantiate their own model clients/semaphores or scheduler. Enforce one worker/data owner and close resources on partial startup/shutdown.
3. **Implement persistent operator authentication.** Store hashed login-session tokens and expiration/revocation in state.db; keep credentials out of URLs and logs. Support secure cookie and explicit bearer paths, login throttling, logout/revocation, and documented binding/TLS assumptions. Persist login sessions across restart without exposing raw tokens in backups/reports.
4. **Enforce state-changing request checks.** Cookie-auth mutations require CSRF protection plus explicit allowed-origin validation; SameSite alone is insufficient for sibling origins. Missing/foreign/null origins and forwarded headers have documented behavior. Bearer-only clients are exempt from browser CSRF checks only when no ambient cookie authenticates the request; define mixed cookie/bearer precedence and reject ambiguity. CORS is not authentication.
5. **Expose admission and events.** Submission returns the durable run receipt; optional wait returns completed state or a pending receipt on timeout. Enforce idempotency key/payload conflicts. SSE starts with a bounded snapshot, then durable event replay and transient previews. Scope sequence/cursors, authorization, retention gaps, reset-to-snapshot, heartbeat, buffer caps, and disconnect cleanup. A reconnect can recover durable state, not unrecorded tokens; clients replace previews from current state.
6. **Complete the four-command CLI.** `serve`, offline/live-check options under `check`, `prompt`, and offline `search`. `check --explain` reports effective authority with provenance. Live probes/evaluations/index rebuild/migrations stay developer scripts. Do not start background tasks for offline commands.

## Verification and exit gate

Deliver OpenAPI/contract fixtures, auth/origin matrix, event protocol, CLI documentation, and tests. Cover absent/expired/revoked cookies, bearer clients, mixed auth, missing origins, sibling-origin mutation, hostile Host/forwarded headers, secret-safe failures, idempotent retry, and bounded artifact retrieval.

With the actual in-process runtime, disconnect during a long request and verify completion continues. Reconnect after preview loss and durable replay gaps without duplicating messages or terminal events. Slow clients must not block inference or accumulate unbounded queues. Test pagination separately from live snapshots.

Exit requires API tests through shared services, persistent auth across restart, all four CLI commands, and headless end-to-end runs over HTTP. Browser usability is a Phase 11 gate; legacy Android responses are not a release requirement.
