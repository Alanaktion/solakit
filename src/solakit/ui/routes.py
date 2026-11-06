"""Server-rendered UI P1. Every application-data operation uses existing API routes."""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
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
from solakit.ui.markdown import markdown, preview_text
from solakit.ui.view import timestamp, turns

TURN_WINDOW = 20
ROOT = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))
TEMPLATES.env.filters["markdown"] = markdown


def display_time(value):
    try:
        date = datetime.fromisoformat(value).astimezone(UTC)
        return date.strftime("%b %-d, %Y · %H:%M:%S UTC")
    except (TypeError, ValueError):
        return value or "—"


def status_digest(health):
    visible = {
        key: (health or {}).get(key, "unknown")
        for key in ("status", "model", "storage", "memory")
    }
    return hashlib.sha256(json.dumps(visible, sort_keys=True).encode()).hexdigest()[:16]


TEMPLATES.env.filters["display_time"] = display_time
TEMPLATES.env.filters["status_digest"] = status_digest
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

    async def form(request, limit=512000, max_fields=8, multiple=()):
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
        return {
            key: entries if key in multiple else entries[-1]
            for key, entries in values.items()
        }

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

    def schema_views(schemas):
        # Schema property names are definitions, not diagnostic credential keys.
        # Mask resolved values before JSON encoding (including escaped secrets).
        def masked(value):
            if isinstance(value, dict):
                return {redact(key): masked(item) for key, item in value.items()}
            if isinstance(value, list):
                return [masked(item) for item in value]
            return redact(value)

        return [
            {
                "name": schema["function"]["name"],
                "text": json.dumps(masked(schema), ensure_ascii=False, indent=2),
            }
            for schema in schemas
        ]

    async def health(request):
        return await api(request, "GET", "/health")

    @router.get("/favicon.ico")
    async def favicon():
        return RedirectResponse("/static/moon.svg", headers=HEADERS)

    async def session_page(request, error=None, values=None, status=200):
        filters = {
            key: request.query_params.get(key, default)
            for key, default in (
                ("status", "active"),
                ("exclude_source", "cron"),
                ("q", ""),
                ("profile", ""),
                ("offset", "0"),
            )
        }
        parameters = {key: value for key, value in filters.items() if value}
        parameters["page"] = "true"
        catalog, profiles, state = await asyncio.gather(
            api(request, "GET", "/api/sessions?" + urlencode(parameters)),
            api(request, "GET", "/api/profiles"),
            health(request),
        )

        def page_url(offset):
            return "/?" + urlencode({**filters, "offset": offset})

        return render(
            request,
            "sessions.html",
            {
                "sessions": catalog["sessions"],
                "catalog": catalog,
                "profiles": profiles,
                "filters": filters,
                "health": state,
                "error": error,
                "values": values or {},
                "previous": page_url(max(0, catalog["offset"] - catalog["limit"]))
                if catalog["offset"]
                else None,
                "next": page_url(catalog["offset"] + catalog["limit"])
                if catalog["offset"] + catalog["limit"] < catalog["total"]
                else None,
            },
            status,
        )

    async def conversation(request, identity, number=None):
        # Read run state first: a terminal run's transcript is already durable
        # before the following session read (avoids ending a stream on stale text).
        records = await api(
            request,
            "GET",
            "/api/runs?"
            + urlencode({"session_id": identity, "include_archived": "true"}),
        )
        data = await api(request, "GET", f"/api/sessions/{quote(identity, safe='')}")
        if number is not None:
            archived = await api(
                request,
                "GET",
                f"/api/sessions/{quote(identity, safe='')}/archives/{number}",
            )
            data = {**data, "messages": archived, "context_started_at": None}
        for line, message in enumerate(data["messages"], 1):
            message["anchor"] = f"message-{line}"
        grouped = turns(data, records, {}, include_empty_runs=number is None)
        before = request.query_params.get("before")
        around = request.query_params.get("turn")
        anchor = request.query_params.get("anchor", "")
        if anchor.startswith("turn-"):
            around = anchor.removeprefix("turn-")
        elif anchor.startswith("message-"):
            around = next(
                (m["run_id"] for m in data["messages"] if m["anchor"] == anchor), around
            )

        def turn_key(turn):
            return turn["node_id"] if turn.get("boundary") else turn["run"]["id"]

        end = len(grouped)
        if before:
            end = next(
                (i for i, t in enumerate(grouped) if turn_key(t) == before),
                end,
            )
        if around:
            at = next(
                (
                    i
                    for i, t in enumerate(grouped)
                    if not t.get("boundary") and t["run"]["id"] == around
                ),
                None,
            )
            if at is not None:
                positions = [
                    i for i in range(at, len(grouped)) if not grouped[i].get("boundary")
                ]
                end = positions[min(TURN_WINDOW, len(positions)) - 1] + 1
        start, remaining = end, TURN_WINDOW
        while start and remaining:
            start -= 1
            if not grouped[start].get("boundary"):
                remaining -= 1
        while start and grouped[start - 1].get("boundary"):
            start -= 1
        selected = grouped[start:end]
        ids = {t["run"]["id"] for t in selected if not t.get("boundary")}
        data["earlier"] = turn_key(selected[0]) if start and selected else None
        data["later"] = end < len(grouped)
        data["pending_run"] = min(
            (r for r in records if r["status"] in {"queued", "running"}),
            key=lambda r: timestamp(r["created_at"]),
            default=None,
        )
        with_tools = {
            m["run_id"]
            for m in data["messages"]
            if m["run_id"] in ids and m.get("tool_calls")
        }
        logs = await asyncio.gather(
            *(
                api(
                    request,
                    "GET",
                    f"/api/runs/{quote(run_id, safe='')}?view=true&messages=false",
                )
                for run_id in sorted(with_tools)
            )
        )
        hydrated = turns(
            {**data, "messages": [m for m in data["messages"] if m["run_id"] in ids]},
            [r for r in records if r["id"] in ids],
            dict(zip(sorted(with_tools), logs, strict=True)),
            include_empty_runs=number is None,
        )
        indexed = {t["run"]["id"]: t for t in hydrated if not t.get("boundary")}
        return data, [
            t if t.get("boundary") else indexed[t["run"]["id"]] for t in selected
        ]

    async def chat_context(request, data, history, error=None):
        all_sessions, state, catalog = await asyncio.gather(
            api(request, "GET", "/api/sessions"),
            health(request),
            api(request, "GET", "/api/tools"),
        )
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

    @router.get("/search")
    async def search_sessions(request: Request):
        if not logged_in(request):
            return login_redirect(request)
        query = request.query_params.get("q", "")
        mode = request.query_params.get("mode", "sessions")
        results, previous, following, error, status = [], None, None, None, 200
        try:
            if mode not in {"sessions", "text"}:
                raise APIError(422, "Choose session names/IDs or full text.")
            if query.strip():
                if mode == "sessions":
                    catalog = await api(
                        request,
                        "GET",
                        "/api/sessions?"
                        + urlencode(
                            {
                                "status": "all",
                                "q": query,
                                "page": "true",
                                "offset": request.query_params.get("offset", "0"),
                            }
                        ),
                    )
                    results = catalog["sessions"]

                    def page_url(offset):
                        return "/search?" + urlencode(
                            {
                                "q": query,
                                "mode": mode,
                                "offset": offset,
                            }
                        )

                    if catalog["offset"]:
                        previous = page_url(
                            max(0, catalog["offset"] - catalog["limit"])
                        )
                    if catalog["offset"] + catalog["limit"] < catalog["total"]:
                        following = page_url(catalog["offset"] + catalog["limit"])
                else:
                    results = await api(
                        request,
                        "GET",
                        "/api/search?"
                        + urlencode(
                            {
                                "query": query,
                                "limit": 100,
                            }
                        ),
                    )
                    for hit in results:
                        target = "/sessions/" + quote(hit["session_id"], safe="")
                        if hit["archive"] != "active":
                            number = hit["archive"].rsplit(".", 1)[-1]
                            target += "/archives/" + quote(number, safe="")
                        hit["url"] = (
                            target
                            + "?"
                            + urlencode({"turn": hit["run_id"]})
                            + f"#message-{hit['line_no']}"
                        )
        except APIError as exc:
            error, status = exc.detail, exc.status
        return render(
            request,
            "search.html",
            {
                "query": query,
                "mode": mode,
                "results": results,
                "previous": previous,
                "next": following,
                "error": error,
                "health": await health(request),
            },
            status,
        )

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
                    "source": "web",
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

    @router.post("/sessions/{identity}/lifecycle/{operation}")
    async def session_lifecycle_action(request: Request, identity: str, operation: str):
        if not logged_in(request):
            return login_redirect(request)
        if operation not in {"archive", "restore"}:
            return render(
                request, "error.html", {"error": "Unknown lifecycle action."}, 404
            )
        try:
            await api(
                request,
                "POST",
                f"/api/sessions/{quote(identity, safe='')}/lifecycle",
                {"archived": operation == "archive"},
            )
            target = (
                "/?status=archived"
                if operation == "archive"
                else f"/sessions/{quote(identity, safe='')}"
            )
            return RedirectResponse(target, 303, headers=HEADERS)
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.get("/sessions/cleanup/preview")
    async def cleanup_preview(request: Request):
        if not logged_in(request):
            return login_redirect(request)
        try:
            days = int(request.query_params.get("days", "90"))
            result = await api(
                request,
                "POST",
                "/api/sessions/archive-batch",
                {
                    "inactive_days": days,
                    "q": request.query_params.get("q", ""),
                    "profile": request.query_params.get("profile") or None,
                },
            )
            return render(
                request,
                "session_cleanup.html",
                {
                    "preview": result,
                    "health": await health(request),
                    "selection": json.dumps(
                        {
                            "ids": [s["id"] for s in result["sessions"]],
                            "before": result["before"],
                            "expected_activity": {
                                s["id"]: s["last_activity_at"]
                                for s in result["sessions"]
                            },
                            "dry_run": False,
                        }
                    ),
                },
            )
        except (APIError, ValueError) as exc:
            return render(
                request,
                "error.html",
                {"error": "Invalid cleanup filters."},
                getattr(exc, "status", 422),
            )

    @router.post("/sessions/cleanup/review")
    async def cleanup_review(request: Request):
        if not logged_in(request):
            return login_redirect(request)
        try:
            values = await form(request, max_fields=510, multiple=("session_ids",))
            ids = values.get("session_ids", [])
            if not ids:
                raise APIError(422, "Select at least one session.")
            result = await api(
                request, "POST", "/api/sessions/archive-batch", {"ids": ids}
            )
            payload = {
                "ids": [s["id"] for s in result["sessions"]],
                "expected_activity": {
                    s["id"]: s["last_activity_at"] for s in result["sessions"]
                },
            }
            return render(
                request,
                "session_cleanup.html",
                {
                    "preview": result,
                    "selection": json.dumps(payload),
                    "health": await health(request),
                },
            )
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.get("/sessions/{identity}/lifecycle/archive")
    async def lifecycle_confirmation(request: Request, identity: str):
        if not logged_in(request):
            return login_redirect(request)
        try:
            data = await api(
                request, "GET", f"/api/sessions/{quote(identity, safe='')}"
            )
            return render(
                request,
                "lifecycle_confirm.html",
                {"session": data, "health": await health(request)},
            )
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.post("/sessions/cleanup/apply")
    async def cleanup_apply(request: Request):
        if not logged_in(request):
            return login_redirect(request)
        try:
            values = await form(request, max_fields=510, multiple=("session_ids",))
            if "selection" in values:
                payload = json.loads(values["selection"])
                if not isinstance(payload, dict) or not payload.get("ids"):
                    raise ValueError("Select at least one session")
                if values.get("from_preview") == "1":
                    chosen = set(values.get("session_ids", []))
                    payload["ids"] = [
                        identity for identity in payload["ids"] if identity in chosen
                    ]
                    if not payload["ids"]:
                        raise ValueError("Select at least one session")
                payload["dry_run"] = False
            else:
                ids = values.get("session_ids", [])
                if not ids:
                    raise ValueError("Select at least one session")
                payload = {"ids": ids, "dry_run": False}
            operation = values.get("operation", "archive")
            if operation not in {"archive", "restore"}:
                raise ValueError("Invalid lifecycle operation")
            result = await api(
                request, "POST", f"/api/sessions/{operation}-batch", payload
            )
            return render(
                request,
                "session_cleanup_result.html",
                {
                    "result": result,
                    "operation": operation,
                    "undo": json.dumps(
                        {
                            "ids": [s["id"] for s in result["sessions"]],
                            "expected_activity": {
                                s["id"]: s["last_activity_at"]
                                for s in result["sessions"]
                            },
                        }
                    ),
                    "health": await health(request),
                },
            )
        except (APIError, ValueError) as exc:
            return render(
                request,
                "error.html",
                {"error": getattr(exc, "detail", str(exc))},
                getattr(exc, "status", 422),
            )

    @router.get("/sessions/{identity}/earlier")
    async def earlier(request: Request, identity: str):
        if not logged_in(request):
            return JSONResponse({"detail": "Sign in again."}, 401, headers=HEADERS)
        try:
            number = request.query_params.get("archive")
            data, history = await conversation(
                request, identity, int(number) if number else None
            )
            page = render(request, "fragments/message_tail.html", {"turns": history})
            return JSONResponse(
                {
                    "html": page.body.decode(),
                    "before": data["earlier"],
                    "later": data["later"],
                },
                headers=HEADERS,
            )
        except (APIError, ValueError) as exc:
            return JSONResponse(
                {"detail": str(exc)}, getattr(exc, "status", 422), headers=HEADERS
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

    async def rename_page(request, identity, values=None, error=None, status=200):
        data = await api(request, "GET", f"/api/sessions/{quote(identity, safe='')}")
        return render(
            request,
            "session_rename.html",
            {
                "session": data,
                "title": data["title"] if values is None else values.get("title", ""),
                "error": error,
                "health": await health(request),
            },
            status,
        )

    @router.get("/sessions/{identity}/rename")
    async def rename_confirmation(request: Request, identity: str):
        if not logged_in(request):
            return login_redirect(request)
        try:
            return await rename_page(request, identity)
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.post("/sessions/{identity}/rename")
    async def rename_session(request: Request, identity: str):
        if not logged_in(request):
            return login_redirect(request)
        values = {}
        try:
            values = await form(request)
            await api(
                request,
                "PATCH",
                f"/api/sessions/{quote(identity, safe='')}",
                {"title": values.get("title", "")},
            )
            return RedirectResponse(
                f"/sessions/{quote(identity, safe='')}", 303, headers=HEADERS
            )
        except APIError as exc:
            try:
                return await rename_page(
                    request, identity, values, exc.detail, exc.status
                )
            except APIError as unavailable:
                return render(
                    request,
                    "error.html",
                    {"error": unavailable.detail},
                    unavailable.status,
                )

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
        try:
            values = await form(request)
            record = await api(
                request,
                "POST",
                f"/api/sessions/{quote(identity, safe='')}/messages?wait=false"
                + (
                    "&stream=true"
                    if request.headers.get("accept") == "application/json"
                    else ""
                ),
                {"message": values.get("message", "")},
            )
            if request.headers.get("accept") == "application/json":
                return JSONResponse(
                    {"run_id": record["id"], "created_at": record["created_at"]},
                    status_code=202,
                    headers=HEADERS,
                )
            return RedirectResponse(
                f"/sessions/{quote(identity, safe='')}", 303, headers=HEADERS
            )
        except APIError as exc:
            if request.headers.get("accept") == "application/json":
                return JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status, headers=HEADERS
                )
            try:
                data, history = await conversation(request, identity)
            except APIError as unavailable:
                return render(
                    request,
                    "error.html",
                    {"error": unavailable.detail},
                    unavailable.status,
                )
            return render(
                request,
                "chat.html",
                await chat_context(request, data, history, exc.detail),
                exc.status,
            )

    @router.get("/sessions/{identity}/events")
    async def conversation_events(request: Request, identity: str):
        if not logged_in(request):
            return Response(status_code=401, headers=HEADERS)
        try:
            records = await api(
                request,
                "GET",
                "/api/runs?"
                + urlencode({"session_id": identity, "include_archived": "true"}),
            )
            # Discover once; subsequent ticks fetch only this run, not the session.
            active = [r for r in records if r["status"] in {"queued", "running"}]
            active.sort(key=lambda r: (timestamp(r["created_at"]), r["id"]))
            requested = request.query_params.get("run")
            record = (
                next((r for r in records if r["id"] == requested), None)
                if requested
                else (active[0] if active else (records[-1] if records else None))
            )
            if (
                record
                and active
                and timestamp(active[0]["created_at"]) < timestamp(record["created_at"])
            ):
                record = active[0]
            if record is None:
                await api(request, "GET", f"/api/sessions/{quote(identity, safe='')}")
                return Response(status_code=204, headers=HEADERS)
        except APIError as exc:
            return JSONResponse(
                {"detail": exc.detail}, status_code=exc.status, headers=HEADERS
            )

        async def snapshots():
            observed = record
            watched = {r["id"] for r in active} | {record["id"]}
            seen = set()
            cutoff = max(timestamp(r["created_at"]) for r in records)
            previous, heartbeat = None, time.monotonic()
            while not await request.is_disconnected():
                if not logged_in(request):
                    yield "event: expired\ndata: {}\n\n"
                    return
                try:
                    current = await api(
                        request,
                        "GET",
                        f"/api/runs/{quote(observed['id'], safe='')}"
                        "?live=true&view=true",
                    )
                    data = {
                        "id": identity,
                        "messages": current["messages"],
                        "default_profile": current["profile"],
                    }
                    history = turns(data, [current], {current["id"]: current})
                    boundaries = [turn for turn in history if turn.get("boundary")]
                    history = [turn for turn in history if not turn.get("boundary")]
                    if history:
                        history[0]["context_boundaries"] = boundaries
                    pending = current["status"] in {"queued", "running"}
                    following = None
                    if not pending:
                        seen.add(current["id"])
                        catalog = await api(
                            request,
                            "GET",
                            "/api/runs?"
                            + urlencode(
                                {"session_id": identity, "include_archived": "true"}
                            ),
                        )
                        # A queued successor may finish between observer ticks.
                        # Deliver its final delta too, without replaying history.
                        following = next(
                            (
                                r
                                for r in sorted(
                                    catalog,
                                    key=lambda r: (timestamp(r["created_at"]), r["id"]),
                                )
                                if r["id"] not in seen
                                and (
                                    r["id"] in watched
                                    or timestamp(r["created_at"]) > cutoff
                                    or r["status"] in {"queued", "running"}
                                )
                            ),
                            None,
                        )
                    preview = current.get("preview")
                    saved = sum(
                        m["role"] == "assistant"
                        and m.get("kind", "message") == "message"
                        for m in current["messages"]
                    )
                    if history and pending and preview and saved <= preview["baseline"]:
                        history[-1]["preview"] = preview_text(preview["content"])
                    page = render(
                        request, "fragments/message_tail.html", {"turns": history}
                    )
                    snapshot = json.dumps(
                        {
                            "run_id": current["id"],
                            "created_at": current["created_at"],
                            "html": page.body.decode(),
                            "pending": pending or following is not None,
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
                        if following is None:
                            return
                        observed, previous = following, None
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
        recorded_run = request.query_params.get("run_id")
        if recorded_run is not None:
            try:
                evidence = await api(
                    request,
                    "GET",
                    "/api/prompt?"
                    + urlencode(
                        {
                            k: v
                            for k, v in {
                                "run_id": recorded_run,
                                "step": request.query_params.get("step"),
                            }.items()
                            if v is not None
                        }
                    ),
                )
                return render(
                    request,
                    "prompt.html",
                    {
                        "evidence": evidence,
                        "recorded_run": recorded_run,
                        "schema_views": schema_views(evidence["tools"]),
                        "health": await health(request),
                    },
                )
            except APIError as exc:
                return render(request, "error.html", {"error": exc.detail}, exc.status)
        values = {
            "job": request.query_params.get("job", ""),
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
                "schema_views": schema_views(evidence["tools"]) if evidence else [],
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
            target = f"/runs/{quote(identity, safe='')}"
            if request.query_params.get("return_to_session") == "true":
                record = await api(
                    request, "GET", f"/api/runs/{quote(identity, safe='')}"
                )
                target = f"/sessions/{quote(record['session'], safe='')}"
            await api(request, "POST", f"/api/runs/{quote(identity, safe='')}/cancel")
            return RedirectResponse(target, 303, headers=HEADERS)
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
                "resident_mode": "inherit"
                if values.get("tool_resident") is None
                else "selected",
                "tool_resident": " ".join(values.get("tool_resident") or []),
                "delivery_mode": delivery.get("mode", "automatic"),
                "delivery_to": delivery.get("to", ""),
                "tools_mode": "inherit"
                if values.get("tools") is None
                else ("none" if values["tools"] == [] else "selected"),
            }
        elif identity and "tools_mode" not in values:
            try:
                prior = await api(
                    request, "GET", f"/api/jobs/{quote(identity, safe='')}"
                )
            except APIError as exc:
                return render(request, "error.html", {"error": exc.detail}, exc.status)
            selected = prior.get("tools")
            values = {
                **values,
                "tools": selected,
                "tools_mode": "inherit"
                if selected is None
                else ("none" if selected == [] else "selected"),
            }
        return render(
            request,
            "job_form.html",
            {
                "identity": identity,
                "values": values,
                "options": options,
                "tool_choices": options["tools"]
                + [
                    {"name": name, "profiles": []}
                    for name in (values.get("tools") or [])
                    if name not in {t["name"] for t in options["tools"]}
                ],
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
            values = await form(request, max_fields=280, multiple=("tools",))
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
                "sub_session",
                "polling",
                "catch_up",
                "tools_mode",
                "tool_disclosure",
                "resident_mode",
                "tool_resident",
                "tools",
            }
            if set(values) - allowed:
                raise APIError(422, "Unknown job form fields.")
            payload = {
                key: values.get(key, "")
                for key in ("timezone", "profile", "task", "bind_session")
            }
            if "tool_disclosure" in values:
                payload["tool_disclosure"] = values["tool_disclosure"]
            if "resident_mode" in values:
                if values["resident_mode"] not in {"inherit", "selected"}:
                    raise APIError(422, "Invalid resident selection.")
                payload["tool_resident"] = (
                    None
                    if values["resident_mode"] == "inherit"
                    else values.get("tool_resident", "").split()
                )
            tools_mode = values.get("tools_mode", "inherit")
            if tools_mode not in {"inherit", "selected", "none"}:
                raise APIError(422, "Choose inherited, selected, or no job tools.")
            if "tools_mode" in values or identity is None:
                payload["tools"] = (
                    None
                    if tools_mode == "inherit"
                    else (values.get("tools", []) if tools_mode == "selected" else [])
                )
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
                    for key in ("enabled", "sub_session", "polling", "catch_up")
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
                if exc.status != 422 or exc.detail.startswith("Job tools ")
                else (
                    "Check the schedule, date/time, timezone, profile, tools "
                    "and task fields."
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
                + ("" if operation == "delete" else f"/{operation}")
                + ("?admission=true" if operation == "run" else ""),
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
        state = await health(request)
        if request.query_params.get("known") == status_digest(state):
            return Response(status_code=204, headers=HEADERS)
        return render(request, "fragments/status.html", {"health": state})

    return router
