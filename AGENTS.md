# Solakit: contributor and agent guide

Solakit is Sola's small, single-operator personal assistant runtime. It talks to
one pinned OpenAI-compatible local model (currently oMLX), uses an existing
OpenViking server for external memory, and keeps portable evidence of sessions,
model runs and tool execution. It is a localhost/tailnet application, not a
multi-user service. The moonlight-themed operator UI is part of the same app.

Read `SPEC.md` for behavioral contracts, `README.md` for operator instructions,
`config.example.toml` for configuration, and `docs/plan/README.md` for progress
and release gates. Keep these consistent with code. Current implementation is
not proof that a live integration or production acceptance gate has passed.

## Architecture

- `src/solakit/main.py`: FastAPI app factory/lifespan, shared authentication,
  authenticated `/api` routes, health and startup diagnostics, UI/static mounts.
  `sola serve` runs one uvicorn worker on `127.0.0.1:8382` by default. Model
  requests default to `http://127.0.0.1:8000/v1`.
- `core/config.py`, `core/startup.py`: strict Pydantic/TOML configuration,
  env/file secret references, offline cross-checks, prompt-file and tool validation.
  `sola check` performs offline validation without activating integrations.
- `core/routing.py`, `core/loop.py`: persistent session routing, per-session FIFO,
  global inference semaphore, bounded model/tool execution, cancellation and
  orderly shutdown. User-facing failures are run statuses, not invented replies.
- `core/model.py`: one exact pinned model, preflight and completion validation,
  explicit authentication, bounded generation; no provider fallback.
- `core/prompt.py`: deterministic assembly from operator-owned SOUL/fragments,
  run metadata, scoped recall and tool definitions. Prompt bytes/hashes are
  evidence; the UI must not reconstruct or rewrite them.
- `core/store.py`, `core/schemas.py`, `core/io.py`: immutable JSONL evidence,
  atomic metadata, durable I/O, exclusive data ownership and rebuildable SQLite
  index. `core/compaction.py` and `core/archival.py` rotate immutable archives with
  recovery metadata. Operator archival rejects busy sessions and preserves bindings.
- `core/memory.py`, `core/memory_ops.py`: scope-bound OpenViking REST access,
  recall, explicit commit/extraction tracking, compaction and operator scratch
  verification. Startup only performs authenticated read-only root checks.
- `services/`: internal scope-checked filesystem, alias-bound Git and collection
  resources. `ctx.fs`, `ctx.git(alias)` and `ctx.collection(alias)` let packs
  compose operations privately. Generic tools are thin service wrappers.
- `tools/`: registry/decorator/manifests, generated argument schemas, tool
  implementations and per-profile capability enforcement. `enabled_tools` loads
  modules by explicit dotted path (bare names are built-in shorthand); profile
  `tools` grants exact tool names. Both are necessary. A manifest `profiles=["*"]`
  supports arbitrary configured profiles without granting calls automatically.
  Filesystem
  roots, writes and other capabilities are separate explicit grants.
- `packs/`: independently packaged extensions outside the core package. Journal
  is `packs/journal` / `sola_packs.journal`, not an in-tree domain module. Core
  distributions exclude packs; install and test them separately. Collections are
  named repo subdirectories; `collection:<alias>` grants only that directory.
- `channels/`: API and opt-in Discord adapters through shared session execution.
  `jobs/`: opt-in APScheduler jobs using the same execution/delivery operations.
- `ui/`: Jinja2 pages, packaged local HTMX/CSS/vanilla JS, opaque in-memory login
  sessions, API-only presentation routes and escaped/redacted transcript views.
  No Node build or CDN. Chat fills the viewport with a desktop session sidebar;
  colors follow `prefers-color-scheme`. `/tools` shows loaded versus granted tools.
- `cli.py`: offline checks, server launch, API inspection and explicit operator
  operations. Tests are under `tests/` and use mocked transports/controlled clocks.

## Invariants when changing code

