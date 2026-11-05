# Implementation and plan audit

For current live checks, see the [2026-11-04 local integration audit](local-integration.md).
The inventory and verification totals below describe the earlier source audit.

Audit baseline: `696cc73`. Scope: reconcile all numbered plans with implementation,
configuration, API/UI routes, pack packaging and regression coverage. This is a
source/test and plan-status audit, not a new production rollout or exhaustive
security review. The changes in this audit are documentation only.

## Operator evidence

The operator confirmed: “I have confirmed the OpenViking round trip. General
functionality looks good.” This satisfies the original phase 6 live round-trip
gate and SPEC §14.11 on operator attestation. The previously supplied settings
were OpenViking `0.4.17.1`, `http://127.0.0.1:1933`, root
`viking://user/sola/memories`, credential reference `OPENVIKING_API_KEY`.
The confirmation did not supply probe run IDs/results or reconfirm server settings;
no private evidence or credentials are copied into this repository. Retain probe
records on the operator host. No new live request was made for this audit.

General functionality approval does not separately establish live compaction,
letters/email/device checks, every browser/viewport combination, a Discord pilot,
or clean weeks of scheduled jobs. Current outstanding checks are in [operator acceptance](../operations/acceptance.md).

## Implemented inventory

| Area | Implementation inspected | Main regression evidence |
| --- | --- | --- |
| Config/lifecycle/auth | Strict TOML and cross-checks, env/file secrets, operator auth, model token, separate model/server ports, sanitized startup/debug logging | `test_config`, `test_api`, `test_model`, `test_logging`, `test_startup_diagnostics`, `test_transport`, `test_cli` |
| Storage/routing | Append-only transcripts/run events, atomic metadata, exact prompts, rebuildable index, ownership lock, interrupted/torn/corrupt evidence handling | `test_store`, `test_execution`, `test_archival` |
| Prompt sovereignty | Live persona/fragments, deterministic assembly and hash, API/CLI dry run without inference or recall | `test_prompt`, `test_evidence_ui` |
| Agent execution | Pinned model, FIFO/global limits, bounded loops/repair/output, tool ordering, cancellation/admission/shutdown, tool-free and token-limit behavior | `test_execution`, `test_model`, `test_tools` |
| Memory/compaction | Scoped recall, intent/extraction tracking, no ambiguous replay, scratch probe, whole-group summaries, immutable rotation/recovery, explicit and opt-in mid-run compaction | `test_memory`, `test_compaction`, `test_compaction_controls`; operator-confirmed live round-trip |
| Discord/delivery | Adapter lifecycle, author/mention gating, channel/thread routing, stored final/artifact/silent delivery, oversized fenced attachments, durable send intents | `test_channels`; mocked gateway/transport |
| Scheduling | TOML schedules, durable cursors, timezone/misfire behavior, catch-up, pause/manual controls, queued/running non-overlap and channel continuity | `test_jobs`; controlled clocks and mock channel/model |
| Extension boundary | Explicit dotted imports, built-in shorthand, generated schemas, per-profile grants, internal filesystem/Git/collection services, core distributions exclude packs | `test_services`, `test_tools`; distribution inspection |
| Generic tools | Files read/create/edit/append/list, alias-bound Git status/diff/log/pull/commit-push, live runtime status, core shell argv/pipeline tools | `test_services`, `test_shell`, `test_tools` |
| Journal | Independent package, configuration-only collections, create/list/publish with collection limits and explicit push grants | `packs/journal/tests/test_journal_pack.py`; real local bare Git remote |
| Letters | Independent package, immutable sequential replies, peer markers, durable deduplication/uncertain-publication evidence | `packs/letters/tests/test_letters_pack.py`; local bare remotes |
| Email/common | Named TLS mailboxes, stable IDs, bounded PEEK reads, source-derived replies, redaction, durable send receipts and replay refusal | `packs/email/tests/test_email_pack.py`; mock IMAP/SMTP |
| Printer (subsequent implementation) | Independent python-escpos USB/serial thermal pack, 32-column text, feed without cut, explicit grants and durable uncertain-send refusal | `packs/printer/tests/test_printer_pack.py`; Dummy/capture only, no physical print |
| Operator UI | Cookie login, compact moonlight light/dark shell, sessions/chat/tools, Enter/Shift+Enter/IME handling, prompt/run evidence/cancellation, job controls, archival/history and compaction | `test_ui`, `test_evidence_ui`, `test_ui_keyboard`, `test_archival`, `test_compaction_controls`; HTTP/unit checks |

