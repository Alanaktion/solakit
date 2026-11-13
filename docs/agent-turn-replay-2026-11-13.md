# Empty-response follow-up: controlled replay, 2026-11-13

This follows the [initial turn audit](agent-turn-audit-2026-11-13.md) and run
`e5277fd6-bfe2-427f-817e-15dbf580d6eb`. Investigation proceeded through rejected
response instrumentation, controlled replay, thinking-control checks, and a
transport fix. The pinned model remained Qwen3.6-35B-A3B-mxfp4; the installed
oMLX source reports version 0.7.0.dev2. No backend installation, operator
configuration, live session, integration or schedule was modified.

## 1. Capture rejected attempts

`ModelUnavailable` exceptions now carry private response diagnostics. The turn
loop saves each rejection, including the final exhausted attempt, in the existing
`runs/<id>.diagnostics.jsonl` sidecar. Records include step and attempt numbers,
exception/validation error, hash of the request, actual generation/transport
parameters, offered tool names, and response details including reasoning, finish
reason and usage when available. Interrupted streams retain their observed output.

Diagnostics omit request messages and authorization headers. The shared runtime
redactor masks resolved values before response JSON is limited to 32,768
characters; original character count and truncation are explicit. Diagnostics
remain separate from assistant/user/tool transcript evidence and are never resent
to the model. These are normalized response details, not a byte-for-byte packet
capture. Earlier failed runs cannot gain missing evidence retroactively.

## 2. Replay without tool execution

Reconstructed the original first request from its saved prompt, saved turn block,
saved initial schemas, and the active transcript entries preceding admission.
The transcript's current entries all preceded this run. The replay matched the
original backend's reported 20,716 default prompt tokens and 20,694 tokens with
thinking disabled. Model calls were made directly; no tool dispatcher ran.

| Probe | Result |
|---|---|
| Historical context, original schemas, backend thinking default | 85 visible characters plus 332 reasoning characters; announcement, no call |
| Same context/schemas, thinking explicitly off | Empty visible response, normal stop; backend reports 98 generated tokens |
| Same context, no offered schemas, thinking off | 324 visible characters, no reasoning |
| Minimal greeting, thinking off | 6 visible characters, no reasoning; 2 generated tokens |
| Minimal greeting, thinking on | 46 visible characters plus 5,272 reasoning characters |
| Historical context, image schemas added, thinking off | Structured `image.generate` call |
| Historical context, only image schemas, thinking off | Structured `image.generate` call |
| Original context/schemas, **non-streaming** chat, thinking off | Structured `image.generate` call despite that name not being offered |

The non-streaming request's prompt-token count remained 20,694. A separate raw
completion of a locally rendered template also produced a Qwen XML
`image.generate` envelope. That rendering differed slightly in token count and
is corroborating evidence rather than an exact replay.

## 3. Verify thinking control and identify the discrepancy

The installed model template emits an open `<think>` prefix when thinking is on,
and an already closed block when off. oMLX merges the request's thinking toggle
over the model default, with no forced override configured for this model. The
minimal on/off probes demonstrate a corresponding reasoning-output difference.
Thus simply advising the operator to disable thinking does not resolve this case.

The installed oMLX streaming code filters early/terminal Qwen tool envelopes
against registered (offered) function names. The original request offers deferred
search/load and resident tools, but not `image.generate`; the transcript contains
previous `image.generate` calls. The model attempts to reuse that function without
loading its schema. Streaming suppresses it and returns no usable output, while
the non-streaming endpoint returns the call. This is a demonstrated transport
inconsistency around unoffered calls, not evidence that the model generated zero
tokens or that the image service itself failed.

The model's failure to reload a deferred schema is real too. Solakit already has
an inactive-tool notice/activation path, but that path cannot work when the server
silently removes the call before Solakit receives it.

## 4. Fix and validation

The existing one-time empty-response recovery now requests `stream: false` from
the same pinned endpoint, retaining thinking-off and the recorded continuation
notice. No retries were added. The body still uses bounded incremental HTTP reads
and closes on timeout/cancellation. Subsequent normal steps use streaming again.

Recovered calls pass normal envelope validation and registry dispatch. An inactive
call receives the existing activation notice first; it is not immediately
executed. Narrowed per-run tool ceilings still block calls. Malformed or
interrupted responses are not retried through this path. The operator hint now
points to rejected-attempt diagnostics rather than asserting zero model output.

Regressions verify private/redacted/bounded diagnostics for all three rejected
attempts, preserved protocol failure statuses, JSON recovery without SSE options,
return to streaming on later steps, deferred activation before execution, narrowed
authority, and preservation of transcript evidence. The full core/pack suite
passed **1,037 tests**. Mypy, Ruff lint/format checks and all distribution builds
passed. Operator hint tests also passed after the wording update.

A second isolated run used the historical prompt/history with thinking off and
fake implementations for every granted domain tool. The patched turn loop
recovered the first empty stream via JSON, activated the missing image schema,
and reached a visible final reply in six steps. No image, file, mail, memory or
other external operation was executed. This verifies loop progress, not image
integration health or the correctness of every statement in the model's reply.
The fake backend also does not certify real pack resource-alias validation.

Isolated `sola eval` runs of `no-tool-chitchat`, `zero-arg-runtime`, and
`named-image-workflow` passed 6/6 trials (two per scenario). Compared matching
scenario subsets with the previous baselines using `sola eval compare`: all three
remained 2/2, pass-rate delta 0.00, bootstrap interval [0.00, 0.00]. These tiny
samples do not establish broad accuracy improvement. After-results are in
`/tmp/solakit-rejection-eval`; temporary subset files preserve only matching
trials for comparable suite totals.

Private transient replay results are under the OS temporary directory in
`solakit-e527-replay-*` and `solakit-e527-loop-*`; private prompts and model
reasoning are intentionally excluded from this repository report.

## Limits

The backend may still return unusable JSON, and the model may still misuse aliases,
stop after an announcement, or overstate what tool output establishes. Recovery
remains bounded and fails honestly if it cannot obtain a usable response. This
change works around the demonstrated SSE discrepancy; it does not patch oMLX or
certify the image pipeline. A running Solakit server must restart to load it.
