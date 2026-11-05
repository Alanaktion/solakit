"""Server-rendered UI P1. Every application-data operation uses existing API routes."""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

from solakit.ui.auth import COOKIE, LIFETIME, UISessions
from solakit.ui.markdown import markdown
from solakit.ui.view import timestamp, turns

ROOT = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))
TEMPLATES.env.filters["markdown"] = markdown
HEADERS = {
    "Cache-Control": "no-store",
    "Vary": "Cookie",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    ),
}


class APIError(Exception):
    def __init__(self, status: int, detail):
        self.status = status
        if isinstance(detail, list):
            self.detail = "\n".join(
                f"{'.'.join(map(str, e.get('loc', [])))}: "
                f"{e.get('msg', 'Invalid input')}"
                for e in detail
            )
        else:
            self.detail = str(detail)


def job_local_time(job):
    return (
        datetime.fromisoformat(job["run_at"])
        .astimezone(ZoneInfo(job["timezone"]))
        .replace(tzinfo=None)
        .isoformat()
    )


def same_job_local_time(job, value):
    try:
        return datetime.fromisoformat(job_local_time(job)) == datetime.fromisoformat(
            value
        )
    except ValueError:
        return False


def build_ui_router(app, sessions: UISessions, operator_token, redact) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    def logged_in(request):
        return sessions.authorized(
            request.headers.get("authorization"),
            request.cookies.get(COOKIE),
            operator_token(),
        )

    def login_redirect(request):
        if request.headers.get("HX-Request") == "true":
            return Response(headers={**HEADERS, "HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303, headers=HEADERS)

    def render(request, template, context=None, status=200, headers=None):
        # API transcripts are deliberately verbatim. UI presentation additionally
        # masks resolved credentials, without changing API/history/prompt bytes.
        return TEMPLATES.TemplateResponse(
            request=request,
            name=template,
            context=redact(context or {}),
            status_code=status,
            headers=HEADERS | (headers or {}),
        )

    async def form(request, limit=512000, max_fields=8):
        if (
            request.headers.get("content-type", "").split(";", 1)[0]
            != "application/x-www-form-urlencoded"
        ):
            raise APIError(400, "Expected a form submission.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                raise APIError(413, "Form submission is too large.")
        try:
            values = parse_qs(
                body.decode("utf-8"), keep_blank_values=True, max_num_fields=max_fields
            )
        except (ValueError, UnicodeError):
            raise APIError(400, "Invalid form submission.") from None
        return {key: entries[-1] for key, entries in values.items()}

    async def api(request, method, path, payload=None):
        headers = {}
        if request.headers.get("authorization"):
            headers["Authorization"] = request.headers["authorization"]
        identity = request.cookies.get(COOKIE)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://solakit.internal",
            timeout=None,
            trust_env=False,
            headers=headers,
            cookies={COOKIE: identity} if identity else {},
        ) as client:
            response = await client.request(method, path, json=payload)
        data = response.json()
        if response.is_error:
            raise APIError(
                response.status_code, data.get("detail", "API request failed.")
            )
        return data

    async def health(request):
        return await api(request, "GET", "/health")

    async def session_page(request, error=None, values=None, status=200):
        data, profiles, state = await asyncio.gather(
            api(request, "GET", "/api/sessions"),
            api(request, "GET", "/api/profiles"),
            health(request),
        )
        data.sort(key=lambda item: timestamp(item["created_at"]), reverse=True)
        return render(
            request,
            "sessions.html",
            {
                "sessions": data,
                "profiles": profiles,
                "health": state,
                "error": error,
                "values": values or {},
            },
            status,
        )

    async def conversation(request, identity, number=None):
        # Read run state first: a terminal run's transcript is already durable
        # before the following session read (avoids ending a stream on stale text).
        records = await api(request, "GET", "/api/runs")
        data = await api(request, "GET", f"/api/sessions/{quote(identity, safe='')}")
        if number is not None:
            archived = await api(
                request,
                "GET",
                f"/api/sessions/{quote(identity, safe='')}/archives/{number}",
            )
            data = {**data, "messages": archived, "context_started_at": None}
        with_tools = {m["run_id"] for m in data["messages"] if m.get("tool_calls")}
        logs = await asyncio.gather(
            *(
                api(request, "GET", f"/api/runs/{quote(run_id, safe='')}")
                for run_id in sorted(with_tools)
            )
        )
        return data, turns(
            data,
            records,
            dict(zip(sorted(with_tools), logs, strict=True)),
            include_empty_runs=number is None,
        )

    async def chat_context(request, data, history, error=None):
        all_sessions, state, catalog = await asyncio.gather(
            api(request, "GET", "/api/sessions"),
            health(request),
            api(request, "GET", "/api/tools"),
        )
        all_sessions.sort(key=lambda item: timestamp(item["created_at"]), reverse=True)
        return {
            "session": data,
            "turns": history,
            "sessions": all_sessions,
            "tool_count": len(catalog["profiles"].get(data["default_profile"], [])),
            "health": state,
            "error": error,
        }

    @router.get("/login")
    async def login_page(request: Request):
        if logged_in(request):
            return RedirectResponse("/", 303, headers=HEADERS)
        return render(request, "login.html")

    @router.post("/login")
    async def login(request: Request):
        try:
            values = await form(request, 32768)
        except APIError:
            return render(request, "login.html", {"error": "Unable to sign in."}, 400)
        peer = request.client.host if request.client else "local"
        identity, status = sessions.login(
            values.get("token", ""), operator_token(), peer
        )
        if identity is None:
            error = (
                "Too many attempts. Try again in a minute."
                if status == 429
                else "Unable to sign in. Check your operator token."
            )
            return render(
                request,
                "login.html",
                {"error": error},
                status,
                {"Retry-After": "60"} if status == 429 else None,
            )
        response = RedirectResponse("/", 303, headers=HEADERS)
        # Single operator, localhost/tailnet: SameSite=Lax plus POST-only mutations.
        # No cross-origin CORS or GET mutations; no separate CSRF framework in v1.
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

    @router.post("/logout")
    async def logout(request: Request):
        sessions.revoke(request.cookies.get(COOKIE))
        response = RedirectResponse("/login", 303, headers=HEADERS)
        response.delete_cookie(
            COOKIE,
            path="/",
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
        return response

    @router.get("/")
    async def index(request: Request):
        if not logged_in(request):
            return login_redirect(request)
        try:
            return await session_page(request)
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.post("/sessions")
    async def create_session(request: Request):
        if not logged_in(request):
            return login_redirect(request)
        values = {}
        try:
            values = await form(request)
            created = await api(
                request,
                "POST",
                "/api/sessions",
                {
                    "id": values.get("id", ""),
                    "default_profile": values.get("default_profile", ""),
                    "memory_scope": values.get("memory_scope", "shared"),
                },
            )
        except APIError as exc:
            try:
                return await session_page(request, exc.detail, values, exc.status)
            except APIError as unavailable:
                return render(
                    request,
                    "error.html",
                    {"error": unavailable.detail},
                    unavailable.status,
                )
        return RedirectResponse(
            f"/sessions/{quote(created['id'], safe='')}", 303, headers=HEADERS
        )

    @router.get("/sessions/{identity}")
    async def chat(request: Request, identity: str):
        if not logged_in(request):
            return login_redirect(request)
        try:
            data, history = await conversation(request, identity)
            return render(
                request,
                "chat.html",
                await chat_context(request, data, history),
            )
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.get("/sessions/{identity}/compact")
    async def compaction_confirmation(request: Request, identity: str):
        if not logged_in(request):
            return login_redirect(request)
        try:
            data = await api(
                request, "GET", f"/api/sessions/{quote(identity, safe='')}"
            )
            return render(
                request,
                "compact_confirm.html",
                {"session": data, "health": await health(request)},
            )
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.post("/sessions/{identity}/compact")
    async def compact_session(request: Request, identity: str):
        if not logged_in(request):
            return login_redirect(request)
        try:
            await api(
                request, "POST", f"/api/sessions/{quote(identity, safe='')}/compact"
            )
            return RedirectResponse(
                f"/sessions/{quote(identity, safe='')}", 303, headers=HEADERS
            )
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.get("/sessions/{identity}/archive")
    async def archive_confirmation(request: Request, identity: str):
        if not logged_in(request):
            return login_redirect(request)
        try:
            data = await api(
                request, "GET", f"/api/sessions/{quote(identity, safe='')}"
            )
            return render(
                request,
                "archive_confirm.html",
                {"session": data, "health": await health(request)},
            )
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.post("/sessions/{identity}/archive")
    async def archive_session(request: Request, identity: str):
        if not logged_in(request):
            return login_redirect(request)
        try:
            await api(
                request, "POST", f"/api/sessions/{quote(identity, safe='')}/archive"
            )
            return RedirectResponse(
                f"/sessions/{quote(identity, safe='')}", 303, headers=HEADERS
            )
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.get("/sessions/{identity}/archives/{number}")
    async def archived_chat(request: Request, identity: str, number: int):
        if not logged_in(request):
            return login_redirect(request)
        try:
            data, history = await conversation(request, identity, number)
            context = await chat_context(request, data, history)
            return render(request, "chat.html", {**context, "archive_number": number})
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.post("/sessions/{identity}/messages")
    async def send(request: Request, identity: str):
        if not logged_in(request):
            if request.headers.get("accept") == "application/json":
                return JSONResponse(
                    {"detail": "Sign in again."}, status_code=401, headers=HEADERS
                )
            return login_redirect(request)
        error, status, completed = None, 200, False
        try:
            values = await form(request)
            asynchronous = request.headers.get("accept") == "application/json"
            record = await api(
                request,
                "POST",
                f"/api/sessions/{quote(identity, safe='')}/messages"
                + ("?wait=false&stream=true" if asynchronous else ""),
                {"message": values.get("message", "")},
            )
            if asynchronous:
                return JSONResponse(
                    {"run_id": record["id"]}, status_code=202, headers=HEADERS
                )
            completed = record["status"] == "completed"
        except APIError as exc:
            if request.headers.get("accept") == "application/json":
                return JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status, headers=HEADERS
                )
            error, status = exc.detail, exc.status
        try:
            data, history = await conversation(request, identity)
        except APIError as exc:
            return render(
                request,
                "fragments/message_tail.html",
                {"turns": [], "error": exc.detail},
                exc.status,
            )
        headers = {"HX-Trigger": "chatCompleted"} if completed else None
        if request.headers.get("HX-Request") == "true":
            return render(
                request,
                "fragments/message_tail.html",
                {"turns": history, "error": error},
                status,
                headers,
            )
        if completed:
            return RedirectResponse(
                f"/sessions/{quote(identity, safe='')}", 303, headers=HEADERS
            )
        return render(
            request,
            "chat.html",
            await chat_context(request, data, history, error),
            status,
        )

    @router.get("/sessions/{identity}/events")
    async def conversation_events(request: Request, identity: str):
        if not logged_in(request):
            return Response(status_code=401, headers=HEADERS)
        try:
            await api(request, "GET", f"/api/sessions/{quote(identity, safe='')}")
        except APIError as exc:
            return JSONResponse(
                {"detail": exc.detail}, status_code=exc.status, headers=HEADERS
            )

        async def snapshots():
            previous, heartbeat = None, time.monotonic()
            while not await request.is_disconnected():
                if not logged_in(request):
                    yield "event: expired\ndata: {}\n\n"
                    return
                try:
                    data, history = await conversation(request, identity)
                    pending = [turn for turn in history if turn.get("pending")]
                    for turn in pending:
                        record = await api(
                            request,
                            "GET",
                            f"/api/runs/{quote(turn['run']['id'], safe='')}?live=true",
                        )
                        preview = record.get("preview")
                        # API snapshots may race a durable assistant append. Do not
                        # render its preview a second time once that step is stored.
                        saved = sum(
                            m["role"] == "assistant"
                            and m.get("kind", "message") == "message"
                            and m["run_id"] == turn["run"]["id"]
                            for m in data["messages"]
                        )
                        if preview and saved <= preview["baseline"]:
                            turn["preview"] = preview["content"]
                    page = render(
                        request, "fragments/message_tail.html", {"turns": history}
                    )
                    snapshot = json.dumps(
                        {
                            "html": page.body.decode(),
                            "pending": bool(pending),
                        },
                        ensure_ascii=True,
                    )
                    if snapshot != previous:
                        yield f"event: snapshot\ndata: {snapshot}\n\n"
                        previous, heartbeat = snapshot, time.monotonic()
                    elif time.monotonic() - heartbeat >= 15:
                        yield ": keepalive\n\n"
                        heartbeat = time.monotonic()
                    if not pending:
                        return
                except APIError:
                    yield "event: unavailable\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.2)

        return StreamingResponse(
            snapshots(),
            media_type="text/event-stream",
            headers={**HEADERS, "X-Accel-Buffering": "no"},
        )

    async def operations_page(request, error=None, status=200):
        jobs, channels, state = await asyncio.gather(
            api(request, "GET", "/api/jobs"),
            api(request, "GET", "/api/channels"),
            health(request),
        )
        return render(
            request,
            "operations.html",
            {
                "jobs": jobs,
                "channels": channels,
                "health": state,
                "error": error,
            },
            status,
        )

    @router.get("/prompt")
    async def prompt_page(request: Request):
        if not logged_in(request):
            return login_redirect(request)
        values = {
            "profile": request.query_params.get("profile", "interactive"),
            "session": request.query_params.get("session", "inspection"),
            "origin": request.query_params.get("origin", "api"),
            "at": request.query_params.get("at", ""),
        }
        try:
            profiles, all_sessions, state = await asyncio.gather(
                api(request, "GET", "/api/profiles"),
                api(request, "GET", "/api/sessions"),
                health(request),
            )
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)
        evidence, error, status = None, None, 200
        try:
            if values["profile"] not in profiles:
                raise APIError(422, "Choose a configured profile.")
            if any(len(value) > 128 for value in values.values()):
                raise APIError(422, "Inspector fields must be at most 128 characters.")
            query = urlencode({key: value for key, value in values.items() if value})
            evidence = await api(request, "GET", "/api/prompt?" + query)
        except APIError as exc:
            error, status = exc.detail, exc.status
        return render(
            request,
            "prompt.html",
            {
                "values": values,
                "profiles": profiles,
                "sessions": all_sessions,
                "evidence": evidence,
                "error": error,
                "health": state,
            },
            status,
        )

    @router.get("/runs")
    async def runs_page(request: Request):
        if not logged_in(request):
            return login_redirect(request)
        try:
            records, state = await asyncio.gather(
                api(request, "GET", "/api/runs"),
                health(request),
            )
            session_filter = request.query_params.get("session", "")
            status_filter = request.query_params.get("status", "")
            statuses = sorted(
                {record["status"] for record in records}
                | ({status_filter} if status_filter else set())
            )
            records = [
                record
                for record in records
                if (not session_filter or record["session"] == session_filter)
                and (not status_filter or record["status"] == status_filter)
            ]
            records.sort(
                key=lambda record: timestamp(record["created_at"]), reverse=True
            )
            return render(
                request,
                "runs.html",
                {
                    "runs": records,
                    "statuses": statuses,
                    "health": state,
                    "session_filter": session_filter,
                    "status_filter": status_filter,
                },
            )
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.get("/runs/{identity}")
    async def run_page(request: Request, identity: str):
        if not logged_in(request):
            return login_redirect(request)
        try:
            record, state = await asyncio.gather(
                api(request, "GET", f"/api/runs/{quote(identity, safe='')}"),
                health(request),
            )
            # Transcript tool args/results are verbatim; reuse the redacted-log
            # presentation path rather than exposing raw message JSON.
            history = turns(
                {
                    "id": record["session"],
                    "messages": record["messages"],
                    "default_profile": record["profile"],
                },
                [record],
                {record["id"]: record},
            )
            return render(
                request,
                "run.html",
                {
                    "run": record,
                    "turns": history,
                    "health": state,
                },
            )
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.post("/runs/{identity}/cancel")
    async def cancel_run(request: Request, identity: str):
        if not logged_in(request):
            return login_redirect(request)
        try:
            await api(request, "POST", f"/api/runs/{quote(identity, safe='')}/cancel")
            return RedirectResponse(
                f"/runs/{quote(identity, safe='')}", 303, headers=HEADERS
            )
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.get("/operations")
    async def operations(request: Request):
        if not logged_in(request):
            return login_redirect(request)
        try:
            return await operations_page(request)
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    async def job_form_page(
        request, identity=None, values=None, error=None, status=200
    ):
        options, state, sessions_data = await asyncio.gather(
            api(request, "GET", "/api/job-options"),
            health(request),
            api(request, "GET", "/api/sessions"),
        )
        if values is None:
            values = (
                await api(request, "GET", f"/api/jobs/{quote(identity, safe='')}")
                if identity
                else {"timezone": options["timezone"], "bind_session": "ephemeral"}
            )
            delivery = values.get("deliver") or {}
            values = {
                **values,
                "schedule_type": "once" if values.get("run_at") else "cron",
                "run_at": job_local_time(values) if values.get("run_at") else "",
                "delivery_mode": delivery.get("mode", "automatic"),
                "delivery_to": delivery.get("to", ""),
            }
        return render(
            request,
            "job_form.html",
            {
                "identity": identity,
                "values": values,
                "options": options,
                "session_ids": [session["id"] for session in sessions_data],
                "health": state,
                "error": error,
            },
            status,
        )

    @router.get("/operations/jobs/new")
    async def job_new(request: Request):
        if not logged_in(request):
            return login_redirect(request)
        try:
            return await job_form_page(request)
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.get("/operations/jobs/{identity}/edit")
    async def job_edit(request: Request, identity: str):
        if not logged_in(request):
            return login_redirect(request)
        try:
            return await job_form_page(request, identity)
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    async def save_job_form(request, identity=None):
        values = None
        try:
            values = await form(request, max_fields=20)
            allowed = {
                "id",
                "cron",
                "run_at",
                "schedule_type",
                "timezone",
                "profile",
                "task",
                "bind_session",
                "delivery_mode",
                "delivery_to",
                "enabled",
                "polling",
                "catch_up",
            }
            if set(values) - allowed:
                raise APIError(422, "Unknown job form fields.")
            payload = {
                key: values.get(key, "")
                for key in ("timezone", "profile", "task", "bind_session")
            }
            schedule_type = values.get("schedule_type", "cron")
            if schedule_type not in {"cron", "once"}:
                raise APIError(422, "Invalid schedule type.")
            payload["cron"] = (
                values.get("cron", "") if schedule_type == "cron" else None
            )
            payload["run_at"] = (
                values.get("run_at", "") if schedule_type == "once" else None
            )
            if identity and schedule_type == "once":
                prior = await api(
                    request, "GET", f"/api/jobs/{quote(identity, safe='')}"
                )
                if (
                    prior.get("run_at")
                    and prior["timezone"] == payload["timezone"]
                    and same_job_local_time(prior, payload["run_at"])
                ):
                    # Preserve the exact instant/fold when only other fields change.
                    payload["run_at"] = prior["run_at"]
            payload.update(
                {
                    key: values.get(key) == "on"
                    for key in ("enabled", "polling", "catch_up")
                }
            )
            mode = values.get("delivery_mode", "automatic")
            if mode not in {"automatic", "final", "artifact", "silent"}:
                raise APIError(422, "Invalid delivery mode.")
            payload["deliver"] = (
                None
                if mode == "automatic"
                else {
                    "mode": mode,
                    "to": values.get("delivery_to", ""),
                }
            )
            if identity:
                if values.get("id", identity) != identity:
                    raise APIError(422, "Job ID cannot be changed.")
                await api(
                    request, "PATCH", f"/api/jobs/{quote(identity, safe='')}", payload
                )
            else:
                await api(
                    request,
                    "POST",
                    "/api/jobs",
                    {"id": values.get("id", ""), **payload},
                )
            return RedirectResponse("/operations", 303, headers=HEADERS)
        except APIError as exc:
            message = (
                exc.detail
                if exc.status != 422
                else (
                    "Check the schedule, date/time, timezone, profile and task fields."
                )
            )
            return await job_form_page(request, identity, values, message, exc.status)

    @router.post("/operations/jobs")
    async def job_create_form(request: Request):
        if not logged_in(request):
            return login_redirect(request)
        return await save_job_form(request)

    @router.post("/operations/jobs/{identity}/edit")
    async def job_update_form(request: Request, identity: str):
        if not logged_in(request):
            return login_redirect(request)
        return await save_job_form(request, identity)

    @router.get("/operations/jobs/{identity}/delete")
    async def job_delete_confirm(request: Request, identity: str):
        if not logged_in(request):
            return login_redirect(request)
        try:
            record = await api(request, "GET", f"/api/jobs/{quote(identity, safe='')}")
            return render(
                request,
                "job_delete.html",
                {
                    "job": record,
                    "health": await health(request),
                },
            )
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.post("/operations/jobs/{identity}/{operation}")
    async def job_action(request: Request, identity: str, operation: str):
        if not logged_in(request):
            return login_redirect(request)
        if operation not in {"run", "pause", "resume", "delete"}:
            return render(
                request, "error.html", {"error": "Unknown job operation."}, 404
            )
        try:
            record = await api(
                request,
                "DELETE" if operation == "delete" else "POST",
                f"/api/jobs/{quote(identity, safe='')}"
                + ("" if operation == "delete" else f"/{operation}"),
            )
            target = (
                f"/runs/{quote(record['id'], safe='')}"
                if operation == "run"
                else "/operations"
            )
            return RedirectResponse(target, 303, headers=HEADERS)
        except APIError as exc:
            try:
                return await operations_page(request, exc.detail, exc.status)
            except APIError as unavailable:
                return render(
                    request,
                    "error.html",
                    {"error": unavailable.detail},
                    unavailable.status,
                )

    @router.get("/tools")
    async def tools_page(request: Request):
        if not logged_in(request):
            return login_redirect(request)
        try:
            catalog, state = await asyncio.gather(
                api(request, "GET", "/api/tools"), health(request)
            )
            return render(request, "tools.html", {"catalog": catalog, "health": state})
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.get("/ui/status", response_class=HTMLResponse)
    async def status_chips(request: Request):
        if not logged_in(request):
            return login_redirect(request)
        return render(
            request, "fragments/status.html", {"health": await health(request)}
        )

    return router
