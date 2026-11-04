"""Application lifecycle and operator API; root is reserved for the UI."""

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, StringConstraints

from solakit.core.config import Config, load_config
from solakit.core.loop import Execution
from solakit.core.model import ModelClient, ModelUnavailable
from solakit.core.prompt import inspection, read_prompt
from solakit.core.routing import SessionRouter
from solakit.core.schemas import Origin, Session, now
from solakit.core.secrets import RedactingFilter, Redactor
from solakit.core.store import Store, safe_id
from solakit.tools import Registry


class RunRequest(BaseModel):
    message: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32000)
    ]
    profile: str | None = None


async def probe_model(model: ModelClient) -> dict[str, str]:
    try:
        await model.preflight()
        return {"status": "ready", "model_id": model.config.model_id}
    except ModelUnavailable:
        return {"status": "unavailable", "reason": "pinned model unavailable"}


def create_app(
    config: Config | None = None, model: ModelClient | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = config if config is not None else load_config()
        token = cfg.operator_token.resolve()
        app.state.config = cfg
        app.state.operator_token = token
        app.state.redactor = Redactor([token])
        app.state.registry = Registry(cfg)
        app.state.store = await asyncio.to_thread(
            Store, cfg.paths.data, app.state.redactor, exclusive=True
        )
        app.state.router = SessionRouter(cfg, app.state.store)
        app.state.model = model if model is not None else ModelClient(cfg.model)
        app.state.execution = Execution(
            cfg, app.state.store, app.state.registry, app.state.model
        )
        log_filter = RedactingFilter(app.state.redactor)
        handlers = logging.getLogger().handlers
        for handler in handlers:
            handler.addFilter(log_filter)
        try:
            app.state.model_health = await probe_model(app.state.model)
            yield
        finally:
            await app.state.execution.close()
            app.state.store.close()
            for handler in handlers:
                handler.removeFilter(log_filter)

    app = FastAPI(title="Solakit", version="0.1.0", lifespan=lifespan)

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
        status = app.state.model_health["status"]
        return {"status": "ok" if status == "ready" else "degraded", "model": status}

    @app.get("/health/model")
    async def model_health() -> dict:
        app.state.model_health = await probe_model(app.state.model)
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
    def sessions() -> list[dict]:
        return app.state.store.list_records("sessions")

    @api.post("/sessions", status_code=201)
    def create_session(session: Session) -> dict:
        try:
            safe_id(session.id)
            if session.default_profile not in app.state.config.profiles:
                raise ValueError("Unknown profile")
            if session.memory_scope == "isolated":
                raise ValueError("Isolated memory is not implemented")
            if session.bound_channel is not None:
                raise ValueError("Channel bindings are controlled by configuration")
            return app.state.store.create_session(session).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/sessions/{session_id}")
    def session(session_id: str) -> dict:
        try:
            metadata = app.state.store.session(session_id).model_dump(mode="json")
            return {**metadata, "messages": app.state.store.transcript(session_id)}
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, "Session not found") from exc

    @api.post("/sessions/{session_id}/messages")
    async def message(session_id: str, request: RunRequest) -> dict:
        try:
            run = await app.state.execution.submit(
                session_id,
                request.message,
                profile=request.profile,
                origin=Origin(channel="api", channel_id=session_id),
            )
            return await asyncio.to_thread(app.state.store.run, run.id)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Session not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/runs")
    def runs() -> list[dict]:
        return app.state.store.list_records("runs")

    @api.get("/runs/{run_id}")
    def run_record(run_id: str) -> dict:
        try:
            return app.state.store.run(run_id)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, "Run not found") from exc

    @api.post("/runs/{run_id}/cancel")
    async def cancel(run_id: str) -> dict:
        try:
            await asyncio.to_thread(app.state.store.run, run_id)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, "Run not found") from exc
        await app.state.execution.cancel(run_id)
        return await asyncio.to_thread(app.state.store.run, run_id)

    app.include_router(api)
    return app


app = create_app()
