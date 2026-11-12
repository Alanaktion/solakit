# Runtime operations

The `/operations` page keeps job schedules, state, timing, and actions visible.
Expand **Job details** for the task, session binding, and labeled tool settings;
profile-drift warnings remain visible without expanding. Narrow screens stack
records so actions remain accessible without horizontal scrolling. Channels show
adapter-reported connection state separately from whether they are enabled.

UI logins survive restarts using private `ui-sessions.json` in the data directory.
Sessions expire after five days without requests and renew on use. Signing out
revokes the current session; changing the operator token invalidates all sessions.
Malformed or unreadable saved session data is discarded with a sanitized warning
and requires fresh browser login; persistence errors still surface normally.
With the server stopped, removing this file signs out every browser. Login returns
to the requested local page, including its query parameters.

Each run in session history shows its creation date and time beside the run ID,
formatted in your browser’s timezone (UTC before JavaScript loads).

Chat tool calls use compact expandable rows, with a spinner while a call is
running. Animated dots indicate model activity and typing during streaming
previews; indicators stop when the run settles and respect reduced motion.

## API and execution

The web UI opens `/` into the most recently active, non-archived root topic,
excluding scheduled sessions. With no topics it shows New topic; visiting a page
never creates a session. “Topic” is UI language for the existing session concept.
The sidebar pins configured channel topics, lists other topics by activity, and
keeps archived topics in a collapsed section. Its title filter covers the rendered
page; More topics and More archived page through older records. Untitled topics
use the first run's user text where available, otherwise their session ID.
Delegated topics and parent navigation live in Topic Details, outside the sidebar. On phones, Topics opens a modal
drawer: background controls become inert, Tab stays inside, and Escape restores
focus to its opener. Resizing to desktop releases the modal state. Details uses
the same keyboard behavior and preserves focus when its contents refresh.
Filtering announces matches (or no matches) and reveals matching archived topics.
Code/evidence blocks support keyboard scrolling; Copy results are announced.
Relative dates use the browser locale; the full timestamp is available on hover.

Use **Details** (refreshed when opened) for recent runs, duration/tokens when recorded, prompt inspection,
numbered archives, memory scope/status and session metadata. **Actions** holds
rename, compact, transcript rotation, and topic archive/restore. Archived topics
remain read-only until restored. Transcript rotation and topic archival remain
different operations. Full-text search opens the aggregated topic at the matching
message, including messages in numbered archives.

**Settings** contains System, Jobs, Tools & profiles, Sessions, Evidence and
Search. Evidence includes runs and the prompt inspector. The Sessions table,
filters, bulk archival and cleanup live at `/settings/sessions`; advanced session
creation still exposes profile and memory controls there. Sign out lives in the
Settings sidebar. Back to chat returns to the latest topic. Old `/operations`,
`/tools`, `/prompt`, `/runs`, `/model-health` and `/search` URLs permanently redirect
to their Settings pages, preserving query parameters and subordinate pages;
existing POST action URLs remain usable.

The header status dot links to System. Green means model/storage healthy and
memory ready or disabled; red means model unavailable or a reported storage fault;
amber means unknown/degraded health, a job whose last outcome failed, or delayed
memory extraction. A read-only check every 15 seconds changes the dot and its
accessible description without replacing header markup. Extraction checks sample
the latest 100 runs, including archived sessions: failed/lost extraction states,
or accepted/pending states older than five minutes, produce amber. This is a
bounded diagnostic, not proof of successful memory writes; memory connectivity
still describes the startup check. Details does not report an exact lifetime
compaction count because the existing API does not expose one independently of
loading all transcript archives.