Test modules above are under `tests/` unless a pack path is given. Core remains
free of journal/letters/email implementation imports; domain code and sidecar
schemas live in independently installed packs. Shell is correctly in core and
profile access is configuration-driven. UI actions/read access go through the
existing APIs; no parallel browser configuration store was added.

## SPEC §14 acceptance reconciliation

“Covered” below means automated coverage for the stated mock/scratch criterion,
not validation against every production resource.

| Check | Status and evidence | Remaining |
| --- | --- | --- |
| 1. Golden prompt | Covered: `test_prompt.py::test_golden_dry_run` | Operator prompt approval is a separate §15 gate |
| 2. No hidden context | Covered: `test_execution.py::test_exact_context_tool_order_and_usage`; prompt golden checked separately | No additional implementation item identified |
| 3. Tool ordering | Covered by the same execution test, matching assistant/tool IDs | No additional implementation item identified |
| 4. Profile leak | Covered: five runner-driven blog-profile attempts at loaded `shell.exec`, `shell.run`, `email.search`, `email.read`, `email.reply`; schemas exclude them, dispatch refuses them, transcript/tool logs preserve denial, implementation sentinels remain untouched (`packs/email/tests/test_profile_acceptance.py`) | Operator audit of intended live profile grants remains separate |
| 5. Model-down | Covered: unavailable/wrong-model execution tests and `test_jobs.py::test_model_down_is_a_gap_without_channel_messages` | Production availability is operational, not a missing fallback feature |
| 6. Session pinning | Covered: `test_channels.py::test_gating_parent_pinning_context_and_stored_delivery` | Real gateway/pilot remains separate |
| 7. Cron/channel continuity | Covered: `test_jobs.py::test_nonoverlap_includes_queued_jobs_and_channel_continuity` | Deliberate live job/channel validation |
| 8. Drop-in tool | Covered: external dotted module and fixture registry tests in `test_services`/`test_tools` | No entry-point discovery framework needed |
| 9. Journal end-to-end | Covered: `test_journal_pack.py::test_runner_creates_and_publishes_to_scratch_remote` drives a mock model through create/publish to a local bare remote, verifies transcript/request ordering and exact tool names without shell calls | Production publishing remains a separate rollout gate |
| 10. Misfire | Covered: `test_jobs.py::test_restart_misfire_skip_or_exactly_one_catch_up` | Staged operator rollout remains separate |
| 11. Recall round-trip | Operator-confirmed live; mocked recall and explicit probe checks also pass | Retain private probe evidence; do not invent IDs |

The old `blog.create_post`/`blog.publish` names in §14.9 were stale: the accepted
collection architecture implements `journal.create`/`journal.publish`. The spec
now names the implemented tools. The runner-level check now passes against a
scratch remote; it does not establish live publishing acceptance.

## Plan disposition

Removed ten completed plans; the implementation remains and history retains the
original plan text:

