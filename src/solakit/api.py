"""Versioned HTTP boundary; the lifespan owns one explicitly composed runtime."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from solakit.auth import COOKIE, CSRF_COOKIE, AuthService, Login, RequestError
from solakit.client import ARTIFACT_BYTES, MAX_STREAMS, PAGE_BYTES, ClientService, page
from solakit.config import (
    DEFAULT_CONFIG,
    ConfigError,
    EffectiveProfile,
    load_config,
    resolve_profile,
)
from solakit.evidence import StorageError, canonical
from solakit.records import (
    RUN_TRANSITIONS,
    Job,
    MemoryCommit,
    Occurrence,
    RecordID,
    Run,
    SchedulerControl,
    Session,
    SessionMode,
)
from solakit.run_loop import QueueFull
from solakit.runtime import ApplicationContext, compose
from solakit.search import search

ID = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
Limit = Annotated[int, Query(ge=1, le=100)]
After = Annotated[int, Query(ge=0)]
SEND_TIMEOUT_SECONDS = 30


def failure(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def bounded(result: dict[str, Any]) -> dict[str, Any]:
    if len(canonical(result)) > PAGE_BYTES:
        raise RequestError(
            413, "response_too_large", "Narrow the query or retrieve recorded artifacts"
        )
    return result


def time_cursor(value: str | None) -> tuple[str, str] | None:
    from solakit.store import TIME_ADAPTER, valid_id

    if value is None:
        return None
    at, separator, identity = value.rpartition(",")
    try:
        if not separator:
            raise ValueError
        valid_id(identity)
        instant = TIME_ADAPTER.validate_json(canonical(at))
    except ValueError:
        raise RequestError(422, "invalid_cursor", "Cursor requires UTC time and ID") from None
    return instant.isoformat(), identity


class Boundary:
    """Authenticate before parsing; bound bodies without trusting Content-Length."""

    def __init__(self, app: ASGIApp, auth: AuthService) -> None:
        self.app, self.auth = app, auth
        self.hosts = {urlsplit(origin).netloc for origin in auth.settings.allowed_origins}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        started = False

        async def output(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] += [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ]
            async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                await send(message)

        consumed = 0

        async def bounded_receive() -> Message:
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > 131072:
                raise RequestError(413, "body_too_large", "Request body exceeds 128 KiB")
            return message

        try:
            for name in ("host", "origin", "authorization", "x-csrf-token", "content-length"):
                if len(request.headers.getlist(name)) > 1:
                    raise RequestError(400, "ambiguous_headers", "Duplicate security header")
            if request.headers.get("host", "").lower() not in self.hosts:
                raise RequestError(400, "invalid_host", "Host is not configured")
            if any(
                key.lower() in {"token", "access_token", "password", "csrf_token"}
                for key in request.query_params
            ):
                raise RequestError(
                    400, "credentials_in_url", "Credentials belong in headers or login body"
                )
            origin = request.headers.get("origin")
            if origin is not None and origin not in self.auth.settings.allowed_origins:
                raise RequestError(403, "invalid_origin", "Origin is not allowed")
            cookies = ";".join(request.headers.getlist("cookie"))
            if sum(part.strip().partition("=")[0] == COOKIE for part in cookies.split(";")) > 1:
                raise RequestError(400, "ambiguous_auth", "Duplicate authentication cookie")
            cookie = request.cookies.get(COOKIE)
            authorization = request.headers.get("authorization")
            if cookie is not None and authorization is not None:
                raise RequestError(400, "ambiguous_auth", "Use cookie or bearer authentication")
            public = request.url.path == "/health"
            login_route = request.url.path == "/api/v1/auth/login"
            mutation = request.method not in ("GET", "HEAD", "OPTIONS")
            if login_route:
                if origin is None:
                    raise RequestError(403, "origin_required", "Login requires an allowed Origin")
            elif not public:
                login = self.auth.authenticate(cookie, authorization)
                scope.setdefault("state", {})["login"] = login
                if mutation and login.cookie:
                    if origin is None:
                        raise RequestError(
                            403, "origin_required", "Cookie mutations require an allowed Origin"
                        )
                    self.auth.csrf(login, request.headers.get("x-csrf-token"))
            length = request.headers.get("content-length")
            if length is not None and (not length.isdecimal() or len(length) > 10):
                raise RequestError(400, "invalid_length", "Invalid Content-Length")
            if length is not None and int(length) > 131072:
                raise RequestError(413, "body_too_large", "Request body exceeds 128 KiB")
            await self.app(scope, bounded_receive, output)
        except Exception as error:
            if started:
                raise
            if isinstance(error, RequestError):
                response = failure(error.status, error.code, error.message)
            else:
                response = failure(
                    503, "service_unavailable", "Service unavailable; inspect local state"
                )
            await response(scope, receive, output)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class LoginInput(Input):
    password: str = Field(
        min_length=1, max_length=4096, repr=False, json_schema_extra={"writeOnly": True}
    )
    transport: Literal["cookie", "bearer"] = "cookie"


class SessionInput(Input):
    mode: SessionMode = SessionMode.SHARED
    title: str = Field(default="", max_length=256)


class SessionPatch(Input):
    title: str | None = Field(default=None, max_length=256)
    archived: StrictBool | None = None
    pinned: StrictBool | None = None


class Submission(Input):
    session_id: RecordID
    profile: str | None = Field(default=None, min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=32768)


class JobPatch(Input):
    profile: str | None = None
    task: str | None = Field(default=None, max_length=32768)
    schedule: str | None = Field(default=None, max_length=256)
    timezone: str | None = Field(default=None, max_length=128)
    session_id: RecordID | None = None
    mode: SessionMode | None = None
    enabled: StrictBool | None = None
    paused: StrictBool | None = None
    tools: tuple[str, ...] | None = None
    workspace_roots: tuple[str, ...] | None = None
    budgets: dict[str, StrictInt] | None = None
    delivery: Literal["session", "none"] | None = None


class JobInput(Input):
    profile: str | None = None
    task: str = Field(min_length=1, max_length=32768)
    schedule: str = Field(min_length=1, max_length=256)
    timezone: str = "America/Denver"
    session_id: RecordID | None = None
    mode: SessionMode = SessionMode.SHARED
    origin: Literal["scheduled", "proactive"] = "scheduled"
    enabled: StrictBool = False
    tools: tuple[str, ...] | None = None
    workspace_roots: tuple[str, ...] | None = None
    budgets: dict[str, StrictInt] = Field(default_factory=dict)
    delivery: Literal["session", "none"] = "session"


class PauseInput(Input):
    paused: StrictBool


class ReconcileInput(Input):
    provider_task_id: str = Field(min_length=1, max_length=256)


class PromptInput(Input):
    profile: str | None = None
    content: str = Field(min_length=1, max_length=32768)
    hashes: StrictBool = False


class EventResponse(StreamingResponse):
    def __init__(self, service: ClientService, content: AsyncGenerator[bytes]) -> None:
        super().__init__(
            content, media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )
        self.service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.service.streams >= MAX_STREAMS:
            raise RequestError(429, "streams_full", "Event connection limit reached")
        self.service.streams += 1
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.service.streams -= 1

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            await cast(AsyncGenerator[bytes], self.body_iterator).aclose()


def idempotency(request: Request) -> str:
    from solakit.store import valid_id

    values = request.headers.getlist("idempotency-key")
    if len(values) != 1:
        raise RequestError(422, "idempotency_required", "One Idempotency-Key header is required")
    try:
        return valid_id(values[0])
    except ValueError:
        raise RequestError(
            422, "invalid_idempotency_key", "Idempotency-Key must be 32 lowercase hex characters"
        ) from None


def create_app(
    context: ApplicationContext | None = None, *, profile_names: tuple[str, ...] = ()
) -> FastAPI:
    if context is None:
        config = load_config()

        def current_profile(admitted: EffectiveProfile) -> EffectiveProfile:
            return resolve_profile(
                load_config(),
                admitted.name,
                base=DEFAULT_CONFIG.parent,
                origin=admitted.origin,
                mode=admitted.mode,
            )

        context = compose(
            resolve_profile(config, base=DEFAULT_CONFIG.parent), current_profile=current_profile
        )
        profile_names = tuple(config.profiles)
    assert context.store and context.runs and context.tools and context.memory and context.jobs
    store, runs, memory, jobs = context.store, context.runs, context.memory, context.jobs
    service = ClientService(context, profile_names)
    auth = AuthService(store, context.profile.http)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await context.start()
        try:
            yield
        finally:
            await context.stop()

    app = FastAPI(title="Sola", version="1", lifespan=lifespan)
    app.state.context, app.state.service, app.state.auth = context, service, auth
    app.add_middleware(Boundary, auth=auth)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return failure(error.status_code, "http_error", "HTTP resource or method unavailable")

    @app.exception_handler(RequestError)
    async def request_error(request: Request, error: RequestError) -> JSONResponse:
        return failure(error.status, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return failure(422, "invalid_input", "Request fields are invalid")

    @app.exception_handler(KeyError)
    async def missing(request: Request, error: KeyError) -> JSONResponse:
        return failure(404, "not_found", "Resource does not exist")

    @app.exception_handler(ValueError)
    async def conflict(request: Request, error: ValueError) -> JSONResponse:
        return failure(409, "conflict", "Request conflicts with current state or authority")

    @app.exception_handler(ConfigError)
    async def configuration(request: Request, error: ConfigError) -> JSONResponse:
        return failure(409, "configuration", "Current profile is unavailable")

    @app.exception_handler(QueueFull)
    async def full(request: Request, error: QueueFull) -> JSONResponse:
        return failure(429, "queue_full", "Pending run limit reached")

    @app.exception_handler(StorageError)
    async def storage(request: Request, error: StorageError) -> JSONResponse:
        return failure(503, "storage_unavailable", "Storage requires operator inspection")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": context.state, "phase": "run-loop"}

    @app.get("/api/v1/health")
    async def status() -> dict[str, Any]:
        return {
            "status": context.state,
            "accepting": runs.accepting,
            "memory": memory.status(),
            "scheduler_failed": jobs.failure is not None,
        }

    @app.post("/api/v1/auth/login")
    async def login(body: LoginInput) -> Response:
        token, csrf, expires = auth.login(body.password)
        result: dict[str, Any] = {
            "expires_at": datetime.fromtimestamp(expires, UTC).isoformat(),
            "transport": body.transport,
        }
        if body.transport == "bearer":
            result["token"] = token
        else:
            result["csrf_token"] = csrf
        response = JSONResponse(result)
        if body.transport == "cookie":
            for name, value in ((COOKIE, token), (CSRF_COOKIE, csrf)):
                response.set_cookie(
                    name,
                    value,
                    max_age=auth.settings.session_seconds,
                    secure=auth.settings.secure_cookie,
                    httponly=name == COOKIE,
                    samesite="strict",
                    path="/",
                )
        return response

    @app.get("/api/v1/auth/session")
    async def login_session(request: Request) -> dict[str, Any]:
        current: Login = request.state.login
        return {
            "expires_at": datetime.fromtimestamp(current.expires_at, UTC).isoformat(),
            "transport": "cookie" if current.cookie else "bearer",
        }

    @app.post("/api/v1/auth/logout")
    async def logout(request: Request) -> Response:
        auth.logout(request.state.login)
        response = Response(status_code=204)
        for name in (COOKIE, CSRF_COOKIE):
            response.delete_cookie(name, secure=auth.settings.secure_cookie, samesite="strict")
        return response

    @app.delete("/api/v1/auth/sessions")
    async def revoke(request: Request) -> Response:
        auth.logout(request.state.login, all_sessions=True)
        return Response(status_code=204)

    @app.post("/api/v1/sessions", status_code=201)
    async def create_session(body: SessionInput) -> Session:
        return store.create_session(mode=body.mode, title=body.title)

    @app.get("/api/v1/sessions")
    async def sessions(
        limit: Limit = 50,
        after: str | None = None,
        archived: bool = False,
        include_ephemeral: bool = False,
    ) -> dict[str, Any]:
        cursor = time_cursor(after)
        items = store.list_sessions(
            after=cursor, limit=limit, archived=archived, include_ephemeral=include_ephemeral
        )
        return bounded(
            {
                "items": [item.model_dump(mode="json") for item in items],
                "next": f"{items[-1].created_at.isoformat()},{items[-1].id}" if items else after,
            }
        )

    @app.get("/api/v1/sessions/{session_id}")
    async def session(session_id: ID) -> Session:
        return store.session(session_id)

    @app.patch("/api/v1/sessions/{session_id}")
    async def update_session(session_id: ID, body: SessionPatch) -> Session:
        return store.update_session(session_id, **body.model_dump(exclude_unset=True))

    @app.get("/api/v1/sessions/{session_id}/messages")
    async def messages(session_id: ID, after: After = 0, limit: Limit = 50) -> dict[str, Any]:
        return page(list(store.messages(session_id, after=after, limit=limit)), after)

    @app.get("/api/v1/sessions/{session_id}/runs")
    async def session_runs(session_id: ID, after: After = 0, limit: Limit = 50) -> dict[str, Any]:
        return page(list(store.runs(session_id, after=after, limit=limit)), after)

    @app.post("/api/v1/runs")
    async def submit(
        body: Submission, request: Request, wait: Annotated[float, Query(ge=0, le=30)] = 0
    ) -> Response:
        run = await service.submit(
            body.session_id, body.profile, body.content, idempotency(request), wait
        )
        return JSONResponse(
            {"run": run.model_dump(mode="json")},
            status_code=202 if run.state in RUN_TRANSITIONS else 200,
            headers={"Location": f"/api/v1/runs/{run.id}"},
        )

    @app.get("/api/v1/runs/{run_id}")
    async def run(run_id: ID, wait: Annotated[float, Query(ge=0, le=30)] = 0) -> Run:
        if wait:
            try:
                return await runs.wait(run_id, timeout=wait)
            except TimeoutError:
                pass
        return store.run(run_id)

    @app.post("/api/v1/runs/{run_id}/cancel")
    async def cancel(run_id: ID) -> Run:
        return runs.cancel(run_id)

    @app.get("/api/v1/runs/{run_id}/records")
    async def records(run_id: ID, after: After = 0, limit: Limit = 50) -> dict[str, Any]:
        return page(store.run_records(run_id, after=after, limit=limit), after)

    @app.get("/api/v1/runs/{run_id}/prompts")
    async def prompts(run_id: ID, after: After = 0, limit: Limit = 50) -> dict[str, Any]:
        return service.prompts(run_id, after, limit)

    @app.get("/api/v1/runs/{run_id}/artifacts/{artifact_id}")
    async def artifact(
        run_id: ID,
        artifact_id: ID,
        offset: After = 0,
        limit: Annotated[int, Query(ge=1, le=ARTIFACT_BYTES)] = 65536,
    ) -> Response:
        raw, reference = service.artifact(run_id, artifact_id, offset, limit)
        return Response(
            raw,
            media_type="application/octet-stream",
            headers={
                "X-Artifact-Length": str(reference.length),
                "X-Artifact-SHA256": reference.sha256,
                "X-Next-Offset": str(offset + len(raw)),
                "Content-Disposition": "attachment",
            },
        )

    @app.get("/api/v1/sessions/{session_id}/events")
    async def events(session_id: ID, request: Request, cursor: str | None = None) -> Response:
        header = request.headers.getlist("last-event-id")
        if len(header) > 1 or (header and cursor is not None and header[0] != cursor):
            raise RequestError(422, "invalid_cursor", "Event cursors disagree")
        after = service.event_cursor(session_id, header[0] if header else cursor)
        return EventResponse(
            service,
            service.events(session_id, after, auth, request.state.login),
        )

    @app.get("/api/v1/jobs")
    async def list_jobs(after: str | None = None, limit: Limit = 50) -> dict[str, Any]:
        return service.job_page(time_cursor(after), limit)

    @app.post("/api/v1/jobs", status_code=201)
    async def create_job(body: JobInput) -> Job:
        definition = body.model_dump(mode="json") | {
            "profile": body.profile if body.profile is not None else context.profile.name
        }
        return jobs.create(Job.model_validate_json(canonical(definition)))

    @app.get("/api/v1/jobs/control")
    async def control() -> SchedulerControl:
        return jobs.control()

    @app.put("/api/v1/jobs/control")
    async def pause_all(body: PauseInput) -> SchedulerControl:
        return jobs.global_pause(body.paused)

    @app.get("/api/v1/jobs/{job_id}")
    async def job(job_id: ID) -> Job:
        return store.job(job_id)

    @app.get("/api/v1/jobs/{job_id}/occurrences")
    async def occurrences(
        job_id: ID, after: str | None = None, limit: Limit = 50
    ) -> dict[str, Any]:
        return service.job_page(time_cursor(after), limit, job_id)

    @app.patch("/api/v1/jobs/{job_id}")
    async def update_job(job_id: ID, body: JobPatch) -> Job:
        return jobs.update(job_id, **body.model_dump(exclude_unset=True))

    @app.delete("/api/v1/jobs/{job_id}")
    async def delete_job(job_id: ID) -> Job:
        return jobs.delete(job_id)

    @app.post("/api/v1/jobs/{job_id}/run")
    async def manual(job_id: ID, request: Request) -> Occurrence:
        return jobs.manual(job_id, request_id=idempotency(request))

    @app.get("/api/v1/memory")
    async def memory_status() -> dict[str, Any]:
        return memory.status()

    @app.post("/api/v1/memory/{commit_id}/reconcile")
    async def reconcile(commit_id: ID, body: ReconcileInput) -> MemoryCommit:
        return await memory.reconcile(commit_id, body.provider_task_id)

    @app.get("/api/v1/tools")
    async def tools(profile: str | None = None) -> dict[str, Any]:
        selected = service.profile(profile, SessionMode.SHARED)
        return bounded({"items": service.tools.registry.visible(selected.tools)})

    @app.get("/api/v1/profiles")
    async def profiles() -> dict[str, Any]:
        return bounded(
            {
                "items": [
                    {"name": name, **service.profile(name, SessionMode.SHARED).explain()}
                    for name in service.profiles
                ]
            }
        )

    @app.get("/api/v1/search")
    async def find(
        q: Annotated[str, Query(min_length=1, max_length=4096)],
        limit: Limit = 20,
        session_id: RecordID | None = None,
    ) -> dict[str, Any]:
        return bounded({"items": search(store.root, q, limit=limit, session_id=session_id)})

    @app.post("/api/v1/prompt")
    async def prompt(body: PromptInput) -> dict[str, Any]:
        return service.prompt(body.profile, body.content, body.hashes)

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema["components"]["securitySchemes"] = {
            "OperatorCookie": {"type": "apiKey", "in": "cookie", "name": COOKIE},
            "OperatorBearer": {"type": "http", "scheme": "bearer"},
        }
        schema["components"]["schemas"]["Error"] = {
            "type": "object",
            "required": ["error"],
            "properties": {
                "error": {
                    "type": "object",
                    "required": ["code", "message"],
                    "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
                }
            },
        }
        for path, operations in schema["paths"].items():
            for method, operation in operations.items():
                operation["security"] = (
                    []
                    if path in ("/health", "/api/v1/auth/login")
                    else [{"OperatorCookie": []}, {"OperatorBearer": []}]
                )
                if method in ("post", "put", "patch", "delete"):
                    operation.setdefault("parameters", []).extend(
                        [
                            {
                                "in": "header",
                                "name": "Origin",
                                "required": path == "/api/v1/auth/login",
                                "description": "Allowed origin; required for login/cookie mutation",
                                "schema": {"type": "string"},
                            },
                            {
                                "in": "header",
                                "name": "X-CSRF-Token",
                                "required": False,
                                "description": "Required for cookie mutation; bearer-only exempt",
                                "schema": {"type": "string"},
                            },
                        ]
                    )
                for code in (400, 401, 403, 404, 409, 413, 416, 422, 429, 503):
                    operation["responses"][str(code)] = {
                        "description": "Structured error; see docs/contracts/api.md",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}
                        },
                    }
        for path in ("/api/v1/runs", "/api/v1/jobs/{job_id}/run"):
            schema["paths"][path]["post"].setdefault("parameters", []).append(
                {
                    "in": "header",
                    "name": "Idempotency-Key",
                    "required": True,
                    "schema": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                }
            )
        for response_code, description in (
            ("200", "Terminal receipt"),
            ("202", "Durably admitted, still pending"),
        ):
            schema["paths"]["/api/v1/runs"]["post"]["responses"][response_code] = {
                "description": description,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["run"],
                            "properties": {"run": {"$ref": "#/components/schemas/Run"}},
                        }
                    }
                },
            }
        schema["paths"]["/api/v1/sessions/{session_id}/events"]["get"].setdefault(
            "parameters", []
        ).append(
            {
                "in": "header",
                "name": "Last-Event-ID",
                "required": False,
                "description": "session_id:sequence; must agree with cursor if both are provided",
                "schema": {"type": "string"},
            }
        )
        schema["paths"]["/api/v1/sessions/{session_id}/events"]["get"]["responses"]["200"] = {
            "description": "Snapshot, replay, previews; see docs/contracts/events.md",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
    return app
