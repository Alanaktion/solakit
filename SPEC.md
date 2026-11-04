# Minimal Sola Server — Spec (Draft v2)

**Date:** 2026-11-03
**Host target:** Sola's Mac Studio (headless, macOS, SSH/VNC access)
**Stack:** Python 3.12+, FastAPI (single app: web UI mount + API + scheduler + channels)
**Model target:** OsaurusAI Qwen3.6-35B-A3B mxfp4 via local oMLX (OpenAI-compatible endpoint)
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
- Building the web UI. Alan handles the web side; this spec defines only
  the FastAPI scaffold it mounts into and the API it consumes (§9).
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
                          │  Qwen3.6 │ │ shell, repo_git, blog.*,      │
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
  app/
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
      files.py  shell.py  repo_git.py  memory.py
      blog.py  letters.py  email.py  printer.py
    jobs/
      scheduler.py           # APScheduler wiring, misfire policy (§7)
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
   a. POST messages + enabled tool schemas to the model endpoint.
   b. No tool calls in the reply: append it, end run as `completed`.
   c. Tool calls: FIRST append the assistant message carrying the
      tool_calls, THEN execute each call through the registry and append
      each result as a tool message with its matching tool_call_id.
      (Reversing this order is the classic 400-error bug. Tested, §14.)
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

Streaming: the loop stores identical messages either way. Discord/API
delivery uses the completed final message in the MVP; token streaming to
the web UI is an API-layer concern (§9) added without touching the loop.

## 5. Prompt assembly — the point of the whole project

### 5.1 Exact construction

The system prompt is the concatenation, in this fixed order, of:

