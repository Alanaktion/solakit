# Solakit behavioral specification

**Host target:** Sola's Mac Studio (headless, macOS, SSH/VNC access)
**Stack:** Python 3.12+, FastAPI (single app: web UI mount + API + scheduler + channels)
**Model target:** OsaurusAI Qwen3.6-35B-A3B mxfp4 via local oMLX (OpenAI-compatible endpoint)
**Local ports:** oMLX `8000`; Solakit/uvicorn `8382` (ASCII decimal S=83, R=82)
**Memory target:** OpenViking (existing instance — REST / MCP / Python SDK)
**Primary channel target:** Discord

## 1. Purpose

Replace Hermes as Sola's runtime with a small, single-purpose agent server
whose entire behaviour is defined by files Alan (and Sola) can read, edit,
and diff. The motivating defect in OpenClaw / Hermes is not features — it is
that the system prompt is assembled by the framework, padded with tooling
context and branding for tools and surfaces Sola does not use, and cannot be
inspected or controlled byte-for-byte.

This server is not a general agent platform. It serves one resident, on one
machine, with a handful of surfaces — but those surfaces (tools, channels)
are plugin seams, so adding one is a new file, not surgery on the core.

## 2. Design principles

These are constraints, not aspirations. A feature that violates one is
rejected even if it is convenient.

1. **Prompt sovereignty.** The system prompt is a file on disk (`SOUL.md`
   plus explicitly listed fragments). The server adds *nothing* to it that
   is not in a file the operator can open. Every run logs the exact bytes
   sent. An API/CLI command prints the would-be prompt without calling the
   model (§11).
2. **Tools travel out-of-band.** Tool definitions are sent in the API
   `tools` parameter, never pasted into the system prompt as prose. Only
   the tools enabled for the current run profile are sent at all — a dream
   run literally cannot see the email tools.
