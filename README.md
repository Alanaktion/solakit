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

Run `uv run sola check` before starting the server. It checks configuration,
secret references, prompt files, and tool grants offline, with concise errors.
Startup performs the same checks before allocating runtime services. Uvicorn
still includes its lifespan traceback when startup fails; the final
`ConfigurationError` identifies the invalid field or missing file.

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
Session transcripts are also intentionally verbatim, including user messages,
assistant tool-call arguments and tool-result messages. They are reused unchanged
as conversation history. Redaction applies to diagnostic records, not those
archives; see SPEC §12 for the explicit distinction.

Session metadata is published with an atomic rename after the file is fsynced;
the containing directory is fsynced too. Abandoned temporary files are ignored.
If existing metadata is damaged, that session is unavailable rather than being
recreated with default privileges or memory scope. Healthy sessions remain usable.

`GET /api/storage/health` (authenticated) lists damaged files, issue types, and
byte offsets. `/health` reports degraded storage without exposing file details.
Only a malformed, unterminated final JSONL fragment may be omitted during recovery.
Before appending past it, the store persists a record in `recovery/` containing its
path, offset, and SHA-256. Future reads skip only those exact damaged bytes.
Interior corruption without matching recovery evidence fails closed with HTTP 503.
Original transcript and journal bytes remain intact, and recovered tails remain
visible in storage health. Keep `recovery/` alongside transcripts in every backup.

For damaged metadata or interior records, stop the server, preserve a copy of the
damaged files, and atomically restore verified files from a trusted backup. Do
not delete metadata and recreate a session: its memory scope and binding matter.
Restart to rebuild the index and refresh storage health. A recovery record is
not permission to discard other corrupt records; do not edit its hash to bypass
a corruption report. Transcripts are not silently repaired or shortened.

Unexpected execution failures keep a concise public `error` and retain a redacted
exception type, traceback, and chained cause in `<run-id>.diagnostics.jsonl`.
Authenticated run inspection includes these `diagnostics`. If diagnostic storage
fails, the original redacted diagnostic is emitted to operator logs instead.

## Prompt audit

Create `fragments/profiles/<profile>.md` for each profile (an empty file is valid).
`fragments/context.md` is optional. Startup validates the SOUL and all profile
files, even for profiles not yet used. When memory is enabled it also requires
`fragments/recall-header.md`; supplied recall in dry-run inspection requires it
as well. All files, including the live SOUL reference, are
read anew without stripping whitespace or normalizing line endings. An empty
context file remains a fragment with separators; a newline-terminated recall
header keeps that newline before the builder adds another newline for the block.

`GET /api/prompt?profile=interactive&session=inspection` returns exact prompt,
SHA-256, nullable token count, and enabled tool schemas. Optional `recall`, `at`
(timezone-aware ISO timestamp), and `origin` parameters make audits repeatable.
This endpoint performs no inference or memory retrieval and writes no history.
`sola prompt --profile interactive` prints exact UTF-8 bytes; `--json` includes
metadata. Set `SOLA_URL` or `sola --url ...` to select the API server.

Model and memory HTTP clients default to `trust_env = false`: local services do
not inherit proxy or certificate-environment settings accidentally. Set
`[model].trust_env = true` or `[memory].trust_env = true` when that service needs
your managed proxy or environment-provided CA trust. The CLI likewise defaults
to explicit direct API access; use `sola --trust-env ...` to opt in. These settings
do not change the process environment or other tools' networking.

## Tool configuration

Set `enabled_tools = ["files", "repo_git"]`, then explicitly grant full tool
names in each profile's `tools` list. `read_scopes` and `write_scopes` accept
`repo:<configured-name>` or `path:/absolute/root`. Network and other non-file
rights belong in `capabilities`. For example, `files.write` requires a write
root and checks each resolved target against it; it only creates new files.
`files.read` bounds reads and accepts only regular files. It opens paths with
nonblocking/no-follow flags and checks the opened descriptor, so a FIFO cannot
hold a run or shutdown waiting for a writer. `repo_git.status` accepts a configured
repository name.
No filesystem roots or capabilities are granted by default.

Add a typed function decorated with `@tool` to `solakit.tools.<module>` and list
that module in configuration. The registry validates grants at startup and
again at dispatch, validates arguments strictly, enforces timeouts, truncates
output visibly, and logs every call. Complete outputs use a counted marker such
as `[truncated 18463 chars]`. Bounded file reads use a truthful lower bound such
as `[truncated >=27 chars]` and do not read the entire file just to count its tail.
The marker fits within the configured character budget. Logs distinguish exact
`output_chars`/`omitted_chars` from `output_observed_chars`/`omitted_chars_min`, and
results carry `content_complete` to indicate whether the full output was returned.
Sync tools run in worker threads. Python cannot stop a running worker thread on timeout: blocking mutating integrations
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

`POST /api/runs/{id}/cancel` cancels queued/running work. Concurrent cancellation
requests share one operation; disconnecting a cancellation request does not
interrupt finalization. Shutdown waits for submissions already being accepted,
stops admission, finalizes accepted work, then closes workers and HTTP clients.
Concurrent/repeated shutdown calls share one operation. Interrupted tool groups remain in history but are excluded
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
