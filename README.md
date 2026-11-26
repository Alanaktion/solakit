# 🌙 Sola

A local personal-agent runtime with
[durable evidence, sessions, and offline search](docs/contracts/storage.md).
Python 3.15, FastAPI at the HTTP boundary, and the OpenAI SDK for local oMLX evaluation.
The core wheel contains `solakit` only; developer evaluations and future packs are separate.

```sh
uv sync --locked
```

Copy [config.example.toml](config.example.toml) to `~/.config/sola/config.toml` and set
`model.pin` and `defaults.workspace_roots` before the configured commands below.
The example records built-in effective defaults and is unconfigured until edited.
The [configuration reference](docs/config.md) covers every value, range and
inheritance rule. Keep it and the example current when changing configuration.

```sh
uv run sola check --explain
uv run sola check --config ~/.config/sola/config.toml --explain
uv run sola check --config ~/.config/sola/config.toml --live
uv run sola serve --config ~/.config/sola/config.toml
```

`serve` validates configuration and owns the data-directory lock for `/health`
at `http://127.0.0.1:8765` with one worker. Set the data directory and workspace
roots before using the example; relative paths resolve beside the TOML file.
Set the `SOLA_OPERATOR_PASSWORD` environment variable before serving to enable
operator login. Open `/` for the [chat and Settings UI](docs/chat-ui.md).
Phase 10 exposes the [authenticated HTTP API](docs/contracts/api.md),
[SSE transport](docs/contracts/events.md) and [operator CLI](docs/cli.md).
Use [`sola service`](docs/cli.md#run-sola-at-login) to run one or more configured
instances at macOS login.
The default bind is localhost. Exact private LAN and Tailnet IP origins support HTTP.
Public IPs and hostnames require HTTPS/Secure-cookie configuration and a private
backend behind a TLS proxy. See [LAN and Tailnet setup](docs/contracts/api.md#deployment-and-login).
It opens/reconciles the
evidence store and owns a pinned oMLX client before serving
health; startup contacts oMLX only when resuming queued work. Phase 6 adds the
[headless run service](docs/contracts/run-loop.md): durable bounded queues,
model/tool turns, cancellation, recovery and inspectable outcomes.
Run `sola doctor` for live diagnostics or `sola config diff old.toml new.toml` to review grant changes offline. See the [operator CLI guide](docs/cli.md). Search works offline:

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
uv run sola prompt 'Hello' --config ~/.config/sola/config.toml --hashes
uv run sola prompt --config ~/.config/sola/config.toml --frozen /private/selection.json
uv run sola prompt --request-body /private/body --manifest /private/manifest --hashes
uv run python scripts/probe_model.py --config ~/.config/sola/config.toml --output /tmp/sola-model-probe
```

The developer probe uses disposable evidence, loads the selected credential from
local `.env` when present, and never dispatches its synthetic tool calls. Normal
runtime reads `OPENAI_API_KEY` or the configured credential environment variable.
See the [model/prompt contract](docs/contracts/model-prompts.md) for exact placement,
request artifacts, timeouts, reasoning fields and estimated token accounting.

`check` validates installation/configuration without starting services or reading
credentials. `--live` explicitly checks model/memory health without inference,
operational state or background tasks. `--explain` includes effective authority
and each value's source,
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
and [validation evidence](docs/validation/phase-12.md). The
[tool SDK and access matrix](docs/contracts/tools.md) describe explicit pack loading,
guarded file access, transcript search, status, and scoped memory tools.
The separately installable [example pack](examples/phase5_pack/) demonstrates
reads, writes, and fake image generation. The optional built-in ComfyUI adapter
provides the real [file/image workflows](docs/image-workflows.md), durable generation
receipts/recovery, PNG previews and same-topic image attachments. Installation
alone enables no tool actions.
Live task reliability is measured separately from deterministic checks; see the
Phase 6 report. Phase 7 adds [OpenViking memory (stable >=0.5.5,<0.6.0)](docs/contracts/memory.md),
exact note staging, restart-safe extraction observation, and copied-configuration
migration. Setup, isolated live probes and reconciliation are in the
[memory runbook](docs/memory.md). Phase 8 adds
[context compaction](docs/contracts/context-compaction.md), private summary provenance,
and restart recovery while preserving original searchable history. Phase 9 adds
[durable scheduled/proactive jobs](docs/contracts/jobs.md), bounded wake-ups,
quiet-hour delivery and inspectable occurrence outcomes through `context.jobs`.
Phase 10 exposes authenticated job controls and headless conversation over HTTP.
Browser UI and production cutover retain their later gates.

Phase 11 adds chat/Settings, bounded history, stable streamed turns, durable browser
submission retries and recovery controls. See the [UI guide](docs/chat-ui.md) for
use and disposable browser acceptance; operator acceptance remains separate.