| Removed phase | Reason |
| --- | --- |
| 01 configuration/lifecycle | Implemented and regression covered |
| 02 storage/routing | Implemented and regression covered |
| 03 prompt inspection | Implemented and golden/API covered |
| 04 capabilities | Registry and enforcement implemented; full acceptance matrix tracked in 09 |
| 05 model execution | Implemented and mock endpoint covered |
| 06 memory/compaction | Implementation complete; original live round-trip gate now operator-confirmed; newer live control checks retained in 21 |
| 14 pack boundary | Dotted imports, services, named collections and separate packaging implemented |
| 15 generic tools | File/Git/runtime primitives implemented and locally scratch-tested |
| 16 journal pack | Independently packaged; scratch contract and mock-model runner acceptance verified |
| 20 shell | Built-in migration complete; ongoing operator guidance is in `docs/operations/shell.md` |

All remaining implementation plans (07, 08, 09, 17, 19) are now retired.
Their implementation is complete, including the image/calendar seam proofs.
Unverified live gates are preserved in [operator acceptance](../operations/acceptance.md).
The UI browser matrices were subsequently completed as recorded below. Historical adversarial-review resolutions moved from plans to
`docs/audits/`; they are not remaining tasks.

## Remaining work and known limits

1. Audit intended operator-owned profile/resource grants and approve prompts in plan 09; the automated blog-profile isolation gap is closed.
2. Live compaction acceptance is complete; retain private summary/archive/extraction evidence.
3. Streaming, compaction and foundation/evidence/operations/archival browser matrices are complete; retain their acceptance evidence.
4. Complete deliberate letters correspondence (17); the actual repository protocol and live self-addressed mailbox verification are recorded.
5. Verify physical 58mm USB/serial thermal printer output and real image/calendar resources; implementation is complete.
6. Record acceptance and prompt-audit approval, then deliberately stage Discord,
   shadow publishing, jobs and rollback under SPEC §15. No automatic activation.

