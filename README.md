# 🌙 Sola

Sola is a local personal-agent runtime for interactive conversations, tool use,
semantic memory, and scheduled work. `main-v2` is the production branch and the
v1 cutover is complete.

## Setup

On macOS, install the locked Python 3.15 environment:

```sh
uv sync --locked
```

Copy [config.example.toml](config.example.toml) to `~/.config/sola/config.toml`.
Set `model.pin` to the exact Qwen model ID served by oMLX and
`defaults.workspace_roots` to the directories Sola may use. Relative paths
resolve beside the TOML file. Set `SOLA_OPERATOR_PASSWORD` in the environment
to enable operator login. [Configuration reference](docs/config.md)

```sh
uv run sola check --explain
uv run sola check --live
uv run sola serve
```

`check --explain` shows effective configuration and authority offline.
`check --live` checks model and memory availability without running inference.
Open `http://127.0.0.1:8765/` for chat and Settings. The default bind is
localhost. Exact private LAN and Tailnet IPs can use direct HTTP, while public
IPs and hostnames require HTTPS. [Deployment and login](docs/contracts/api.md#deployment-and-login)

Use `sola service` to manage a macOS LaunchAgent for each configured instance,
`sola doctor` for model diagnostics, and `sola config diff` to review grant
changes offline. [Operator CLI](docs/cli.md)

## Documentation

- [Chat and Settings](docs/chat-ui.md) and [authenticated HTTP API](docs/contracts/api.md)
- [Schedules and proactive work](docs/contracts/jobs.md), [session modes](docs/contracts/session-modes.md),
  and [OpenViking setup and recovery](docs/memory.md)
- [Tool SDK and access rules](docs/contracts/tools.md), [file and image workflows](docs/image-workflows.md),
  and [blog and dream tools](docs/blog.md)
- [Evidence and storage](docs/contracts/storage.md), [backup and restore](docs/backup-restore.md),
  and [legacy import](docs/migration/cutover.md)
- [Product scope](docs/product.md), [design decisions](docs/decisions.md), and
  [follow-up plans](docs/plans/README.md)

## Development

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv run --locked python scripts/smoke_wheel.py
```

The core wheel contains `solakit`. The separately installable
[SDK example pack](examples/phase5_pack/) demonstrates guarded reads and writes
and fake image generation. See the [evaluation guide](evals/README.md) for
disposable model checks and the [regression scenarios](docs/contracts/acceptance.md)
for end-to-end behavior.