Create `POST /api/sessions` with `{"id":"web-main","default_profile":"interactive"}`.
Post `{"message":"Hello"}` to `/api/sessions/web-main/messages`; optional `profile`
selects that run's profile. Unknown/misplaced fields are rejected, not ignored.
Inspect `/api/tools` for loaded tools and effective profile grants. The UI’s
`/settings/tools` page groups tools by family, shows profile grants on each row, and
uses compact rows with descriptions and argument schemas available on demand.
Profile access separates tool grants, read/write tiers and roots, effects,
resources, and available tools into labeled rows. Expand Profile access
for tool links or Configuration & diagnostics for modules and disclosure details.
Session listings default to active sessions, paginated and ordered by last activity.
Sessions bound to a configured channel (enabled or not) are created at startup and are
pinned by default; any session can be pinned or unpinned with
`POST /api/sessions/{id}/pin` and `{"pinned": true|false|null}` (`null` restores the
default). The choice is persisted in session metadata. Listings and
`GET /api/sessions/{id}` report the effective `pinned` flag; filter with `pinned=true|false`
and list pinned sessions first with `pin_channels=true`. The chat sidebar fetches
pinned chats separately from the paged Recent list, so paging never hides them.
Use `status=archived|all`, `q`, `profile`, `before`, `limit`, `offset`, and `page=true`
for the full catalog. [Session archival](operations/session-archive.md) describes
reversible metadata archival, bulk cleanup and undo for imported history. Message
search includes archived sessions; default run listings exclude their runs unless
`include_archived=true` is requested.
Inspect `/api/sessions`, `/api/sessions/{id}`, `/api/runs`, and `/api/runs/{id}`.
`GET /api/runs` keeps its array response and defaults to 100 records. Use
`limit=1..1000` and a nonnegative `offset` to page through it; continue until a
page is shorter than the requested limit. Optional `session_id` and `status`
filters apply before pagination. Default order follows index insertion order;
`descending=true` sorts by creation time and ID, newest first. Pages reflect live
state, not a frozen snapshot. The Runs UI provides filtered pages of 100 records.

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
The conversation shows one continuous timeline: numbered archives from oldest to
newest, followed by the active context. The active context's latest 20 turns appear first; load
earlier messages to fill a 20-turn window across archive boundaries. Compaction
summaries stay collapsed as **Context compacted** dividers, with a date, exact
summary, archive link and reminder that Sola no longer holds the original turns
verbatim. Manual rotations have distinct **Archived** dividers with a date and
record count. An empty active context shows its latest archive seam and explains
that earlier messages live in the archives; use Load earlier to read them. The history picker also opens individual
archives. Nothing is deleted or editable.
On narrow screens the rename/compact/archive actions sit under **Actions**.
Menus close on Escape or an outside click. Settled code and evidence blocks have
a **Copy** button for the displayed (credential-masked) text, and **Latest
messages** returns to the end of a long conversation.
A failed prepared rotation blocks further transcript writes; restart after fixing
storage to finish recovery.

Per-session FIFO workers feed a global inference semaphore (default one). Model
preflight and the first validated completion precede transcript mutation. Tool-call
messages precede their matching results. Malformed arguments/forbidden calls get
a bounded repair budget; max steps ends as `budget_exceeded`.
Invalid-call responses explain the expected type, bounds or choices, identify
required/accepted arguments, and confirm that execution did not start. Correct the
arguments and retry; permission denials require a granted tool or authorized
resource rather than a different spelling of the same call. Error guidance does
not echo submitted values or arbitrary unknown argument names.

The registry tolerates unambiguous scalar formatting mistakes: `"3"` for an
integer, `"0.5"` for a number, and `"false"` for a boolean. These conversions also
apply to nested fields, arrays and optional scalars. Bounds and access checks
still apply. Text stays exact, unknown fields are rejected, and ambiguous unions,
yes/no booleans, fractional integers and singleton-to-array guesses stay strict.
Ordinary tool logs show redacted original `args` and the `validated_args` actually
passed to the tool, including defaults; transcript evidence remains verbatim.

