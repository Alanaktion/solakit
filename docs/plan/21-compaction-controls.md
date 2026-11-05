# Compaction controls and summary placement

Status: locally implemented; live OpenViking and browser visual checks pending.

- Profile `mid_run_compaction=false` preserves existing behavior. Opt-in enables
  compaction between completed model/tool steps when serialized context exceeds
  the profile budget. Never rotate an incomplete assistant/tool-result group.
- Continue with the original system prompt, active summary and original user task
  reminder. Do not duplicate stored user messages, replay tools, reset counters or
  send requests still over budget. Multiple rotations remain in append-only run
  events; run metadata/view shows the latest compaction.
- Normal end-of-run memory commits include the complete current run across archives.
- POST `/api/sessions/{id}/compact` rejects busy/shutting-down sessions, requires
  shared OpenViking and retains immutable history and bindings. The UI explanation
  distinguishes summarization from an empty archival restart.
- Accepted compaction and run attribution settle before cancellation/shutdown can
  close clients or allow conflicting writes. Prepare/rotate is one durable step.
  A failed prepared rotation refuses new writes until startup recovery finishes.
- UI boundaries precede dependent messages by using visible-message ordering, not
  a run's earlier creation timestamp. Pending/failed runs that compacted still
  appear after their summary even when no new user message was persisted.

Checks: default/opt-in behavior, complete tool groups, original task and counters,
oversized summaries, full archived-run memory commits, auth/API/UI/manual actions,
empty/ephemeral/busy rejection, cancellation/admission safety, prepared rotation
cancellation and failure recovery, and pre-turn summary/message ordering.

These are generic execution/compaction changes explicitly authorized here, not a
domain-tool exception to the rule against modifying the loop to add a pack.
