# Corpus evaluation

Run from the repository root. These are developer commands, independent of FastAPI
and excluded from the core wheel. Each case runs three times in a fresh temporary
directory. The only callable implementations are this harness's fixed tool list;
publications append to a fake in-memory destination, and recall is a fixture.
No production memory, mail, publishing, printing, or process execution is available.

```sh
uv run --locked python -m evals.runner --output /tmp/sola-fake-baseline
uv run --locked python -m evals.replay /tmp/sola-fake-baseline
uv run --locked python -m evals.compare /tmp/sola-fake-baseline /tmp/sola-fake-baseline
```

The fake corpus deliberately fails announcement-only, reasoning-only, incorrect-success,
and duplicate-effect cases. This is expected: the scorer must reject them. Runner exit
status reports successful evidence collection, not model reliability. Pytest verifies
the expected outcomes, including both successful work and these negative controls.

For a live collection, put `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and the exact `OPENAI_MODEL`
in the ignored `.env`. Clear inherited variables so uv reads that file's values:

```sh
env -u OPENAI_API_KEY -u OPENAI_BASE_URL -u OPENAI_MODEL \
  uv run --locked --env-file .env python -m evals.runner \
  --driver openai --base-url http://127.0.0.1:8000/v1 \
  --server-version 0.7.0.dev2 --output /tmp/sola-qwen-baseline
```

Use the installed server version, not necessarily the example above. An optional
`SOLA_EVAL_API_KEY` overrides `OPENAI_API_KEY`. The live driver checks `/models` for
the explicit pin and rejects a different returned model. SDK retries are disabled;
requests time out after 60 seconds. A model name does not verify the weight files.
Use a new output directory for every measurement; existing evidence is never overwritten.

`metadata.json` freezes corpus hash, package versions, model pin, server-version label,
parameters, runtime policy, scorer version, and thresholds. `results.jsonl` preserves complete canonical
request objects, request/prompt/schema hashes, SDK response dumps (including unknown
reasoning fields), errors, receipt call IDs, final disposable artifacts, effects, timing,
and usage when supplied. `report.json` provides per-case and aggregate denominators.
These are private evidence files; credentials and authorization headers are excluded.
Canonical request hashes are not a claim about exact HTTP serialization bytes, which
is a Phase 4 gate. Replay uses the embedded case snapshot and never calls a service.

The interrupted-generation case is explicitly fixture-only even in a live collection.
It validates scoring of preserved partial output; it does not prove live streaming
interruption handling. Historical announcement/reasoning cases are regression
hypotheses, not reconstructed legacy prompts. This small corpus and repeated prompts
do not estimate general model capability or statistical significance.

Scorer version 1 requires actual artifacts, receipts, and expected fake publications;
success prose is never proof of action. Denial and expected interruption can meet their
case expectations while remaining outside task-completion successes/denominators.
Conversation requires a nonempty final answer, a normal stop, and no unexpected effects;
semantic quality and honest interpretation of memory failures need operator review.
Malformed arguments may be repaired naturally; a valid first call is also acceptable.
Missing-tool refusal may happen without hallucinating an attempted call.

The Phase 1 baseline has no narration continuation: it stops on a final response,
detects unattempted action cases in scoring, and exposes the baseline failure.
Phase 6 runtime collection below uses the production application entry point.
`repair_count` counts invalid-argument feedback, independently of successful repair.

[thresholds.json](thresholds.json) freezes implementer-selected initial reliability
gates: >=90% applicable task completion and first-tool accuracy, with zero silent
unattempted tasks, false continuations, incorrect effects, or duplicate effects.
These are future candidate gates, not a claim that a baseline must already meet them.
Any revision must precede candidate measurement and be recorded as a decision.
Deferred disclosure remains off until matched live pairs lose no successes, add a
median <=1 model step, reduce total tool-heavy schema bytes >=20%, and meet reliability
gates. Unmatched model/server/corpus/sampling/scorer/threshold/driver conditions are
labeled and cannot pass that gate; fake runs cannot enable it. Legacy and other-harness
comparisons are deferred until their measurement conditions can be matched.

Phase 6 makes `--harness runtime` the default. It calls `context.runs.submit/wait`
with the real store, model adapter, SDK dispatcher and guarded file tools. The
Phase 1 loop remains explicitly available with `--harness baseline` for historical
comparison. Existing case/threshold snapshots are unchanged. Candidate scores also
require a completed runtime outcome for action/conversation successes; an honest
failed run with prose cannot count as completed work. False continuation includes
unneeded narration/disclosure recovery, even if no tool ultimately ran.

Runtime collections keep each trial's JSONL, state/search databases, exact requests,
raw SSE and recovery notes under `OUTPUT/evidence/CASE-TRIAL/data`. File workspaces
are temporary directories outside the protected source checkout. Final file
snapshots and every fake publication remain in results.jsonl. Malformed JSON batches
are preserved as nonexecuted validation evidence (`executed: false`), separately from
actual ToolReceipts. Live runs use production transport; fake runs replace only that
transport with corpus responses. The interrupted case remains fixture-only.

Use explicit pin/endpoint values when `.env` still names a previous model/server:

```sh
env -u OPENAI_API_KEY -u OPENAI_BASE_URL -u OPENAI_MODEL \
  uv run --locked --env-file .env python -m evals.runner --driver openai \
  --harness runtime --disclosure eager --base-url http://127.0.0.1:8000/v1 \
  --model Qwen3.6-35B-A3B-mxfp4 --server-version 0.7.0.dev2 \
  --output /tmp/sola-runtime-eager
# Repeat under the same conditions with --disclosure deferred and a fresh output.
uv run --locked python -m evals.compare /tmp/sola-runtime-eager /tmp/sola-runtime-deferred
```

Runtime sampling records the existing 1024 output-token corpus budget, streamed
transport and thinking disabled. Thinking-enabled trial collections remain
separate, unmatched measurements; their sampling labels are not rewritten.
Truncation is a failure, never tool execution from partial output. A complete collection can fail the declared task/effect gates;
read the aggregate counts instead of interpreting process exit 0 as acceptance.
Deferred mode is an experiment-only constructor setting. `compose` and `serve`
start eager and have no switch enabling it. Paired comparisons label different
harnesses as unmatched; fake results cannot authorize deferred disclosure.

The denial fixture matches the baseline's advertised-schema/denied-dispatch
conditions: admission advertises its tool, then a current-authority callback
revokes the configured denied names once an intent exists. The production
dispatcher writes the denied receipt before any file effect. Initial schema
hiding would measure a different refusal task and is not used by the final pair.
The `runtime_policy` metadata label distinguishes this correction and the JSON
receipt envelope from earlier collections; comparisons label policy differences
as unmatched, without changing corpus cases, scorer version or thresholds.
