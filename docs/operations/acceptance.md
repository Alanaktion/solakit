# Operator acceptance and staged rollout

All implementation work from the numbered plans is complete. Those plan documents
are retired; Git history retains their scope and completion records. This page
tracks outstanding **live acceptance**, not missing code. The
[implementation audit](../audits/implementation-status.md) records mock/scratch
coverage and subsequent operator/live evidence. Configuration and passing tests
do not establish production acceptance or authorize uncertain sends to replay.

## Confirmed evidence

- The operator confirmed the original live OpenViking round-trip and general
  functionality. Do not repeat that probe just to update documentation. Retain
  its private records on the operator host; do not invent run IDs.
- Live Discord DM/server replies were operator-confirmed on 2026-11-04.
- Live mailbox TLS/search/read and self-addressed threaded send/receipt, explicit
  and mid-run compaction, nonempty tool arguments, and streaming/compaction UI
  browser checks are recorded in the implementation audit.
- Runner-driven journal create/publish and profile isolation, Discord lifecycle,
  scheduler restart/misfire/non-overlap, letters protocol and printer capture
  tests pass with mock/scratch resources. New calendar and image packs have
  scratch/mock acceptance only.

## Prompt and resource approval (former phase 9)

- [ ] Audit operator-owned persona/profile fragments, tool schemas, capabilities
  and actual intended resources; record the prompt approval required by SPEC §15.
- [ ] Record the complete SPEC §14 acceptance decision. General functionality
  approval does not establish each named check or start rollout.

The journal mock-model end-to-end test uses a local bare remote; blog-profile
attempts at shell and email tools are absent from schemas, refused at dispatch
and recorded. These do not approve production publishing or live grants.

## Letters correspondence (former phase 17)

- [ ] Agree the deliberate message and recipient, then validate live
  pull/read/reply/publish. Retain immutable old-letter bytes and commit/publication
  evidence. Do not edit past letters or replay an uncertain publication.

The previous read-only inspection found `letters/001-2026-11-03-dingus.md`.
The repository requires global numbering, a `letters/` directory and an opening
`# Letter NNN — From, to To, date` heading. Its operator-owned sidecar was corrected
to that directory and heading/reply-reference layout. Inspection did not write a
reply or publish anything. The pack's local bare-remote tests cover concurrent
number allocation, duplicate refusal, immutable bytes and uncertain receipts.

## Devices and extension acceptance (former phase 19)

- [ ] Deliberately print on the connected 58mm USB/serial ESC/POS printer and
  verify physical wrapping/feed and no cutter operation. Transport acceptance is
  not proof of paper output. Read-only discovery previously identified TECH CLA58
  IDs `0x6868`/`0x0200`; these are not core defaults or proof of a print test.
- [ ] Verify a real [ComfyUI image workflow](../../packs/image/README.md), installed
  model/custom nodes, explicit seed, resulting images and server history. Returned
  output references stay on ComfyUI; no local artifact/download is claimed.
- [ ] Verify a real [calendar export](../../packs/calendar/README.md), freshness,
  timezone and supported recurrence. Refresh ICS through the operator's client;
  this pack has no remote calendar writes or automatic synchronization.

## Discord pilot (former phase 7)

- [ ] Complete the acceptance decision above before a pilot.
- [ ] Deliberately validate real gateway author/mention gating, reconnect,
  independent channel/thread sessions, stored delivery and oversized attachments.
- [ ] Complete the one-week low-stakes pilot required by SPEC §15.

Keep Discord disabled until explicitly configured. Parent-thread session routing
must preserve context and send replies in the originating thread; final delivery
uses stored transcript text and retains send evidence. Existing mock lifecycle,
pinning, ignored author/bot, attachment, silent and duplicate-refusal tests are not
proof of real reconnect behavior or a clean pilot week.

## Jobs and rollout (former phase 8)

- [ ] After acceptance, deliberately run a low-stakes job against its actual
  configured channel. Verify delivery, next conversational turn's context,
  schedule timezone, next fire time and visible failure state.
- [ ] Follow SPEC §15: prompt audit, Discord pilot, shadow publishing, individual
  jobs, clean-week observations and rollback checks. Inbox jobs remain separately
  gated despite the completed live mailbox probe.

Job scheduling uses declarative TOML plus durable cursor evidence, not pickled
callbacks. Controlled-clock tests cover skip/one-time catch-up, restart recovery,
pause persistence and non-overlap while queued/running. Live activation stays
explicit per job. Do not change the existing Hermes installation or schedules.

## Working agreement

Synchronize `main`, implement bounded features, run relevant exit checks plus the
full core/pack pytest suite, Ruff lint/format and distribution builds, update the
audit, then commit and push directly to `main` without force. Keep integrations
opt-in and never mark live checks complete from mocks. Preserve append-only
evidence and use operator review to reconcile ambiguous external effects.

Per-message profile selection and Discord archival commands remain deferred
product decisions, outside the retired implementation plans.
