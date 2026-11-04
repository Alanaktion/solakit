"""Application lifecycle and operator API; root is reserved for the UI."""

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, StringConstraints

from solakit.core.config import Config, load_config
from solakit.core.prompt import inspection, read_prompt
from solakit.core.routing import SessionRouter
from solakit.core.schemas import now
from solakit.core.secrets import RedactingFilter, Redactor
from solakit.core.store import Store
from solakit.runtime import AgentNotConfigured, Runtime
from solakit.tools import Registry


class RunRequest(BaseModel):
    message: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32000)
    ]


class RunResponse(BaseModel):
    response: str


async def probe_model(config: Config) -> dict[str, str]:
    try:
        async with httpx.AsyncClient(timeout=min(config.model.timeout_s, 5)) as client:
            response = await client.get(f"{config.model.base_url.rstrip('/')}/models")
            response.raise_for_status()
            ids = [entry["id"] for entry in response.json()["data"]]
        if config.model.model_id not in ids:
            return {"status": "unavailable", "reason": "pinned model not served"}
        return {"status": "ready", "model_id": config.model.model_id}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return {"status": "unavailable", "reason": "model endpoint unavailable"}


def create_app(runtime: Runtime | None = None, config: Config | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = config if config is not None else load_config()
        token = cfg.operator_token.resolve()
        app.state.config = cfg
        app.state.operator_token = token
        app.state.redactor = Redactor([token])
        log_filter = RedactingFilter(app.state.redactor)
        handlers = logging.getLogger().handlers
        for handler in handlers:
            handler.addFilter(log_filter)
        app.state.registry = Registry(cfg)
        app.state.store = Store(cfg.paths.data, app.state.redactor)
        app.state.router = SessionRouter(cfg, app.state.store)
        app.state.model_health = await probe_model(cfg)
        try:
            yield
        finally:
            for handler in handlers:
                handler.removeFilter(log_filter)

    app = FastAPI(title="Solakit", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime if runtime is not None else Runtime()

    async def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = f"Bearer {app.state.operator_token}"
        if authorization is None or not secrets.compare_digest(
            authorization.encode(), expected.encode()
        ):
            raise HTTPException(
                401, "Operator token required", headers={"WWW-Authenticate": "Bearer"}
            )

    api = APIRouter(prefix="/api", dependencies=[Depends(authorize)])

    @app.get("/health")
    async def health() -> dict:
        model = app.state.model_health
        return {
            "status": "ok" if model["status"] == "ready" else "degraded",
            "model": model["status"],
        }

    @app.get("/health/model")
    async def model_health() -> dict:
        app.state.model_health = await probe_model(app.state.config)
        return app.state.model_health

    @api.get("/channels")
    async def channels() -> list[dict]:
        return [
            {
                "id": c.id,
                "type": c.type,
                "enabled": c.enabled,
                "status": "disabled" if not c.enabled else "unimplemented",
            }
            for c in app.state.config.channels
        ]

    @api.post("/runs", response_model=RunResponse, deprecated=True)
    async def run(request: RunRequest) -> RunResponse:
        try:
            response = await app.state.runtime.run(request.message)
        except AgentNotConfigured as exc:
            raise HTTPException(503, "No agent configured") from exc
        return RunResponse(response=response)

    @api.get("/prompt")
    async def prompt(
        profile: str = "interactive",
        session: str = "inspection",
        recall: str | None = None,
        at: datetime | None = None,
        origin: str = "api",
    ) -> dict:
        try:
            result = await asyncio.to_thread(
                read_prompt,
                app.state.config,
                profile=profile,
                session=session,
                origin=origin,
                at=at or now(),
                recall=recall,
            )
            return inspection(result, app.state.registry.schemas(profile))
        except (OSError, ValueError) as exc:
            raise HTTPException(422, "Prompt files or profile are invalid") from exc

    @api.get("/sessions")
    async def sessions() -> list[dict]:
        return app.state.store.list_records("sessions")

    @api.get("/sessions/{session_id}")
    async def session(session_id: str) -> dict:
        try:
            metadata = app.state.store.session(session_id).model_dump(mode="json")
            return {**metadata, "messages": app.state.store.transcript(session_id)}
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, "Session not found") from exc

    @api.get("/runs")
    async def runs() -> list[dict]:
        return app.state.store.list_records("runs")

    @api.get("/runs/{run_id}")
    async def run_record(run_id: str) -> dict:
        try:
            return app.state.store.run(run_id)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, "Run not found") from exc

    app.include_router(api)
    return app


app = create_app()
