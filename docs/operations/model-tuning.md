# Model request tuning, diagnostics and evaluation

Solakit talks to one pinned model. These settings shape each completion request;
with none of them set the wire request is byte-for-byte what earlier versions sent.

## Request parameters

```toml
[model]
max_tokens = 8192
tool_call_fallback = false      # see below

[model.request]                 # defaults for every completion
temperature = 0.7
top_p = 0.8
top_k = 20
presence_penalty = 0.5
# parallel_tool_calls = false   # force serial calls (latency for validity)
# thinking_control = "chat_template_kwargs"  # or "none" if the server rejects it
[model.request.extra_body]      # passed through verbatim (≤ 4 KB JSON)
# chat_template_kwargs = { enable_thinking = false }

[profiles.interactive.model]    # per-profile overrides
thinking = "auto"               # "off" | "on" | "auto"; unset sends nothing
max_tokens = 8192               # replaces [model].max_tokens for this profile
max_reasoning_tokens = 4096     # optional cap, ~4 chars per token
temperature = 0.6
```

Known sampling keys are typed and bounded; `extra_body` may not override
`model`, `messages`, `tools`, `tool_choice`, `stream`, `max_tokens` or `n`.
Each run records the effective parameters of its first step as `request_params`.
Verify current recommended sampling values against the pinned model's card.

## Thinking

`thinking = "on"`/`"off"` sends `chat_template_kwargs.enable_thinking` (unless
`thinking_control = "none"`). `"auto"` thinks for the fresh user message and after
any repairable tool error, and turns thinking off for plain tool-result
continuations. Keep `on`/`off` as the stable modes; `auto` stays only if
`sola eval` shows a gain.

Reasoning (`reasoning_content` deltas, or a leading `<think>…</think>` in content)
is removed from the visible reply and stored in `runs/<id>.reasoning.jsonl`
(redacted like diagnostics). It is shown on the run page behind a disclosure and
is never written to the transcript or sent back to the model.

- `max_reasoning_tokens`: when reasoning passes the cap before any answer or tool
  call, the stream is closed and the step is retried once with thinking off; the
  run lists it under `retries`.
- `finish_reason: length` with only reasoning ends `budget_exceeded` with
  "Thinking exhausted model.max_tokens …" rather than the generic message.

Empty normal stops have a separate one-time recovery using `stream: false` on
the same pinned endpoint, thinking off and a request-only runtime notice. This
handles servers that silently suppress unoffered tool calls in SSE; returned
calls still require the existing activation and authority checks. The HTTP body
remains bounded, timeout-controlled and closed on cancellation. Every rejected
attempt records private masked response details and actual parameters in
`runs/<id>.diagnostics.jsonl` (`kind: model_rejection`); response JSON is capped at
32,768 characters with an explicit truncation flag. No request messages or
headers are copied into those diagnostics.

## Tool-call fallback

Some server/template combinations leave Qwen-style `<tool_call>` blocks in the
content. With `tool_call_fallback = true`, a reply that is exactly prose followed by
well-formed blocks (Hermes JSON or Qwen3-Coder XML) naming offered tools becomes
tool calls with ids `call_fb_<n>`. The assistant message keeps its verbatim content
and records `tool_call_source = "content_fallback"`. Anything ambiguous (code
fences, unknown names, malformed JSON, trailing text, truncated output) is left as
text. `sola doctor` reports whether the server needs it.

## Token budgets

`context_budget` remains the byte ceiling. Optional per-profile `context_tokens`
adds a token budget: every completion records `request_bytes / prompt_tokens` in
`calibration.json` in the data directory (bounded 1.5–8 bytes/token, running
average). When a sample exists, a request whose estimated tokens plus the output
budget exceed `context_tokens`, or that exceeds 85% of it, triggers compaction (or
`context_limit`). Within a run the previous step's server-reported
`prompt_tokens` is used as a floor. Without a sample, only the byte rule applies.

## `sola doctor`

Live counterpart of `sola check`. Run with the model key in the environment:

```sh
set -a; source .env; set +a
uv run sola doctor            # full probe set
uv run sola doctor --quick    # skip prefix-cache and concurrency probes
uv run sola doctor --json
```

Probes: model reachable and pinned id served; streaming terminates; tool-call round
trip (WARN when calls arrive as content); argument fidelity for int/bool/array/enum;
sampling parameters accepted; thinking toggle; advertised context window and
measured bytes/token; prefix-cache speed-up on a shared long prefix; concurrency
throughput/p95 at 1/2/4 with a recommended `[model].concurrency` (the knee);
per-profile tool schema bytes; memory read-only checks. Results are cached in
`doctor.json` in the data directory. Exit status is 1 when any probe fails.

## `sola eval`

See `evals/README.md`. Every behavioural change to prompts, tools or request
parameters should land with a before/after `sola eval compare`.
