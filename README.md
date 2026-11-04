# Solakit

A small FastAPI runtime for Sola, with a file-defined prompt, pinned model,
profile-enforced tools, external OpenViking memory, and portable session evidence.
Python 3.12+ and uv are required. [SPEC.md](SPEC.md) defines behavior;
[phase plans](docs/plan/README.md) record implementation and release gates.

Phases 1–8 are locally implemented. Phase 6's live OpenViking round-trip is still
pending on the Sola host, and phase 9's domain tools/complete acceptance remain
unfinished. Discord and jobs are disabled by default. No production pilot,
deployment, Hermes changes, or live memory writes have been performed.

## Development

```sh
uv sync --frozen
cp config.example.toml config.toml
# Configure the reference to the existing live SOUL.md and an external data tree.
# Provide an operator-token env reference or a mode-0600 token-file reference.
# Create fragments/profiles/<profile>.md for every profile (empty is valid).
uv run sola check
uv run uvicorn solakit.main:app --host 127.0.0.1 --port 8000
```

`sola check` validates TOML, secret references, prompt files, tools, cron and rollout
settings offline. Errors name files and safe TOML locations without echoing their
contents. Startup performs the same checks. Model unavailability leaves the app
up with degraded health; runs use the exact pinned model or fail without fallback.
Run uvicorn with one worker; a data ownership lock prevents concurrent recovery.
The root `/` is reserved for Alan's UI. `/api` requires
`Authorization: Bearer <operator-token>`.

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

In the managed workspace use `UV_CACHE_DIR=/workspace/.cache/uv`.

## API and execution

Create `POST /api/sessions` with `{"id":"web-main","default_profile":"interactive"}`.
Post `{"message":"Hello"}` to `/api/sessions/web-main/messages`; optional `profile`
selects that run's profile. Unknown/misplaced fields are rejected, not ignored.
Inspect `/api/sessions`, `/api/sessions/{id}`, `/api/runs`, and `/api/runs/{id}`.
Run inspection includes prompt, messages, tool calls, diagnostics, memory and
send evidence. Session inspection lists numbered archives, available through
`GET /api/sessions/{id}/archives/{number}`. The old scaffold routes are retired.

Per-session FIFO workers feed a global inference semaphore (default one). Model
preflight and the first validated completion precede transcript mutation. Tool-call
messages precede their matching results. Malformed arguments/forbidden calls get
a bounded repair budget; max steps ends as `budget_exceeded`. Token usage is summed
only when every response supplies counts; otherwise it remains null.

`POST /api/runs/{id}/cancel` shares one durable operation across concurrent callers.
Disconnecting a cancellation request does not interrupt finalization. Shutdown
stops job admission, finalizes accepted work, drains dispatches, and closes clients.
Interrupted tool groups remain in evidence and are explicitly counted when excluded
from future model context. Unexpected exceptions retain redacted chained tracebacks
in `.diagnostics.jsonl`, with operator-log fallback if persistence fails.

## Prompt sovereignty

SOUL and profile files must exist and be readable UTF-8. `context.md` is optional;
when memory is enabled, `recall-header.md` is required. All are read live without
stripping whitespace or normalizing line endings. Empty-but-present context keeps
its separators; a newline-terminated recall header keeps its newline too.

`GET /api/prompt?profile=interactive&session=inspection` returns exact system bytes,
SHA-256, nullable token count, and enabled tool schemas, without inference, retrieval,
or history writes. Optional `recall`, `at` and `origin` make audits repeatable.
`sola prompt --profile interactive` prints exact UTF-8 bytes; `--json` adds metadata.
Set `SOLA_URL` or `sola --url ...` for the API. Model/memory clients default to
`trust_env = false`; opt into proxy/CA environment settings per service when needed.
The CLI's equivalent is `sola --trust-env ...`. Process environment is untouched.

## Storage and recovery

Runtime data lives outside the code tree. Authoritative JSONL transcripts, run
journals, tool/memory/delivery/diagnostic evidence, job cursors, immutable prompt
files and session metadata survive deletion/rebuild of `app.db`. Metadata publishes
atomically with file/directory fsync. Unfinished runs recover as `interrupted`.

Transcripts and prompt files are private, intentionally verbatim archives, including
user text and tool arguments/results. They are reused as actual conversation history.
Run metadata and diagnostic logs redact secret-key suffixes and resolved credentials;
these deliberately different policies are explicit in SPEC §12.

`GET /api/storage/health` identifies damage and byte offsets; public `/health`
reports degraded storage without file details. Only an unterminated damaged JSONL
tail can be omitted. Before appending past it, immutable recovery metadata records
its path/offset/hash; future reads skip only those exact bytes. Interior corruption
fails closed with HTTP 503. Damaged session metadata is never recreated with default
privileges; healthy sessions remain available. Keep `recovery/` in backups.

