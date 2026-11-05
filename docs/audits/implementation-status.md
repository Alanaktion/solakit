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
or clean weeks of scheduled jobs. Those remain explicit in active plans.

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

Retained partial plans: 07, 08, 09, 17, 19 and 22. Each now
separates completed work from remaining checks. Plan 19 now has a separately packaged USB/serial 58mm ESC/POS printer implementation;
its physical-device gate remains open.
UI plans remain because their named browser matrices have not been specifically
reported complete. Historical adversarial-review resolutions moved from plans to
`docs/audits/`; they are not remaining tasks.

## Remaining work and known limits

1. Audit intended operator-owned profile/resource grants and approve prompts in plan 09; the automated blog-profile isolation gap is closed.
2. Live compaction acceptance is complete; retain private summary/archive/extraction evidence.
3. Complete streaming browser checks (plan 22); foundation, evidence, operations and archival browser matrices are complete.
4. Complete deliberate letters correspondence (17); the actual repository protocol and live self-addressed mailbox verification are recorded.
5. Verify physical 58mm USB/serial thermal printer output (19); image/calendar remain later priorities.
6. Record acceptance and prompt-audit approval, then deliberately stage Discord,
   shadow publishing, jobs and rollback under SPEC §15. No automatic activation.

At the audit baseline, Markdown and streaming were deferred; the later authorized
[plan 22](../plan/22-ui-markdown-streaming.md) implements them and records remaining
live/browser checks. Per-message profile selection and Discord archival commands
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