Generic tool timeout results report unknown completion and direct the model to
inspect current state or an operation receipt before retrying side effects.
Tools with their own timeout handling retain their operation-specific receipts.
Every completion sends `[model].max_tokens` (default 4096). This is a per-response
output cap, separate from the profile's step limit and input context budget.
A backend `finish_reason: "length"` preserves partial output as evidence but ends
as `budget_exceeded` with an explicit error, without retries or tool execution.
Tool-free profiles send `tool_choice: "none"` and omit tool schemas. Empty
`enabled_tools` loads no plugins; individual profiles still determine tool grants.
Set `tools = ["*"]` in a profile to grant every tool loaded by `enabled_tools`
whose manifest permits that profile. It does not load additional modules, bypass
manifest profile restrictions, or grant effects/filesystem tiers. Missing
write grants still fail startup validation. You can mix `"*"` with explicit names;
schemas are deduplicated, and unknown or disallowed explicit names remain errors.
The tool catalog, prompts and runtime status show the expanded tool names.
A long repetitive assistant message with no `tool_calls` is a single model
response, not a registry loop. Check the run's steps and tool evidence to tell
these apart. If reasoning appears in `content`, check the backend's model/chat
format settings; the runtime preserves returned text rather than silently
rewriting it. Backends must honor the requested output cap.
Token usage is summed
only when every response supplies counts; otherwise it remains null.

`POST /api/runs/{id}/cancel` shares one durable operation across concurrent callers.
On the session page, **Stop** beside **Waiting for Sola** cancels a queued or
running turn, including a scheduled job run, and returns to the conversation.
Stopping closes the active model stream immediately. Saved assistant output,
tool calls and completed tool results remain in session history; an unfinished
streaming preview is discarded. Backend generation stops when the backend handles
the disconnected stream. Stop does not pause future job firings.
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

## Delegated sub-agents

Load `delegate` and grant `delegate.run` to the parent. The current run profile
is always allowed and is the default; configure `delegation.allowed_profiles`
to permit other worker profiles. Delegation runs an ephemeral child inside the
parent's existing inference slot, returning only its final report and evidence
IDs. It works at concurrency one. See [configuration, continuation and
cancellation](operations/delegation.md). Worker tool grants control authority and
whether further delegation is possible; no live profile is enabled automatically.

## Renaming sessions

Open a conversation and choose **Rename session**, enter a name and save.
Names are also searchable from **Search**. Clear the name to display its ID again.
Archived sessions can be renamed without restoring them.

`PATCH /api/sessions/{session_id}` accepts `{"title":"New name"}` and returns
updated session metadata. Titles are strings of up to 200 characters after
trimming whitespace; an empty title clears the display name. Missing sessions
return 404 and invalid/unsupported fields return 422. The route uses the same
Cookie/Bearer operator authentication as other API routes. Renaming changes only
the title, preserving IDs, history, bindings, archive state and importer metadata.

`files.find` applies patterns without `/` to basenames at any depth. Patterns
containing `/` are relative to the search directory: `docs/*.md` matches direct
children, and `docs/**/*.md` includes nested directories. `*`, `?` and bracket
classes match within a path component; a complete `**` component spans zero or
more directories. Hidden-directory and symlink exclusions still apply.

## Storage and recovery

Runtime data lives outside the code tree. Authoritative JSONL transcripts, run
journals, tool/memory/delivery/diagnostic evidence, job cursors, immutable prompt
files and session metadata remain authoritative outside `app.db`. Conversation
indexes are rebuildable, but **managed job definitions live only in `app.db`**.
Back up the database as well as the evidence tree; do not delete it to repair
an index. `Store.rebuild()` preserves job definitions. Metadata publishes
atomically with file/directory fsync. Unfinished runs recover as `interrupted`.

Startup reuses persisted conversation/search indexes instead of rereading all
history. It scans source file metadata once and refreshes only changed sessions,
run journals and job cursors. Title and lifecycle-only edits reuse the indexed
transcript. Archived history stays searchable without loading its messages.
Startup logs the refresh mode, changed/reused source counts and index time.
Run-message byte addresses are also derived in `app.db`: full run inspection reads
matching active/archive evidence without scanning unrelated historical messages.
Changed file fingerprints reconcile stale addresses; explicit rebuild revalidates
all evidence. If the offset index is unavailable, run inspection falls back to a
validated history scan and reports degraded storage health.

The first start after this upgrade (or with missing/incompatible derived tables)
performs a full rebuild. Later starts compare file size, nanosecond modification
and change times, inode and device; new, replaced and deleted files are detected.
These are cache fingerprints, not a cryptographic integrity audit. Unchanged
historical bytes are validated when read or during an explicit full
`Store.rebuild()`. Cached storage damage remains visible until the affected
evidence is repaired. Recovery-marker changes force full revalidation.

