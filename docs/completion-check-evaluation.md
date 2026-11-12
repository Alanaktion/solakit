# Completion check evaluation, 2026-11-12

Pinned local model: Qwen3.6-35B-A3B-mxfp4. Isolated `sola eval` trials
used fake tool implementations and no memory writes.

Command: `sola eval --only zero-arg-runtime,no-tool-chitchat --trials 2
--matrix profile.completion_check=false,true`.

`sola eval compare` between the two cells reported:

| Scenario | Disabled | Enabled | Pass-rate difference, 95% bootstrap interval |
|---|---:|---:|---|
| no-tool-chitchat | 2/2 | 1/2 | -0.50 [-1.00, 0.00] |
| zero-arg-runtime | 2/2 | 2/2 | 0.00 [0.00, 0.00] |
| overall | 4/4 | 3/4 | -0.25 [-0.75, 0.00] |

The failed chitchat trial hit the output-token limit at step one, before
completion checking. These small samples demonstrate neither a success-rate
improvement nor a causal regression. They do not establish live recovery of the
historical failure. Controlled regression tests verify continuation, verbatim
evidence preservation, and bounded failure on repeated announcements.

Prior inference-only historical replays reproduced both reasoning-only normal
stops and announcement-only replies at about 16.7K input tokens. Prompt guidance
alone was inconsistent. Context saturation is not established as the cause.
The operator's interactive compaction ceilings are an initial tuning choice;
this evaluation does not measure compaction quality.
