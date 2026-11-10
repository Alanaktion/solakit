"""Application lifecycle, operator API, and server-rendered operator UI."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from solakit.channels import ApiAdapter, Channels
from solakit.core.config import Config
from solakit.core.loop import Execution
from solakit.core.memory import MemoryClient, MemoryUnavailable
from solakit.core.memory_ops import watch_memory
from solakit.core.model import ModelClient, ModelUnavailable, safe_url
from solakit.core.prompt import front_page_status
from solakit.core.routing import SessionRouter
from solakit.core.schemas import now
from solakit.core.secrets import RedactingFilter, Redactor
from solakit.core.session_lifecycle import (
    SessionLifecycle,
)
from solakit.core.startup import prepare
from solakit.core.store import StorageCorruption, Store
from solakit.jobs import Jobs
from solakit.ui import build_ui_router
from solakit.ui.auth import COOKIE, LIFETIME, UISessions
from solakit.ui.context import internal_client
from solakit.ui.routes import ROOT as UI_ROOT


async def probe_model(model: ModelClient) -> dict[str, str]:
    try:
        await model.preflight()
        return {"status": "ready", "model_id": model.config.model_id}
    except ModelUnavailable:
        return {"status": "unavailable", "reason": "pinned model unavailable"}


def create_app(  # noqa: C901
    config: Config | None = None, model: ModelClient | None = None
) -> FastAPI:
    ui_sessions = UISessions()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ui_sessions.clear()
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
        ui_sessions.load(app.state.store.root / "ui-sessions.json", token)
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
        app.state.registry.job_manager = app.state.jobs
        app.state.registry.channel_poster = app.state.channels.post
        app.state.session_lifecycle = SessionLifecycle(
            cfg, app.state.execution, app.state.jobs
        )

        def runtime_status(profile):
            return app.state.redactor.redact(
                {
                    "model": {
                        "id": cfg.model.model_id,
                        "available": app.state.model.available,
                    },
                    "channels": app.state.channels.status(),
                    "jobs": app.state.jobs.status(),
                    "front_page": front_page_status(cfg),
                    "profile": profile,
                    "tool_distributions": app.state.registry.distributions,
                    "granted_tools": [
                        schema["function"]["name"]
                        for schema in app.state.registry.schemas(profile)
                    ],
                }
            )

        app.state.registry.runtime_status = runtime_status
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
        app.state.ui_client = internal_client(app)
        memory_watch = None
        try:
            runtime_logger.info("Starting Solakit: checking model and memory backends")
            app.state.model_health = await probe_model(app.state.model)
            runtime_logger.info(
                "Model: %s; endpoint=%s; pinned=%s; max_tokens=%d",
                app.state.model_health["status"],
                safe_url(cfg.model.base_url),
                cfg.model.model_id,
                cfg.model.max_tokens,
            )
            app.state.memory_health = {
                "status": "disabled",
                "checked_at": None,
            }
            if app.state.memory:
                try:
                    await app.state.memory.preflight()
                    memory_status = "ready"
                except MemoryUnavailable:
                    memory_status = "unavailable"
                app.state.memory_health = {
                    "status": memory_status,
                    "checked_at": now().isoformat(),
                }
                log = (
                    runtime_logger.info
                    if memory_status == "ready"
                    else runtime_logger.warning
                )
                log(
                    "Memory: %s; endpoint=%s; roots=%s; authenticated=%s; "
                    "read-only startup check (not extraction/write verification)",
                    memory_status,
                    safe_url(cfg.memory.base_url),
                    ", ".join(cfg.memory.roots),
                    bool(memory_key),
                )
                if memory_status == "unavailable":
                    runtime_logger.warning(
                        "Check memory endpoint, credentials, user "
                        "and root permissions; "
                        "continuing with memory unavailable at startup"
                    )
            else:
                runtime_logger.info("Memory: disabled")
            catalog = app.state.registry.catalog()
            runtime_logger.info(
                "Tools: modules=%s; loaded=%s",
                ", ".join(catalog["modules"]) or "none",
                ", ".join(tool["name"] for tool in catalog["tools"]) or "none",
            )
            for provider in catalog["distributions"]:
                runtime_logger.info(
                    "Tool module %s: distribution=%s; version=%s",
                    provider["module"],
                    provider["distribution"] or "unpackaged",
                    provider["version"] or "unknown",
                )
            for profile, _names in catalog["profiles"].items():
                runtime_logger.info(
                    "Profile %s: %s", profile, catalog["summaries"][profile]
                )
            await app.state.channels.start()
            await app.state.jobs.start()
            runtime_logger.info(
                "Storage: %s; configured channels=%d; managed jobs=%d",
                app.state.store.storage_health()["status"],
                len(cfg.channels),
                len(app.state.jobs.jobs),
            )
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
                await app.state.ui_client.aclose()
                app.state.store.close()
                for handler in handlers:
                    handler.removeFilter(log_filter)
                runtime_logger.setLevel(previous_level)
                ui_sessions.clear()

    app = FastAPI(title="Solakit", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def refresh_ui_cookie(request: Request, call_next):
        identity = request.cookies.get(COOKIE)
        valid = ui_sessions.valid(identity)
        if (
            valid
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and not ui_sessions.authorized(
                request.headers.get("authorization"), None, app.state.operator_token
            )
            and (
                request.headers.get("sec-fetch-site", "same-origin") != "same-origin"
                or (
                    "origin" in request.headers
                    and request.headers["origin"]
                    != f"{request.url.scheme}://{request.url.netloc}"
                )
            )
        ):
            return JSONResponse(
                status_code=403, content={"detail": "Origin not allowed"}
            )
        response = await call_next(request)
        if valid and request.url.path not in {"/login", "/logout"}:
            response.set_cookie(
                COOKIE,
                identity,
                max_age=LIFETIME,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
                path="/",
            )
        return response

    @app.exception_handler(StorageCorruption)
    async def storage_corruption(request, exc: StorageCorruption):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    async def authorize(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> None:
        if not ui_sessions.authorized(
            authorization, request.cookies.get(COOKIE), app.state.operator_token
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
            if status == "ready"
            and storage["status"] == "ok"
            and app.state.memory_health["status"] in {"ready", "disabled"}
            else "degraded",
            "model": status,
            "storage": storage["status"],
            "memory": app.state.memory_health["status"],
            "memory_checked_at": app.state.memory_health["checked_at"],
        }

    @app.get("/health/model", dependencies=[Depends(authorize)])
    async def model_health() -> dict:
        app.state.model_health = await probe_model(app.state.model)
        return app.state.model_health

    from solakit.api import inspection, jobs, runs, sessions

    for resource in (inspection, jobs, sessions, runs):
        api.include_router(resource.router)
    app.include_router(api)
    app.state.ui_sessions = ui_sessions
    app.include_router(
        build_ui_router(
            app,
            ui_sessions,
            lambda: app.state.operator_token,
            lambda value: app.state.redactor.redact(value),
        )
    )
    app.mount("/static", StaticFiles(directory=str(UI_ROOT / "static")), name="static")
    return app


app = create_app()
