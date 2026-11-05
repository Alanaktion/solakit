# Solakit

A small FastAPI runtime for Sola, with a file-defined prompt, pinned model,
profile-enforced tools, external OpenViking memory, and portable session evidence.
Python 3.12+ and uv are required. [SPEC.md](SPEC.md) defines behavior;
[phase plans](docs/plan/README.md) record implementation and release gates.
[AGENTS.md](AGENTS.md) maps the architecture and contribution rules.

Core execution, memory/compaction, Discord/scheduling, operator UI, generic
services, journal/letters/email packs and built-in shell tools are implemented.
The operator has confirmed the live OpenViking round-trip and general functionality.
The [implementation audit](docs/audits/implementation-status.md) distinguishes
regression coverage from remaining acceptance, browser, provider and rollout checks.
The optional 58mm USB/serial ESC/POS printer pack is capture-tested; physical output
remains unverified. Discord/jobs are disabled by default; this repository
work has not activated a production pilot, deployment or changed Hermes.

## Development

```sh
uv sync --frozen
cp config.example.toml config.toml
# Configure the reference to the existing live SOUL.md and an external data tree.
# Provide an operator-token env reference or a mode-0600 token-file reference.
# Create fragments/profiles/<profile>.md for every profile (empty is valid).
uv run sola check
uv run sola serve
```

`sola serve` launches uvicorn at `127.0.0.1:8382` by default (ASCII decimal
`S=83`, `R=82`, sola-ryu); oMLX defaults to `http://127.0.0.1:8000/v1`.
`[server]` configures `host`, `port`, and `log_level`; CLI flags override them:

```sh
uv run sola serve --log-level debug
# Direct uvicorn invocation must specify the port; its own default is still 8000.
uv run uvicorn solakit.main:app --host 127.0.0.1 --port 8382
```

The API CLI defaults to `http://127.0.0.1:8382`; use `SOLA_URL` or `--url` when
serving at another address. For direct uvicorn launches, set `[server].log_level`
to `debug` to enable Solakit's backend diagnostics. The project's serve command
sets both application and uvicorn levels together. Existing `config.toml` values
override the new defaults; update an old model URL explicitly to port 8000.