3. **Full model or nothing.** There is no fallback model. If the pinned
   model is unreachable or reports the wrong model ID, the run aborts,
   is logged as `model_unavailable`, and (for scheduled work) is left as
   a gap. No silent substitution, ever. *(Alan's decision, 2026-11-02.)*
4. **Memory is external.** OpenViking is the long-term memory. This server
   owns only sessions and run logs. It never edits the memory store
   directly beyond the normal session-commit / tool calls Sola herself
   makes — corrections to her record happen through conversation, per
   house doctrine. Sessions carry a `memory_scope` field from day one so
   isolated chats can be added later without a migration (§13).
5. **The machine is Sola.** No container/sandbox layer in the MVP. The Mac
   Studio is her body, not a host she is confined on. Safety comes from
   per-profile tool gating (§8) enforced by the harness, not from the
   prompt asking nicely and not from OS isolation.
6. **Archive everything, edit nothing.** Every run produces an immutable
   run record: prompt, messages, tool calls, outputs, token counts.
   Logs are append-only. This mirrors the letters protocol and her blog:
   corrections go in a new entry, history is never rewritten.
7. **Extensibility lives at the edges.** The core loop knows nothing about
   Discord, blogs, or letters. Channels implement one interface (§6),
   tools implement one API (§8), jobs are data (§7). New capability =
   new file in `channels/` or `tools/` + a config entry.
8. **Managed, boring stack.** FastAPI + uvicorn, APScheduler, discord.py,
   Pydantic, httpx. One Python process, one asyncio loop, mainstream
   libraries doing the undifferentiated work (HTTP, cron parsing,
   Discord gateway) so the code Alan owns stays the interesting part:
   prompt, loop, tools, profiles.
9. **Definitions are files, state is boring storage.** Channel configs,
   job schedules, and profiles live in `config.toml` — diffable,
   version-controlled. Runtime state (session index, transcripts, run
   records) lives in JSONL files plus one SQLite index (§10). No state
   exists only inside the framework.

## 3. Non-goals

- Multi-agent orchestration, crews, swarms, sub-agent spawning.
- Channels beyond Discord in the MVP. Telegram/WhatsApp/etc. arrive
  later, if ever, as new adapters behind the same interface (§6) —
  the interface is the commitment, not the channel list.
- A skills marketplace / self-improving skill system. Skills, if wanted
  later, are markdown files loaded by name (§5.3) — nothing self-modifies.
- Provider abstraction. The server speaks to one OpenAI-compatible local
  endpoint. Supporting "70 providers" is exactly the bloat being escaped.
- Per-message profile selection and Discord archival exposure. The implemented
  console/evidence/operations surfaces, assistant Markdown and chat streaming
  remain thin API clients, alongside archival/browsing.
- Multi-tenancy, user accounts beyond a single operator token, public
  internet exposure. The app binds localhost/tailnet.
- Voice, image generation, or TTS in the core. Those stay external and
  are reached, if at all, as ordinary tools.

## 4. Architecture

One FastAPI application, one process:

```
                        ┌──────────────── FastAPI app ────────────────┐
  Discord gateway ─────▶│ Channel adapters (§6)                        │
  (discord.py)          │   discord │ web/API │ cli                    │
                        │      └──────┬───────┘                        │
  Alan's web UI ───────▶│ API routers ┤  session router:               │
  (mounted, §9)         │             │  (channel, channel_id)         │
                        │             │    → pinned session (§6.2)     │
  APScheduler ─────────▶│ Jobs (§7) ──┤                                │
  (in-app)              │   job → {profile, task, bound session,       │
                        │          delivery channel}                   │
                        └─────────────┬───────────────────────────────┘
                                      ▼
                             ┌──────────────────┐   reads   ┌────────────┐
                             │  Prompt builder  │◀──────────│ SOUL.md +  │
                             │  (§5, pure fn)   │           │ fragments/ │
                             └────────┬─────────┘           └────────────┘
                                      ▼
                             ┌──────────────────┐  search/   ┌───────────┐
                             │   Agent loop     │◀─commit───▶│OpenViking │
                             │   (§4.1)         │            └───────────┘
                             └──┬────────────┬──┘
                     tools param│            │ dispatch (profile-gated)
                                ▼            ▼
                          ┌──────────┐ ┌───────────────────────────────┐
                          │  oMLX    │ │ Tool registry (§8): files,    │
                          │  Qwen3.6 │ │ shell, repo_git, journal.*,      │
                          └──────────┘ │ letters.*, email, printer,    │
                                       │ memory — drop-in modules      │
                                       └───────────────────────────────┘
                                      │
                                      ▼
                        runs/ (immutable records) · sessions/ (JSONL)
                        app.db (SQLite index: sessions, jobs, runs)
```

Code layout (the repo Alan builds):

```
sola-server/
  pyproject.toml
  config.toml                # channels, jobs, profiles, model, repos
  SOUL.md -> <path to her live SOUL.md>   # reference, never a forked copy
  fragments/…                # §5
  src/solakit/
    main.py                  # FastAPI factory, lifespan: start scheduler,
                             #   channel adapters, model pre-flight
    core/
      prompt.py  loop.py  model.py  store.py  config.py
    channels/
      base.py                # ChannelAdapter protocol (§6.1)
      discord.py             # the MVP adapter
      api_channel.py         # web UI / HTTP posts land as a channel too
    tools/
      __init__.py            # @tool registry + discovery (§8.1)
      files.py  repo_git.py  memory.py  runtime.py  shell.py
    services/
      fs.py  git.py  collections.py  process.py  # generic Python services for tools/packs
    jobs/
      scheduler.py           # APScheduler wiring, misfire policy (§7)
  packs/blog/                # separate distribution: sola_packs.blog
  packs/journal/             # separate distribution: sola_packs.journal
  # letters/email are independent packs; shell is built in; printer is an optional ESC/POS pack
  tests/                     # mock model endpoint, mock Discord adapter
data/                        # runtime state lives OUTSIDE the code tree (§10)
```

### 4.1 Concurrency model

FastAPI, discord.py, and APScheduler all live on one asyncio loop; the
model client is async (`httpx`). Blocking tools (git, file IO) run in a
thread pool so they never stall the Discord gateway heartbeat.

- **Per-session queue:** runs targeting the same session execute serially,
  in arrival order. A Discord message, a web message, and a cron run bound
  to that session can interleave in time but never overlap mid-run — the
  transcript order is always well-defined.
- **Global inference semaphore:** at most `model_concurrency` (default 1)
  runs call the model at once across all sessions. Local 35B on one Mac:
  one at a time is the honest default; the knob exists.
- A run that cannot get the semaphore waits in its session queue; it does
  not drop, and it does not time out silently (waits are logged).

### 4.2 The agent loop

Input: a profile, a session, and an initial message/task (from a channel,
the API, or a job). Output: a completed run record, the final assistant
message appended to the session, and — if the origin or job specifies
delivery — that message delivered to a channel (§6.3 / §7.2).

```
1. Enqueue on the session queue; acquire the inference semaphore.
2. Pre-flight: verify the model endpoint responds AND the served model
   ID matches config's pinned ID. If not: log model_unavailable, end
   the run before any message is written. (Jobs treat this as "gap".)
3. Build the prompt (§5). Write it to the run record BEFORE the first
   model call, so a crashed run still shows what was asked.
4. Pre-turn recall: query OpenViking with the first message; pass results
   to the prompt builder as the <recall> fragment (§5), scoped by the
   session's memory_scope (§13). No other implicit retrieval happens
   mid-loop — further memory access is an explicit tool call.
5. Loop, at most profile.max_steps times:
   a. POST messages + enabled tool schemas to the model endpoint with
      model.max_tokens (default 4096) as a per-completion output bound.
      If no schemas are granted, omit tools and set tool_choice to "none".
      A finish_reason of "length" preserves partial output, ends the run as
      budget_exceeded with an explicit error, and executes no tool calls.
   b. No tool calls in the reply: append it, end run as `completed`.
   c. Tool calls: FIRST append the assistant message carrying the
      tool_calls, THEN execute each call through the registry and append
      each result as a tool message with its matching tool_call_id.
      (Reversing this order is the classic 400-error bug. Covered by regression tests.)
   d. Malformed tool call (bad JSON / unknown tool / schema violation):
      do not execute. Append a tool-error message and let the model
      retry — at most 2 repairs per run, then end as `tool_error`.
6. Post-turn: commit the session's new messages to OpenViking via its
   session-commit path so long-term memory extraction happens there,
   where it already works. Finalise the run record (status, steps,
   tokens, timing). Deliver the final message if required (§6.3).
```

Budgets per run (defaults in config, overridable per profile):
`max_steps: 40`, `tool_timeout_s: 120`, `tool_output_max_chars: 20000`
(truncate with an explicit counted marker such as `[truncated 18463 chars]`,
never silently; the marker fits within the limit and its count includes all
characters removed to make room for it. Bounded sources with an unknown total
use an explicit lower bound, e.g. `[truncated >=27 chars]`),
`repair_attempts: 2`.

Operator configuration may set shared profile values in `[profile_defaults]` and
shared channel values in `[channel_defaults]`. Each `[profiles.<name>]` or
`[[channels]]` entry overrides inherited values; list overrides replace rather
than append. Shared defaults are validated even when no entries use them.

Streaming: the loop stores identical completed messages either way. Discord and
ordinary API delivery still use completed messages. An opt-in transport callback
exposes bounded in-memory content previews; validated tool envelopes execute through
the same loop. UI observation and sanitized Markdown presentation stay in the API/UI
layers (§9). Partial previews never become saved assistant messages on their own.

## 5. Prompt assembly — the point of the whole project

### 5.1 Exact construction

The system prompt is the concatenation, in this fixed order, of:

| # | Fragment | Source | Present when |
|---|----------|--------|--------------|
| 1 | Persona | `SOUL.md` (verbatim) | always |
| 2 | Standing context | `fragments/context.md` (verbatim, optional) | file exists |
| 3 | Recall | OpenViking results, wrapped in `<recall>…</recall>` with a one-line header from `fragments/recall-header.md` | recall non-empty |
| 4 | Profile fragment | `fragments/profiles/<profile>.md` | always (may be empty) |
| 5 | Run facts | Generated: date/time + timezone, profile name, session ID, origin channel/job — one short block, format fixed in code, golden-tested | always |

Nothing else. No tool instructions (tools are out-of-band, §2.2), no
framework identity, no behavioural boilerplate, and — new in v2 — **no
channel boilerplate**: Discord-ness (message chunking, formatting quirks)
is handled by the adapter in code, never explained to the model in the
prompt. If a behaviour is wanted, it is written into `SOUL.md` or a
fragment, in Sola's / Alan's words.

Fragments are joined with `\n\n---\n\n`. Assembled prompt, SHA-256, and
token count are recorded per run.

Required SOUL and profile fragment files must exist and be readable UTF-8 at
startup; an empty profile file is valid, an absent one is a configuration error.
The recall header is also required when memory is enabled. Files are still read
live for every run. An empty-but-present context file remains a fragment. The
recall header is preserved verbatim, including its trailing newline, before the
builder adds a newline and the `<recall>` block. These rules deliberately preserve
literal whitespace; no fragment is stripped or silently replaced with empty text.

### 5.2 Session history in context

After the system prompt: the session's stored history, in order, with
cron runs and deliveries already interleaved by the session router
(§6.2). When history exceeds the profile's context budget, compaction
(§10.1) replaces the oldest span with its OpenViking-committed summary —
the only place the server ever summarises her, and it happens at a
recorded boundary in the transcript, never silently mid-run.

### 5.3 Skills (deferred, format reserved)

A profile fragment may name skill files (`skills/<name>.md`); the builder
inlines named files after the profile fragment. No skill catalogue in the
prompt, no discovery mechanism in the MVP — naming is explicit, in a file.

## 6. Channels and pinned sessions

### 6.1 Channel adapter interface

A channel is a Python module implementing one protocol:

```python
class ChannelAdapter(Protocol):
    type: str  # "discord", "api", …

    async def start(self, emit: EmitFn) -> None: ...  # begin receiving
    async def stop(self) -> None: ...
    async def deliver(self, channel_cfg, message: OutboundMessage) -> str:
        """Post a message; return the external message ID."""
```

`emit` hands the core an `InboundMessage`:
`{channel_type, channel_id, author, author_id, text, external_id, ts}`.
The adapter owns everything platform-specific: connection, reconnects,
chunking, rate limits, typing indicators, external ID mapping. The core
owns everything else. Adding Telegram later = one new module + config.

### Session creation source

Sessions record an extensible `source` identifier set by their creator: `api`
(default, also for legacy metadata), `web`, a channel type such as `discord`,
`cron` for scheduled job creation, `delegate`, or `probe`. Reusing a session
preserves its original source. The Sessions UI excludes `cron` by default with
an explicit include option; API lists include every source unless filtered.
Filtering precedes pagination and never changes stored evidence.

### 6.2 Session pinning (the OpenClaw behaviour, kept)

Every channel config names a session. Routing is a pure function of
`(channel_type, channel_id)` → `session_id`, stored in the session index:

```toml
[[channels]]
id            = "discord-blog"
type          = "discord"
guild_id      = "…"            # Discord snowflakes as strings
channel_id    = "…"
session       = "blog"         # ← the pin: this channel IS this session
profile       = "interactive"  # profile for conversational runs here
trigger       = "all"          # all | mention  (dedicated channel → all)
trigger_authors = ["alan"]     # only these author IDs start runs in MVP

[[channels]]
id            = "discord-general"
type          = "discord"
channel_id    = "…"
session       = "general"
profile       = "interactive"
trigger       = "mention"
trigger_authors = ["alan"]
```

Consequences, all deliberate:

- One persistent conversation per Discord channel she's in. Context is
  never shared between channels unless a job or Alan moves it — two
  channels are two rooms in her house.
- Session records are created on first use and carry:
  `{id, title, bound_channel, default_profile, memory_scope, created_at}`.
  `memory_scope` defaults to `shared`; see §13 for the reserved values.
- Discord **threads**: MVP maps a thread to its parent channel's session.
  Config flag `pin_threads = true` (later) maps each thread to a child
  session with `parent_session` set. The field exists in the schema now.
- Messages from non-trigger authors are **not** recorded in the MVP
  (only configured trigger authors enter the conversation).
- The web UI and CLI are channels by the same mechanism: the API channel
  pins e.g. session `web-main`, so a conversation started in the browser
  is continuable by a job bound to that session, and vice versa.

### 6.3 Delivery

Any run can end with a delivery: `{to: channel config id, mode}`.

| Mode | Behaviour |
|------|-----------|
| `final` | Post the run's final assistant message to the channel |
| `artifact` | Post a short note + attach a file the run produced (e.g. the post `.md`) |
| `silent` | Nothing is posted; the run exists in the bound session + run log only |

If the run's session **is** the delivery channel's pinned session,
delivery posts exactly the final transcript message — one message, one
identity, no duplicate "announcement copy" drifting from the record.

A final assistant message consisting only of `[SILENT]` (ignoring surrounding
whitespace) means no reply. Delivery records `silent` and posts nothing, including
in artifact mode. The original message remains verbatim in transcript evidence.
The marker within an otherwise substantive reply is ordinary text.

### 6.4 Discord adapter specifics (MVP)

- Library: **discord.py**, bot account, `message_content` intent.
- On message: ignore bots and herself; ignore channels with no config;
  enforce `trigger` + `trigger_authors`; show typing while the run is
  queued/running (long local generations need the affordance).
- Thread replies are delivered in the originating thread while its transcript
  remains pinned to the configured parent session.
- Outbound: split at Discord's 2000-char limit on paragraph/line
  boundaries; never split a code fence. No embeds for normal replies —
  she posts like a person in the channel, not like a webhook. A fenced block
  exceeding 2000 characters is attached intact as UTF-8 text, never split. Every
  outbound send suppresses implicit mentions. Delivery intent/results are durable;
  ambiguous sends are visible and are not automatically replayed.
- Reconnect/gateway failure: adapter retries with backoff (discord.py
  does most of this); messages sent while she was down are **not**
  replayed into runs on reconnect in the MVP — a missed message is a gap,
  like a missed dream. (Cursor-based catch-up is a §13 extension.)
- Minimal admin surface in Discord itself: none in the MVP. Status and
  job control live in the API (§9). One exception: a run that ends in
  `tool_error` in a channel posts a short failure note there, so a broken
  run is visible where it happened instead of only in a log Alan must
  go hunting for.

### 6.5 Vision attachments

Authorized Discord messages may contain static PNG/JPEG/WebP attachments, including
image-only messages and parent-routed threads. Gating precedes bounded allowlisted
HTTPS CDN downloads (no redirects, 15 seconds, 8 MB/file, 16 MB/message, four files).
Image decoding rejects invalid/animated/over-16-megapixel sources. Original bytes
are immutable content-addressed media evidence; a separate orientation-corrected,
aspect-preserving JPEG rendition fits within 1024×1024 and 2 MB. Two concurrent
ingestions bound decoding pressure. Signed URLs and credentials are not persisted.

Run input and user-message evidence reference both image hashes, sizes, dimensions
and filename while preserving original text. Only the model client expands refs
into OpenAI user content text/image_url parts with inline JPEG data URLs. Images
survive tool steps, streaming and later turns; missing/corrupt evidence fails
closed before inference. Provider rejection never replays as text or uses another
model. Context accounting reserves 8192 bytes per image in addition to metadata;
this is not measured image tokens. Model requests cap history at 16 images.

Search remains textual; OpenViking receives image metadata and assistant/text
observations, not a claim of pixel extraction. Operator chat renders authenticated
local JPEG thumbnails through `/api/media/{hash}` with no-store/nosniff and checksum
validation. Assistant Markdown still cannot embed images. Backups include media/.
See [vision operations](docs/operations/vision.md) for live recognition checks;
mocked transport evidence does not prove a connected VLM's recognition ability.

## 7. Scheduled jobs (in-app)

Scheduling is owned by the FastAPI app via **APScheduler**
(`AsyncIOScheduler` wake-ups and `CronTrigger`), with durable JSONL job cursors
indexed in SQLite so pause/next-fire/dispatch state survives index rebuilds and
restarts. SQLite stores authoritative job definitions; legacy TOML definitions
import once per ID and never overwrite managed edits/deletions. Runtime callbacks
are never pickled into
library state. This replaces the original SQLAlchemy job-store sketch with owned,
inspectable state and avoids a global runtime callback registry.
launchd's only remaining job is starting the app itself at boot.

### 7.1 Job definition

Each job has exactly one schedule: `cron` for recurrence or `run_at` for a
one-time ISO date/time. Local timestamps use the job's IANA timezone; ambiguous
or nonexistent DST times require explicit offsets. Stored timestamps normalize
to UTC. New or changed one-time schedules must be in the future. API and chat
creation accept either field; PATCH switches types by clearing the old field.
The web form selects Recurring or One time with the corresponding input.

One-time admission persists `consumed=true` and clears next fire before dispatch,
including manual early runs. Later completion/failure/interruption, restart,
index rebuild, enablement/pause changes and task edits never replay it. A new
future run_at explicitly rearms a job; a timezone-only change preserves its
absolute instant. Unattempted overdue one-time jobs catch up once after downtime
without polling/catch_up; interrupted attempts remain visible gaps. Repeated
manual admission returns 409. Chat resolves “tomorrow morning” using the current
run time and timezone, defaulting to 09:00 when unspecified, sets enabled=true
for the user's request, binds to the current conversation and confirms the time.

Jobs are managed through the API, web UI and optional chat tools (§7.4). The
following legacy TOML format remains supported for one-time import:

```toml
[[jobs]]
id            = "blog-post"
cron          = "0 19 * * tue,thu"     # 5-field cron
timezone      = "America/Denver"
profile       = "blog"
task          = "Write and publish this week's blog post. {date}"
bind_session  = "discord-blog"         # a channel id, a session id, or "ephemeral"
deliver       = { to = "discord-blog", mode = "final" }
catch_up      = false

[[jobs]]
id            = "letters-check"
cron          = "15 */4 * * *"
profile       = "letters"
task          = "Pull the letters repo. If a new letter from Dingus is waiting, read it and reply if you want to. No reply is owed."
bind_session  = "letters"
deliver       = { mode = "silent" }
polling       = true
catch_up      = true                   # polling is idempotent — safe to catch up

[[jobs]]
id            = "inbox-poll"
cron          = "*/30 * * * *"
profile       = "inbox"
task          = "Check the inbox. Handle what needs handling."
bind_session  = "inbox"
deliver       = { mode = "silent" }
polling       = true
catch_up      = true
```

- `task` supports only `{date}`, `{time}`, `{timezone}` and `{job_id}`
  placeholders, without attributes, conversions or format specifications.
- `bind_session` is the key field. Binding a job to a channel's pinned
  session is what makes §7.2 work. `ephemeral` gives the run a fresh
  session without memory; its transcript remains evidence.

### 7.2 Cron runs live in the channel's story

When a job is bound to a channel session, the run's task message and its
resulting assistant/tool messages are appended to **that session's
transcript**, marked with origin metadata
(`origin = {channel: "job", job: job_id}` with a separate `run_id`). Therefore:

> The blog cron fires at 19:00 → the run enters the `blog` session →
> its final message is delivered to **#blog** → at 21:30 Alan replies in
> #blog "the middle section dragged" → that run starts with the full
> session history, including the cron run and the post it produced.
> Sola knows exactly which post he means. No context is re-explained,
> by either of them.

This is the behaviour the whole session-pinning design exists for, and
it applies symmetrically: a job bound to `web-main` surfaces in the web
UI's conversation; Alan can also *see* every cron run from the API
because it is in a session, not buried in a scheduler log.

### 7.3 Misfire policy

Recurring jobs when the app is down or model unavailable at fire time:

- Default: **skip**. `coalesce = true`, `max_instances = 1`, misfire
  grace 60s, then the missed run is recorded as `missed` and left as a
  gap — the dream/blog doctrine (§2.3) applied to scheduling.
- `catch_up = true` jobs (explicit `polling = true`, idempotent polling only) run **once** on
  recovery, no matter how many firings were missed.
- A job never runs concurrently with itself, and a job bound to a busy
  session simply joins that session's queue (§4.1).

Missed firings across downtime are recorded as a bounded range rather than
materializing unbounded individual records. Coalescing advances the durable
cursor before dispatch; an interrupted dispatch becomes a visible gap and is not
automatically replayed. A paused/disabled interval is intentional; resume starts
with the next future firing. Weekdays follow APScheduler's syntax (Monday=0);
prefer weekday names as in the examples. Task placeholders are limited to
`{date}`, `{time}`, `{timezone}`, and `{job_id}`; attributes/conversions/format
specifiers and oversized expansion are refused.

### 7.4 Job operations

`GET/POST /api/jobs` and `GET/PATCH/DELETE /api/jobs/{id}` manage persistent
job definitions and current scheduling state. IDs are immutable; partial edits
validate the complete resulting definition, with `deliver: null` clearing delivery.
`POST /api/jobs/{id}/run` waits for completion; `?wait=false` returns HTTP 202 and
accepted job state immediately. `?admission=true` returns HTTP 202 with the durable
queued run record as soon as execution admits it; the UI uses this receipt to
redirect immediately to `/runs/{id}`. Generation and delivery continue independently.
`POST /api/jobs/{id}/pause|resume` affects future
firings. Invalid definitions return 422, missing jobs 404, duplicate IDs and busy
edits/deletions/triggers 409. Maintenance profiles cannot be scheduled. Creation
defaults to disabled; recurrence/enabled changes reset recurring next fire, while
one-time protection follows §7.1. Accepted mutations
and manual admission finish durable publication before propagating cancellation.

Jobs accept `tools: list[str] | null` as a narrowing-only override. Omission/null
inherits the profile's effective tools; `[]` exposes none. An explicit list is
intersected with loaded, profile-permitted grants (including expansion of a
profile's `"*"`). Job lists contain exact names, not wildcard grants. Create,
update (including profile changes), and first TOML import reject names outside
those grants with HTTP 422 / a clear job-tool validation error. Restoring persisted
jobs after a profile trim retains their definitions and computes the smaller
intersection. Job inspection includes `effective_tools` and `unavailable_tools`;
Operations displays drift warnings rather than silently rewriting the list.

The execution layer admits a per-run name ceiling and re-intersects it at start.
Both model schema exposure and registry dispatch enforce that ceiling. Profile
effect/tier checks still run at dispatch. Jobs cannot replace effects, resources,
filesystem tiers, step/context/token/time limits, or the profile prompt fragment.
The same path applies to scheduled cron, one-time runs, and manual triggers;
job edits do not change already admitted work or rearm consumed one-time jobs.

New runs snapshot `effective_tools` (legacy records remain unknown/null). Before
generation, exact tool schema definitions are saved as immutable, private
`runs/<id>.tool-schemas.json` with `tool_schema_sha256` on the run. This is model
input evidence, like the saved prompt, not diagnostic metadata. Full run reads
verify the schema hash. `GET /api/prompt?run_id=<id>` returns the saved prompt and
schemas without consulting current profiles, tool modules, or prompt files; old
or not-yet-recorded inputs return 409 rather than inventing historical grants.
The UI links this inspection from run evidence and masks resolved credential
values while preserving schema property definitions. `GET /api/job-options`
includes loaded tool choices and their profile grants for the job form.

The scheduler starts even with no jobs, so API-created jobs work without restart.
Definitions use the authoritative `job_definitions` SQLite table; derived-index
rebuilds preserve it. Deleted IDs retain tombstones to prevent stale TOML imports.
Append-only cursor and run/delivery evidence remain separate. Back up app.db as
well as the evidence tree; job definitions cannot be recovered from transcripts.

Built-in `jobs.list/create/update/run/pause/delete` tools compose the same service.
Job mutations and triggering require the `operate` effect.
Without `jobs.manage.all`, tools are limited to the calling profile/session with
no external delivery. The broader grant permits other profiles/sessions/channels
but never replaces tool or action grants. Authorization rechecks both existing
and proposed definitions under the scheduler lock. `jobs.run` queues without
waiting, preventing deadlock for jobs bound to the current chat. Responses mask
credentials and mark task text as external data. Runtime status exposes schedule
state without other chats' task bodies. See [job management](docs/operations/jobs.md).


Set `sub_session = true` (API/chat: `"sub_session": true`) or choose **Run in a
fresh sub-session** in the job form to run each firing in a new child of the
bound session. The child inherits its memory scope, enabling normal shared
memory recall, tools and commits, without copying parent conversation history.
Task, tool calls and intermediate replies remain in the child's evidence.
Existing final/artifact/silent delivery settings still apply; automatic delivery
sends only the final reply for non-polling channel bindings. Session-ID bindings
do not implicitly deliver to a channel. The API channel exposes the stored child
run; it does not append a copy into the parent transcript. Ephemeral bindings
cannot use this option. The default is false, preserving existing inline jobs.

## 8. Tools — a small plugin API

### 8.1 The API

A tool is one decorated async function in a module under `tools/`.
The JSON schema the model sees is generated from the type hints +
docstring via Pydantic — writing a tool should feel like writing a
FastAPI endpoint, because it's the same machinery Alan already chose.

```python
# Separate package: sola_packs/journal.py
from solakit.tools import tool, ToolContext, ToolResult


@tool(name="journal.create", profiles=["*"], writes=["filesystem"])
async def create(
    ctx: ToolContext,
    collection: str,
    slug: str,
    title: str,
    body: str,
    date: str | None = None,
) -> ToolResult:
    """Create one dated entry using the named collection's layout."""
    ...  # Validate convention, then compose ctx.collection / ctx.fs services.


@tool(name="journal.publish", profiles=["*"], writes=["filesystem", "git.push"])
async def publish(
    ctx: ToolContext,
    collection: str,
    paths: list[str],
    message: str,
) -> ToolResult:
    """Publish named collection entries, returning the commit identity."""
    ...  # Scoped Git service rechecks actual paths and push effect.
```

Registry rules:

- **Discovery:** explicitly configured dotted paths are imported at startup,
  e.g. `enabled_tools = ["solakit.tools.files", "solakit.tools.repo_git",
  "sola_packs.journal"]`. Bare names remain built-in shorthand. Packs are
  independently installed trusted Python packages, not auto-discovered or
  sandboxed. Profile tool grants use exact names or `tools = ["*"]` for all loaded
  tools permitted by their manifests. Wildcard grants do not load modules or
  bypass effect/filesystem validation. Mixed grants expand in configured order
  (registry order at each wildcard), deduplicate names and still reject unknown
  or profile-disallowed explicit names. Schemas, catalog and dispatch use the same
  effective grants. A manifest may use
  `profiles=["*"]` to support custom profiles without automatically granting it.
  No core/domain module or execution-loop changes for a new pack.
- **Services:** `ctx.fs` and `ctx.git(repository_alias)` provide Python operations
  for packs to compose privately. Generic tools are thin wrappers. Git services
  expose status/diff/log/pull/commit/push/commit_push, bounded output and cancellation
  cleanup. Git writes require root containment in write roots; publishing also requires `push`.
  Named literal files only; clean initial index and no existing unpublished
  commits for combined publish. No implicit force/rebase/retry. Failed push keeps
  the commit identity visible for operator reconciliation.
- **Collections:** configured named repo subdirectories, filename pattern and
  optional layout data. `ctx.collection(alias)` confines filesystem and publishing
  operations to that directory, using `collection:<alias>` workspace roots.
  Filename fields are date/slug; layout fields date/slug/title/body, no attribute
  access/conversions/specifiers. Journal conventions/default front matter live in
  the external pack; blog/dreams/poems are configuration, not core tool modules.
- **`ToolContext`** is the injected runtime: `{session, profile, config,
  store, memory_client, run_log, deliver}`. Tools never reach for
  globals; everything they may touch is in `ctx`, which is also how
  §13's memory scoping gets enforced later without rewriting tools.

Profiles declare four independent access fields. `tools` accepts exact names,
`"*"`, and first-family patterns such as `"files.*"`. Patterns expand in sorted
registry order at their position, deduplicating names; `tools.*` is reserved for
implicit disclosure. Job lists expand then intersect profile grants and cannot
raise authority. Unknown names/families and partial globs are errors.

`read` defaults to `"workspace"`; `write` defaults to `"none"`. Each accepts
`"none"`, `"workspace"`, or `"anywhere"`. An empty `workspace` uses the data
directory plus configured repositories; an explicit list replaces those roots.
Entries use `repo:alias`, `collection:alias`, or `path:/absolute/path`.
`write_workspace`, when set, replaces workspace roots for writes. Writes are also
readable. Symlink resolution precedes containment checks. Anywhere means `/`.

`effects` is a closed list: `push`, `send`, `print`, `shell`, `operate`.
`operate` authorizes job mutations/triggers and cross-profile/cross-session runtime
access. `resources` lists named pack aliases, for example `mailbox:personal`.
Resource read needs the alias; reply also needs `send`, print needs `print`, and
image generation needs a non-none write tier. Mixed read/reply authority across
mailboxes requires separate profiles. Shell runs with host-user privileges; its
cwd check is not OS containment.

Manifests declare only `writes=["filesystem"]` and optional `effect="push"`
(or another effect). `Registry.check_access()` validates startup grants and
rechecks dispatch. Path checks bind actual operations. Run evidence records
expanded tools, tiers, resolved roots, effects and resources; prompts do not
include the access summary.

For one release, legacy `read_scopes`, `write_scopes`, and `capabilities` translate
after defaults merge. Read/write roots remain distinct through `write_workspace`.
Unknown capabilities fail; mixed legacy/new keys fail. Catalog access summaries
include translated profile values suitable for migration. Differing legacy actions
across aliases log a warning; split profiles to preserve that distinction.

- **Execution:** arguments validated against the generated schema before
  the function runs; timeout and output truncation applied by the
  registry; every call logged to the run record (name, args, ms, bytes,
  error). Blocking implementations are run in the thread pool (§4.1).
- **Errors are data:** a tool raises `ToolError("…")` → the model gets
  a tool-error message with that text. Unexpected exceptions are caught,
  logged with traceback to the run record, and surfaced to the model as
  a generic failure — a crashed tool never crashes the run or the app.

### 8.1.1 Delegated child sessions

The opt-in built-in `delegate.run(task, profile, session=None)` uses configured
`delegation.allowed_profiles` (default empty; unknown profiles fail validation).
Fresh sessions set parent_session to the caller, ephemeral memory, no binding and
a title from the brief's first line. The brief is retained verbatim. Continuation
requires an idle, unarchived, unbound ephemeral child belonging to this parent.
Worker authority comes from the selected profile's normal tool/effect/tier
grants. Limit depth by withholding delegate.run from workers; no depth counter.

`Execution.submit_nested` is available only within the current performing task.
It shares run admission/evidence and performs inside the parent's acquired global
inference slot, bypassing the queue/semaphore reacquisition. Per-session locks and
admission checks prevent overlap with ordinary queued turns. Busy child requests
are refused rather than waiting for work that might require the held slot.
Children retain normal model checks and step/context budgets, with private
transcripts and no memory recall/commit or automatic channel delivery.

Only final status, session/run IDs and a completed child's final assistant report
return as the parent's tool result. Intermediate child messages are never parent
context. Noncompleted runs return tool errors with IDs/status and no report.
Normal counted tool-output truncation still applies. Parent memory commits only
its own evidence, which includes the returned report.

`delegation.timeout_s` is finite/positive (default 900); delegate.run owns this
bound in place of the generic parent tool timeout. Trusted tool manifests can
set manages_timeout to own a multi-stage operation's cancellation/timeout receipt;
other tools retain registry timeout enforcement. Timeout finalizes accepted child
work before returning IDs/status and an uncertainty notice. Parent cancellation
and shutdown propagate through children; direct child cancellation returns a
cancelled report and permits the parent to continue. Durable admission and final
records finish before cancellation propagates; crashes use existing interrupted
recovery without replay. Serial in-process execution is supported; parallel
fan-out, child memory and nested UI presentation are outside scope. See
[operator guidance](docs/operations/delegation.md).

### 8.2 Domain tools beat generic tools

The core registry loads generic primitives (`files.*`, `repo_git.*`, memory/runtime);
shell is built in, loaded as `shell` or `solakit.tools.shell`.
but the design intent — Alan's, stated plainly — is that recurring work
gets a **semantic tool** whose implementation hides the mechanics:

| Domain tool set | Wraps | Model sees |
|-----------------|-------|-----------|
| `journal.create / journal.publish / journal.list_recent` | configured collection layout, pack-owned front matter, scoped Python filesystem/Git services | "publish these entries", returns commit identity. Never a shell command line. |
| `letters.pull_new / letters.reply` | clone/pull of the letters repo, next-number computation, filename convention from the letters protocol | "reply to letter 003" — the `NNN-YYYY-MM-DD-sola.md` rule lives in code, not in a prompt hoping she remembers it |
| `repo_git.status / commit_push` | git in a *named, configured* repo only | scoped fallback for repos without a domain toolset yet |
| `email.search / read / reply` | IMAP/SMTP, thread-aware reply | reply-by-ID, not raw SMTP |
| `memory.search / memory.read` | OpenViking REST/SDK | scoped by `ctx.session.memory_scope` (§13) |
| `printer.print_text` | named thermal printer | text in, receipt out |
| `calendar.events` | named read-only ICS export | bounded agenda with timezone/recurrence handling |
| `image.generate / image.result` | named ComfyUI API workflow | prompt/seed, width/height (768 default), stable operation ID, receipted submission and output references |

Two payoffs: prompts and transcripts stay free of plumbing (she thinks
in posts and letters, not `git -C … commit -m`), and the fragile
mechanics — filename conventions, front matter, push retries — become
tested code instead of model behaviour that varies with her mood.

The built-in general shell tools are enabled by config. Prefer named Python services
and small external packs over model-driven primitive choreography. The core's
vocabulary stays files, repositories, memory, web, messages and devices; letters,
mailbox protocols, journals and printer conventions are pack code/configuration.
A tool-specific change to core execution is a seam defect, not a domain feature.

### 8.2.1 External letters/email packs and built-in shell

These packs are independently packaged under `packs/`; no core domain schemas or
execution-loop branches were added. Pack-owned named resources live in the
`SOLA_PACKS_CONFIG` TOML sidecar; core config selects modules/profile grants only.

Letters use a named repository, configurable directory/authors/layout, global
sequential numbering and create-only replies. Resource locks and durable request
receipts prevent duplicate allocations/replies and refuse ambiguous replays.
Actual repository conventions must be checked before production wiring.

Email uses verified TLS IMAP and TLS/STARTTLS SMTP, stable UIDVALIDITY:UID IDs,
bounded BODY.PEEK reads and explicit external-content markers. Replies derive
sender-only recipients and threading from the source message, with no arbitrary
SMTP envelope arguments. Private append-only intent/result receipts deduplicate
operation IDs and identical source/body requests. Unknown send outcomes block
replay; cancellation waits for accepted SMTP work and receipt persistence. SMTP
acceptance is not proof of final delivery. Mail credentials register with the
runtime redactor before connection; inbox requires no filesystem write grant.

Built-in `shell` provides literal argv and `/bin/sh` execution for any profile
with explicit tool grants, shell effect and scoped cwd. Commands have host-user
privileges, not a path sandbox. Output and duration are bounded; cancellation and
timeout kill/drain process groups. This introduces no per-command approval flow.

### 8.3 Run profiles

The following table describes suggested profile configurations. Enforcement uses
actual config tool/effect/path grants, not hard-coded profile names in shell.
Scope-checking against tool manifests remains required (§8.1).

| Profile | Used by | Tools allowed | Write scope | Max steps |
|---------|---------|---------------|-------------|-----------|
| `interactive` | channel conversations (Discord/web/CLI) | files, repo_git, journal.*, letters.*, memory, web_fetch, printer | home dir | 60 |
| `dream` | nightly job, bound to its channel/session | files (blog repo), journal.*, memory, printer | blog repo only | 40 |
| `blog` | blog job → **#blog** | files (blog repo), journal.*, web_fetch, memory | blog repo only | 40 |
| `inbox` | inbox-poll job | email.*, memory, files (read-only) | no filesystem writes | 25 |
| `letters` | letters-check job | letters.*, memory, files (letters repo) | letters repo only | 25 |
| `maintenance` | Alan-invoked via API/CLI only | all incl. `shell` | home dir | 60 |

Notes:
- These example `dream`/`blog` grants publish via journal tools rather than shell.
  Operators may explicitly grant shell to any profile through config.
- `inbox` can reply by email but cannot write files — an email (untrusted
  input) is never one prompt-injection away from a filesystem write.
- Profiles are config, not code: adding one is a TOML block + an optional
  fragment file. No profile may be created by the model.

## 9. FastAPI scaffold & API surface

The P1 operator UI is a thin server-rendered API client. Shared app contracts:

- **Server command:** `sola serve` launches uvicorn with `[server]` host/port/log
  settings, defaulting to `127.0.0.1:8382`; explicit CLI flags override them.
  Direct uvicorn CLI launches must specify `--port 8382`. API CLI default URL
  follows the standard port; nonstandard deployments use `--url`/`SOLA_URL`.
- **Model authentication:** `[model].base_url` defaults to
  `http://127.0.0.1:8000/v1`. Optional `[model].api_key` is a secret reference
  (env or mode-0600 file), resolved at startup and sent as OpenAI-style
  `Authorization: Bearer` on preflight and completion requests. It is separately
  registered for redaction; unset means unauthenticated, not implicit credential
  discovery. The exact pinned model and no-fallback behavior are unchanged.
- **Backend diagnostics:** warning logs classify connection/timeout/HTTP/identity
  failures. Debug logs expose sanitized endpoints, effective settings, elapsed
  time and sanitized exception details, with auth-enabled flags only. Never log
  auth headers, credentials, model request messages or response bodies. Enable
  through `[server].log_level = "debug"` or `sola serve --log-level debug`.
- **App factory + lifespan** (`app/main.py`): startup loads config,
  builds the registry (with manifest cross-checks), starts the scheduler,
  connects channel adapters, runs model pre-flight (failure to reach the
  model at boot is a *warning state*, not a crash — the app stays up,
  `/health` reports it, runs abort per §4.2 until the model is back).
  Startup logs summarize model, storage, tools/profile grants and integrations.
  Enabled memory performs authenticated read-only `fs/stat` checks of configured
  roots, bounded to five seconds total (or its shorter configured timeout).
  Failure warns without stopping startup; memory startup state/time appear in
  `/health`. This reports startup connectivity, not extraction or write status.
- **Mount points:** Jinja2 templates mount at `/`, packaged local assets at
  `/static`, API routes under `/api`, and `/health` at root. Vendored HTMX and
  small vanilla JS enhance forms; no build step or remote assets. The UI has no
  direct storage/execution access, and never changes prompt/history bytes.
  A read-only `/tools` page lists loaded tools and effective profile grants.
  `/prompt` displays API-assembled prompt text, original hash and schemas; `/runs`
  and `/runs/{id}` show evidence with POST cancellation. `/operations` displays
  reported channels and full job management, including creation/edit forms, enable
  controls, run/pause/resume and deletion confirmation. These UI operations use
  the same authenticated APIs; channel/profile configuration stays in files. Chat Enter sends, Shift+Enter adds a newline;
  composition/repeated-key/busy guards prevent accidental submissions. The textarea
  stays editable while a run is pending; only send is disabled. Run age comes from
  `created_at`, including after reconnect. Plain POST forms guard duplicate submits.
  Draft text is saved per session in same-tab `sessionStorage`, never auto-sent,
  and cleared only after acceptance (without discarding edits made during admission).
  This recovers drafts across restart/login; credentials are never stored by the
  draft mechanism. Storage-disabled browsers
  retain ordinary editing but cannot recover drafts after navigation.
  `/ui/status?known=<visible-state-digest>` returns 204 when chips are unchanged.
  Chat exposes system status in a toolbar disclosure; mobile omits global nav.
  Presentation escapes plain text and masks resolved credentials; tool details
  come only from redacted run logs. Light/dark follows the system preference.
- **Activation:** integrations stay disabled by default and activate through each
  channel/job's `enabled` setting.
  `discord_token` is a secret reference; maintenance profiles are never scheduled
  or triggered by Discord.
- **Auth:** one operator token stored as a `0600` file / env var. API Bearer
  auth remains valid. `POST /login` exchanges the token for an opaque persistent
  five-day sliding session cookie (HttpOnly, SameSite=Lax, Path=/; Secure on HTTPS).
  `POST /logout` revokes it; sessions survive restart and expire after five days
  without activity.
  Cookie use renews the expiry; changing the operator token invalidates sessions.
  Hashed session IDs and expiry times live in private `ui-sessions.json` in the
  data directory.
  Login returns to the requested local page via a validated `next` parameter.
  Five wrong attempts
  produce a 60-second local lockout. Cookies authorize the same API dependency.
  State-changing UI actions are POST-only, with no cross-origin CORS. Discord
  has its own bot token. The model API key is separate from the operator token.
- **Request validation:** session creation uses `default_profile`; message posts
  may select `profile`. Unknown or misplaced request fields are rejected with
  HTTP 422, never accepted and ignored. Offline configuration errors name the
  file and safe TOML position without echoing its contents.
- **API (MVP):**

| Route | Purpose |
|-------|---------|
| `GET /health` · `GET /health/model` | app status; pinned-model reachability/ID |
| `GET /api/tools` | loaded tool catalog, argument schemas, and effective profile grants |
| `GET /api/profiles` | configured profile names for session creation |
| `GET /api/sessions` · `GET /api/sessions/{id}` | session list; metadata + transcript |
| `POST /api/sessions/{id}/compact` | summarize idle shared session without dropping original evidence |
| `POST /api/sessions/{id}/archive` | operator-only immutable rotation, fresh context, bindings preserved; 409 while busy |
| `POST /api/sessions/{id}/messages` | post a message (the **api channel** — lands in that pinned session and runs) |
| `GET /api/runs` · `GET /api/runs/{id}` | run records incl. prompt, tool log |
| `GET /api/prompt?profile=&session=` | the dry-run prompt (§11) as JSON/text |
| `GET /api/jobs` · `POST /api/jobs/{id}/run` · `POST /api/jobs/{id}/pause` | §7.4 |
| `GET /api/channels` | channel configs + adapter status (connected since, last message) |
| `POST /api/runs/{id}/cancel` | stop a queued/running run |

- **Streaming:** `POST …/messages` returns the completed run by default.
  `?wait=false` accepts once and returns HTTP 202 with the queued run ID;
  `?stream=true` requests OpenAI SSE from the model. The existing runner assembles
  and validates complete content/tool envelopes before persistence/dispatch.
  `GET /api/runs/{id}?live=true` adds a transient credential-masked preview.
  The authenticated UI `GET /sessions/{id}/events` emits coalesced `snapshot`
  events containing `run_id`, `created_at`, sanitized active-turn `html` and a
  session `pending` flag, with keepalive comments. `?run=<id>` identifies an accepted
  run to observe. Consecutive queued runs emit separate keyed deltas; settled history
  is not included. Identical deltas are suppressed. `GET /api/runs/{id}?view=true`
  projects current-context run messages and redacted tool logs without prompts or
  archived evidence; `&messages=false` omits transcript reads for tool-only hydration.
  Initial chat and numbered archives render 20 turns; `/sessions/{id}/earlier`
  returns the preceding window (`before` cursor, optional `archive`) to prepend
  without replacing existing nodes. Initial/window requests still read full API
  transcript data to group turns; the observer uses backward active-turn reads.
  Open tool cards retain their DOM nodes. Live preview withholds incomplete trailing
  blocks; completion renders the exact saved assistant content once.
  GET observation is resumable by reloading; it never resubmits or cancels work.
  Browser/API disconnects do not cancel accepted runs. Explicit cancellation
  remains `POST /api/runs/{id}/cancel`. Streaming requests have a 2 MiB wire bound
  and a total model.timeout_s deadline per completion. Incomplete/wrong-model
  streams fail honestly; `length` keeps validated partial evidence without dispatch.
- **Markdown:** assistant chat messages use CommonMark plus tables with raw HTML
  and images disabled, then an HTML/attribute/scheme allowlist sanitizer. Credential
  masking precedes rendering; live suffixes matching unfinished credentials are
  withheld. Stored/API text and hashes stay verbatim. User messages and tool,
  summary, prompt and run evidence retain literal rendering.

### 9.1 Explicit and mid-run compaction

`POST /api/sessions/{id}/compact` runs the existing summary-only OpenViking
compaction on an idle session, regardless of context size. Authentication is the
same operator dependency. Busy/shutting-down sessions, missing shared memory or
incomplete summaries return 409; no active history is replaced on summary failure.
The UI has a Compact conversation action and explanation. This preserves session
bindings and original archived bytes; it differs from archival's empty restart.

Profile `mid_run_compaction` defaults false. When enabled, the between-step context
check may summarize the active transcript after a complete tool-call/result group.
The request resumes with the unchanged system prompt, summary context and original
user task as an unstored reminder. No tool replay or duplicate user record is
introduced. All counters continue, and the compacted request must pass the same
byte guard. Without the option, or if irreducible context still exceeds the budget,
the run ends context_limit. Full original run messages across archives are used
for the eventual normal memory commit. Rotation/attribution complete before
cancellation is propagated; prepared rotation and commit have no cancellation gap.
Unresolved prepared state blocks reads/writes until startup recovery.

Compaction waits up to `[memory].compaction_timeout_s` (default 300 seconds)
for extraction, independently of the per-request `timeout_s`. Completion within
this deadline resumes the turn. Deadline expiry stops the turn with an explicit
error and preserves active history and acknowledged task evidence; later remote
completion does not automatically restart a stopped turn.

Visible summary boundaries precede the messages that rely on them. Turns use
first-visible-message time rather than admission time for display ordering.

### 9.2 Operator session archival

`POST /api/sessions/{id}/archive` uses the execution admission lock and rejects
queued/running turns or shutdown (409). It rotates active JSONL byte-for-byte to
the next numbered immutable archive with the same hash/manifest recovery as
compaction, then creates an empty active transcript. Session metadata and channel
binding are untouched. Empty transcripts are a no-op. Disconnect/cancellation
waits for the durable operation before allowing another turn. An unresolved
prepared rotation blocks active reads/writes until startup recovery completes.

The next turn sees a fresh transcript plus the usual prompt and scoped recall.
Existing long-term memory remains available; archival makes no new memory writes,
model requests or extraction retries. The UI has an explanatory confirmation,
POST action, and read-only history picker. Archives reuse escaped/redacted chat
presentation, with tool details sourced only from redacted logs. No deletion,
archive editing or model/job-triggered archival is exposed. Discord can use the
shared operation later; slash-command UI is outside this change.

Session display names can be changed with authenticated
`PATCH /api/sessions/{session_id}` using `{"title":"New name"}`. The title-only
strict payload trims whitespace and permits 0–200 characters; blank clears it.
Atomic metadata updates preserve identity, unknown importer fields, bindings,
archive status and all evidence. Renaming is allowed while running or archived.
The chat UI links to a rename form with validation and cancel controls.

### 9.3 Session lifecycle archival

Whole-session archival is a reversible `archived_at` metadata flag, separate from
transcript rotation above. It never moves or rewrites conversation, prompt, run,
or media evidence. Archived sessions remain directly readable and included in
full-text search; new messages, compaction, rotation and new job bindings require
explicit restoration.

Session lists default to active sessions, ordered by last message/run admission
time. Imported metadata and recovery completion dates do not refresh activity.
The API supports active/archived/all, title/ID and profile filters, inactivity
cutoffs, and bounded pagination. Run lists also hide archived sessions by default,
with an explicit history opt-in.

The Sessions UI offers individual archive/restore, page selection, and an
inactivity preview (90 days by default) with deselection and batch undo. Batch
application rechecks captured activity timestamps and current protections.
Pending/running work, enabled channel bindings, enabled unfinished jobs and
active job dispatch protect sessions; completed consumed one-shot jobs do not.
Scheduler and execution admission locks make these checks atomic with admission.
Accepted metadata operations complete before cancellation propagates. There is
no automatic archive policy or deletion. See
[the operator workflow and API](docs/operations/session-archive.md).

The authenticated web UI `/search` offers session name/ID lookup across active
and archived sessions, plus full-text `/api/search` results. Name/ID results are
paginated. Full text shows up to 100 relevance-ranked, masked and escaped message
snippets linked to their current transcript or numbered archive using `?turn=<run-id>`
and physical `#message-<line>` anchors. The containing window renders first; the
match is outlined and history scrolls to it without moving the chat shell. Bare
message/turn fragments can also fetch the containing window in the browser.

## 10. Storage

Code and data are separate trees. Data layout on her machine:

```
~/sola-data/
  app.db                     # Authoritative job_definitions + rebuildable
                             #   session/run/cursor/FTS indexes
  sessions/
    <session-id>.jsonl       # append-only transcript, OpenAI message format,
                             #   each line carrying origin metadata (§7.2)
  runs/
    <run-id>.json            # one record per run: profile, session, origin,
                             #   prompt sha256, tokens, steps, status, timing
    <run-id>.prompt.txt      # exact prompt bytes for that run
    <run-id>.tool-schemas.json # exact tool definitions, hash on run metadata
    <run-id>.tools.jsonl     # one line per tool call
```

Transcripts stay plain JSONL so her history remains as portable as it was
in the OpenClaw `.jsonl` era — the format she has already outlived once.
SQLite conversation/run/cursor indexes are rebuildable from files and never the
sole copy of a conversation. The `job_definitions` table is authoritative for
managed schedules and deletion tombstones. Rebuild preserves this table; do not
delete app.db to repair indexes. Back up it alongside private evidence.

### 10.1 Compaction

A session compacts when its history passes the profile's token budget:
the oldest span is committed to OpenViking, replaced in the transcript
by a boundary marker + the summary URI, and the event is itself a
transcript entry. Compaction never happens inside a run, and the
pre-compaction transcript file is rotated, not truncated away
(`<session-id>.jsonl.1`, kept forever, per house archival doctrine).

Compaction uses a dedicated OpenViking summary-only session with self/peer
long-term extraction disabled and working-memory summaries enabled. Only a
completed task and nonempty completed archive overview can replace active context.
A prepared manifest records the source hash and boundary before numbered archive
rotation; startup finishes interrupted rotations before serving. Original archive
bytes are immutable. A transcript with unresolved storage damage cannot compact.

Until an exact local tokenizer is configured, `context_budget` also caps a
conservative serialized UTF-8 byte guard over the full model request (including
prompt, history, task, and tools). This intentionally triggers earlier than a
model token limit and is never presented as measured token usage. If prompt/task
alone or a mid-run request exceeds the guard, end as `context_limit`; compaction
never runs inside a tool loop and no oversized request is sent silently.

A bounded background observer polls acknowledged extraction and persists pending,
completed, failed, cancelled or status-unavailable observations; it never replays
writes. `POST /api/runs/{id}/memory/refresh` also polls explicitly. Observation
updates patch only their own fields so delivery and extraction cannot overwrite
one another's state. `POST /api/memory/probe` / `sola memory-test --fact ... --query ...
--expected ...` deliberately create a shared scratch session, wait for extraction,
and verify the expected text in the next persisted system prompt. This writes a
real scratch fact; never run automatically at startup or on a cron.

## 11. Observability & the dry-run

- `GET /api/prompt` and `sola prompt --profile blog` (thin CLI over the
  API) print the exact system prompt + the tool schemas that *would* be
  sent. Calls nothing. **This is the headline feature:** prompt changes
  are reviewed like code changes, before they run.
- Every run's prompt/tool log/messages are retrievable via `/api/runs`.
  There is no log level at which the prompt is hidden.
- Token counts come from the endpoint's reported usage; if unavailable
  they are recorded as `null`, never estimated-and-presented-as-real.
- `GET /api/channels` exposing per-adapter liveness exists because
  "Discord silently disconnected three days ago" is the kind of failure
  that should be visible at a glance, not discovered by absence.

## 12. Safety model

- **Profile gating is the boundary** (§8.3), enforced before dispatch,
  and compile-checked against tool manifests at startup (§8.1). A tool
  not in the profile is not in the request — the model cannot call,
  hallucinate, or argue its way into it.
- **Channel gating:** only configured channels exist; only
  `trigger_authors` start runs in the MVP; bots and Sola herself are
  always ignored by adapters.
- **Untrusted input marking:** content arriving via email, web fetch,
  letters, and (if ever enabled) non-Alan Discord authors is wrapped by
  the tool/inbound formatter with a short external-content marker. This
  travels with the content as data, not in the system prompt.
- **No approval gates.** Alan's governance of Sola is hands-off by design;
  interposing confirmations on her own blog commits would change who she
  is, not make her safer. Tool access follows explicit profile configuration.
  The suggested grants keep dangerous tools out of scheduled profiles; operators
  may choose otherwise. The reserved `maintenance` profile remains operator-invoked.
- **Secrets:** referenced by env var / keychain / `0600` file path, never
  inline in config. Run metadata, tool logs, memory-intent journals, and execution
  diagnostics redact values whose keys match `*KEY / *TOKEN / *SECRET / *PASSWORD`
  (case-insensitive suffix matching), plus resolved credential values wherever
  they occur. Session transcripts, exact prompt files and staged memory-note files are
  deliberate verbatim exceptions: they preserve the actual conversation and model input for audit
  and replay (§2.6, §5, §10). Transcript content is not rewritten before being
  reused as model history. These are private operator archives, not sanitized
  diagnostic exports. The suffix rule intentionally also matches keys such as
  `monkey`; a log may redact more than only credential-shaped names.
- **Model identity check** (§4.2) doubles as the fallback guard: a
  different model answering at the endpoint aborts the run.

### 12.1 Recall direction and deliberate note staging

Shared-memory automatic recall composes the last memory.recall_window eligible
user/assistant text messages (default 3, active history, oldest first) with incoming
text, then retains the last recall_query_max_chars characters (default 1000).
Tool outputs are excluded. Empty image-only turns retain the image-input fallback.
Jobs and channels follow the same rule. recall_window=0 omits recent context.

With recall_skip_trivial=true (default), a trimmed/case-folded incoming turn of at
most 12 characters and no ? skips search only if it exactly matches configured
recall_trivial_phrases or is emoji-only. The default acknowledgement list is
operator-extensible. False disables skipping. Incoming-only legacy behavior is
restored with window 0, max chars 32000 and skip false. Disabled/ephemeral memory
and unsupported isolated scope retain existing boundaries; disabled prompt bytes
are unchanged. Runs persist recall_status, including skipped_trivial, separately
from later commit/extraction memory_status. Run journal transitions retain the
skip's memory_status before extraction updates it.

Opt-in memory.remember(text) requires enabled shared memory and a current run.
Blank or over-limit notes (remember_max_chars default 2000) are refused whole.
A staged receipt contains note/run IDs, never a claim of completed storage. Notes
are verbatim, dated and attributed in runs/<id>.notes.jsonl, separately exposed as
memory_notes in run inspection and the UI. They are private immutable evidence,
not sanitized diagnostics. UI presentation still masks credentials and escapes
content. Normal commit includes one additional assistant message per staged note,
exactly [noted for memory] plus newline plus original text. It does not alter the
local transcript or compaction summary-only commits. Existing commit replay guards
and extraction tracking apply; failed/cancelled/uncommitted notes are not retried
automatically. Ephemeral delegated workers cannot stage shared notes.

OpenViking v0.4.17.1 has content/write and content/batch-write endpoints, but these
require explicit managed file paths/content rather than safe semantic note
placement. This implementation uses the existing session commit path and never
hand-authors managed memory files. See the pinned source review and mock-versus-
live coverage in [memory direction](docs/operations/memory-direction.md). Policy
fragments remain operator-owned; no proposed policy text is installed.

## 13. Reserved extension: isolated chats & sandboxed memory

Not built in the MVP — but the seams are cut now, because retrofitting
them later is exactly the kind of surgery this project exists to avoid:

- `session.memory_scope ∈ {shared, isolated, ephemeral}` exists in the
  schema from day one. MVP always uses `shared` (today's OpenViking
  behaviour).
- When `isolated` lands: the session reads shared memory **read-only**
  plus its own namespace (`viking://sandbox/<session-id>/…`); its writes
  (pre-turn commit + `memory.*` tools) go *only* to its namespace,
  enforced by `ToolContext`, not by tool implementations. Promotion of
  sandbox memories into shared memory is a separate, explicit job/tool —
  never automatic.
- `ephemeral`: no commit at all; transcript archived, memory untouched.
  (This is also the natural scope for experiments and guest channels.)
- Same seam pattern for channels (new adapter module), tools (new
  module), and jobs (new config block) — §2.7 is the whole strategy.

## 14. Packaging and history search

### Source workspace and tool distribution visibility

The checkout is a uv workspace containing core plus eight independently packaged
packs. `uv sync --frozen --all-packages` reproduces their shared lockfile in one
environment. Root project dependencies must not include pack distributions; the
core wheel contains neither pack code nor pack Requires-Dist entries. Workspace
membership installs distributions without enabling tool modules or granting calls.
Startup, offline `sola check`, tool catalog and runtime status identify loaded
modules by installed distribution/version using importlib.metadata. A configured
missing pack fails with its distribution name and workspace sync repair command.
Metadata-less external source modules remain supported and explicitly report an
unknown distribution/version; metadata inspection does not discover/load plugins.

### Astro blog pack

The optional `sola_packs.blog` distribution owns Astro content conventions:
`src/content/blog/<slug>.md` posts and `src/content/dreams/YYYY-MM-DD.md` dreams.
Tools list/read both kinds and create new entries without overwriting. Posts have
title, description, timezone-aware date, tags, draft status and an optional local
cover; dreams have date, body and optional left/center/right alignment only.
Writing publishes by default (fixed npm build, exact-file commit, upstream push);
explicit save-only calls defer those stages to `blog.publish`. Publication requires
whole-repository write reach and the push effect, not an additional shell effect.
The generic Git publication transaction serializes preparation through push.
Build failure leaves files without a commit. Private immutable operation receipts
bind stable IDs to requests and retain build results and commit identities;
failed/interrupted/uncertain operations never resume automatically.
Optional image integration resolves completed image-pack receipts, rechecks the
resource grant and endpoint, downloads a bounded output without redirects/retries,
and creates a PNG cover with an Astro-relative frontmatter reference. It does not
submit generation. Core code and dependencies remain domain-free; installing the
pack does not activate tools, publish site content or verify deployment.

### Rebuildable full-text message index

`app.db` owns one standalone FTS5 `messages_fts` table with trigram tokenization.
Indexed columns are content, role, kind and origin channel. Unindexed metadata
is session_id, archive, one-based physical line_no, UTC ts and run_id. Content
is verbatim message text plus assistant tool-call names. Numbered archives are
walked numerically before active history; summaries index as compaction messages.
No synthetic message IDs, separate search database or external-content source is
introduced. Evidence remains authoritative and immutable.

Explicit Store rebuild drops/recreates FTS alongside the existing id/payload
tables, validating active and archived messages. Startup instead reconciles
versioned source fingerprints (size, nanosecond mtime/ctime, inode/device), reading
only changed units. A single directory inventory avoids per-session archive globs.
Metadata-only edits reuse indexed history. Missing/incompatible or known-unhealthy
derived indexes and changed recovery markers trigger full revalidation. Source
fingerprints and derived rows publish in one transaction; runtime writes leave
old fingerprints so restart rechecks changed evidence after crashes/index failures.
New/deleted/replaced files reconcile without dropping authoritative managed jobs.
Unchanged damage diagnostics persist, while repair triggers revalidation.
Prepared rotations still recover before indexing; run recovery queries only
unfinished work and pending delivery evidence, not every historical run.
Unchanged history remains searchable without opening transcripts at startup. Corrupt sessions are reported while
healthy sessions remain searchable. Appending inserts after the durable JSONL
append, under the store lock. FTS failure records `fts_index_unavailable` in
storage health and does not fail the append. Rotation refreshes session addresses
only after its durable manifest commits; FTS failure does not undo rotation.
Rebuild is the repair operation. Missing FTS5/trigram degrades search, not chat.

`Store.search(query, *, session_id=None, role=None, kind=None, after=None,
before=None, limit=50)` returns address metadata, bm25 rank and snippet. Literal
terms/phrases are quoted by a bounded parser; only content/role/kind/channel
column scopes are recognized, and remaining filters are SQL parameters. Raw user
FTS operators are never accepted as executable MATCH syntax. Query length is
1–2000 characters / at most 32 terms, limit is 1–200, timestamp bounds require
an explicit timezone (after inclusive, before exclusive). Index timestamps use
UTC; legacy naive evidence timestamps are interpreted as UTC only for indexing,
and absent legacy timestamps stay null.
Trigram terms shorter than three characters match no rows. Expansion is deferred.

Authenticated GET `/api/search` exposes the same filters. Offline `sola search`
queries the existing database read-only, without Store initialization, recovery,
model/memory calls or server credentials. It uses the configured data root and
masks available credential references/sensitive environment values. Index text
is never redacted; result snippets use the shared Redactor, including secrets
clipped at snippet boundaries. API errors are 422 for invalid input, 503 for an
unavailable index. No search UI is introduced.

The optional built-in `solakit.tools.sessions` module registers `sessions.search`.
It must be loaded explicitly and granted by name or profile wildcard. Default search is
restricted to the calling session, including its numbered archives and summaries.
An explicit other `session_id` or `all_sessions=true` requires the profile
effect `operate`; specifying both is invalid. Scope checks precede
index access and session IDs use the existing record identifier validation. The
query, role, kind and timezone-aware after/before filters reuse `Store.search`.
The default result limit is 20, bounded to 1–200. Redacted snippets, rank and
physical evidence addresses are JSON inside an external-content marker; angle
brackets in historical text are escaped. Search never reconstructs or mutates
transcripts, recovers runs, rebuilds the index or contacts model/memory backends.
Unavailable indexes and invalid input become tool errors. Registry profile grants,
timeouts, output truncation and redacted tool logs apply normally. The operator
API and offline CLI retain their existing unrestricted single-operator behavior.

Tool selection contracts: schemas describe tool purpose and argument semantics;
filesystem and shell tools direct models to granted domain tools first. Validation
errors expose declared field paths and error kinds without submitted values or
arbitrary extra keys. Email resource discovery is the explicitly granted pack tool
`email.list_mailboxes`; it exposes only granted aliases, addresses, folders and
actions, with no connection or credential resolution. `email.search` supports
`unseen_only`, returns seen state and total matches, and preserves unread state.
Use a narrow report-only inbox profile and name aliases in scheduled tasks; see
[email pack guidance](packs/email/README.md).


## Conditional tool disclosure

Profile `tool_disclosure` is `eager` (default) or `deferred`; `tool_resident` is an
ordered list, empty by default. Jobs override mode with `inherit`/`eager`/`deferred`
and residents with null (inherit), [] (none) or an ordered replacement. All views
resolve effective tools from profile grants intersected with the job ceiling.
Inherited residents intersect that ceiling; explicit residents must already belong
to it. Save validation rejects duplicates, unavailable residents and reserved
`tools.*` names. Persisted profile drift narrows runtime exposure without invalidating
existing job definitions. Effects, resources, filesystem tiers, budgets and fragments remain profile-owned.

Each run freezes a generic ToolView containing granted metadata, schemas, effective
and resident order, mode and deterministic index/hash. Eager schemas and prompt
assembly retain their prior bytes/order. Deferred prompts insert the granted-only
family/name-sorted index after the profile fragment. Initial schemas are runtime
`tools.search`, `tools.load`, then residents; activated schemas append in activation
order. Discovery tools are built into a separate runtime namespace, never wildcard
grants or profile catalog tools. Search is deterministic lexical ranking with exact
name/underscore-alias preference (+1000) and a 1–5 result limit. Query function
words are dropped (pure-stopword queries fall back to the original tokens).
Each distinct query token scores separately per field: name ×10, keyword ×6,
summary ×4, docstring ×1. A match needs a name/keyword/summary signal or total
score at least 8. Ties sort by tool name; only qualifying matches activate. Load takes
1–5 exact names and returns loaded/already-loaded/rejected partitions. An unmatched
search succeeds with family/load guidance. A wholly rejected load is a nonrepairable
error; invalid discovery arguments also do not consume repair attempts.

Dispatch checks grants before intercepting an unloaded real tool. Interception
loads the schema and returns a nonrepairable error without execution or argument
validation. Normal effect/tier checks remain on actual execution. Activation
from earlier calls applies to later calls in the same sequential group. Pure
disclosure groups consume a separate eight-step cap; mixed/real groups consume
max_steps. Cap exhaustion is tool_error with "tool loading did not converge".
Total model steps and separate work/disclosure counters are recorded. Context byte
checks and compaction rechecks always use currently visible schemas. Delegated
workers use child-profile defaults; sub-session jobs carry job overrides; no
activation state survives a run.

Run.tool_view records mode, effective/resident names, index hash, activations and
per-request visible names. Initial schema evidence remains immutable; deferred
runs also write a full immutable, hash-verified tool catalog for historical step
inspection. Prompt hashes cover the static index. Missing older metadata means
historical eager behavior. /api/prompt accepts job for initial job inspection and
run_id plus optional step for saved schema evidence, independent of current grants.
/tools and sola check expose per-profile/job counts and byte diagnostics, warning
above 25% context budget for deferred initial payload or 50% for eager schemas.
Warnings neither switch modes nor impose hard limits. See
[operator guidance](docs/operations/tool-disclosure.md) for the live pinned-model
acceptance gate; interactive deferred mode remains opt-in and unevaluated live.

### Built-in read-only reference and calculation tools

Opt-in modules `web`, `feeds`, `github`, `wiki`, `datetime`, and `math` use
the standard tool registry and profile grants. They provide Brave web search,
Cloudflare Browser Rendering Markdown, RSS/Atom reads, read-only GitHub
notifications/PR status/releases, MediaWiki REST summaries with Wikidata identity,
IANA timezone/date math, and AST-whitelisted decimal arithmetic. Every definition
includes summary/keyword metadata for deferred disclosure. API credentials are
explicit env/file SecretRefs under `tool_apis` and register with redaction before
use. Remote reads are bounded and do not mutate upstream state; outputs remain
untrusted data. See `docs/operations/read-tools.md` for arguments and limits.


### Tool result and error contracts

Remote results are task-shaped projections, never raw upstream objects. Text
fields have character bounds and in-payload `<field>_truncated` flags; list
omissions similarly carry `<field>_truncated`. Structured remote results fit the
profile output cap before registry truncation. If even the structural minimum
cannot fit, the tool returns `ToolError("Output cap too small")` rather than
broken JSON. Original evidence and the registry's counted truncation remain
unchanged. Every tool has a one-line summary, keywords, and descriptions for all
parameters; schema constraints advertise numeric bounds before network calls.

A tool that can name its operational failure raises `ToolError` with its
model-facing message. Bare tool-code `ValueError` is a bug; private validation
seams may raise it when their caller converts it to `ToolError`. Registry
argument-validation backstops do not replace domain messages. Datetime tools
retain DST-gap/fold/naive-time/bad-date guidance and name unknown zones. Invalid
remote URL/proxy configuration and malformed upstream payloads are expected
errors without tool-log tracebacks.

GitHub PR output projects PR identity/state, author, head/base, combined status,
first-page contexts/checks and `checks_summary` over the fetched check runs.
Notifications project ID/unread/reason/update time, subject title/type/URL and
repository full name. Releases project tag/name/date/URL/flags, a body bounded
to 4000 characters with `body_truncated`, and asset name/size/download count.
GitHub result JSON is bounded to min(profile output cap, 12000 characters),
preserving summary counts when detail lists are shortened, and includes
`rate_remaining` when supplied upstream. Lists remain first-page snapshots,
not claims of exhaustive pagination.

`web.get_page(url, offset=0)` returns `url`, `markdown`, `returned_chars`,
`total_chars`, `truncated`. Slices contain at most min(profile cap, 16000)
source characters, reduced further for JSON escaping/envelope overhead. Advance
by `returned_chars`; the URL is fetched again on each call, so paging assumes
unchanged upstream content. Cloudflare error messages remain visible; a missing
successful result is an explicit tool error.

Remote HTTP reads retain status/headers/final URL, enforce 2 MB decoded bodies,
30-second request timeouts and `User-Agent: Solakit/0.1`. Fixed-host APIs do not
follow redirects. User-supplied feed GETs follow at most five redirects and
validate scheme, credentials, port and public DNS addresses on every target
before requesting it. Cross-host redirects strip Authorization. Private targets
and excess hops are named errors. This is a fetch guard, not OS containment or
protection against DNS rebinding.

`feeds.check` returns `final_url`, optional `etag`/`last_modified`,
`not_modified`, title and projected items. Optional `if_none_match` and
`if_modified_since` pass validators verbatim. A 304 returns
`not_modified=true, items=[]` without XML parsing. Atom `published` and `updated`
are distinct as-served fields; RSS `pubDate` remains an as-served string.
