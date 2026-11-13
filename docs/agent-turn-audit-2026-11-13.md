# Agent-turn reliability audit, 2026-11-13

## Evidence and scope

Reviewed saved run metadata and the adjacent active Discord transcript, then traced
admission/FIFO execution, prompt/history construction, deferred tool activation,
SSE assembly and model validation, reasoning recovery, tool dispatch/repair,
completion checks, structured output, and budget wrap-up. No operator transcript,
configuration, external integration, or schedule was changed.

The supplied ID `08194b73-5aef-497c-8313-e4e44f0251b0` identifies an interactive
run in `discord-dm`, not a managed scheduled job. It started at 20:28:13 UTC and
finished at 20:29:31 UTC on November 13. Admission recorded `Try just "image"`.
Its status was `model_unavailable`, error `Empty completion`, steps 0, no tool
activations, no dispatched calls, and one `reasoning_exhausted` retry. The pinned
model was Qwen3.6-35B-A3B-mxfp4, max_tokens 8192, with deferred disclosure.

The recorded retry establishes that the first attempt produced reasoning without
visible output (no configured reasoning cap). The following attempt with thinking
disabled returned empty output. The old runtime had no recovery for that second
failure. Rejected response bodies were not saved, so their exact bytes and the
backend cause cannot be reconstructed. Neither step exhaustion nor a failed image
submission explains this run.

A metadata scan of runs created after November 11 found 59 records: 56 completed,
one cancelled, one missed and this model-unavailable run. The missed scheduled
letter check also recorded `Empty completion`. These are runtime statuses, not
proof of task success; older announcement-only responses were marked completed.

## Findings and changes

1. **Empty normal stops had no recovery.** Add a distinct `EmptyCompletion`
   exception, retain healthy-endpoint availability, and retry once with thinking
   disabled plus a recorded request-only continuation notice. Retain the separate
   one-time reasoning recovery, with at most three requests per step total. Save
   retry decisions durably and report the actual thinking mode and request size
   for the accepted response. Repeated empties still fail honestly. Do not retry
   network/protocol failures or replay any accepted tool execution. Recovery
   notices obey context budgets and do not enter the transcript as user messages.
2. **Named resource grants were absent from prompt assembly.** In preceding run
   `6480ac83`, the model loaded the image schemas, guessed `txt2img`, received
   `Missing resource: image:txt2img`, and described the workflow as unconfigured.
   The current profile grants `image:studio`. Add a deterministic resource fragment
   listing only the selected profile's grants, explaining alias syntax and that
   grants do not establish live availability. No sidecar secrets are exposed.
   The user-suggested `image` is also not the current granted alias.
3. **Answer-mode wrap-up bypassed final-output validation.** A budget wrap-up could
   mark invalid job JSON completed and leave structured/rendered output unset.
   Validate it using the existing structured-output path with no further work
   steps. Also reject action-only wrap-ups when completion checking is enabled.
   Preserve the original reply as evidence in either case.
4. **Null token-limited content was classified as empty failure.** Make `length`
   handling consistent for null, empty and partial content: retain partial
   evidence, end as `budget_exceeded`, and never invoke empty-response recovery.

Other audited safeguards remain: preflight and first-response validation before
transcript mutation, complete assistant/tool groups in model history, no execution
of length-truncated calls, authority separate from deferred schema visibility,
per-run repair/disclosure/work budgets, and evidence before tool execution.

## Validation

Controlled regressions cover direct empty stops, the historical reasoning→empty
sequence, successful recovery and bounded repeated failure, preservation of tool
results without reexecution, resource scoping, structured/rendered wrap-ups,
action-only wrap-ups, and null/empty/partial length stops.

Isolated real-model evaluations used fake tool backends and disabled memory.
Before/after runs of `no-tool-chitchat,zero-arg-runtime`, two trials each, passed
4/4 on both revisions. `sola eval compare` reported a pass-rate delta of 0.00
and bootstrap interval [0.00, 0.00] for each scenario and overall. This tiny
sample does not establish an accuracy improvement or recovery of the historical
run. Mean elapsed trial time rose from 13.56s to 25.18s; the after evaluation
shared the model with another evaluation, so this is not a latency comparison.

The new `named-image-workflow` scenario passed 2/2 trials using the `studio`
alias, real schemas and fake submission responses. No image was generated and no
external write was tested. Raw eval results stay in `/tmp/solakit-turn-audit-*`;
this report intentionally excludes private prompt and conversation payloads.

Workspace validation passed: 1,033 tests across core and packs, `sola check`,
Mypy, Ruff lint/format checks, and builds of all nine distributions. The test
suite emitted the existing Starlette/httpx deprecation warning.

Commands:

```sh
sola eval --only zero-arg-runtime,no-tool-chitchat --trials 2 --out <before/after>
sola eval compare <before.json> <after.json>
sola eval --only named-image-workflow --trials 2 --out <image-results>
```

## Remaining limitations

The model may still stop prematurely, invent unsupported claims, choose a wrong
alias, or ignore continuation guidance. Completion checking remains an opt-in,
English action-announcement heuristic; it cannot certify task completion.
Recovery can add up to two bounded inference requests to a step. Rejected
reasoning/empty attempts still lack full response/usage evidence; this audit does
not claim complete inference accounting for those attempts.

The failed run also recalled lengthy historical material about the former runtime,
and the active compaction summary contained old claims about scheduler/memory
availability. Those are potential sources of stale beliefs, not demonstrated
causes of empty generation. Operator-owned memory was not rewritten. This change
does not certify ComfyUI, Discord connectivity, scheduled delivery, or memory
extraction. Running server processes require a restart to load the fixes.