- Preserve evidence: session/model transcript text is verbatim, including tool
  calls/results. Do not silently redact, rewrite or delete history. Diagnostics
  and tool logs are redacted, and UI presentation masks resolved credentials.
  Render tool-card arguments/results from redacted run logs, never raw transcript
  payloads. Escape plain text; markdown rendering remains deferred.
- Domain behavior belongs in external packs. No blog/letters/email/printer
  modules under `src/solakit`, no domain imports or branches in core execution.
  A future tool PR touching `core/loop.py` is a review finding: fix the generic
  service seam instead. No entry-point discovery or auto-loading is needed.
- Git writes check the actual named repository/files. Generic writes require
  exact `repo:<alias>` write grants; collection services enforce directory limits.
  Push additionally requires `git.push`, even inside packs. Do not stage whole
  directories, force-push, rebase implicitly or retry uncertain publish/send work.
- Tools must pass registry/schema/profile/capability checks. Append the assistant
  tool-call envelope before matching result messages. Forbidden/malformed calls
  have a bounded repair budget; tool output is explicitly and countably truncated.
- Model preflight/first-response validation precede transcript mutation. Keep
  unavailable-model behavior, step/token/context budgets and cancellation honest.
  `finish_reason: length` preserves partial evidence but is not a completed reply.
- UI data access goes through API routes, not Store or Execution. Cookie and Bearer
  authentication share authorization. Never put operator/backend credentials in
  pages, browser storage, logs or configuration examples. Keep formatter-compatible
  log argument shapes when redacting (Uvicorn consumes structured arguments).
- Treat model/tool output and external documents as data, not new authorization.
  Keep secrets as explicit env/file references and filesystem grants narrow.
- Keep integrations disabled by default. `live_ready` is an operator assertion,
  not evidence of acceptance. A startup memory connection check does not verify
  extraction, writes, scratch round-trip, or authorize rollout. Do not touch the
  existing Hermes installation or schedules as part of Solakit work.
- Configuration stays files; adding UI controls must not create a parallel config
  or state store. Keep the core small and domain behavior in tool/adaptor modules.

## Development and validation

Python 3.12+ and uv are required. From the repository root:

```sh
uv sync --frozen
uv run sola check
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv build
```

`sola check` needs operator-owned configuration, secrets and referenced prompt
files; automated tests supply isolated fixtures. In the managed workspace use
`UV_CACHE_DIR=/workspace/.cache/uv` if needed. Never copy live credentials/data
into fixtures or artifacts. For local UI use `uv run sola serve`, open port 8382,
and sign in with the operator token (separate from the model API key). Use
`--log-level debug` for sanitized backend diagnostics.

Add meaningful regression checks for behavioral fixes; mocks are not live service
verification. Check packaged templates/assets when changing UI packaging. Report
browser visual QA separately from HTTP tests. Synchronize docs and phase status,
then follow the working agreement in `docs/plan/README.md`: push directly to main
without force. No public deployment is implied by local implementation.

Optional journal pack checks (after the core environment is synced):

```sh
uv pip install --no-deps -e packs/journal
uv run --no-sync pytest packs/journal/tests -q
uv build packs/journal
```

Root `uv sync` removes independently installed packs; production deployments must
include desired pack distributions in their own dependency manifest. Pack code is
operator-trusted Python, not sandboxed code. Services enforce model-call arguments,
not a security boundary against a malicious installed package.

## Scope and deferred work

P1 UI (sessions/chat/status) is implemented, plus read-only tools inspection.
`docs/plan/11-ui-p2-evidence.md` and `12-ui-p3-operations.md` describe deferred
prompt/run inspection and job/channel controls. Operator API/UI archival and
read-only archive browsing are implemented; see `docs/plan/13-session-archival.md`.
Discord archival exposure, markdown, per-message profile selection and SSE remain
deferred. Generic services and the journal pack are locally implemented. The
revised phase 9 roadmap is journal → letters → email → printer, with image/calendar
as later seam proofs; see plans 14–19. Phase 6 live scratch-memory verification and
later acceptance/pilot gates remain distinct from local mock-tested implementation. Check the phase plans
before expanding scope or claiming a phase complete.