Source fingerprints and their derived rows commit in one transaction. Runtime
writes leave the previous fingerprint in place, so the next start rechecks those
files even after a crash or failed index update. Prepared transcript rotations
still recover before indexing; unfinished runs and pending deliveries recover
before serving. No model requests or evidence deletion are part of indexing.

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

## Full-text history search

`GET /api/search?query=journal.publish` searches messages in the same
`app.db` as the other indexes, under the existing Cookie/Bearer operator auth.
Optional filters: `session_id`, `role`, `kind`, `after`, `before`, and `limit`
(default 50, maximum 200). Timestamps require ISO 8601 with a timezone; `after`
is inclusive and `before` exclusive. Results contain `session_id`, `archive`,
`line_no`, UTC `ts` (null if absent in legacy evidence), `run_id`, `role`, `kind`, `channel`, BM25 `rank` (lower is
better), and a plain-text credential-masked `snippet`.

Search also works without the server, prompt files or integration credentials:

```sh
uv run sola search 'journal.publish role:assistant'
uv run sola search '"moonlit lake"' --session-id evening --json
uv run sola search 'channel:discord compaction' --after 2026-11-01T00:00:00Z
```

The offline command reads the existing SQLite index in read-only mode. It does
not initialize Store, recover/finalize runs, acquire runtime ownership, call an
API, or rebuild the database. It resolves available configured credentials and
sensitive environment values for presentation masking. Set `SOLA_CONFIG` for a
nondefault configuration file. `--json` returns the same result fields as the API.

