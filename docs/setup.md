# Setup and configuration

Fastest path on a new machine:

```sh
uv sync --frozen --all-packages
uv run sola init ~/sola-site --preset qwen3-35b-a3b-omlx   # or generic-openai
cd ~/sola-site && cp .env.example .env && chmod 600 .env    # fill in the token
set -a; source .env; set +a
SOLA_CONFIG=config.toml uv run --project /path/to/solakit sola check
SOLA_CONFIG=config.toml uv run --project /path/to/solakit sola doctor
SOLA_CONFIG=config.toml uv run --project /path/to/solakit sola serve
```

`sola init` writes `config.toml` from a packaged preset (annotated with the
`sola doctor`/`sola eval` measurements that justified it), a SOUL skeleton,
`fragments/profiles/<profile>.md` for every preset profile, a recall header, a
sample playbook, `.gitignore` and `.env.example` (names only, never values). It
refuses to overwrite; `--force` writes `<name>.new` siblings and prints a diff.
`--yes` accepts defaults non-interactively. Templates are starting points: the
runtime only reads your files.

Manual path:

```sh
uv sync --frozen --all-packages
cp config.example.toml config.toml
# Configure the reference to the existing live SOUL.md and an external data tree.
# Provide an operator-token env reference or a mode-0600 token-file reference.
# Create fragments/profiles/<profile>.md for every profile (empty is valid).
uv run sola check
uv run sola serve
```

Core and all eight packs share one uv workspace lockfile. The sync above installs
every distribution; enabled modules and profile grants still control use. Run
`uv run pytest tests packs/*/tests -q` for the full suite. Plain `uv sync` selects
only core when that is wanted; the core wheel has no pack code or dependencies.

`sola check`, startup logs and `runtime.status` report each configured tool
module's installed distribution/version. The same list is available as
`distributions` in `/api/tools` and `tool_distributions` in runtime status.
Missing pack metadata fails before import with the missing distribution and
`uv sync --frozen --all-packages` repair command. External source-only modules
without distribution metadata are reported as unpackaged with unknown version.

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
contents. Startup performs the same readability checks; the curated front-page size cap
is enforced only by `sola check`. Model unavailability leaves the app
up with degraded health; runs use the exact pinned model or fail without fallback.
Run uvicorn with one worker; a data ownership lock prevents concurrent recovery.

When TLS terminates at a trusted reverse proxy, launch Uvicorn with proxy headers
and restrict trust to the proxy's actual connection address. For a local IPv4
proxy (including a locally configured `tailscale serve` backend), for example:

```sh
uv run uvicorn solakit.main:app --host 127.0.0.1 --port 8382 \
  --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1
```

Use `::1` instead if that is the proxy peer; never trust arbitrary forwarded
headers with `*`. Configure the proxy to preserve the public Host and forward
`X-Forwarded-Proto` and `X-Forwarded-For`. These determine HTTPS cookie security
and the client address used by the login rate limit. Without trusted forwarding,
a TLS-terminating proxy can produce non-Secure cookies and put all clients in
one login failure bucket. `sola serve` uses Uvicorn's default proxy trust settings;
use the explicit invocation above when configuring a proxy.

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
and reported health. UI icons use packaged local Lucide SVG symbols; they need
no network access, JavaScript initialization, or additional configuration.
Opening `/` selects the latest active topic or shows New topic. Chat fills the
viewport with a topic sidebar, Markdown assistant replies, user bubbles and a
bottom-anchored composer. On mobile, Topics opens a drawer. The sidebar excludes
cron sessions and offers title filtering, pinned channels and archived topics.
Sessions use their configured default profile for chat. Details refreshes recent
runs and exposes prompt/history links, recorded metrics and session metadata;
Actions contains rename, compaction, transcript rotation and topic archival.

Settings contains System, Jobs, Tools & profiles, Sessions, Evidence and Search.
Tools & profiles lists loaded tools, argument schemas, write requirements and
profile grants. Sessions holds administrative filters, bulk actions and advanced
creation. Evidence preserves API-assembled prompts, original hashes, stored
messages, redacted tools, diagnostics, memory/delivery events and cancellation.
Jobs shows adapter state and lets you create, edit, delete, run, pause and resume
jobs. The status dot links to System; old navigation URLs permanently redirect.
Press Enter to send in chat, Shift+Enter for a newline (IME composition is preserved).

