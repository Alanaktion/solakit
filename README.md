# Solakit

A small FastAPI runtime for Sola. Python 3.12+ and uv are required.
Implementation progress and exit checks are in [docs/plan](docs/plan/README.md).
The full target is [SPEC.md](SPEC.md); integrations are implemented in phases.

## Development

```sh
uv sync --frozen
cp config.example.toml config.toml
# Edit paths, including the reference to the existing live SOUL.md.
# Supply SOLA_OPERATOR_TOKEN through your environment or use a 0600 token file.
uv run uvicorn solakit.main:app --host 127.0.0.1 --port 8000
```

Startup requires valid configuration and an operator secret. The model may be
unavailable: the app stays up and `/health` reports degraded status.
`/health/model` checks the exact pinned model ID, with no fallback.
The root `/` is reserved for Alan's UI. `/api` requires an
`Authorization: Bearer <operator-token>` header. `/api/channels` explicitly
reports disabled or unimplemented adapters. No adapters or jobs run yet.

The scaffold's `/v1/runs` was removed. Its temporary injected-agent test seam is
`POST /api/runs`; phase 5 replaces it with the persistent session execution path.
The default runtime returns 503 while no agent is configured.

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

In the managed cloud environment, use `UV_CACHE_DIR=/workspace/.cache/uv`.
Runtime data must live outside the code tree. Never commit operator configuration
or secrets. This work does not deploy a pilot or change Hermes.

## Storage and inspection

`GET /api/sessions`, `/api/sessions/{id}`, `/api/runs`, and `/api/runs/{id}`
inspect local evidence. Session metadata and transcripts, run event journals,
exact prompt files, and redacted tool logs are authoritative. `app.db` is rebuilt
at startup; deleting it loses no evidence. Unfinished runs become `interrupted`
on restart, with partial evidence preserved. Each `.json` run snapshot is backed
by append-only `.events.jsonl`. JSONL writes are fsynced. Prompt files preserve
exact UTF-8 bytes, including CRLF; they are intentionally not redacted. Treat the
data directory as private operator material, just like the source persona.
