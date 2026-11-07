# Solakit: contributor and agent guide

Solakit is Sola's small, single-operator personal assistant runtime. It talks to
one pinned OpenAI-compatible local model (currently oMLX), uses an existing
OpenViking server for external memory, and keeps portable evidence of sessions,
model runs and tool execution. It is a localhost/tailnet application, not a
multi-user service. The moonlight-themed operator UI is part of the same app.

Read `SPEC.md` for behavioral contracts, `README.md` for an introduction,
`docs/setup.md` and `docs/operations.md` for operator guidance, and
`config.example.toml` for configuration. Keep these consistent with code.

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
  Delegation reuses the current inference slot via submit_nested and per-session
  locks; never queue-and-wait while holding the semaphore. Inject nested_submit
  through ToolContext, allow the current run profile by default and restrict other
  workers with delegation.allowed_profiles. Retain private ephemeral child evidence
  and return only final reports. Propagate parent
  cancellation through children; manifests that own timeouts must bound them and
  retain operation receipts. Worker grants control further delegation.
- `core/model.py`, `core/model_stream.py`: one exact pinned model, preflight and
  completion validation, bounded OpenAI SSE assembly,
  explicit authentication, bounded generation; no provider fallback.
- `core/prompt.py`: deterministic assembly from operator-owned SOUL/fragments,
  run metadata, scoped recall and tool definitions. Prompt bytes/hashes are
  evidence; the UI must not reconstruct or rewrite them.
- `core/store.py`, `core/schemas.py`, `core/io.py`: immutable JSONL evidence,
  atomic metadata, durable I/O, exclusive data ownership and rebuildable SQLite
  indexes. The app.db `job_definitions` table is authoritative for managed jobs;
  index rebuilds preserve it, and database backups are required.
  Startup compares versioned source fingerprints and refreshes changed units only;
  explicit rebuilds revalidate all evidence. Cache rows/fingerprints commit together;
  runtime writes leave stale fingerprints for crash-safe startup reconciliation.
  Metadata-only session edits reuse FTS; cached damage stays visible until repaired.
  Session lifecycle archival is the archived_at field in session metadata, distinct
  from transcript rotation. Default lists hide archived sessions; FTS retains them.
  The derived session_activity table uses messages and run admission times, not
  import/recovery bookkeeping dates. Bulk operations protect live bindings/work
  under scheduler-then-execution locks; restore is explicit before new work.
  `core/search.py` queries standalone trigram FTS in the same `app.db`;
  physical archive/active line addresses survive rebuild, and failed indexing
  never fails an evidence append. Offline `sola search` reads the index without
  initializing/recovering Store. `core/compaction.py` and `core/archival.py` rotate immutable archives with
  recovery metadata. Operator archival and explicit compaction reject busy sessions
  and preserve bindings. Profile `mid_run_compaction` opts into between-step
  compaction after complete tool groups; full run memory commits include archives.
  Compaction preparation/rotation is one durable operation, and unresolved prepared
  state blocks further transcript writes until recovery.
