# Bounded delegation validation

Base commit `9060f9c`, with the delegation changes in the working tree. Checks
ran on macOS arm64 with Python 3.15.2.

| Gate | Result |
| --- | --- |
| `uv run --locked pytest -q` | 705 passed. |
| `uv run --locked pytest -q tests/test_delegation.py` | Passed. Covers one-permit parent/child execution, prompt isolation, worker and parent grant narrowing, nested calls and writes, shared budgets, FIFO slot contention, cancellation in both directions, deadline cleanup, restart after the atomic child admission, and authenticated submission conflicts. |
| `uv run --locked ruff check .`, `uv run --locked ruff format --check .`, `uv run --locked mypy` | Passed. |
| `uv run --locked python scripts/smoke_wheel.py` | Passed. Clean wheel install, offline commands and external pack dispatch. |
| `scripts/check_chat.mjs` against `scripts/chat_fixture.py` | 28 checks passed in headless Chromium. Verified matching links in the parent turn and its run evidence, followed the link to the child's brief and report, confirmed the composer was disabled, and opened child model evidence. The fixture uses a synthetic model transport. |
| Live pinned Qwen with one inference permit | Passed for a disposable local-file task and a busy refusal. Uses the configured local oMLX endpoint and `Qwen3.6-35B-A3B-AntiLoop-NVFP4`. |

The live probe used a disposable data dir, one workspace containing
`survey.txt`, a `research` worker with `files.read`, and a worker fragment
directing it to open the named source before producing a short factual report.
With two active slots, the child's receipt verified it read the file and its
report identified 17 waxwings. The parent attributed the result to the worker.
With one active slot, the parent received `busy`, no child was admitted, and
the parent described the attempt as incomplete. An earlier trial with a bare
worker profile did not produce a completed report. The live pass covers the
configured fragment and this task.

The live probe used disposable files and local evidence. OpenViking, production
operator settings, and a broader model reliability corpus were outside these
checks. Child continuation and timed follow-ups have a separate plan.