Stop the server and preserve damaged evidence before restoring verified files
atomically from a trusted backup, then restart to rebuild indexes. Do not delete
metadata to recreate a session or change recovery hashes to bypass damage reports.

## Memory and compaction

Memory is opt-in, scoped to configured canonical roots. Recall precedes prompt
persistence; failure ends `memory_unavailable` without transcript mutation.
Post-turn commits retain intent and acknowledgment evidence; ambiguous writes are
never automatically replayed. A bounded background observer polls extraction and
records terminal failures/status-unavailable states. Explicit refresh is
`POST /api/runs/{id}/memory/refresh`. Observation updates patch only their own fields
so extraction and delivery do not erase each other's state. Ephemeral sessions make
no memory calls; isolated scope fails closed until supported.

Before a new turn's first inference, oversized context can compact using a completed
OpenViking summary-only archive (self/peer long-term extraction disabled). Original
transcripts rotate to numbered immutable archives. A prepared hash/boundary manifest
recovers interrupted rotations before serving. Whole tool groups and summaries
survive repeated compaction and index rebuild. Damaged history cannot compact.

Until an exact local tokenizer is configured, `context_budget` also caps conservative
serialized UTF-8 request bytes, including prompt, history, task and tools. This is
not reported as measured tokens. If safe compaction is unavailable, the summary
cannot fit, or a tool loop exceeds the guard, the run ends `context_limit`; it never
silently drops history or sends unbounded requests.

Optional `[memory].user` sends the verified server's user hint and checks the
created session URI before appending messages; the Sola guide selects `sola`.
The supplied endpoint/version/root/key reference and deliberate live scratch
procedure are in [first contact](docs/operations/first-contact.md). That guide checks
`openviking-server 0.4.17.1`; the workspace cannot reach the Sola host's loopback or
read its env binding. `sola memory-test --fact ... --query ... --expected ...` writes
a real scratch fact and verifies actual recall plus the next persisted prompt,
not a model's success claim. Never automate this first-contact ceremony.

## Channels and jobs

Discord uses discord.py with author/channel/mention/bot gating, parent-session thread
routing, typing while queued/running, reconnect and observable liveness. Replies stay
in the originating thread. Final delivery uses stored transcript text, with no
announcement duplicate. Silent and scoped regular-file artifact delivery are
supported. Oversized fenced blocks attach intact as UTF-8 text; implicit mentions
are suppressed. Intent/results are durable; uncertain sends are not replayed.
`GET /api/channels` reports status, connection time and last accepted message.

APScheduler 3 supplies wake-ups and cron calculations. TOML defines jobs; JSONL
cursors indexed in SQLite persist next fire, pause, active dispatch and last result.
Runtime callbacks are not pickled into a library job store. Cron-to-channel jobs use
the same session queue, so their task/result appear in the next conversational turn.
Queued jobs count as active and never overlap themselves. Model-down jobs become
`missed` gaps. Downtime misses record a bounded range; `catch_up = true` requires
`polling = true` and runs once. Intentional paused/disabled intervals are not replayed.

`GET /api/jobs` and `POST /api/jobs/{id}/run|pause|resume` provide operations. Tasks
allow only `{date}`, `{time}`, `{timezone}`, `{job_id}`, without attributes, conversions
or format specifications. Use named weekdays in cron (APScheduler Monday=0 semantics).
Channel/session/ephemeral bindings are supported. Maintenance is operator-API only.

Live Discord or enabled jobs require explicit `live_ready = true`, asserting the live
memory check and phase 9 acceptance suite passed. This flag does not perform those
checks. Supply a `discord_token` secret reference and enable integrations only after
both gates, then follow SPEC §15's pilot, shadow publishing and rollback stages.

## Tool extensions

Add a typed decorated function in `solakit.tools.<module>`, enable the module, and
grant its full names in profiles. `read_scopes`/`write_scopes` use `repo:<name>` or
`path:/absolute/root`; network/other rights use separate `capabilities`. No grants
exist by default. Registry checks occur at startup and dispatch; arguments are
strictly validated. `files.read` bounds regular-file reads using nonblocking,
no-follow descriptor checks. `files.write` creates new files only; `repo_git.status`
uses a configured repository name. Domain tools remain phase 9 work.

Counted truncation markers fit within output budgets. Complete outputs report exact
omitted characters; bounded reads report a lower bound. Logs distinguish exact from
observed counts; `content_complete` indicates full output. Tools may return scoped
artifact paths. Sync tools run in worker threads: Python cannot stop an arbitrary
thread, so blocking integrations must bound/cancel their own operations. Plugins
are trusted operator code; path checks are not OS isolation against concurrent
filesystem changes. Nothing self-modifies profiles or imports tools for the model.