Login exchanges the token for an opaque, HttpOnly, SameSite=Lax cookie valid for
five days of inactivity (Secure on HTTPS), renewed on use. Sessions survive server
restarts; signing out or changing the operator token revokes them. Private
`ui-sessions.json` in the data directory stores hashed IDs and expiry times
(sliding renewals are written at most once a minute).
Login returns to the originally requested local page. Five failed attempts lock
login for 60 seconds. `/api` accepts either
this cookie or the existing `Authorization: Bearer <operator-token>` header.
The token is never placed in page content or browser storage.
Unsafe cookie-authenticated requests require `Sec-Fetch-Site: same-origin` when
that header is present, and an `Origin` matching the public request origin
(including port) when supplied. This rejects forms from another port on the same
host. Clients without both headers remain supported. Valid Bearer-authenticated
API requests do not depend on this cookie-origin policy.

Secret values are redacted longest-first in diagnostics and UI presentation.
Matching covers exact resolved values, not base64, URL-encoded or other transformed
representations; never intentionally expose transformed credentials in tool output.


Chat streams assistant text as an explicitly unsaved preview, then replaces it
with the stored turn and redacted tool evidence. The composer stays disabled with
elapsed time while queued/running. Reloading resumes observation; disconnecting
does not cancel accepted work or resubmit it. Check recorded state before retrying
a dropped connection. Cancellation is available from the run evidence page.

Assistant messages support headings, lists, quotes, links, code fences and tables.
Raw HTML and image embeds in assistant Markdown are disabled; rendered HTML is
sanitized. Discord attachments have a separate
[authenticated vision path](operations/vision.md). User text, tool
logs, summaries and prompt/run evidence remain literal; original messages and hashes
are unchanged. Live output masks full credentials and unfinished secret prefixes.
The streaming parser accepts oMLX's legacy exact empty keepalive frame as a
transport heartbeat. It cannot carry content, tool calls, usage or a terminal
reason; all completion data still requires the exact pinned model ID.

All templates, CSS, JavaScript and vendored HTMX ship locally; Markdown is rendered
by Python (`markdown-it-py` plus `nh3`), without a Node build, CDN or hosting service.

API messages still wait for completion by default. `?wait=false` returns HTTP 202
with the accepted run ID; `?stream=true` requests OpenAI token streaming from the
model. `/api/runs/{id}?live=true` adds a credential-masked transient preview.
The web UI observes authenticated `/sessions/{id}/events` SSE HTML snapshots,
coalesced to at most five updates per second. Backends must support OpenAI SSE,
including `stream_options.include_usage`; unsupported/malformed streams fail
explicitly, with no automatic fallback/replay.

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

In the managed workspace use `UV_CACHE_DIR=/workspace/.cache/uv`.

Scheduled jobs can opt into `sub_session = true` for fresh child sessions with
shared memory and final-only channel delivery. Configure this in the job form,
API or chat tools; see [job management](operations/jobs.md).

Relative tool paths resolve against optional `[server].cwd`, including `files.list(".")`,
shared filesystem services and shell directory arguments. The directory must exist;
`~` expands and relative configuration values resolve against the configuration
directory. When omitted, paths use the server process working directory as before.
This setting does not change the process working directory or grant filesystem
access: profile read/write scopes and symlink checks still apply. Absolute tool
paths are unaffected.

An optional operator-owned `fragments/memory.md` holds curated facts and pointers,
read verbatim on every run before context and recall. It needs no installation
and is included even when `[memory].enabled = false`. Set
`[memory].front_page_max_chars` (default 8192, minimum 256) for offline curation
checks: 75–100% warns, above 100% errors. Runtime preserves the entire file.

Synchronous tool schemas disclose that worker threads can continue after timeout.
Prefer async tools for effects; inspect state or receipts before retrying.

`GET /health` remains public. The active `GET /health/model` probe requires the
operator Bearer token or a logged-in UI cookie.

Runtime data must remain outside the source checkout when running from source.
Installed wheels protect their own `solakit` package directory; the interpreter's
entire library directory is not treated as the Solakit code tree. Symlink-resolved
data paths receive the same check.
