# Sola v2

A local personal-agent runtime, currently implementing the
[Phase 1 development skeleton](docs/plans/PHASE-01-skeleton-evaluation.md).
Python 3.15, FastAPI at the HTTP boundary, and the OpenAI SDK for local oMLX evaluation.
The core wheel contains `solakit` only; developer evaluations and future packs are separate.

```sh
uv sync --locked
uv run sola check --explain
uv run sola serve
```

`serve` exposes the skeleton's `/health` at `http://127.0.0.1:8765` with one worker.
It does not yet provide chat or a configured agent. The four public commands are
`serve`, `check`, `prompt`, and `search`; the latter two report their pending phases.
`check` verifies installation without starting services or reading credentials.

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv run --locked python scripts/smoke_wheel.py
```

See [evaluation instructions](evals/README.md), the [phase plans](docs/plans/README.md),
and [validation evidence](docs/validation/phase-01.md). Production storage, permissions,
memory, tools, and the run loop arrive in later phases.
