# Real-model evaluation

`sola eval` drives the configured model through the real execution loop against
deterministic fake tool backends. It answers "did this change help the pinned
model?" with numbers instead of opinion.

```sh
set -a; source .env; set +a
uv run sola eval --quick                      # flagged subset, 2 trials each
uv run sola eval --trials 5                   # full suite
uv run sola eval --family selection,arguments
uv run sola eval --matrix disclosure=eager,deferred --quick
uv run sola eval compare evals/results/A.json evals/results/B.json
```

## Isolation

- A fresh temporary data directory per trial; the operator's sessions, runs,
  channels and jobs are never touched.
- Memory is disabled in the evaluation config unless `--allow-memory` is given.
- Tools keep their real schemas and descriptions, so the model sees exactly the
  production surface, but implementations are replaced by canned responses from
  the scenario. Only tools listed in `real_tools` execute for real, and then only
  inside a throwaway workspace seeded from the scenario's `[files]` table.
- The live SOUL and the scenario profile's fragment are read (not written), so
  prompt changes are measured as the model will see them.

## Scenario format

One TOML file per scenario under `scenarios/`, named `<id>.toml`.

| Key | Meaning |
|---|---|
| `family` | selection, arguments, zero_arg, multi_step, recovery, loop, formatting, long_context, thinking, disclosure |
| `prompt`, `followups` | first user turn, then later turns in the same session |
| `history`, `filler_chars` | prior messages; deterministic filler of about N chars |
| `tools`, `real_tools` | offered tools (all granted); subset executed for real |
| `responses."<tool>"` | ordered `{match, content, error}` canned results; first match wins |
| `expect_first_tool`, `expect_calls` | required first tool; required successful calls (`args_subset` string values match case-insensitively as substrings) |
| `allow_tools`, `forbid_tools`, `expect_no_tools` | non-required but acceptable tools; tools that must not be called |
| `expect_status`, `expect_final_contains`, `expect_final_any` | terminal status and final-reply text checks (last turn) |
| `max_steps`, `quick`, `overrides` | profile step budget; include in `--quick`; dotted config overrides |

## Metrics

Per scenario and overall: `pass_rate`, `first_tool_accuracy`, `arg_valid_rate`,
`repairs_per_run`, `steps_to_finish`, `spurious_call_rate`,
`forbidden_call_rate`, `length_stop_rate`, `empty_reply_rate`, prompt and
completion tokens, seconds, measured `bytes_per_token` (request bytes divided by
server `prompt_tokens`) and `second_turn_ttfb_ratio` (time to first response
byte of a follow-up turn relative to the first turn; well below 1 means the
server reuses the prefix cache).

Results are written to `evals/results/<timestamp>-<git sha>-<config hash>.json`
(git-ignored except an optional `baseline.json`). With 2–5 trials per scenario
only large differences are meaningful; `compare` prints a bootstrap interval.

## Matrix axes

`--matrix key=a,b` runs the cross-product. Keys are dotted config paths;
`profile.<field>` targets the scenario's profile. Aliases: `disclosure`,
`thinking`, `result_format`, `aging`, `max_tokens`, `concurrency`.
