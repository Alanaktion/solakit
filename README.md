# Solakit

A small FastAPI runtime for Sola. Python 3.12+ and uv are required.
Implementation progress and exit checks are in [docs/plan](docs/plan/README.md).
The full target is [SPEC.md](SPEC.md); integrations are implemented in phases.

## Development

```sh
uv sync --frozen
cp config.example.toml config.toml
# Edit paths, including the reference to the existing live SOUL.md.
# Supply SOLA_OPERATOR_TOKEN through your environment or use a 0600 token file.
uv run uvicorn solakit.main:app --host 127.0.0.1 --port 8000
```

Startup requires valid configuration and an operator secret. The model may be
unavailable: the app stays up and `/health` reports degraded status.
`/health/model` checks the exact pinned model ID, with no fallback.
The root `/` is reserved for Alan's UI. `/api` requires an
`Authorization: Bearer <operator-token>` header. `/api/channels` explicitly
reports disabled or unimplemented adapters. No adapters or jobs run yet.

The scaffold's `/v1/runs` and temporary `POST /api/runs` were retired.
Create a session with `POST /api/sessions` (`{"id":"web-main"}`), then send
`POST /api/sessions/web-main/messages` (`{"message":"Hello"}`). This returns the
completed persistent run record. Optional `profile` selects an operator-granted
profile. The authenticated API may invoke maintenance; channels/jobs may not.

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

In the managed cloud environment, use `UV_CACHE_DIR=/workspace/.cache/uv`.
Runtime data must live outside the code tree. Never commit operator configuration
or secrets. This work does not deploy a pilot or change Hermes.

## Storage and inspection

`GET /api/sessions`, `/api/sessions/{id}`, `/api/runs`, and `/api/runs/{id}`
inspect local evidence. Session metadata and transcripts, run event journals,
exact prompt files, and redacted tool logs are authoritative. `app.db` is rebuilt
at startup; deleting it loses no evidence. Unfinished runs become `interrupted`
on restart, with partial evidence preserved. Each `.json` run snapshot is backed
by append-only `.events.jsonl`. JSONL writes are fsynced. Prompt files preserve
exact UTF-8 bytes, including CRLF; they are intentionally not redacted. Treat the
data directory as private operator material, just like the source persona.

## Prompt audit

Create `fragments/profiles/<profile>.md` for each profile (an empty file is valid).
`fragments/context.md` is optional. Nonempty supplied recall requires
`fragments/recall-header.md`. All files, including the live SOUL reference, are
read anew without stripping whitespace or normalizing line endings.

`GET /api/prompt?profile=interactive&session=inspection` returns exact prompt,
SHA-256, nullable token count, and enabled tool schemas. Optional `recall`, `at`
(timezone-aware ISO timestamp), and `origin` parameters make audits repeatable.
This endpoint performs no inference or memory retrieval and writes no history.
`sola prompt --profile interactive` prints exact UTF-8 bytes; `--json` includes
metadata. Set `SOLA_URL` or `sola --url ...` to select the API server.

## Tool configuration

Set `enabled_tools = ["files", "repo_git"]`, then explicitly grant full tool
names in each profile's `tools` list. `read_scopes` and `write_scopes` accept
`repo:<configured-name>` or `path:/absolute/root`. Network and other non-file
rights belong in `capabilities`. For example, `files.write` requires a write
root and checks each resolved target against it; it only creates new files.
`files.read` bounds reads; `repo_git.status` accepts a configured repository name.
No filesystem roots or capabilities are granted by default.

Add a typed function decorated with `@tool` to `solakit.tools.<module>` and list
that module in configuration. The registry validates grants at startup and
again at dispatch, validates arguments strictly, enforces timeouts, truncates
output visibly, and logs every call. Sync tools run in worker threads. Python
cannot stop a running worker thread on timeout: blocking mutating integrations
must supply their own bounded/cancellable operations. Configured Python modules
are trusted operator code; profile checks constrain exposed operations, not
arbitrary Python imported by the operator. The filesystem is operator-controlled;
resolved-path checks do not provide OS isolation against concurrent path changes.

## Execution and cancellation

One FIFO queue per session feeds the global inference semaphore. Every run checks
the exact pinned model, persists its prompt before inference, and sends only
registered profile tools. No history is appended until the first validated
completion arrives; model unavailability or an incorrect model before that leaves
the session untouched. Partial evidence from later failures remains archived.
Tool call envelopes precede all matching results. Bad JSON, schema violations,
and forbidden calls get at most two repair opportunities; tools never execute
when forbidden. Other tool failures are structured results. Max steps ends a run
as `budget_exceeded`. Endpoint usage is summed only when every response supplies
counts; otherwise tokens remain null.

`POST /api/runs/{id}/cancel` cancels queued/running work; shutdown cancels work and
closes HTTP clients. Interrupted tool groups remain in history but are excluded
from future model requests, with the skipped count in each run record. A data
ownership lock prevents a second server from recovering an active server's runs.
Run uvicorn with one worker. No fallback model is attempted.

Memory is disabled by default. Context compaction is not implemented yet;
runs currently use the full valid transcript. Isolated memory is refused.
Discord and scheduling remain disabled.

## OpenViking (phase 6, in progress)

Opt-in `[memory]` configuration references the existing server URL, canonical
shared-memory URI roots, and optionally an API-key secret reference. The client
uses OpenViking's documented `/api/v1/search/find`, session message batch/commit,
and content-read endpoints. Confirm compatibility with the installed version
before enabling; no local or production instance was available for validation.

Pre-turn recall happens before final prompt construction and persistence. Recall
failure ends as `memory_unavailable` without transcript mutation. Post-turn commit
failure preserves the conversation and sets `memory_status` to
`commit_failed_or_uncertain`. Every commit uses a stable run-owned remote session,
with durable intent recorded in `<run-id>.memory.jsonl`. Acknowledged retries reuse
the recorded result. Ambiguous writes are never replayed automatically, because
the documented API provides no message-append idempotency key. Reconcile those
remote sessions before any manual recovery. This favors a visible gap over
silently duplicating long-term memories.

`accepted` means OpenViking accepted the archive; extraction still runs in the
background. Run records expose its task ID and archive URI. Completion polling,
operator recovery, context compaction with archived transcript rotation and
summary references, and validation against the existing instance remain open.
Ephemeral sessions make no memory calls; isolated scope fails closed. The
`memory.search` and `memory.read` tools use a session-bound facade and enforce
configured canonical URI roots. Prompt inspection still makes no memory calls.

Current implementation: phases 1–5 complete; phase 6 partial; phases 7–9 planned.
No Discord pilot, production configuration, deployment, or Hermes changes were made.
