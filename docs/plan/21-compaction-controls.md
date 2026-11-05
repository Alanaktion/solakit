# Compaction controls and summary placement

Status: partial — live summary/rotation/continuation and browser checks pass;
nonempty tool-argument preservation in remote memory remains to verify.

## Audit checklist

- [x] Mid-run opt-in, explicit API/UI actions and summary placement implemented.
- [x] Mock regression tests cover complete groups, preserved task/budgets, archive
  commits, busy rejection and durable cancellation/recovery.
- [x] General live OpenViking round-trip confirmed by the operator.
- [x] Deliberately verify completed summary retrieval and archive rotation against
  the live server, then continuation and end-of-run memory commit across archives.
  Exercise explicit and mid-run paths; a recall round-trip alone does not test these.
- [x] Browser-check confirmation/errors and summary-before-message placement in
  light/dark and narrow-screen chat.

## Implemented behavior

- [x] Profile `mid_run_compaction=false` preserves existing behavior. Opt-in enables
  compaction between completed model/tool steps when serialized context exceeds
  the profile budget. Never rotate an incomplete assistant/tool-result group.
- [x] Continue with the original system prompt, active summary and original user task
  reminder. Do not duplicate stored user messages, replay tools, reset counters or
  send requests still over budget. Multiple rotations remain in append-only run
  events; run metadata/view shows the latest compaction.
- [x] Normal end-of-run memory commits include the complete current run across archives.
- [x] POST `/api/sessions/{id}/compact` rejects busy/shutting-down sessions, requires
  shared OpenViking and retains immutable history and bindings. The UI explanation
  distinguishes summarization from an empty archival restart.
- [x] Accepted compaction and run attribution settle before cancellation/shutdown can
  close clients or allow conflicting writes. Prepare/rotate is one durable step.
  A failed prepared rotation refuses new writes until startup recovery finishes.
- [x] UI boundaries precede dependent messages by using visible-message ordering, not
  a run's earlier creation timestamp. Pending/failed runs that compacted still
  appear after their summary even when no new user message was persisted.

Checks: default/opt-in behavior, complete tool groups, original task and counters,
oversized summaries, full archived-run memory commits, auth/API/UI/manual actions,
empty/ephemeral/busy rejection, cancellation/admission safety, prepared rotation
cancellation and failure recovery, and pre-turn summary/message ordering.

These are generic execution/compaction changes explicitly authorized here, not a
domain-tool exception to the rule against modifying the loop to add a pack.

## Live and browser verification (2026-11-05)

Using local `.env`, isolated scratch storage and the configured live OpenViking
server, explicit compaction retrieved a completed summary and preserved the
source archive byte-for-byte. A deterministic mock model then continued from it.
A second scratch run forced between-step compaction after one complete tool
group; the live summary was retrieved and the continuation completed without
replaying that tool. The full-run extraction task completed. Read-back of the
actual remote archive matched the original task, tool identity/status/full
15,686-character result and final continuation. Four transcript records map to
three OpenViking messages because tool results are nested in assistant parts.

Private scratch evidence includes runs `068204a2-221a-4306-8da1-9b71226ae444`
and `ba5b4199-0436-4386-904a-93324acc793b`; authoritative transcripts/manifests,
model requests and extraction receipts were retained in private temporary storage.
No operator session was rotated and no channel/job was activated. Model control
flow was mocked deliberately; OpenViking summary generation and extraction were
real. This is distinct from the earlier general recall round-trip attestation.

Brave browser checks passed in light/dark at 1440×1000 and 390×844 with screenshot
review: confirmation wording, disabled ephemeral controls, busy-operation errors,
expandable summary before dependent messages, no page overflow or script errors.
Reproduce phase `21` in [browser QA](../../tests/browser/README.md).

### Newly observed provider limit

- [ ] Verify preservation of nonempty tool arguments in remote memory. This live
  probe used an empty argument object. The server's archive API returns tool
  identity/status/output but omits the submitted `tool_input` field. Local
  immutable transcript evidence remains intact. Do not claim full argument
  round-trip fidelity from this probe; investigate the supported provider schema
  before closing this plan.