Whitespace-separated terms are combined with AND; double quotes preserve a
phrase. Only `content:`, `role:`, `kind:` and `channel:` are recognized column
filters. Terms are always quoted for FTS, so operator words, punctuation and SQL
text are literal data. Apostrophes and unquoted backslashes remain literal; phrase quotes can be
doubled or backslash-escaped. Queries
are bounded to 2000 characters / 32 terms. [SQLite trigram substring matching](https://www.sqlite.org/fts5.html#the_trigram_tokenizer)
requires at least three Unicode characters per term/phrase; shorter ones match nothing.
Stemming and query expansion remain outside this index.

Addresses are physical one-based JSONL lines, including lines skipped through
explicit recovery markers. `archive="active"` means `sessions/<session_id>.jsonl`;
otherwise `archive` is the numbered filename, such as `evening.jsonl.2`. Archives
are indexed in numeric order, then the active file. Assistant tool-call names
are appended to searchable content; compaction summaries retain their kind.
Manual archival and compaction refresh addresses immediately after durable
rotation. No transcript bytes or message IDs are rewritten.

FTS stores raw private evidence text; masking happens when snippets are read,
including credential fragments clipped at snippet boundaries. Snippets have no
HTML highlight markup. FTS insert failures degrade storage health but do not
fail an evidence append. Search then reports index unavailability until
`Store.rebuild()` regenerates it from validated files. Startup automatically
refreshes changed sources and rebuilds missing/incompatible or known-unhealthy
derived indexes. Offline search uses the last available index; it is never authoritative.
SQLite must provide FTS5 with the trigram tokenizer; unavailable FTS leaves
conversation writes usable and search unavailable.

To let the model search history, load `sessions` (or `solakit.tools.sessions`) in
`enabled_tools` and grant `sessions.search` to the intended profile.
`sessions.search(query, limit=20)` searches only the calling session, including
numbered archives and compaction summaries. Cross-session search is a separate
tool, `sessions.search_all(query, session_id=None, role=None, kind=None,
after=None, before=None, limit=20)`, which declares the `operate` effect (omit
`session_id` to search every session); granting it, including through `*` or
`sessions.*`, requires that effect. Installation does not enable either tool.

Both use the same literal query syntax and filters as the API, with 1–200 results. It returns masked snippets and physical addresses
inside an external-content data marker. Results are historical evidence, not new
instructions. Normal profile tool time/output limits and run logging apply; an
unavailable index produces a tool error without recovery or transcript changes.
Only the operator API/CLI has unrestricted search by default.

## Memory and compaction

Compaction is automatic before a turn when its request exceeds the profile's
serialized-byte `context_budget`. Set `[profiles.<name>].mid_run_compaction = true`
to also compact **between model steps**, after all matching tool results are
stored. Default is false. The original task is carried forward as a request-only
continuation reminder that identifies the preceding summary as progress already
made and directs the model to finish remaining work without repeating completed
actions; step/repair/token accounting continues. Requests that still do not fit
end as `context_limit`, without an oversized model call.
The latest complete tool exchange is also retained verbatim in the resumed
request when it fits the same byte budget, preserving exact receipts, paths and
errors. Parallel-call groups stay together. If the exchange is too large, the
request uses the summary alone. This does not append duplicate transcript records
or execute those calls again.

Compaction waits up to `[memory].compaction_timeout_s` (default 300 seconds)
for extraction, independently of the per-request `timeout_s`. Completion within
this deadline resumes the turn. Deadline expiry stops the turn with an explicit
error and preserves active history and acknowledged task evidence; later remote
completion does not automatically restart a stopped turn.

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

[Recall direction and deliberate notes](operations/memory-direction.md) explains
recent-context queries, configurable trivial-turn skips and `memory.remember`.
The tool stages exact notes for the existing commit; staging is not a storage or
extraction acknowledgment. Run inspection separates recall status from extraction
and lists staged note evidence.

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

Configure this per profile as `[profiles.<name>].context_budget`. The default is
32,000 bytes. For a larger model window, raise it deliberately while leaving room
for `[model].max_tokens`; serialized bytes are a conservative proxy, not a token
count. Configuration changes take effect when the server restarts.

Values in `[profile_defaults]` are inherited by every configured profile, and
values in `[channel_defaults]` are inherited by every `[[channels]]` entry.
Entry-specific values take precedence. Lists replace their inherited value; they
are not appended. Defaults are validated even when there are no matching entries.

Profiles isolate session access from tool calls in two independent directions:
`sessions_private = true` hides that profile's sessions from every other profile's
tools, and `sessions_read_others = false` stops that profile's tools reading any
other profile's sessions (both default to open access). A message's owner is the
profile of the run that wrote it, or the session's default profile when it has no
run, so a private run inside a shared session stays hidden. The rules apply to
`sessions.search_all` (hidden matches are filtered in the index query, so they do
not consume the limit, and targeting a hidden session returns nothing) and to
`jobs.*` tools, which refuse a job whose profile may not read the bound session.
They do not limit `sessions.search` on the current conversation, delegated
children, or the operator API/UI.

Optional `[memory].user` sends the verified server's user hint and checks the
created session URI before appending messages; it must match the server identity.
A profile may isolate its memory with `[profiles.<name>.memory]` (`user` plus
non-empty canonical `roots`). Every recall, `memory.*` tool call, commit,
extraction refresh and compaction summary for that profile's runs sends the
override user and searches/reads only its roots; other profiles keep the global
`[memory]` values and cannot reach the override's roots. It requires
`[memory].enabled`, startup preflight also stats the override roots, and
operator compaction uses the session's default profile. Isolation is by
OpenViking user/root, so the server must enforce those permissions.
`sola memory-test --fact ... --query ... --expected ...` writes a real scratch
fact and verifies actual recall plus the next persisted prompt. Run it deliberately
when diagnosing memory; never automate it at startup or replay uncertain writes.

## Channels and jobs

When the model's final reply is exactly `[SILENT]` apart from surrounding
whitespace, channel delivery is marked `silent` and sends nothing, including
attachments. The original reply remains in session evidence. Replies that mention
the marker alongside other text are delivered normally.

Discord uses discord.py with author/channel/mention/bot gating, parent-session thread
routing, typing while queued/running, reconnect and observable liveness. Replies stay
in the originating thread. Final delivery uses stored transcript text, with no
announcement duplicate. Silent and scoped regular-file artifact delivery are
supported. Oversized fenced blocks attach intact as UTF-8 text; implicit mentions
are suppressed. Intent/results are durable; uncertain sends are not replayed.
`GET /api/channels` reports status, connection time and last accepted message.

Load the built-in `messages` module for tool-initiated posts. `messages.channels`
lists enabled channel ids granted to the profile; `messages.send(channel, text,
operation_id)` posts verbatim text (up to 8000 characters, chunked by the adapter,
mentions suppressed) to one of them. It requires the `send` effect and a
`channel:<id>` resource per destination; unknown channel grants fail configuration.
API channels have no outbound surface and are refused. Receipts live under
`channel-posts/` in the data tree (back them up with evidence): the same ID and text
returns the original result, different text is refused, and a failed or interrupted
post is uncertain and never replayed. These posts are separate from the run's own
final delivery, which still happens as configured.

```toml
enabled_tools = ["messages"]

[profiles.interactive]
tools = ["messages.*"]
effects = ["send"]
resources = ["channel:discord-blog"]
```

APScheduler 3 supplies wake-ups and cron calculations. Job definitions live in
SQLite and can be managed through **Settings → Jobs → New job**, the API, or chat tools.
Existing TOML jobs import once per ID; later file edits do not overwrite managed
jobs, and deleted jobs cannot be resurrected by stale TOML. JSONL cursors indexed
in SQLite persist next fire, pause, active dispatch and last result.
Jobs support recurring `cron` schedules or a one-time `run_at` date/time. Chat
can schedule “check on this tomorrow morning” in the current conversation; without
a more specific time, morning defaults to 09:00 in the configured timezone.
One-time jobs record their attempt before dispatch and never repeat after restart
or a task/enablement edit. Unattempted overdue jobs run once when the app returns.
The web form offers a **One time** date/time picker. A new future date explicitly
reschedules an attempted one-time job; running it early consumes the planned run.
Runtime callbacks are not pickled into a library job store. Cron-to-channel jobs use
the same session queue, so their task/result appear in the next conversational turn.
Queued jobs count as active and never overlap themselves. Model-down jobs become
`missed` gaps. Downtime misses record a bounded range; `catch_up = true` requires
`polling = true` and runs once. Intentional paused/disabled intervals are not replayed.

`GET/POST /api/jobs`, `GET/PATCH/DELETE /api/jobs/{id}` and
`POST /api/jobs/{id}/run|pause|resume` provide management without a restart.
Load the built-in `jobs` module for `jobs.list/create/update/run/pause/delete` tools.
Mutation tools need `jobs.manage`; triggering needs `jobs.run`. Other profiles,
sessions or delivery destinations additionally require `jobs.manage.all`. Chat
triggering queues immediately so jobs can run after the current conversation turn.
See [job management](operations/jobs.md) for fields, grants, migration and backup
instructions. New jobs default to disabled; active jobs reject edits/deletion. Tasks
allow only `{date}`, `{time}`, `{timezone}`, `{job_id}`, without attributes, conversions
or format specifications. Use named weekdays in cron (APScheduler Monday=0 semantics).
Channel/session/ephemeral bindings are supported. Maintenance is operator-API only.

Discord channels and jobs activate through their individual `enabled` settings.
Discord also requires a `discord_token` secret reference. Integrations stay disabled
by default. Enable only the channels and jobs you intend to use.

## Tool extensions

Add a typed decorated function in `solakit.tools.<module>`, enable the module, and
grant its full names in profiles. `read` and `write` tiers select none, workspace or anywhere. Workspace roots use
`repo:<name>`, `collection:<name>` or `path:/absolute/root`. Outward actions require
`effects`; pack aliases require `resources`. Writes and effects default to none. Registry checks occur at startup and dispatch; arguments are
strictly validated. `files.read` bounds regular-file reads using nonblocking,
no-follow descriptor checks. `files.write` creates new files only; `repo_git.status`
uses a configured repository name. Domain conventions now live in separate packs;
journal, letters and email packs plus built-in shell tools are available; the
[thermal printer pack](../packs/printer/README.md) supports USB/serial ESC/POS.

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

A profile can grant `collection:dreams` workspace roots without granting the
entire repository. Layout strings are optional data; the journal pack owns default
front matter. Simple dated content can use journal collections.

[Journal pack installation and use](../packs/journal/README.md) documents its separate
distribution, create/list/publish tools, and scratch-only contract tests. Core
installation does not install any pack. Calendar reads and receipted ComfyUI workflows
are available as optional packs; see [pack setup](../packs/README.md).

The [blog pack](../packs/blog/README.md) targets Astro posts and dreams directly.
`blog.write_post` and `blog.write_dream` create new Markdown, run `npm run build`,
commit exact files and push in one call by default. Set `publish=false` to save
locally; `blog.publish` publishes a saved entry later. Post covers may reference a
completed image-pack operation; dreams have only a date, text and optional
alignment. Configure a named repository, grant filesystem reach and tools, and
allow `push` for publication. Builds execute trusted site code with repository
write access. Receipts retain failures/commit identities; uncertain pushes never
auto-retry. A successful push does not verify GitHub Actions deployment.

## Letters/email packs and built-in shell

[Optional pack setup](../packs/README.md) covers independent installation and the
[pack-owned resource sidecar](../packs/config.example.toml). Set `SOLA_PACKS_CONFIG`
for letters/mailboxes; core config only loads modules and grants tools, tiers, effects and resources.

- [Letters](../packs/letters/README.md): pull/read numbered peer letters and create
  one immutable, sequential reply per peer letter; existing receipts prevent
  replay after uncertain creation/publication. No edits to past letters.
  `letters.reply` requires the peer letter number; `letters.send` publishes a new
  letter without a reply target. Both assign the next outgoing number automatically.
  Grant `letters.send` explicitly (or through `letters.*`) to enable new letters.
- [Email](../packs/email/README.md): verified TLS search/read, stable UIDVALIDITY/UID
  identities, external-content markers, and receipted sender-only threaded replies.
  Opt-in per mailbox: `email.mark_read` (`allow_mark_read`), `email.delete` (moves to
  `trash_folder`, never expunges permanently) and `email.send` (new mail limited to
  `allowed_recipients`, `send` effect, same non-replaying receipts). The inbox profile needs no filesystem write reach. SMTP acceptance is reported
  separately from recipient delivery; uncertain sends are not auto-replayed.
- [Shell](operations/shell.md): enabled by config literal argv and `/bin/sh` tools,
  explicit `shell.exec` grant, finite timeouts, bounded output and process-group
  cleanup. Commands run with host-user privileges; cwd scopes are not a sandbox.

Scheduled jobs can opt into `sub_session = true` for fresh child sessions with
shared memory and final-only channel delivery. Configure this in the job form,
API or chat tools; see [job management](operations/jobs.md).

Tool selection contracts: schemas describe tool purpose and argument semantics;
filesystem and shell tools direct models to granted domain tools first. Validation
errors expose declared field paths and error kinds without submitted values or
arbitrary extra keys. Email resource discovery is the explicitly granted pack tool
`email.list_mailboxes`; it exposes only granted aliases, addresses, folders and
actions, with no connection or credential resolution. `email.search` supports
`unseen_only`, returns seen state and total matches, and preserves unread state.
Use a narrow report-only inbox profile and name aliases in scheduled tasks; see
[email pack guidance](../packs/email/README.md).


For large granted tool sets, see [conditional disclosure](operations/tool-disclosure.md):
profile/job eager or deferred modes, resident tools, discovery and audit evidence.
Eager remains the default; interactive deferred use requires pinned-model evaluation.

Profile access uses `tools`, filesystem tiers (`read`, `write`, `workspace`,
`write_workspace`), `effects` and pack `resources`. The tools catalog includes
resolved summaries and `profile_toml` replacements for deprecated access keys.
Legacy translation keeps separate read and write roots; live integration
acceptance is required before retiring old configuration keys.

Relative tool paths resolve against optional `[server].cwd`, including `files.list(".")`,
shared filesystem services and shell directory arguments. The directory must exist;
`~` expands and relative configuration values resolve against the configuration
directory. When omitted, paths use the server process working directory as before.
This setting does not change the process working directory or grant filesystem
access: profile read/write scopes and symlink checks still apply. Absolute tool
paths are unaffected.

## Curated memory front page

An optional `fragments/memory.md` is read live after SOUL and before standing
context, recall, profile, deferred tool index and run facts. Empty files remain
fragments; UTF-8, line endings and trailing whitespace are preserved. Inclusion
is independent of OpenViking enablement. It is operator-owned: discuss proposed
changes in chat and land them with a text editor. No tool or automatic writer is
installed and no filesystem grants need widening.

`sola check` enforces `[memory].front_page_max_chars` (8192 by default), warning
from 75% and erroring above the cap. Prune rather than copying archive detail.
Runtime never truncates or refuses the file for this cap; existing profile context
budgets still apply. `runtime.status.front_page` exposes live `chars`, `cap` and
`over`. `/api/prompt` and `sola prompt --json` include ordered fragment names,
character counts and SHA-256 hashes. Plain-text output is unchanged. New runs
save the breakdown beside their prompt; older runs return `fragments: null`.

New sessions created by scheduled jobs use source `cron`, including channel-bound
sessions; existing bindings retain their original source. The web session catalog
and chat sidebar list parent sessions. Open a parent’s Details panel to browse its
paginated delegated topics, including archived children and scheduled children; each child links
back to its parent. API listings remain flat by default; `roots_only=true` selects
parents and `parent_session=<id>` selects direct children before pagination.

## Model tuning and diagnostics

[Model tuning](operations/model-tuning.md) covers sampling and thinking settings,
reasoning evidence, the `<tool_call>` fallback, calibrated token budgets,
`sola doctor` live probes and the `sola eval` harness.

## Model health and configuration review

The **System** page (`/settings/system`, API `GET /api/stats/model?window=100`) shows
the latest runs' outcome mix, repair/unknown-tool/length-stop/empty-completion
rates, normalizations applied, repeated calls, reasoning retries and wrap-ups,
average tokens/steps/seconds per step, the most used and most failing tools, the
argument paths that fail most and links to runs worth inspecting, plus the last
`sola doctor` result. It is computed on demand from the rebuildable run index and
tool logs; nothing on it is authoritative state.

Failed runs carry a static operator `hint` (shown on the run page and appended to
channel status notices), e.g. "Raise [model].max_tokens …" or "Enable local
compaction …". Hints are advice and never alter evidence.

`sola config explain <profile> [--json]` prints the effective profile exactly as
admission resolves it: grants with toolsets expanded, read/write roots, effects,
resources, disclosure mode, visible schemas and bytes, and first-step model
request parameters. `sola config diff old.toml new.toml` lists added/removed tools,
effects, resources and filesystem roots per profile for access review before a
restart. Unknown configuration keys, tool names and profile names suggest the
closest valid spelling.

## Session notes

Load `notes` and grant `notes.*` to let the model keep small facts for one
conversation ("the repo path is X", "prefers UTC"): `notes.set(key, value)`
(`null` deletes) and `notes.list()`. Notes are append-only evidence in
`sessions/<id>.notes.jsonl` (latest value per key, at most 50), appear in each
turn block (newest first, 2,000-character cap), survive compaction and archival,
and are never promoted to OpenViking. `GET /api/sessions/{id}/notes` shows them.


### Early completion and context tuning

Profiles may opt into `completion_check = true` with
`completion_check_retries = 1` (range 1–3). A short single-line final reply
starting with an action announcement (for example, “Let me check…” or
“Checking tools…”) receives a recorded continuation notice. Retries consume
normal work steps; repeated announcements or insufficient steps fail honestly.
Original replies remain verbatim evidence. This English-language heuristic can
misclassify short reports and does not prove that a task was completed. Disable
it for profiles where those false positives matter. Structured outputs retain
their existing validation instead.

`context_tokens` already controls calibrated automatic compaction independently
of the model's physical context capacity; `context_budget` remains the byte
backstop, including before token calibration exists. A starting point for this
Qwen workload is `context_tokens = 32768`, `context_budget = 98304`, and
`mid_run_compaction = true`. The token guard reserves configured output tokens
and also triggers above 85% input occupancy. These are working thresholds, not
measured accuracy limits. The reproduced premature stop occurred around 16.7K
input tokens, so earlier compaction alone does not address it. Compaction also
does not refresh stale operator-owned memory fragments in the system prompt.
