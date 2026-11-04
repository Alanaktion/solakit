# Solakit

A minimal FastAPI foundation for a personal assistant agent runtime. Python 3.12+
and [uv](https://docs.astral.sh/uv/) are required.

## Development

```sh
uv sync --frozen
uv run uvicorn solakit.main:app --reload --host 127.0.0.1 --port 8000
```

`GET /health` returns `{"status":"ok"}`. Interactive API documentation is at
`/docs`; the schema is at `/openapi.json`.

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

In the managed cloud environment, set `UV_CACHE_DIR=/workspace/.cache/uv`
before running uv; the default home cache is read-only.

## Extending the runtime

Implement the async `Agent.run(message: str) -> str` protocol in
`src/solakit/runtime.py`, then inject `Runtime(your_agent)` into `create_app`.
`POST /v1/runs` accepts `{"message":"Hello"}` and returns `{"response":"..."}`.
The default runtime returns HTTP 503 until an agent is configured. No model
provider, credentials, persistence, tools, or background execution are included.
Tests inject a small deterministic agent to exercise the interface.

Bind locally for development. Authentication and authorization must be added
before exposing assistant execution to an untrusted network.
