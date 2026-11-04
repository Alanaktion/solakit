# Phase 1: Configuration, profiles, and application lifecycle

Status: complete

Depends on: none

- Add validated TOML configuration for model, paths, profiles, channels, repositories, and jobs.
- Define shared session, message, run, origin, delivery, and status schemas. Include `memory_scope` and `parent_session` now.
- Add operator-token authentication, secret references, and centralized log redaction.
- Establish FastAPI lifespan wiring and `/api` routing, reserving `/` for Alan’s UI.
- Keep unavailable integrations disabled and report their status explicitly.

**Exit check:** Invalid configuration fails clearly; protected routes require authentication; the app starts without a reachable model and reports degraded health.

## Completion record

Implemented strict TOML models, secret references, shared records, authenticated /api,
lifespan wiring, exact model health probe, and explicit adapter status.
Validated with 7 passing tests, Ruff lint and format checks. Integrations remain
unimplemented and disabled; execution still uses the temporary injected-agent seam.
