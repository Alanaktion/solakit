# Sola v2

A local personal-agent runtime with
[durable evidence, sessions, and offline search](docs/contracts/storage.md).
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
It opens/reconciles the evidence store before serving health; chat and live clients
remain later phases. The four public commands are `serve`, `check`, `prompt`, and
`search`; prompt inspection arrives in Phase 4. Search works offline:

```sh
uv run sola search 'exact phrase' --data-dir /absolute/sola-data
uv run --locked python scripts/storage.py rebuild --data-dir /absolute/sola-data
```

Search is a disposable projection of immutable JSONL. Sessions, admission keys and
operational state live in state.db. See [backup/restore](docs/backup-restore.md)
for coherent checkpoints, recovery and synthetic fixture import.

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
and [validation evidence](docs/validation/phase-03.md). Dispatch, memory, tools,
the run loop, and production migration arrive in later phases.
