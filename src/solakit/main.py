"""Application lifecycle, operator API, and server-rendered operator UI."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StringConstraints
from starlette.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles

from solakit.channels import ApiAdapter, Channels
from solakit.core.compaction import ContextLimit
from solakit.core.config import Config, Job
from solakit.core.loop import Execution
from solakit.core.media import Media, MediaError
from solakit.core.memory import MemoryClient, MemoryUnavailable
from solakit.core.memory_ops import probe, refresh_run, watch_memory
from solakit.core.model import ModelClient, ModelUnavailable, safe_url
from solakit.core.prompt import front_page_status, inspection, read_prompt
from solakit.core.routing import SessionRouter
from solakit.core.schemas import Origin, Session, SessionArchived, now
from solakit.core.search import SearchUnavailable
from solakit.core.secrets import RedactingFilter, Redactor
from solakit.core.session_lifecycle import (
    ArchiveBatch,
    LifecycleRequest,
    SessionLifecycle,
)
from solakit.core.startup import prepare
from solakit.core.store import StorageCorruption, Store, safe_id
from solakit.jobs import Jobs
from solakit.jobs.schemas import JobUpdate
from solakit.ui import build_ui_router
from solakit.ui.auth import COOKIE, LIFETIME, UISessions
from solakit.ui.routes import ROOT as UI_ROOT


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32000)
    ]
    profile: str | None = None


class CreateSessionRequest(Session):
    model_config = ConfigDict(extra="forbid")


class UpdateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


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
            for profile, names in catalog["profiles"].items():
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

    @api.get("/media/{digest}")
    async def image_media(digest: str):
        try:
            payload = await asyncio.to_thread(
                Media(app.state.store.root).read_hash, digest
            )
        except MediaError as exc:
            raise HTTPException(404, "Image unavailable") from exc
        return Response(
            payload,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

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

    @app.get("/health/model")
    async def model_health() -> dict:
        app.state.model_health = await probe_model(app.state.model)
        return app.state.model_health

    @api.get("/channels")
    async def channels() -> list[dict]:
        return app.state.channels.status()

    @api.get("/jobs")
    async def jobs() -> list[dict]:
        return app.state.jobs.status(definitions=True)

    @api.get("/job-options")
    async def job_options() -> dict:
        cfg = app.state.config
        return {
            "timezone": cfg.timezone,
            "profiles": sorted(p for p in cfg.profiles if p != "maintenance"),
            "channels": [c.id for c in cfg.channels],
            "tools": app.state.registry.catalog()["tools"],
        }

    def job_error(exc):
        from solakit.jobs.schemas import JobToolsError

        if isinstance(exc, JobToolsError):
            return HTTPException(422, app.state.redactor.redact(str(exc)))
        if isinstance(exc, KeyError):
            return HTTPException(404, "Job not found")
        if isinstance(exc, (FileExistsError, RuntimeError)):
            return HTTPException(409, str(exc))
        return HTTPException(422, "Invalid job definition or references")

    @api.post("/jobs", status_code=201)
    async def job_create(job: Job) -> dict:
        try:
            return await app.state.jobs.create(job)
        except (ValueError, KeyError, FileExistsError, RuntimeError) as exc:
            raise job_error(exc) from exc

    @api.get("/jobs/{job_id}")
    async def job_get(job_id: str) -> dict:
        try:
            return app.state.jobs.get(job_id)
        except KeyError as exc:
            raise job_error(exc) from exc

    @api.patch("/jobs/{job_id}")
    async def job_update(job_id: str, changes: JobUpdate) -> dict:
        try:
            return await app.state.jobs.update(
                job_id, changes.model_dump(exclude_unset=True)
            )
        except (ValueError, KeyError, RuntimeError) as exc:
            raise job_error(exc) from exc

    @api.delete("/jobs/{job_id}")
    async def job_delete(job_id: str) -> dict:
        try:
            return await app.state.jobs.delete(job_id)
        except (ValueError, KeyError, RuntimeError) as exc:
            raise job_error(exc) from exc

    @api.post("/jobs/{job_id}/run")
    async def job_run(job_id: str, wait: bool = True, admission: bool = False):
        if job_id not in app.state.jobs.states:
            raise HTTPException(404, "Job not found")
        try:
            if admission:
                return JSONResponse(
                    await app.state.jobs.trigger_run(job_id), status_code=202
                )
            if not wait:
                return JSONResponse(
                    await app.state.jobs.trigger(job_id), status_code=202
                )
            run = await app.state.jobs.run_now(job_id)
            return await asyncio.to_thread(app.state.store.run, run.id)
        except KeyError as exc:
            raise HTTPException(404, "Job not found") from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/jobs/{job_id}/{operation}")
    async def job_control(job_id: str, operation: str) -> dict:
        if job_id not in app.state.jobs.states or operation not in {"pause", "resume"}:
            raise HTTPException(404, "Job operation not found")
        try:
            await app.state.jobs.pause(job_id, operation == "pause")
        except KeyError as exc:
            raise HTTPException(404, "Job not found") from exc
        except ValueError as exc:
            raise HTTPException(
                409, "Job unavailable or scheduler shutting down"
            ) from exc
        return next(j for j in app.state.jobs.status() if j["id"] == job_id)

    @api.get("/prompt")
    async def prompt(
        profile: str = "interactive",
        session: str = "inspection",
        recall: str | None = None,
        at: datetime | None = None,
        origin: str = "api",
        run_id: str | None = None,
        job: str | None = None,
        step: int | None = None,
    ) -> dict:
        if run_id is not None:
            try:
                record = await asyncio.to_thread(app.state.store.run, run_id)
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(404, "Run not found") from exc
            if record.get("prompt") is None or record.get("tool_schemas") is None:
                raise HTTPException(
                    409, "Prompt or tool schema snapshot was not recorded for this run"
                )
            schemas = record["tool_schemas"]
            view = record.get("tool_view")
            if step is not None:
                snapshots = (view or {}).get("schema_steps", [])
                snapshot = next((s for s in snapshots if s["step"] == step), None)
                if snapshot is None:
                    raise HTTPException(404, "Schema step not recorded")
                schemas = [record["tool_catalog"][name] for name in snapshot["visible"]]
            return {
                **inspection(record["prompt"], schemas, record.get("prompt_fragments")),
                "tool_view": view,
                "schema_step": step,
                "tool_catalog_sha256": record.get("tool_catalog_sha256"),
                "run_id": run_id,
                "profile": record["profile"],
                "session": record["session"],
                "effective_tools": record["effective_tools"],
                "tool_schema_sha256": record["tool_schema_sha256"],
            }
        try:
            if job is not None:
                try:
                    definition = app.state.jobs.job(job)
                except KeyError as exc:
                    raise HTTPException(404, "Job not found") from exc
                profile = definition.profile
                view = app.state.jobs.tool_view(definition)
            else:
                view = app.state.registry.view(profile)
            result = await asyncio.to_thread(
                read_prompt,
                app.state.config,
                profile=profile,
                session=session,
                origin=origin,
                at=at or now(),
                recall=recall,
                tool_index=view.index_text,
            )
            return {
                **inspection(result, view.schemas()),
                "tool_view": view.metadata(),
                "effective_tools": list(view.effective),
            }
        except (OSError, ValueError) as exc:
            raise HTTPException(422, "Prompt files or profile are invalid") from exc

    @api.get("/storage/health")
    def storage_health() -> dict:
        return app.state.store.storage_health()

    @api.get("/search")
    def search(
        query: str,
        session_id: str | None = None,
        role: str | None = None,
        kind: str | None = None,
        after: str | None = None,
        before: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        try:
            results = app.state.store.search(
                query,
                session_id=session_id,
                role=role,
                kind=kind,
                after=after,
                before=before,
                limit=limit,
            )
            return app.state.redactor.redact(results)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except SearchUnavailable as exc:
            raise HTTPException(503, str(exc)) from None

    @api.get("/tools")
    async def tools() -> dict:
        catalog = app.state.registry.catalog()
        catalog["jobs"] = {
            job.id: app.state.jobs.get(job.id)["tool_view"]
            for job in app.state.jobs.jobs.values()
        }
        return app.state.redactor.redact(catalog)

    @api.get("/profiles")
    async def profiles() -> list[str]:
        return sorted(app.state.config.profiles)

    @api.get("/sessions")
    def sessions(
        status: Literal["active", "archived", "all"] = "active",
        q: str = "",
        before: str | None = None,
        profile: str | None = None,
        exclude_source: str | None = None,
        roots_only: bool = False,
        parent_session: str | None = None,
        limit: int = 50,
        offset: int = 0,
        page: bool = False,
    ):
        try:
            result = app.state.store.list_sessions(
                status=status,
                q=q,
                before=before,
                profile=profile,
                exclude_source=exclude_source,
                roots_only=roots_only,
                parent_session=parent_session,
                limit=limit,
                offset=offset,
            )
            return result if page else result["sessions"]
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/sessions/archive-batch")
    async def archive_sessions(request: ArchiveBatch):
        try:
            return await app.state.session_lifecycle.batch(request)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/sessions/restore-batch")
    async def restore_sessions(request: ArchiveBatch):
        try:
            return await app.state.session_lifecycle.batch(request, archived=False)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/sessions/{session_id}/lifecycle")
    async def session_lifecycle(session_id: str, request: LifecycleRequest):
        try:
            return await app.state.session_lifecycle.change(
                session_id, request.archived
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, "Session not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/sessions", status_code=201)
    def create_session(session: CreateSessionRequest, response: Response) -> dict:
        try:
            safe_id(session.id)
            if session.archived_at is not None:
                raise ValueError(
                    "Use the session lifecycle endpoint to archive a session"
                )
            if session.default_profile not in app.state.config.profiles:
                raise ValueError("Unknown profile")
            if session.memory_scope == "isolated":
                raise ValueError("Isolated memory is not implemented")
            if session.bound_channel is not None:
                raise ValueError("Channel bindings are controlled by configuration")
            store = app.state.store
            with store.lock:
                try:
                    existing = store.session(session.id)
                except FileNotFoundError:
                    return store.create_session(session).model_dump(mode="json")
                exclude = (
                    set()
                    if "created_at" in session.model_fields_set
                    else {"created_at"}
                )
                if existing.model_dump(exclude=exclude) != session.model_dump(
                    exclude=exclude
                ):
                    raise HTTPException(
                        409, "Session ID already exists with different fields"
                    )
                response.status_code = 200
                return existing.model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.patch("/sessions/{session_id}")
    def update_session(session_id: str, request: UpdateSessionRequest) -> dict:
        try:
            return app.state.store.rename_session(session_id, request.title).model_dump(
                mode="json"
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, "Session not found") from exc
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
                "context_started_at": app.state.store.context_started_at(session_id),
            }
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, "Session not found") from exc

    @api.post("/sessions/{session_id}/compact")
    async def compact_session(session_id: str) -> dict:
        try:
            safe_id(session_id)
        except ValueError as exc:
            raise HTTPException(404, "Session not found") from exc
        try:
            return await app.state.execution.compact_session(session_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Session not found") from exc
        except (ValueError, ContextLimit) as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                503, "Compaction storage failure; inspect and restart for recovery"
            ) from exc

    @api.post("/sessions/{session_id}/archive")
    async def archive_session(session_id: str) -> dict:
        try:
            safe_id(session_id)
        except ValueError as exc:
            raise HTTPException(404, "Session not found") from exc
        try:
            return await app.state.execution.archive_session(session_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Session not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                503, "Archival could not finish; check storage and restart for recovery"
            ) from exc

    @api.post("/sessions/{session_id}/messages")
    async def message(
        session_id: str, request: RunRequest, wait: bool = True, stream: bool = False
    ):
        try:
            pending = await app.state.execution.enqueue(
                session_id,
                request.message,
                profile=request.profile,
                origin=Origin(channel="api", channel_id=session_id),
                stream=stream,
            )
            if not wait:
                return JSONResponse(
                    pending.run.model_dump(mode="json"), status_code=202
                )
            run = await asyncio.shield(pending.future)
            return await asyncio.to_thread(app.state.store.run, run.id)
        except SessionArchived as exc:
            raise HTTPException(409, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, "Session not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/runs")
    def runs(
        session_id: str | None = None, include_archived: bool = False
    ) -> list[dict]:
        return app.state.store.list_runs(session_id, include_archived)

    @api.get("/runs/{run_id}")
    async def run_record(
        run_id: str, live: bool = False, view: bool = False, messages: bool = True
    ) -> dict:
        try:
            record = await asyncio.to_thread(
                app.state.store.run, run_id, view=view, include_messages=messages
            )
            if live:
                pending = app.state.execution.pending.get(run_id)
                preview = pending.preview if pending else None
                record["preview"] = (
                    {
                        **preview,
                        "content": app.state.redactor.preview(preview["content"]),
                    }
                    if preview
                    else None
                )
            return record
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
