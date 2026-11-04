"""Application lifecycle and operator API; root is reserved for the UI."""

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, StringConstraints
from starlette.responses import JSONResponse

from solakit.channels import ApiAdapter, Channels
from solakit.core.config import Config
from solakit.core.loop import Execution
from solakit.core.memory import MemoryClient, MemoryUnavailable
from solakit.core.memory_ops import probe, refresh_run, watch_memory
from solakit.core.model import ModelClient, ModelUnavailable
from solakit.core.prompt import inspection, read_prompt
from solakit.core.routing import SessionRouter
from solakit.core.schemas import Origin, Session, now
from solakit.core.secrets import RedactingFilter, Redactor
from solakit.core.startup import prepare
from solakit.core.store import StorageCorruption, Store, safe_id
from solakit.jobs import Jobs


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32000)
    ]
    profile: str | None = None


class CreateSessionRequest(Session):
    model_config = ConfigDict(extra="forbid")


class MemoryProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]
    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ]
    expected: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ]


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
        prepared = await asyncio.to_thread(prepare, config)
        cfg, token, memory_key = (
            prepared.config,
            prepared.operator_token,
            prepared.memory_key,
        )
        app.state.config = cfg
        app.state.operator_token = token
        credentials = [token, memory_key, prepared.discord_token, prepared.model_key]
        app.state.redactor = Redactor([value for value in credentials if value])
        app.state.registry = prepared.registry
        app.state.store = await asyncio.to_thread(
            Store, cfg.paths.data, app.state.redactor, exclusive=True
        )
        app.state.memory = (
            MemoryClient(cfg.memory, memory_key) if cfg.memory.enabled else None
        )
        app.state.router = SessionRouter(cfg, app.state.store)
        app.state.model = (
            model
            if model is not None
            else ModelClient(cfg.model, api_key=prepared.model_key)
        )
        app.state.execution = Execution(
            cfg, app.state.store, app.state.registry, app.state.model, app.state.memory
        )
        adapters = {"api": ApiAdapter()}
        if prepared.discord_token:
            from solakit.channels.discord import DiscordAdapter

            adapters["discord"] = DiscordAdapter(cfg, prepared.discord_token)
        app.state.channels = Channels(cfg, app.state.execution, adapters)
        app.state.jobs = Jobs(cfg, app.state.execution, app.state.channels)
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
        runtime_logger = logging.getLogger("solakit")
        previous_level = runtime_logger.level
        runtime_logger.setLevel(cfg.server.log_level.upper())
        log_filter = RedactingFilter(app.state.redactor)
        handlers = list(
            dict.fromkeys(
                handler
                for name in (
                    "",
                    "solakit",
                    "uvicorn",
                    "uvicorn.error",
                    "uvicorn.access",
                )
                for handler in logging.getLogger(name).handlers
            )
        )
        for handler in handlers:
            handler.addFilter(log_filter)
        memory_watch = None
        try:
            app.state.model_health = await probe_model(app.state.model)
            await app.state.channels.start()
            await app.state.jobs.start()
            if app.state.memory:
                memory_watch = asyncio.create_task(watch_memory(app.state.execution))
            yield
        finally:
            if memory_watch:
                memory_watch.cancel()
                await asyncio.gather(memory_watch, return_exceptions=True)
            try:
                await app.state.jobs.stop_admission()
                try:
                    await app.state.execution.close()
                finally:
                    try:
                        await app.state.jobs.drain()
                    finally:
                        await app.state.channels.stop()
            finally:
                app.state.store.close()
                for handler in handlers:
                    handler.removeFilter(log_filter)
                runtime_logger.setLevel(previous_level)

    app = FastAPI(title="Solakit", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(StorageCorruption)
    async def storage_corruption(request, exc: StorageCorruption):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

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
        status = "ready" if app.state.model.available else "unavailable"
        storage = await asyncio.to_thread(app.state.store.storage_health)
        return {
            "status": "ok"
            if status == "ready" and storage["status"] == "ok"
            else "degraded",
            "model": status,
            "storage": storage["status"],
            "memory": "configured" if app.state.memory else "disabled",
        }

    @app.get("/health/model")
    async def model_health() -> dict:
        app.state.model_health = await probe_model(app.state.model)
        return app.state.model_health

    @api.get("/channels")
    async def channels() -> list[dict]:
        return app.state.channels.status()

    @api.get("/jobs")
    async def jobs() -> list[dict]:
        return app.state.jobs.status()

    @api.post("/jobs/{job_id}/run")
    async def job_run(job_id: str) -> dict:
        if job_id not in app.state.jobs.states:
            raise HTTPException(404, "Job not found")
        try:
            run = await app.state.jobs.run_now(job_id)
            return await asyncio.to_thread(app.state.store.run, run.id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/jobs/{job_id}/{operation}")
    async def job_control(job_id: str, operation: str) -> dict:
        if job_id not in app.state.jobs.states or operation not in {"pause", "resume"}:
            raise HTTPException(404, "Job operation not found")
        await app.state.jobs.pause(job_id, operation == "pause")
        return next(j for j in app.state.jobs.status() if j["id"] == job_id)

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

    @api.get("/storage/health")
    def storage_health() -> dict:
        return app.state.store.storage_health()

    @api.get("/sessions")
    def sessions() -> list[dict]:
        return app.state.store.list_records("sessions")

    @api.post("/sessions", status_code=201)
    def create_session(session: CreateSessionRequest) -> dict:
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
            return {
                **metadata,
                "messages": app.state.store.transcript(session_id),
                "archives": app.state.store.archives(session_id),
            }
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

    @api.get("/sessions/{session_id}/archives/{number}")
    def archive(session_id: str, number: int) -> list[dict]:
        if number < 1:
            raise HTTPException(404, "Archive not found")
        name = f"{session_id}.jsonl.{number}"
        try:
            if name not in app.state.store.archives(session_id):
                raise FileNotFoundError(name)
            return app.state.store._read(app.state.store.root / "sessions" / name)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, "Archive not found") from exc

    @api.post("/runs/{run_id}/memory/refresh")
    async def refresh_memory(run_id: str) -> dict:
        try:
            run = await refresh_run(app.state.execution, run_id)
            return run.model_dump(mode="json")
        except FileNotFoundError as exc:
            raise HTTPException(404, "Run not found") from exc
        except (ValueError, MemoryUnavailable) as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/memory/probe")
    async def memory_probe(request: MemoryProbeRequest) -> dict:
        try:
            return await probe(
                app.state.execution, request.fact, request.query, request.expected
            )
        except (ValueError, MemoryUnavailable) as exc:
            raise HTTPException(409, str(exc)) from exc

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
