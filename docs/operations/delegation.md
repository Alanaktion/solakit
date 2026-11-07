# Delegated sub-agents

`delegate.run` moves a bounded task into its own child session. The parent keeps
the brief and final report; the child's reads, tool calls and intermediate output
stay in its own transcript. Child evidence uses the existing session/run APIs,
web UI and full-text search. This is serial delegation within one process.

## Enable it

Load the `delegate` built-in module alongside the worker's tool modules and grant
`delegate.run` to profiles that may delegate. The active run profile is always
allowed and is used by default. Optionally permit additional worker profiles:

```toml
enabled_tools = ["delegate", "files", "sessions"]

[delegation]
allowed_profiles = ["research"]
timeout_s = 900

[profiles.interactive]
tools = ["delegate.run"]

[profiles.research]
tools = ["files.read", "files.list", "sessions.search"]
workspace = ["path:/absolute/path/to/research-sources"]
read = "workspace"
write = "none"
context_budget = 64000
```

Merge these grants into your existing configuration. The default allowlist is
empty, allowing delegation with the current profile. Unknown worker profiles fail
configuration validation. Each profile needs its operator-owned `fragments/profiles/<name>.md`; this feature installs no persona
or prompt text. Workers receive the normal SOUL/global fragments and their own
profile fragment, task and history, with no parent conversation or memory recall.

Grant worker tools, filesystem tiers, effects and resources deliberately. A worker is authorized to use its selected
profile, which can differ from the parent's grants. A read-only research profile
can use file/history reads and explicitly configured web/wiki reads. Avoid
publish/send/print/shell grants for that example. Keep `delegate.run` out of worker
grants, including wildcard grants, for one-level delegation. There is no runtime
depth counter. Put judgment about when to delegate in the operator-owned parent
profile fragment: the goal, sources, deliverable and completion criteria must fit
in the self-contained brief. Irreversible actions need the operator's authorization.

## Tool contract

```json
{"task":"Investigate these sources and return a short report."}
```

Omit `profile` (or pass null) to use the active parent run profile, including on
continuation. Explicitly selecting that same profile is always allowed. Selecting
a different profile, such as `"profile":"research"`, requires an entry in
`delegation.allowed_profiles`. The session default does not override this choice.

`task` is a nonblank string of up to 32,000 characters and is retained verbatim as
the child's input. New children have `parent_session` pointing to the caller, no
channel binding, ephemeral memory scope, the selected default profile and a title
from the brief's first line (up to 200 characters).

Supply `session` with a previous child session ID to continue it. The child must
belong directly to this parent, be unarchived/unbound/ephemeral and have no queued
or running work. Other parents' children and busy continuations are refused and
logged before a child run is admitted. The selected profile must still be allowed.

Results carry `session_id`, `run_id`, final `status` and, only for `completed`,
the final assistant `report`. Child failures return a tool error with IDs/status
and no report. Reports are historical data in a JSON external-content envelope.
The parent's normal tool-output limit still truncates results countably. Inspect
the identified child session/run for complete evidence; no synthetic summary is
created for failed or incomplete work.

Children perform no memory recall/commit or automatic channel delivery. Only the
parent's usual memory commit contains its own brief, returned report and reply.
Children stay visible in normal session listings and can later be archived with
the existing cleanup workflow; this feature does not auto-archive or delete them.

## Execution, timeout and cancellation

The parent already holds an inference slot. Nested execution reuses that slot
rather than queueing a child behind it, so the default model concurrency of one
works without deadlock. Child runs retain normal admission/run/prompt/evidence,
model validation, tool grants, step and context budgets. Admission checks and
per-session locks serialize nested work with ordinary API/channel/job turns.
An active child is also protected by existing archival/compaction safeguards.
Parallel child fan-out and cross-process execution are not provided.

`delegation.timeout_s` (finite, positive; default 900 seconds) bounds the whole
delegation instead of the parent's generic `tool_timeout_s`. On timeout, accepted
child work is finalized as cancelled before the error receipt returns; the receipt
keeps IDs, last status and `timed_out:true`. Durable writes may take longer to
finish. Side effects may already have happened; reconcile child evidence before
retrying. A timed-out report-read can identify a child that already completed.

Cancelling a parent or shutting down finalizes active children and the parent.
Explicitly cancelling a child through the normal run API returns a cancelled
child error report to the parent, which may continue. No child remains queued or
running after accepted cancellation. A crash uses the normal interrupted-run
recovery on restart; delegated work is not replayed automatically.

## Validation

Mock-model tests cover single-slot completion, private transcripts, parent-only
memory, continuation and ownership, allowlist/grant refusal, failed child reports,
output truncation, configured timeout, parent/child/shutdown cancellation,
cancellation during durable child admission and serialization with normal turns.
These are automated runtime checks, not verification against a live model or
external worker services. No live operator profiles or prompt fragments were
changed by implementation.
