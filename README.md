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
It opens/reconciles the evidence store and owns a pinned oMLX client before serving
health; startup does not contact oMLX. Phase 5 adds a headless tool dispatcher;
the conversation/run loop remains Phase 6.
The four public commands are `serve`, `check`, `prompt`, and `search`. Search works offline:

```sh
uv run sola search 'exact phrase' --data-dir /absolute/sola-data
uv run --locked python scripts/storage.py rebuild --data-dir /absolute/sola-data
```

Search is a disposable projection of immutable JSONL. Sessions, admission keys and
operational state live in state.db. See [backup/restore](docs/backup-restore.md)
for coherent checkpoints, recovery and synthetic fixture import.

Prompt inspection uses the same assembler as model execution, without creating
the server or model client. Output contains private prompt content:

```sh
uv run sola prompt 'Hello' --config config.example.toml --hashes
uv run sola prompt --config config.example.toml --frozen /private/selection.json
uv run sola prompt --request-body /private/body --manifest /private/manifest --hashes
uv run python scripts/probe_model.py --output /tmp/sola-model-probe
```

The developer probe uses disposable evidence, loads the selected credential from
local `.env` when present, and never dispatches its synthetic tool calls. Normal
runtime reads `OPENAI_API_KEY` or the configured credential environment variable.
See the [model/prompt contract](docs/contracts/model-prompts.md) for exact placement,
request artifacts, timeouts, reasoning fields and estimated token accounting.

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
and [validation evidence](docs/validation/phase-05.md). The
[tool SDK and access matrix](docs/contracts/tools.md) describe explicit pack loading,
guarded file access, transcript search, status, and the unavailable memory port.
The separately installable [example pack](examples/phase5_pack/) demonstrates
reads, writes, and fake image generation. No real image endpoint is used here.
Memory, the run loop, and production migration retain their later phase gates.