- `core/memory.py`, `core/memory_ops.py`: scope-bound OpenViking REST access,
  recall, explicit commit/extraction tracking, compaction and operator scratch
  verification. Startup only performs authenticated read-only root checks.
  Recall uses bounded recent text and configurable trivial-turn skips; persist
  recall_status independently of extraction status. memory.remember stages exact
  notes in private immutable runs/*.notes.jsonl, then joins the existing commit
  with a fixed marker. Never hand-author OpenViking's managed memory layout or
  claim staged notes are stored. No operator policy fragments are installed.
- `services/`: internal scope-checked filesystem, alias-bound Git and collection
  resources. `ctx.fs`, `ctx.git(alias)` and `ctx.collection(alias)` let packs
  compose operations privately. Generic tools are thin service wrappers.
- `tools/`: registry/decorator/manifests, generated argument schemas, tool
  implementations and per-profile access enforcement. `enabled_tools` loads
  modules by explicit dotted path (bare names are built-in shorthand); profile
  `tools` grants exact names, first-family patterns (`files.*`), or `"*"`.
  Wildcards do not grant effects/resources or bypass write-tier validation. Both module
  loading and profile grants are necessary. A manifest `profiles=["*"]`
  supports arbitrary configured profiles without granting calls automatically.
  Filesystem reach uses read/write tiers and workspace roots; effects and named
  pack resources are separate explicit grants. Legacy keys translate for one release.
  `sessions.search` reads indexed active/archive evidence for the calling session;
  cross-session or all-session search additionally requires the `operate` effect.
  Return masked snippets as historical data and never rewrite the evidence.
- `packs/`: independently packaged journal, letters and email
  extensions (`sola_packs.*`), plus `solakit-packs-common` support. Core distributions
  exclude packs; install and test them separately. Letters/mailbox schemas live
  in the pack-owned `SOLA_PACKS_CONFIG` sidecar. Resolved pack credentials register
  with the existing redactor before network access. Collections remain named repo
  subdirectories; `collection:<alias>` grants only that directory.
- `channels/`: API and opt-in Discord adapters through shared session execution.
  Discord image attachments use core/media.py for bounded immutable source/JPEG
  evidence. Messages retain image refs; the model client projects those into
  OpenAI image_url data parts, including later/tool/streaming turns. Never silently
  drop pixels or forward signed CDN URLs. Back up media/ with the evidence tree.
  `jobs/`: API-managed SQLite job definitions, one-time TOML import, durable JSONL
  cursors and opt-in APScheduler dispatch using shared execution/delivery.
  Built-in `jobs` tools use the injected scheduler; jobs.run queues without waiting
  to avoid same-chat deadlock. Capability/scope checks happen under the job lock.
  Jobs choose either cron or a one-time run_at instant. Persist consumed state
  before one-time dispatch; restart/enable/task edits must never replay it.
  Only a changed future run_at rearms it. Unattempted overdue jobs catch up once.
  Job tools=None inherits profile grants; an exact list (including []) narrows
  schemas and dispatch through the generic per-run ceiling. Never clone/replace
  profile effects, resources, filesystem tiers, budgets or fragments for a job. Validate saves;
  persisted profile drift shrinks exposure and remains visible in jobs inspection.
  Generic ToolView resolves eager/deferred schema visibility independently of authority.
  Deferred search/load and inactive-tool notices use a separate eight-step budget;
  real/mixed work still uses profile max_steps. Keep activation state per run,
  and budget the currently visible schemas. tools.* is reserved, never wildcard-granted.
  Deferred *.tool-catalog.json plus hash and per-step visible names preserve later
  schema exposure; job overrides apply to sub-sessions, not delegated child profiles.
  effective_tools is admission evidence; immutable *.tool-schemas.json plus its
  run hash preserves schema inputs. Historical prompt inspection uses saved inputs,
  never today's profile grants or modules. Keep schema definitions out of recursive
  diagnostic key redaction; UI masks resolved values before JSON encoding.
- `ui/`: Jinja2 pages, packaged local HTMX/CSS/vanilla JS, opaque persistent login
  sessions, API-only presentation routes and escaped/redacted transcript views.
  No Node build or CDN. Chat fills the viewport with a desktop session sidebar;
  colors follow `prefers-color-scheme`. `/tools` shows loaded versus granted tools.
  Use grid when both axes have designed tracks (chat/sidebar, job fields, record
  columns); use flex for a line or stack whose item count/width can change
  (actions, filters, status chips, metadata). Keep actions in `.action-group`
  with intrinsic button widths. Single-field editors use `.edit-form` and
  `.form-field`; do not reuse `.new-session-form`'s fixed creation grid.
- `cli.py`: offline checks, server launch, API inspection and explicit operator
  operations. Tests are under `tests/` and use mocked transports/controlled clocks.

## Invariants when changing code

- Preserve evidence: session/model transcript text is verbatim, including tool
  calls/results. Do not silently redact, rewrite or delete history. Diagnostics
  and tool logs are redacted, and UI presentation masks resolved credentials.
  Render tool-card arguments/results from redacted run logs, never raw transcript
  payloads. Assistant chat Markdown is parsed with raw HTML/images disabled and
  sanitized after credential masking. User/tool/prompt evidence stays literal.
  Streaming previews are unsaved, bounded and separate from transcript evidence;
  mask trailing secret prefixes before publication. Never execute partial calls.
- Domain behavior belongs in external packs. No blog/letters/email/printer
  modules under `src/solakit`, no domain imports or branches in core execution.
  A future tool PR touching `core/loop.py` is a review finding: fix the generic
  service seam instead. No entry-point discovery or auto-loading is needed.
- Pack send/reply receipts are append-only private evidence. Pending/uncertain
  intents must not auto-replay, including cancellation and restart. Keep external
  mail/letter content explicitly marked as data. Sender-only email replies derive
  their envelope and threading from an existing stable mailbox message identity.
- Shell is built in with explicit tool grants, the `shell` effect and cwd
  write reach. Any profile may be granted access; do not hard-code profile names.
  Its cwd check is not a sandbox: commands have host-user privileges. Bound output
  and kill/drain process groups on timeout/cancellation; no per-command approval
  framework is part of this app.
- Git writes check the actual named repository/files. Generic writes require
  repository containment in write roots; collection services enforce directory limits.
  Push additionally requires the `push` effect, even inside packs. Do not stage whole
  directories, force-push, rebase implicitly or retry uncertain publish/send work.
- Tools must pass registry/schema/profile/access checks. Append the assistant
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
- Keep integrations disabled by default; enable channels/jobs individually.
  Configuration is not evidence of acceptance. A startup memory connection check does not verify
  extraction, writes, scratch round-trip, or authorize rollout. Do not touch the
  existing Hermes installation or schedules as part of Solakit work.
- Profiles/channels/resources stay in files. Managed scheduled jobs are the
  authorized exception: API/UI/chat share authoritative app.db job definitions,
  with one-time legacy TOML import and deletion tombstones. Keep domain behavior
  in tool/adaptor modules and never add a second job store.

## Development and validation

Python 3.12+ and uv are required. From the repository root:

```sh
uv sync --frozen --all-packages
uv run sola check
uv run pytest tests packs/*/tests -q
uv run ruff check .
uv run ruff format --check .
uv build
```

`sola check` needs operator-owned configuration, secrets and referenced prompt
files; automated tests supply isolated fixtures. Before running `sola check`,
you can source `.env` to supply operator secrets (`set -a; source .env; set +a`).
In the managed workspace use
`UV_CACHE_DIR=/workspace/.cache/uv` if needed. Never copy live credentials/data
into fixtures or artifacts. For local UI use `uv run sola serve`, open port 8382,
and sign in with the operator token (separate from the model API key). Use
`--log-level debug` for sanitized backend diagnostics.

Add meaningful regression checks for behavioral fixes; mocks are not live service
verification. Check packaged templates/assets when changing UI packaging. Report
browser visual QA separately from HTTP tests. Synchronize docs, then commit and push directly to main without force. No public deployment is implied by local implementation.

Pack builds (the workspace sync above already installs all members):

```sh
uv build packs/journal
# Or build all distributions: uv build --all-packages
```

Core and eight packs share a uv workspace/lockfile. Always use
`uv sync --frozen --all-packages` for this checkout; plain `uv sync` selects core
only. Workspace membership is not a core dependency: core wheel metadata and
code remain pack-free. `uv run` needs no `--no-sync` after workspace sync.
Standalone deployments outside the workspace declare desired pack distributions
in their own dependency manifest. Pack code is operator-trusted Python, not
sandboxed code. Services enforce model-call arguments, not a security boundary
against a malicious installed package.

## Product scope

Core execution, memory, generic tools/services, optional packs, Discord, scheduled
jobs, and the operator UI are complete and tested. Keep integrations opt-in and
preserve the behavioral contracts when extending the app. Per-message UI profile
selection and Discord archival commands remain outside the current scope.

Chat sends on Enter; Shift+Enter inserts a newline, with IME and busy guards.
Keyboard unit tests use Node if available; the app needs no Node build/runtime.