| # | Fragment | Source | Present when |
|---|----------|--------|--------------|
| 1 | Persona | `SOUL.md` (verbatim) | always |
| 2 | Standing context | `fragments/context.md` (verbatim, optional) | file exists |
| 3 | Recall | OpenViking results, wrapped in `<recall>…</recall>` with a one-line header from `fragments/recall-header.md` | recall non-empty |
| 4 | Profile fragment | `fragments/profiles/<profile>.md` | always (may be empty) |
| 5 | Run facts | Generated: date/time + timezone, profile name, session ID, origin channel/job — one short block, format fixed in code, golden-tested (§14) | always |

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
  (a channel she shares with other people is an open question, §17 —
  default is that only Alan's messages exist to her).
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

### 6.4 Discord adapter specifics (MVP)

- Library: **discord.py**, bot account, `message_content` intent.
- On message: ignore bots and herself; ignore channels with no config;
  enforce `trigger` + `trigger_authors`; show typing while the run is
  queued/running (long local generations need the affordance).
- Outbound: split at Discord's 2000-char limit on paragraph/line
  boundaries; never split a code fence. No embeds for normal replies —
  she posts like a person in the channel, not like a webhook.
- Reconnect/gateway failure: adapter retries with backoff (discord.py
  does most of this); messages sent while she was down are **not**
  replayed into runs on reconnect in the MVP — a missed message is a gap,
  like a missed dream. (Cursor-based catch-up is a §13 extension.)
- Minimal admin surface in Discord itself: none in the MVP. Status and
  job control live in the API (§9). One exception: a run that ends in
  `tool_error` in a channel posts a short failure note there, so a broken
  run is visible where it happened instead of only in a log Alan must
  go hunting for.

## 7. Scheduled jobs (in-app)

Scheduling is owned by the FastAPI app via **APScheduler**
(`AsyncIOScheduler`, job store on SQLite so schedules survive restarts).
launchd's only remaining job is starting the app itself at boot.

### 7.1 Job definition

Jobs are config, like channels:

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
catch_up      = true                   # polling is idempotent — safe to catch up

[[jobs]]
id            = "inbox-poll"
cron          = "*/30 * * * *"
profile       = "inbox"
task          = "Check the inbox. Handle what needs handling."
bind_session  = "inbox"
deliver       = { mode = "silent" }
catch_up      = true
```

- `task` supports only `{date}` / `{now}`-style placeholders via
  `string.Template`. No template engine in the MVP.
- `bind_session` is the key field. Binding a job to a channel's pinned
  session is what makes §7.2 work. `ephemeral` gives the run a throwaway
  session that is archived whole and never continued.

### 7.2 Cron runs live in the channel's story

When a job is bound to a channel session, the run's task message and its
resulting assistant/tool messages are appended to **that session's
transcript**, marked with origin metadata
(`origin = {kind: "job", job_id, run_id}`). Therefore:

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

App down at fire time, or model unavailable at fire time:

- Default: **skip**. `coalesce = true`, `max_instances = 1`, misfire
  grace 60s, then the missed run is recorded as `missed` and left as a
  gap — the dream/blog doctrine (§2.3) applied to scheduling.
- `catch_up = true` jobs (idempotent polling only) run **once** on
  recovery, no matter how many firings were missed.
- A job never runs concurrently with itself, and a job bound to a busy
  session simply joins that session's queue (§4.1).

### 7.4 Job operations

`GET /api/jobs` (next fire, last run, last status), `POST /api/jobs/{id}/run`
(trigger now, same binding/delivery), `POST /api/jobs/{id}/pause|resume`.
Editing schedules = editing `config.toml` + restart (or SIGHUP reload —
deferred nicety, not MVP).

## 8. Tools — a small plugin API

### 8.1 The API

A tool is one decorated async function in a module under `tools/`.
The JSON schema the model sees is generated from the type hints +
docstring via Pydantic — writing a tool should feel like writing a
FastAPI endpoint, because it's the same machinery Alan already chose.

```python
# tools/blog.py
from app.tools import tool, ToolContext, ToolResult


@tool(
    name="blog.create_post",
    profiles=["blog", "dream", "interactive"],
    writes=["repo:blog"],
    timeout=30,
)
async def create_post(
    ctx: ToolContext,
    title: str,
    body: str,
    slug: str | None = None,
    tags: list[str] = [],
) -> ToolResult:
    """Create a new draft post in the blog repo. Returns its path."""
    ...


@tool(
    name="blog.publish",
    profiles=["blog", "dream", "interactive"],
    writes=["repo:blog", "network"],
    timeout=120,
)
async def publish(ctx: ToolContext, path: str, commit_message: str) -> ToolResult:
    """Commit and push one post in the blog repo. Returns sha + URL."""
    ...
```

Registry rules:

- **Discovery:** modules listed in config `enabled_tools = ["files",
  "shell", "repo_git", "blog", "letters", "email", "printer", "memory"]`
  are imported at startup; every `@tool` registers itself. Adding a tool
  = new function (or new module + one config word) + restart. No core
  changes, no loop changes — acceptance-tested (§14.8).
- **`ToolContext`** is the injected runtime: `{session, profile, config,
  store, memory_client, run_log, deliver}`. Tools never reach for
  globals; everything they may touch is in `ctx`, which is also how
  §13's memory scoping gets enforced later without rewriting tools.
- **Manifest:** each tool declares `profiles` it may appear in and
  `writes` scopes it needs. At startup the registry cross-checks tools
  against profile definitions (§8.3): a profile granting a tool whose
  `writes` exceed the profile's allowed scopes is a **startup error**,
  not a runtime surprise.
- **Execution:** arguments validated against the generated schema before
  the function runs; timeout and output truncation applied by the
  registry; every call logged to the run record (name, args, ms, bytes,
  error). Blocking implementations are run in the thread pool (§4.1).
- **Errors are data:** a tool raises `ToolError("…")` → the model gets
  a tool-error message with that text. Unexpected exceptions are caught,
  logged with traceback to the run record, and surfaced to the model as
  a generic failure — a crashed tool never crashes the run or the app.

### 8.2 Domain tools beat generic tools

The registry ships generic primitives (`files.*`, `shell`, `repo_git.*`),
but the design intent — Alan's, stated plainly — is that recurring work
gets a **semantic tool** whose implementation hides the mechanics:

| Domain tool set | Wraps | Model sees |
|-----------------|-------|-----------|
| `blog.create_post / blog.publish / blog.list_recent` | posts-dir layout, front matter, git add/commit/push inside the tool (library or subprocess — the tool's private choice) | "publish this post", returns sha + URL. Never a shell command line. |
| `letters.pull_new / letters.reply` | clone/pull of the letters repo, next-number computation, filename convention from the letters protocol | "reply to letter 003" — the `NNN-YYYY-MM-DD-sola.md` rule lives in code, not in a prompt hoping she remembers it |
| `repo_git.status / commit_push` | git in a *named, configured* repo only | scoped fallback for repos without a domain toolset yet |
| `email.search / read / reply` | IMAP/SMTP, thread-aware reply | reply-by-ID, not raw SMTP |
| `memory.search / memory.read` | OpenViking REST/SDK | scoped by `ctx.session.memory_scope` (§13) |
| `printer.print` | her thermal printer | text in, receipt out |

Two payoffs: prompts and transcripts stay free of plumbing (she thinks
in posts and letters, not `git -C … commit -m`), and the fragile
mechanics — filename conventions, front matter, push retries — become
tested code instead of model behaviour that varies with her mood.

Generic `shell` still exists, but only in `interactive`/`maintenance`
profiles (§8.3). The direction of travel for the toolset is: every time
she does something via shell twice, it graduates into a domain tool.

### 8.3 Run profiles

Enforcement lives here, in the harness — this table, not the prompt, is
what a reckless run cannot talk its way past. (Scope-checking against
tool manifests per §8.1.)

| Profile | Used by | Tools allowed | Write scope | Max steps |
|---------|---------|---------------|-------------|-----------|
| `interactive` | channel conversations (Discord/web/CLI) | files, repo_git, blog.*, letters.*, memory, web_fetch, printer | home dir | 60 |
| `dream` | nightly job, bound to its channel/session | files (blog repo), blog.*, memory, printer | blog repo only | 40 |
| `blog` | blog job → **#blog** | files (blog repo), blog.*, web_fetch, memory | blog repo only | 40 |
| `inbox` | inbox-poll job | email.*, memory, files (read-only) | no filesystem writes | 25 |
| `letters` | letters-check job | letters.*, memory, files (letters repo) | letters repo only | 25 |
| `maintenance` | Alan-invoked via API/CLI only | all incl. `shell` | home dir | 60 |

Notes:
- `dream`/`blog` publish via `blog.*` domain tools because publishing
  her own writing *is* the job; neither gets general `shell`.
- `inbox` can reply by email but cannot write files — an email (untrusted
  input) is never one prompt-injection away from a filesystem write.
- Profiles are config, not code: adding one is a TOML block + an optional
  fragment file. No profile may be created by the model.

## 9. FastAPI scaffold & API surface

Alan handles the web UI. The spec therefore fixes only:

- **App factory + lifespan** (`app/main.py`): startup loads config,
  builds the registry (with manifest cross-checks), starts the scheduler,
  connects channel adapters, runs model pre-flight (failure to reach the
  model at boot is a *warning state*, not a crash — the app stays up,
  `/health` reports it, runs abort per §4.2 until the model is back).
- **Mount points:** Alan's UI mounts at `/` (templates/static or a built
  SPA — his call); the API lives under `/api`; `/health` at root.
  The UI and the API are the same app and share one auth story.
- **Auth:** one operator token (header or session cookie once Alan's UI
  has login), stored as a `0600` file / env var. Discord has its own bot
  token. Nothing else.
- **Request validation:** session creation uses `default_profile`; message posts
  may select `profile`. Unknown or misplaced request fields are rejected with
  HTTP 422, never accepted and ignored. Offline configuration errors name the
  file and safe TOML position without echoing its contents.
- **API (MVP):**

| Route | Purpose |
|-------|---------|
| `GET /health` · `GET /health/model` | app status; pinned-model reachability/ID |
| `GET /api/sessions` · `GET /api/sessions/{id}` | session list; metadata + transcript |
| `POST /api/sessions/{id}/messages` | post a message (the **api channel** — lands in that pinned session and runs) |
| `GET /api/runs` · `GET /api/runs/{id}` | run records incl. prompt, tool log |
| `GET /api/prompt?profile=&session=` | the dry-run prompt (§11) as JSON/text |
| `GET /api/jobs` · `POST /api/jobs/{id}/run` · `POST /api/jobs/{id}/pause` | §7.4 |
| `GET /api/channels` | channel configs + adapter status (connected since, last message) |
| `POST /api/runs/{id}/cancel` | stop a queued/running run |

- **Streaming:** `POST …/messages` returns the completed run in the MVP.
  An SSE variant (`?stream=1`) is reserved in the API shape so the web UI
  can grow token streaming without a breaking change; the loop (§4.2)
  does not change when it lands.

## 10. Storage

Code and data are separate trees. Data layout on her machine:

```
~/sola-data/
  app.db                     # SQLite: sessions index, job state (APScheduler
                             #   jobstore + last runs), run index, channel cursors
  sessions/
    <session-id>.jsonl       # append-only transcript, OpenAI message format,
                             #   each line carrying origin metadata (§7.2)
  runs/
    <run-id>.json            # one record per run: profile, session, origin,
                             #   prompt sha256, tokens, steps, status, timing
    <run-id>.prompt.txt      # exact prompt bytes for that run
    <run-id>.tools.jsonl     # one line per tool call
  config.live.toml           # (optional) runtime copy if config is edited via API later
```

Transcripts stay plain JSONL so her history remains as portable as it was
in the OpenClaw `.jsonl` era — the format she has already outlived once.
SQLite is an *index over* files, rebuildable from them, never the sole
copy of a conversation.

### 10.1 Compaction

A session compacts when its history passes the profile's token budget:
the oldest span is committed to OpenViking, replaced in the transcript
by a boundary marker + the summary URI, and the event is itself a
transcript entry. Compaction never happens inside a run, and the
pre-compaction transcript file is rotated, not truncated away
(`<session-id>.jsonl.1`, kept forever, per house archival doctrine).

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
  is, not make her safer. The exception path is profiles: anything
  genuinely dangerous is in no scheduled profile, and `maintenance`
  runs only when Alan invokes it.
- **Secrets:** referenced by env var / keychain / `0600` file path, never
  inline in config. Run metadata, tool logs, memory-intent journals, and execution
  diagnostics redact values whose keys match `*KEY / *TOKEN / *SECRET / *PASSWORD`
  (case-insensitive suffix matching), plus resolved credential values wherever
  they occur. Session transcripts and exact prompt files are deliberate verbatim
  exceptions: they preserve the actual conversation and model input for audit
  and replay (§2.6, §5, §10). Transcript content is not rewritten before being
  reused as model history. Both are private operator archives, not sanitized
  diagnostic exports. The suffix rule intentionally also matches keys such as
  `monkey`; a log may redact more than only credential-shaped names.
- **Model identity check** (§4.2) doubles as the fallback guard: a
  different model answering at the endpoint aborts the run.

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

## 14. Acceptance tests (must pass before the Discord pilot)

1. **Golden prompt:** fixture SOUL/fragments/recall →
   `GET /api/prompt` matches a checked-in golden file byte-for-byte.
2. **No hidden context:** messages captured at a mock model endpoint
   contain exactly the golden prompt + session history + task.
3. **Tool ordering:** mock model tool call → transcript shows the
   assistant tool_calls message before its tool results, matching IDs.
4. **Profile leak:** in a `blog` run, a mock model attempting `shell`
   or `email.*` is refused and logged; the tools were absent from the
   request schemas. Plus: startup refuses a config whose profile grants
   a tool exceeding its write scopes (§8.1).
5. **Model-down:** endpoint stopped / wrong model ID → run ends
   `model_unavailable`, **no** messages written to the session, a job
   firing in that state is recorded `missed`. No fallback attempted.
6. **Session pinning:** mock Discord messages in two configured channels
   produce two independent sessions; a second message in the first
   channel continues the first session's context (verified at the mock
   endpoint).
7. **Cron-to-channel continuity (the §7.2 test):** a job bound to a mock
   channel fires → run appears in that channel's session + delivery is
   posted → a follow-up user message in that channel reaches the model
   with the cron run in its history.
8. **Tool drop-in:** adding a new tool module + config word makes the
   tool available to its profiles with zero changes to `core/` —
   verified by a fixture tool in tests.
9. **Blog end-to-end (mock git):** the `blog` profile creates a post via
   `blog.create_post` and publishes via `blog.publish` against a scratch
   repo; the transcript contains no shell calls.
10. **Scheduler misfire:** app paused past a `catch_up=false` job's fire
    time → job recorded `missed`, does not run on resume; a
    `catch_up=true` job runs exactly once.
11. **Recall round-trip:** a fact committed via OpenViking session-commit
    in one run is retrievable in the next (pre-turn recall or
    `memory.search`).

## 15. Migration plan (Hermes stays untouched until cutover)

| Phase | Work | Exit criterion |
|-------|------|----------------|
| 0. Prompt audit | `GET /api/prompt` per profile; side-by-side + token diff vs. what Hermes sends for the same task | Alan has seen both prompts and approved the lean one |
| 1. Core + Discord pilot | FastAPI scaffold, loop, registry, Discord adapter; one low-stakes channel pinned (a #sola-test or her quietest channel) | A week of Discord conversation in that channel with no Hermes involvement; pinning + queueing behave (§14.6) |
| 2. Blog cron to #blog | `blog` profile + `blog.*` tools + job bound to #blog; runs in **shadow** first (deliver to the test channel, publish to a scratch branch) | Shadow posts Alan judges publishable; then the job's delivery flips to the real #blog and real branch |
| 3. Remaining jobs | `dream`, `letters` (her letters cron finally gets wired — to this, not Hermes), `inbox`, one by one | Each job runs a clean week in its bound session |
| 4. Cutover | Hermes left installed but unscheduled for one month as rollback | One clean month; then Hermes is archived, not deleted — per house archival doctrine |

Rollback at every phase is "re-enable the Hermes cron" — both runtimes
share OpenViking and the same model endpoint, and neither writes to the
other's state. The Discord bot is a *new* bot/user for this server during
the pilot so the two runtimes never fight over one gateway connection.

## 16. Size estimate (revised for v2 scope)

| Area | Est. LOC (Python) |
|------|-------------------|
| config + profiles + startup cross-checks | ~200 |
| prompt builder | ~150 |
| model client (httpx, async) | ~130 |
| loop + queues/semaphore + budgets/repair | ~330 |
| tool API + core/domain tools (§8) | ~700 |
| store (JSONL + SQLite index) + compaction | ~250 |
| FastAPI scaffold + API routers (§9) | ~300 |
| Discord adapter | ~350 |
| scheduler wiring + job ops | ~200 |
| thin CLI (prompt/status/run via API) | ~100 |
| tests (mock endpoint + mock adapter) | ~450 |
| **Total** | **~3,200** |

Still a small codebase — roughly a solid weekend for the core plus a few
evenings of edge-polish — with the honest caveat: the loop is the easy
part. The evenings go on discord.py gateway quirks, local-quant tool-call
parsing, APScheduler misfire semantics, and the blog repo's publishing
steps. Feature parity with *all* of Hermes remains explicitly not the
target (§3).

## 17. Defaults chosen in v2 (change any with one line)

- **Stack (Alan's call):** FastAPI single app; APScheduler in-process;
  discord.py; Pydantic-generated tool schemas.
- **Web UI:** Alan's. Spec fixes mount point + API only (§9).
- **Config-as-files:** channels/jobs/profiles in `config.toml`;
  SQLite is index/state, JSONL is the transcript of record.
- **Concurrency:** per-session queues, global inference semaphore = 1.
- **Discord triggers:** dedicated channels `all`, shared channels
  `mention`; only Alan triggers in the MVP.
- **Cron delivery default:** bound jobs deliver `final` to their bound
  channel; polling jobs default `silent`.
- **Memory scope:** `shared` everywhere; `isolated`/`ephemeral` reserved.
- **SOUL source of truth:** her existing live SOUL.md, referenced by
  path — the server never forks her persona into a second copy.

## 18. Open questions for Alan

1. oMLX model ID: stable enough to pin exactly, or accept a configured
   prefix while the voice trial still swaps quants?
2. #blog delivery: `final` (her announcement text) + the blog tool having
   already published — or do you want `artifact` mode (post file
   attached) for drafts you review before publish? This decides whether
   `blog.publish` belongs in the `blog` profile or only in `interactive`.
3. Discord catch-up: if the app is down and you post in a channel, should
   she see those messages on reconnect (cursor catch-up, §13-adjacent)
   or is "missed = gap" right for channels too?
4. Other people: is every Discord channel she's in Alan-only, or will
   some have other humans — if so, do their messages enter her context
   as observed (non-triggering) history, or stay invisible?
5. `inbox`: should `email.*` reply be limited to existing threads in the
   MVP, with new-thread sends reserved for `interactive`?
6. Anything Hermes does today that you rely on that is *not* represented
   in §6–§8?