At the audit baseline, Markdown and streaming were deferred; the later authorized
[streaming acceptance record](#phase-22-completion-2026-11-05) now covers their
implementation and completed live/backend/browser checks. Per-message profile selection and Discord archival commands
remain deferred. General web/message/device abstractions should grow only for a
concrete pack need. Trusted Python packs, shell commands and repository Git hooks
run with host-user privileges; existing scope checks are not an OS sandbox.
Uncertain external sends/pushes still require operator reconciliation.

## Verification

The audit reran all core and optional-pack tests: **193 passed**. The only warnings
were existing Starlette TestClient/httpx and Python `audioop` deprecations. Ruff
lint/format, core and all four pack builds, packaged-asset checks and local Markdown
link checks are recorded with this change. Browser and live-provider tests were
not run by the audit; operator-confirmed memory evidence is distinguished above.

## Subsequent phase 12 browser acceptance (2026-11-04)

Plan 12 is complete and removed. Brave/Chromium headless browser checks against
an isolated scratch app passed in light/dark at 1440×1000 and 390×844. Screenshots
were visually reviewed. Browser timezone Europe/London correctly displays GMT
while schedule labels retain America/Denver. Pause/resume, run-now → completed
evidence, disabled controls and read-only disabled channel state passed with no
browser script errors. Narrow tables now preserve readable columns and scroll
inside their panels, without page overflow. Reproducible harness and artifact
instructions: [browser QA](../../tests/browser/README.md).

This is real-browser UI acceptance using a mock model and scratch jobs, not live
scheduling/channel rollout. No operator data or enabled integrations were used.

## Subsequent phase 13 browser acceptance (2026-11-04)

Plan 13 is complete and removed. The isolated Brave browser matrix passed in
light/dark at 1440×1000 and 390×844. Real form navigation showed the explanatory
confirmation, rotated a scratch conversation, retained its numbered read-only
history, hid the archive composer and returned to the same session's current
composer. Preserved user/assistant messages, long-code scrolling, layout and
confirmation readability were checked, including screenshot review. No page
overflow or browser script errors occurred. The reproducible phase `13` check is
in [browser QA](../../tests/browser/README.md). Existing durability, byte-evidence,
busy/recovery/auth tests remain the storage verification; no live memory calls
or operator session mutations were made.

## Subsequent phase 10 browser acceptance (2026-11-04)

Plan 10 is complete and removed. The isolated Brave matrix passed light/dark at
1440×1000 and 390×844 with screenshot review of chat/focus/layout. Real browser
form creation, session navigation, tools navigation, Enter submission, Shift+Enter
newline and keyboard Tab focus passed. Browser-dispatched CompositionEvent and
isComposing KeyboardEvent sequences preserved an unsent IME draft; this is not
an OS input-method/device compatibility certification. A held mock completion
disabled the composer; duplicate form submission admitted only one active run,
and cancellation restored it. No page overflow or script errors occurred. The
phase `10` [browser QA](../../tests/browser/README.md) is reproducible. Operator
backend/live streaming and compaction gates remain separately tracked.

## Subsequent phase 11 browser acceptance (2026-11-04)

Plan 11 is complete and removed. The isolated Brave matrix passed light/dark at
1440×1000 and 390×844, including screenshot review. Fixed-time prompt text/SHA
matched the authenticated API, schema details expanded, and a real runtime.status
tool call through the mock-model runner produced expandable arguments/results.
Stored prompt evidence expanded correctly. Canceling a held run through its
actual UI control displayed the cancelled status and removed the action. Long
evidence stayed within scroll containers with no page overflow or script errors.
Reproduce with phase `11` in [browser QA](../../tests/browser/README.md).

Plans 10–13 are now retired. Browser acceptance used scratch storage/jobs and a
mock model; it does not establish live oMLX streaming, OpenViking compaction,
mailbox/device operation or production scheduling/channel rollout.

Final verification after retiring plans 10–13: 226 core/optional-pack tests passed;
all sixteen phase/theme/viewport browser scenarios passed. Ruff lint/format,
`.env`-backed offline `sola check`, core wheel/sdist build and packaged UI assets
passed. Screenshots remain scratch artifacts under the browser harness output
directory; no operator data or credentials are included.

## Subsequent live verification (2026-11-05)

Live oMLX text/tool SSE checks passed after a narrow fix for exact empty legacy
keepalive frames; malformed/payload-bearing/wrong-model frames remain rejected.
The full suite passed 232 tests, Ruff lint/format, `.env`-backed `sola check` and
core builds. Plan 22 retains its streaming-specific browser matrix.

Actual letters protocol was inspected read-only and the ignored local sidecar's
directory/layout corrected to match the repository. No correspondence was sent.
Plan 17 retains its deliberate live reply/publish gate.

Plan 21 passed live explicit and between-step OpenViking summaries, archive
rotation, deterministic mock-model continuation, completed extraction and actual
remote archive read-back of task/full tool output/final reply. Its light/dark
desktop/mobile browser matrix also passed. A newly observed provider limit keeps
the phase open: remote archive tool parts omit submitted `tool_input`. The test
used empty arguments, so nonempty argument preservation needs investigation.
Local transcript evidence remains verbatim.

## Subsequent phase 18 live mailbox acceptance (2026-11-05)

Plan 18 is complete and removed. The operator explicitly authorized reading the
configured mailbox through `packs/config.toml` + `.env` and self-addressed sending.
A separate verification profile loaded the actual email pack, with only its three
tools and mailbox read/reply capabilities, no filesystem grants, channels or jobs.

- Real provider authentication succeeded over verified IMAP/SMTP TLS 1.3.
- Bounded search/read succeeded against three existing messages without copying
  their contents into repository artifacts.
- A uniquely labeled synthetic self-addressed seed arrived in configured INBOX.
  Search found its stable UIDVALIDITY:UID identity; read preserved IMAP flags.
- `email.reply` derived its self-addressed envelope/thread from that source. SMTP
  acceptance was recorded first, then actual arrival in INBOX was separately
  confirmed by outgoing Message-ID, recipient/sender, body marker, In-Reply-To and
  References headers.
- Repeating the same reply call returned the original durable receipt rather than
  issuing a new SMTP send. Existing mock checks continue to cover uncertain sends.

The first seed generated by the verification harness omitted Date and received
a definitive delivery-failure notice (`5.6.0`, missing required Date header). That
failure/DSN was retained. A distinct corrected seed was then sent; this was not
a retry of uncertain delivery. The pack's reply composer already supplies Date
and required no change. Both test messages and the failure notice remain evidence
in the mailbox; nothing was deleted.

Private operator evidence is retained under
`verification/solakit-mail-check-6a5a344ce3c5/` (successful seed/reply, report,
received raw reply and append-only tool/send receipts) and
`verification/solakit-mail-check-f8f40bede0ea/` (rejected first seed and DSN).
No credentials or existing-message bodies were added to source control.

Verification: 232 core/optional-pack tests, Ruff lint/format, `.env`-backed offline
`sola check` and email wheel/sdist build passed. This closes provider/threading
acceptance for the configured mailbox, not automatic inbox jobs or staged rollout.

## Phase 21 completion (2026-11-05)

Plan 21 is complete and removed. The earlier apparent tool-argument omission was
resolved by inspecting the installed OpenViking message serializer: it deliberately
omits empty dictionaries (`if part.tool_input`). Nonempty arguments use its
existing supported `ToolPart.tool_input`; Solakit's mapping needed no change.
A new summary-only scratch session on the actual provider preserved nested
arguments exactly, including a path, Unicode, false, zero, null and a unique
marker. Remote completed-archive read-back also matched tool identity/status and
output. Added regression checks cover nested arguments, malformed JSON evidence
and unchanged source transcripts.

Private evidence: operation `arguments-4db6410e560047bd9d4e6e2c55732e2b`, under
the operator data directory's corresponding `verification/arguments-.../` folder.
This probe disabled self/peer long-term extraction, retrieved a completed live
summary and retained input/archive/report evidence. No backend upgrade or
configuration change was needed. Together with the earlier live explicit/mid-run
summary, byte-preserving rotation, continuation, full-archive commit/extraction
and four-way browser matrix, this satisfies the remaining phase gates.

## Phase 22 completion (2026-11-05)

Plan 22 is complete and removed. The earlier live oMLX probes established pinned
text/tool SSE, usage, stop/tool_calls terminal events and `[DONE]`. An exact empty
legacy heartbeat is treated only as a transport no-op; payload-bearing variants,
wrong models and heartbeat-only completions still fail closed.

Real Brave/Chromium checks now pass in light/dark at 1440×1000 and 390×844 against
the isolated scratch app with delayed, fragmented model streams:

- Partial Markdown previews are labeled unsaved, then replaced by saved evidence.
- Tool-call deltas execute only after completion; expanded details survive snapshots.
- Long code and table content scroll within their containers; links work and
  page width remains bounded. Screenshot review found squeezed table columns;
  minimum cell widths and normal cell wrapping now preserve readable columns.
- A reader scrolled above the latest content stays there while snapshots arrive;
  the composer regains focus when the run completes.
- Reload observes one accepted run without resubmitting. Both a blocked observer
  GET and a real mid-body SSE connection reset show connection guidance; reload
  recovers the same single run and stored completion.
- Running/queued turns remain observable and cancel independently.
- An interrupted model stream removes its preview, saves no first-step partial
  transcript, and reports model_unavailable.
- Sign-out and actual server-side session expiry invalidate an already-open
  observer; reauthentication shows the accepted run without replay.

The disconnect/expiry controls exist only in `tests/browser/server.py`, never
the production app. Browser checks use mock model/memory transports; live oMLX
verification and live OpenViking compaction are separately recorded above.
Reproduce phase `22` with the [browser harness](../../tests/browser/README.md).
Core and optional-pack suites passed 234 tests. Ruff lint/format, `.env`-backed
`sola check`, core wheel/sdist build and packaged CSS/JS checks passed.
No production channel, job or publishing rollout is implied.

All twenty earlier browser combinations (phases 10, 11, 12, 13 and 21) were rerun
after the harness changes and passed. Private screenshots, scratch application
evidence, fixture source, server log and matrix report are retained under the
operator data directory's `verification/2026-11-05-streaming-browser/`. The QA
server was stopped after verification.

## Workspace pack sync (2026-11-05)

Core and all five pack distributions are now uv workspace members under one
lockfile. Root runtime dependencies contain no packs, and a wheel inspection
regression asserts no pack modules or `solakit-packs-*` Requires-Dist entries in
the core artifact. Workspace source mappings resolve core/common inter-package
dependencies locally; manual editable installation and dependency ordering are
no longer part of the checkout workflow.

`uv sync --frozen --all-packages` was verified in a fresh environment, repeated
without pruning packs, followed by ordinary `uv run` imports/metadata checks.
Core-only sync was verified in that separate environment and all-package sync
restored the six distributions. Existing test environment packs were not removed.
All 240 core/pack tests passed against workspace installations.

The shared tool catalog, startup logs, offline `sola check` and `runtime.status`
report loaded module distribution names/versions from importlib.metadata.
Configured missing packs fail before import with their expected distribution
and the all-package sync repair command, even if a source module is importable.
Missing dependencies receive the same repair guidance. Metadata-less external
modules remain supported and report unknown identity explicitly. No entry-point
discovery, tool auto-loading, profile grant or integration activation was added.

## SQLite full-text history search (2026-11-05)

Standalone FTS5/trigram indexing now lives in the existing disposable `app.db`.
Rebuild validates archived/current messages and recreates FTS; append inserts
incrementally after durable evidence, and committed archival/compaction refresh
addresses. Search results address physical one-based file lines and include
rank/snippet plus metadata. Tool-call names and summary kinds are searchable.
FTS failures report degraded storage, leave appends/rotation successful, and
require rebuild before runtime search resumes. No evidence is redacted or edited.

Authenticated GET `/api/search` and read-only offline `sola search` share bounded
literal-term/phrase parsing, safe column scopes and parameterized filters. Read
presentation masks resolved credentials, including truncated snippet fragments.
Offline search does not instantiate/recover Store, mutate runs or contact an API.
The [operator instructions](../../README.md#full-text-history-search) describe
bounds, timestamps, addressing, tokenizer requirements and repair behavior.

Verification: 29 focused search regressions cover raw-index/redacted-snippet
separation, clipped/duplicate secret fragments, archive/active and recovered-tail
physical addresses, numeric ordering, database deletion/rebuild, committed
archival/compaction refresh, unsupported/broken FTS, continued appends/rotation,
safe query syntax/filters, UTC bounds, legacy timestamps, API auth/errors and
read-only offline behavior. The 269-test core/pack suite passed. A separate CLI
process also passed with missing credentials, persona and tool provider, without
changing app.db. `.env`-backed config checks and all six wheel/sdist builds passed.
No operator session or live integration was changed for these checks.

## Calendar read implementation (2026-11-05)

The optional `solakit-packs-calendar` distribution provides `calendar.events`
over a named operator-exported ICS file. It checks an explicit per-calendar read
capability and the shared filesystem scope on every call, bounds export size,
component count, query window and result count, and marks event text as external
data. UTC results retain all-day semantics; supported recurrence and exceptions
are expanded by the ICS library. Sub-daily/BY* rules fail explicitly. The generic
filesystem reader now accepts an optional bounded internal character limit for
pack composition; model-facing file tool budgets are unchanged.

Scratch tests cover recurring exceptions, all-day timezone conversion, exclusive
query bounds, limits, invalid exports, unsupported recurrence and grant/scope
denial. No live calendar access or integration activation is claimed.

## Image workflow completion and plan retirement (2026-11-05)

`solakit-packs-image` submits operator-owned API-format ComfyUI graphs, including
ZImage workflows, with configured text/seed inputs. Explicit tool, per-resource
and generation grants are required; workflows use the generic bounded filesystem
reader. Named endpoints, optional registered bearer credentials, bounded HTTP
responses and deadlines, disabled redirects/retries and durable operation receipts
keep this outside core execution. A stable operation ID reuses the acknowledgement;
changed, concurrent, torn or uncertain work cannot resubmit. Cancellation waits
for the accepted submission/receipt before propagating. `image.result` checks the
receipt and server endpoint, then performs one history read; it returns server
output references, never invented local artifacts.

Mock transport checks cover preserved graph bytes/other inputs, stable retries,
changed requests, missing grants/scopes, cancellation, concurrency, oversized or
malformed acknowledgements, redirects, torn receipts, pending/failed/completed
states, endpoint changes and credential registration. No GPU/model/service was
contacted. All seven optional distributions remain independently packaged.

All code work identified by the remaining plans is complete. The numbered plan
documents and their index are removed; Git history retains the original scope.
Outstanding prompt/resource approval, letters correspondence, physical printing,
real extension resources, deliberate gateway/job checks and staged rollout are
preserved in `docs/operations/acceptance.md`, without claiming live acceptance.

Verification for calendar/image completion: **296 tests passed**, Ruff lint/format
and all eight distributions (core plus seven packs) built successfully.

## Model-call session search (2026-11-05)

After completing the remaining plan implementations, the built-in opt-in
`sessions` module adds `sessions.search`. It composes `Store.search` with no core
execution changes. Default scope is the calling session; another session or all
sessions requires the explicit `sessions.search.all` capability before any index
query. It supports role/kind/date/limit filters and literal phrases/column terms,
returns credential-masked snippets with physical active/archive addresses, and
marks historical text as external data. Registry schema/profile validation,
timeouts, bounded output and redacted run logs apply. Missing/broken FTS returns
a tool error; no evidence is rewritten, index rebuilt or memory backend called.

Fourteen focused tests verify default/cross-session scopes, explicit grants,
profile schema/dispatch denial, archived/active addresses, compaction/date/role
filters, read-only behavior, credential/markup masking, invalid arguments,
unavailable-index logs, and a mock-model runner round-trip with matching
assistant tool-call/result IDs and nonempty query arguments.

Final verification: **311 tests passed**, Ruff lint/format checks passed, all eight
distributions built, and local Markdown file links resolve after plan removal.
The image response deadline also has a small-chunk regression check. No live
resource configuration or production integration was changed in this session.

## Profile all-tools grant (2026-11-05)

Profiles now accept `tools = ["*"]` to grant every explicitly loaded tool whose
manifest permits that profile. Registry validation still requires filesystem and
capability grants. Schema/catalog expansion and dispatch share the grant logic;
mixed explicit/wildcard entries deduplicate without mutating TOML configuration.
Unknown and disallowed explicit names still fail startup, even beside a wildcard.
No extra modules, integrations or capability grants are enabled by the wildcard.

Verification: five focused wildcard tests cover TOML loading, expansion, mixed
grants, manifest exclusions, schema/catalog/dispatch consistency, missing grants,
unknown tools and an empty loaded set. All **316 tests** passed, Ruff lint/format
passed, and all eight distributions built.

## API, chat and web-managed scheduled jobs (2026-11-05)

Job definitions now live in the authoritative `job_definitions` table in app.db.
Legacy TOML imports once per ID; managed edits take precedence and deletion
tombstones prevent stale configuration from resurrecting jobs. Derived table/FTS
rebuilds preserve definitions. Job cursor JSONL and run/transcript/delivery evidence
remain intact. Backups must now include app.db as well as the evidence tree.
The scheduler starts with an empty job list so new enabled jobs work immediately.

Authenticated API CRUD, partial edits and existing run/pause/resume operations
share scheduler validation and locking. New jobs default disabled. Active jobs
refuse edits/deletion/overlap; schedule and enablement changes reset next fire.
Accepted mutations/admissions finish database/live-state publication before
cancellation propagates. Optional nonblocking manual runs return HTTP 202 with
accepted job state. Existing cron/misfire/catch-up/channel delivery paths remain.

The built-in `jobs` module adds list/create/update/run/pause/delete tools through
the injected scheduler, with no execution-loop changes. Mutation/trigger grants
are separate from permission to affect other profiles/sessions/destinations;
existing and proposed definitions are rechecked under the mutation lock. Chat
triggering never waits for the scheduled run, preventing same-conversation queue
deadlock. Responses mask resolved credentials and mark historical/task text as
data; runtime status does not expose job task bodies across chats.

Operations now has create/edit forms, enable/polling/catch-up and delivery options,
run/pause/resume, and deletion confirmation. UI data/mutations go through the same
Cookie/Bearer authenticated APIs. Validation preserves form input and task markup
stays escaped. New [operator guidance](../operations/jobs.md) covers migration,
API fields, tool grants, scheduling semantics and database backup requirements.

Verification: **336 core/pack tests passed**, including 20 new management cases
covering authenticated CRUD, import/edit/delete/restart, preservation through
index rebuild, invalid definitions and patches, paused manual triggering, busy
rejection, HTTP 202 acceptance while inference is busy, retained evidence,
chat scope/grants, under-lock authorization races,
cancelled publication and a model-runner create/edit/trigger round-trip in the
current conversation. Ruff lint/format and all eight distribution builds passed.
Real Chromium checks passed for new management and existing Operations controls
in light/dark at 1440×1000 and 390×844: create/validation/edit/run/pause/delete,
confirmation, page overflow, timezone display and browser errors. Form/list
screenshots were inspected. These checks used scratch storage, mock model/memory
and disabled real channels; they do not establish a live job rollout.

## One-time scheduled follow-ups (2026-11-05)

Jobs now choose exactly one schedule: recurring `cron` or one-time `run_at`.
Local timestamps resolve in the job's IANA timezone and normalize to UTC;
ambiguous/nonexistent DST wall times require an explicit offset. New or changed
one-time dates must be future instants. Existing cron fingerprints remain stable.

One-time admission durably records `consumed=true` and clears next fire before
creating work. Early manual runs consume the planned attempt. Completion, failure,
interrupted dispatch, restart/rebuild and task/enable/pause edits never rearm it.
An unattempted overdue job catches up once when the app returns; automatic retry
of an ambiguous attempt remains forbidden. A new future run_at explicitly rearms
the job. A timezone-only change preserves the absolute instant and consumed state.

The API and existing chat create/update/run tools support both types. Tool guidance
resolves “tomorrow morning” from current run metadata and timezone, defaults to
09:00 without a more specific time, and creates an enabled follow-up bound to the
current conversation. The mock-model runner verifies the run_at arguments, current
session binding, schema advertising and completed scheduling turn; this is not a
claim about live provider interpretation of arbitrary natural language.

The web form adds Recurring/One time selection and a local date/time picker.
Task-only editing preserves exact offsets/fractions through ambiguous local times,
including browsers that omit zero seconds. Attempted jobs disable repeat-run
controls and show their recorded outcome; editing to a new date rearms them.

Verification: **352 core/pack tests passed**, including 16 focused one-time cases
covering exclusivity/invalid dates, DST gaps/folds, UTC conversion, due/manual
admission, restart/rebuild, edits/toggles, deliberate rearming, overdue catch-up,
interruption, API type switches/past dates, chat scheduling and exact UI round-trip.
Ruff lint/format and all eight distribution builds passed. Chromium checks for
one-time and existing recurring management passed in light/dark at 1440×1000 and
390×844, including date entry, execution, disabled replay, task-only editing,
rescheduling, browser errors, overflow and inspected screenshots. Browser/model
checks used scratch data and mocks; no production jobs were scheduled.
