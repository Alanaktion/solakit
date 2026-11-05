# Phase 9: Remaining acceptance and external packs

Status: partial — core and journal/letters/email/shell implementation complete;
automated profile isolation is covered; operator acceptance and live checks remain open.

## Implemented

- [x] Explicit dotted imports, shared Git/filesystem services and named collections.
- [x] Generic file/Git tools, live `runtime.status` and config-granted built-in shell.
- [x] Separately packaged journal, letters, email and common support; no domain
  imports or branches in the execution loop.
- [x] Core storage/recovery, queue/cancellation/shutdown and API authentication tests.
- [x] OpenViking round-trip: confirmed by the operator. No new probe was run here.

Completed foundation plans 01–06, 14–16 and 20 have been removed. See the
[audit and acceptance matrix](../audits/implementation-status.md) for code/test evidence.

## Remaining

- [x] Exercise the configured acceptance profile matrix: a blog-profile model attempt
  at `shell.exec`, `shell.run` and `email.*` must be absent from advertised schemas,
  refused at dispatch and recorded. Five runner-driven scenarios in
  `packs/email/tests/test_profile_acceptance.py` verify loaded tools granted to
  a separate operator profile, with forbidden implementation sentinels and exact
  transcript/tool-log checks (SPEC §14.4). This fixture does not approve live grants.
- [x] Runner-driven journal end-to-end acceptance check (SPEC §14.9): mock
  model → `journal.create` → `journal.publish` → scratch remote, exact transcript
  ordering and no shell calls. Covered by
  `packs/journal/tests/test_journal_pack.py::test_runner_creates_and_publishes_to_scratch_remote`.
- [ ] Verify operator-owned prompt/profile/tool grants against the actual intended
  resources; record the prompt audit approval required by SPEC §15.
- [ ] Finish [letters protocol/live checks](17-letters-pack.md) and
  [scratch mailbox checks](18-email-pack.md) before wiring those resources live.
- [x] Implement the [printer pack](19-printer-and-seam-provers.md), capture transport
  and grant checks.
- [ ] Verify deliberate physical-device operation of the thermal printer.
- [ ] Record remaining live summary/continuation checks from
  [compaction controls](21-compaction-controls.md).
- [ ] Close targeted UI browser checks in plans 10, 11 and 21.
- [ ] Record the full §14 acceptance decision, then follow
  [Discord](07-channels-discord.md), [job](08-scheduling.md) and SPEC §15 rollout gates.

Image workflow and calendar read are later seam proofs, independently prioritized;
they do not block the current MVP. Per-message profile choice and Discord archival
exposure remain deferred. [Markdown/streaming](22-ui-markdown-streaming.md) are now
implemented, with live/browser verification tracked separately.

Packs compose internal Python services. New domain tools must not require core
execution branches. Publishing grants remain separate from filesystem writes.
Keep existing Hermes installations/schedules untouched. This audit does not enable integrations or authorize automatic retries of uncertain
sends.
