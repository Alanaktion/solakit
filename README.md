# Sola v2

A local personal-agent runtime with
[Phase 2 records, configuration, and resource ownership](docs/contracts/runtime.md).
Python 3.15, FastAPI at the HTTP boundary, and the OpenAI SDK for local oMLX evaluation.
The core wheel contains `solakit` only; developer evaluations and future packs are separate.

```sh
uv sync --locked
uv run sola check --explain
uv run sola check --config config.example.toml --explain
uv run sola serve --config config.example.toml
```

`serve` validates configuration and owns the data-directory lock for `/health`
at `http://127.0.0.1:8765` with one worker. Set the data directory and workspace
roots before using the example; relative paths resolve beside the TOML file.
It does not yet provide chat or live clients. The four public commands are
`serve`, `check`, `prompt`, and `search`; the latter two report their pending phases.
`check` validates installation/configuration without starting services or reading
credentials. `--explain` includes effective authority and each value's source,
with credential references redacted. The default config path is
`~/.config/sola/config.toml`; a missing default reports an unconfigured installation.

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv run --locked python scripts/smoke_wheel.py
```

See [evaluation instructions](evals/README.md), the [phase plans](docs/plans/README.md),
and [validation evidence](docs/validation/phase-02.md). Production storage, permissions,
memory, tools, and the run loop arrive in later phases.