`sola check` validates TOML, secret references, prompt files, tools, cron and rollout
settings offline. Errors name files and safe TOML locations without echoing their
contents. Startup performs the same checks. Model unavailability leaves the app
up with degraded health; runs use the exact pinned model or fail without fallback.
Run uvicorn with one worker; a data ownership lock prevents concurrent recovery.
Startup logs summarize model readiness, authenticated read-only access to every
configured memory root, loaded tool modules/names, effective profile grants,
storage status and configured integration counts. Memory failures warn without
preventing startup. These checks do not write memory or verify extraction.
`/health` and its UI chips report the memory startup result and check timestamp,
not a continuous connectivity guarantee.
Open `http://127.0.0.1:8382/` for the moonlight-themed operator UI. Sign in with
**the operator token**, separately from the model API key. The compact UI takes
its silver/violet palette and soft ambient details from
[Sola’s dreams](https://sola-ryu.github.io/dreams/), with pale lavender light mode
and a near-black dark mode. It supports system light/dark preference, session
creation, sanitized assistant Markdown, collapsed tool evidence,
and reported health. The Tools page lists loaded tools, argument schemas, write
requirements, and effective profile grants. Chat links to its profile’s tools
and shows calls as expandable redacted argument/result cards. Chat fills the viewport with a desktop session sidebar,
compact navigation, Markdown assistant replies, user bubbles, and a bottom-anchored composer; on mobile, the conversation takes
the full width with a back link to sessions. Sessions use their configured default
profile for chat. Session management uses compact rows and a collapsible creation
form; evidence and operations views share the denser spacing.
The Prompt inspector shows API-assembled text, its original SHA and tool schemas.
Runs lists recorded outcomes and opens stored prompts, messages, redacted tools,
diagnostics, memory/delivery events and cancellation for active runs. Operations
shows adapter/job state and job run/pause/resume controls; it does not edit config.
Press Enter to send in chat, Shift+Enter for a newline (IME composition is preserved).

Login exchanges the token for an opaque, HttpOnly, SameSite=Lax cookie valid for
seven days (Secure on HTTPS). Sign out revokes it; server restarts invalidate all
UI sessions. Five failed attempts lock login for 60 seconds. `/api` accepts either
this cookie or the existing `Authorization: Bearer <operator-token>` header.
The token is never placed in page content or browser storage.

Chat streams assistant text as an explicitly unsaved preview, then replaces it
with the stored turn and redacted tool evidence. The composer stays disabled with
elapsed time while queued/running. Reloading resumes observation; disconnecting
does not cancel accepted work or resubmit it. Check recorded state before retrying
a dropped connection. Cancellation is available from the run evidence page.

Assistant messages support headings, lists, quotes, links, code fences and tables.
Raw HTML and images are disabled and rendered HTML is sanitized. User text, tool
logs, summaries and prompt/run evidence remain literal; original messages and hashes
are unchanged. Live output masks full credentials and unfinished secret prefixes.
All templates, CSS, JavaScript and vendored HTMX ship locally; Markdown is rendered
by Python (`markdown-it-py` plus `nh3`), without a Node build, CDN or hosting service.

API messages still wait for completion by default. `?wait=false` returns HTTP 202
with the accepted run ID; `?stream=true` requests OpenAI token streaming from the
model. `/api/runs/{id}?live=true` adds a credential-masked transient preview.
The web UI observes authenticated `/sessions/{id}/events` SSE HTML snapshots,
coalesced to at most five updates per second. Backends must support OpenAI SSE,
including `stream_options.include_usage`; unsupported/malformed streams fail
explicitly, with no automatic fallback/replay. See [remaining streaming checks](docs/plan/22-ui-markdown-streaming.md).

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
Inspect `/api/tools` for loaded tools and effective profile grants.
Inspect `/api/sessions`, `/api/sessions/{id}`, `/api/runs`, and `/api/runs/{id}`.
Run inspection includes prompt, messages, tool calls, diagnostics, memory and
send evidence. Session inspection lists numbered archives, available through
`GET /api/sessions/{id}/archives/{number}`. The old scaffold routes are retired.

`POST /api/sessions/{id}/archive` saves the current transcript byte-for-byte in the
next numbered archive and starts a fresh transcript in the **same session**.
Its profile, memory scope and channel binding stay unchanged. The next turn uses
the normal prompt/recall, without the old transcript; existing memory is retained,
and archiving makes no new memory/model requests. Empty transcripts are a no-op.
Queued/running turns return HTTP 409: wait for them to finish first.
In chat, choose **Archive conversation**, review the explanation and confirm.
The history picker switches between the current conversation and read-only
archives, including compaction archives. Nothing is deleted or editable.
A failed prepared rotation blocks further transcript writes; restart after fixing
storage to finish recovery. See [archival validation](docs/plan/13-session-archival.md).

Per-session FIFO workers feed a global inference semaphore (default one). Model
preflight and the first validated completion precede transcript mutation. Tool-call
messages precede their matching results. Malformed arguments/forbidden calls get
a bounded repair budget; max steps ends as `budget_exceeded`.
Every completion sends `[model].max_tokens` (default 4096). This is a per-response
output cap, separate from the profile's step limit and input context budget.
A backend `finish_reason: "length"` preserves partial output as evidence but ends
as `budget_exceeded` with an explicit error, without retries or tool execution.
Tool-free profiles send `tool_choice: "none"` and omit tool schemas. Empty
`enabled_tools` loads no plugins; individual profiles still determine tool grants.
A long repetitive assistant message with no `tool_calls` is a single model
response, not a registry loop. Check the run's steps and tool evidence to tell
these apart. If reasoning appears in `content`, check the backend's model/chat
format settings; the runtime preserves returned text rather than silently
rewriting it. Backends must honor the requested output cap.
Token usage is summed
only when every response supplies counts; otherwise it remains null.

`POST /api/runs/{id}/cancel` shares one durable operation across concurrent callers.
Disconnecting a cancellation request does not interrupt finalization. Shutdown
stops job admission, finalizes accepted work, drains dispatches, and closes clients.
Interrupted tool groups remain in evidence and are explicitly counted when excluded
from future model context. Unexpected exceptions retain redacted chained tracebacks
in `.diagnostics.jsonl`, with operator-log fallback if persistence fails.

## Model authentication and diagnostics

Optional `[model].api_key` references an environment variable or a mode-0600 file:

```toml
[model]
base_url = "http://127.0.0.1:8000/v1"
api_key = { env = "OPENAI_API_KEY" }
```

Both `/models` preflight and `/chat/completions` send `Authorization: Bearer ...`.
Leave the reference out for an unauthenticated backend. Credentials are resolved
at startup, registered for redaction, and never read implicitly from a provider
environment variable. Restart after rotating the credential. `sola check` reports
missing/empty/invalid references before the server allocates services.

Warnings identify connection failures, timeouts, HTTP status (including 401/403
authentication hints), and invalid/wrong-model replies. Debug logs add the sanitized
endpoint, pinned model, authentication-enabled flag, proxy-environment setting,
timeouts, transport error details and elapsed time. They omit headers, tokens,
request messages and response bodies. Dependency wire-level debug is not enabled.
If the backend is unreachable, verify it listens at port 8000 and that `base_url`
includes `/v1`; inspect `trust_env` if a proxy is involved. A wrong endpoint or
credential still fails closed; diagnostic logging never enables a fallback.

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

Compaction is automatic before a turn when its request exceeds the profile's
serialized-byte `context_budget`. Set `[profiles.<name>].mid_run_compaction = true`
to also compact **between model steps**, after all matching tool results are
stored. Default is false. The original task is carried forward as a request-only
reminder; step/repair/token accounting continues. Requests that still do not fit
end as `context_limit`, without an oversized model call.

`POST /api/sessions/{id}/compact` explicitly summarizes an idle shared-memory
session even below the budget, independently of this flag. In chat choose
**Compact conversation**, then **Summarize and continue**. OpenViking must be
enabled; queued/running turns cause HTTP 409. Original bytes remain in numbered
archives, while active context starts with the summary. Session/channel settings
stay unchanged. Unlike manual archival, compaction retains summarized context.
Cancellation waits for accepted compaction work and its rotation bookkeeping;
unresolved prepared rotations require recovery before further transcript writes.
Summary markers precede subsequent messages in the UI, rather than following the
earlier run-admission timestamp. Mid-run current-turn messages are still committed
to long-term memory in full from archive evidence, not just the active tail.

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
created session URI before appending messages; it must match the server identity.
The deliberate live scratch procedure is in
[first contact](docs/operations/first-contact.md). Current local configuration and
verified server checks are recorded in the [local integration audit](docs/audits/local-integration.md).
`sola memory-test --fact ... --query ... --expected ...` writes
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

Discord channels and jobs activate through their individual `enabled` settings.
Discord also requires a `discord_token` secret reference. Integrations stay disabled
by default. Validate live memory and phase 9 acceptance before rollout, then follow
SPEC §15's pilot, shadow publishing and rollback stages.

## Tool extensions

Add a typed decorated function in `solakit.tools.<module>`, enable the module, and
grant its full names in profiles. `read_scopes`/`write_scopes` use `repo:<name>` or
`path:/absolute/root`; network/other rights use separate `capabilities`. No grants
exist by default. Registry checks occur at startup and dispatch; arguments are
strictly validated. `files.read` bounds regular-file reads using nonblocking,
no-follow descriptor checks. `files.write` creates new files only; `repo_git.status`
uses a configured repository name. Domain conventions now live in separate packs;
journal, letters and email packs plus built-in shell tools are available; the
[thermal printer pack](packs/printer/README.md) supports USB/serial ESC/POS.

Counted truncation markers fit within output budgets. Complete outputs report exact
omitted characters; bounded reads report a lower bound. Logs distinguish exact from
observed counts; `content_complete` indicates full output. Tools may return scoped
artifact paths. Sync tools run in worker threads: Python cannot stop an arbitrary
thread, so blocking integrations must bound/cancel their own operations. Plugins
are trusted operator code; path checks are not OS isolation against concurrent
filesystem changes. Nothing self-modifies profiles or imports tools for the model.

## Tool packs and named collections

`enabled_tools` accepts full Python module paths, e.g. `solakit.tools.files`,
`solakit.tools.repo_git`, `solakit.tools.runtime`, and installed `sola_packs.journal`.
Bare built-in names (`files`, `repo_git`, `runtime`, `memory`) remain shorthand.
Explicitly installed/imported packs are trusted Python; no automatic discovery.
Profile `tools` still grants exact tool names; `profiles=["*"]` in a manifest only
allows that tool to be granted to custom profiles. No tools are enabled by default.

Generic additions: `files.edit` (unique exact replacement), `files.append`
(existing regular file), `files.list`; `repo_git.diff`, `repo_git.log`,
`repo_git.pull` (clean fast-forward), and `repo_git.commit_push` (named files only).
Git writes require the actual `repo:<alias>` write scope, and push additionally
requires `git.push`. Existing index changes or unpublished commits are refused.
Failed push preserves the local commit and requires inspection before retrying.
`runtime.status` returns live model availability, channel/job state including next
fire times, and the calling profile's actual granted tools.

Packs compose Python services (`ctx.fs`, `ctx.git(alias)`, `ctx.collection(alias)`)
inside a single tool call. Core execution is unchanged. Generic service code
lives in `services/`; domain conventions live in separately installed packs.

Collections reference the existing repository map:

```toml
[repositories]
site = "/absolute/path/to/site"

[collections.dreams]
repository = "site"
directory = "_dreams"
filename = "{date}-{slug}.md"
```

A profile can grant `collection:dreams` read/write scopes without granting the
entire repository. Layout strings are optional data; the journal pack owns default
front matter. A blog is simply another collection.

[Journal pack installation and use](packs/journal/README.md) documents its separate
distribution, create/list/publish tools, and scratch-only contract tests. Core
installation does not install any pack. [The revised phase 9 roadmap](docs/plan/09-domain-tools-verification.md)
keeps sequential immutable letters, mailbox replies and device conventions in
their own thin packs, in that order, with production integrations still gated.

## Letters/email packs and built-in shell

[Optional pack setup](packs/README.md) covers independent installation and the
[pack-owned resource sidecar](packs/config.example.toml). Set `SOLA_PACKS_CONFIG`
for letters/mailboxes; core config only loads modules and grants tools/scopes.

- [Letters](packs/letters/README.md): pull/read numbered peer letters and create
  one immutable, sequential reply per peer letter; existing receipts prevent
  replay after uncertain creation/publication. No edits to past letters.
- [Email](packs/email/README.md): verified TLS search/read, stable UIDVALIDITY/UID
  identities, external-content markers, and receipted sender-only threaded replies.
  The inbox profile needs no filesystem write scopes. SMTP acceptance is reported
  separately from recipient delivery; uncertain sends are not auto-replayed.
- [Shell](docs/operations/shell.md): enabled by config literal argv and `/bin/sh` tools,
  explicit `shell.exec` grant, finite timeouts, bounded output and process-group
  cleanup. Commands run with host-user privileges; cwd scopes are not a sandbox.

No core execution-loop changes or new core domain schemas were needed. These
features are scratch/mock verified; no live letters, email or jobs were activated.
