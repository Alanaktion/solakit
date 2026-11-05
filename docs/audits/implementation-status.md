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

Retained partial plans: 07, 08, 09, 10, 11, 17, 18 and 21. Each now
separates completed work from remaining checks. Plan 19 now has a separately packaged USB/serial 58mm ESC/POS printer implementation;
its physical-device gate remains open.
UI plans remain because their named browser matrices have not been specifically
reported complete. Historical adversarial-review resolutions moved from plans to
`docs/audits/`; they are not remaining tasks.

## Remaining work and known limits

1. Audit intended operator-owned profile/resource grants and approve prompts in plan 09; the automated blog-profile isolation gap is closed.
2. Verify live completed summaries, rotation and continuation in plan 21; a recall
   round-trip does not exercise those API paths.
3. Complete targeted light/dark/mobile/keyboard UI checks (plans 10, 11 and 21).
4. Check actual letters protocol and scratch mailbox/provider behavior (17–18).
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
