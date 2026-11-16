# Evaluation baseline

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

For a live baseline, put `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and the exact `OPENAI_MODEL`
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
parameters, scorer version, and thresholds. `results.jsonl` preserves complete canonical
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

The evaluator has no narration continuation: it stops on a final response, detects
unattempted action cases in scoring, and exposes the baseline failure. Its bounded
model/tool cycle will be replaced by the production application entry point in Phase 6.
`repair_count` counts invalid-argument receipts; it is not a production repair policy.

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
