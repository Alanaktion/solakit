"""Server-rendered UI P1. Every application-data operation uses existing API routes."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import (
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from solakit.ui.auth import COOKIE, LIFETIME, UISessions
from solakit.ui.markdown import markdown
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


def build_ui_router(app, sessions: UISessions, operator_token, redact) -> APIRouter:  # noqa: C901
    router = APIRouter(include_in_schema=False)

    def logged_in(request):
        return sessions.authorized(
            request.headers.get("authorization"),
            request.cookies.get(COOKIE),
            operator_token(),
        )

    def return_path(value):
        # Reject browser URL normalization tricks as well as external origins.
        if (
            not value
            or not value.startswith("/")
            or value.startswith("//")
            or "\\" in value
            or any(ord(char) <= 32 for char in value)
            or urlsplit(value).path in {"/login", "/logout"}
        ):
            return "/"
        return value

    def login_redirect(request):
        target = "/"
        current = request.headers.get("HX-Current-URL") or request.headers.get(
            "referer"
        )
        if request.headers.get("HX-Request") == "true" or request.method != "GET":
            if current:
                parsed = urlsplit(current)
                if (parsed.scheme, parsed.netloc) == (
                    request.url.scheme,
                    request.url.netloc,
                ):
                    target = return_path(
                        parsed.path + ("?" + parsed.query if parsed.query else "")
                    )
        else:
            target = return_path(
                request.url.path
                + ("?" + request.url.query if request.url.query else "")
            )
        destination = "/login?" + urlencode({"next": target})
        if request.headers.get("HX-Request") == "true":
            return Response(headers={**HEADERS, "HX-Redirect": destination})
        return RedirectResponse(destination, status_code=303, headers=HEADERS)

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
        try:
            data = response.json()
        except ValueError:
            if response.is_error:
                raise APIError(
                    response.status_code, response.text or "API request failed."
                ) from None
            raise APIError(502, "API returned an invalid JSON response.") from None
        if response.is_error:
            detail = data.get("detail") if isinstance(data, dict) else None
            raise APIError(
                response.status_code, detail or response.text or "API request failed."
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
        parameters["roots_only"] = "true"
        parameters["pin_channels"] = "true"
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

    async def conversation(request, identity, number=None):  # noqa: C901
        # Read run state first: a terminal run's transcript is already durable
        # before the following session read (avoids ending a stream on stale text).
        records = await api(
            request,
            "GET",
            "/api/runs?"
            + urlencode({"session_id": identity, "include_archived": "true"}),
        )
        data = await api(request, "GET", f"/api/sessions/{quote(identity, safe='')}")
        names = sorted(data["archives"], key=lambda name: int(name.rsplit(".", 1)[1]))
        active_empty = not data["messages"] and bool(names) and number is None
        data["active_empty_archived"] = active_empty
        segments = [(None, data["messages"])]
        if number is not None:
            archived = await api(
                request,
                "GET",
                f"/api/sessions/{quote(identity, safe='')}/archives/{number}",
            )
            segments = [(None, archived)]
            data["context_started_at"] = None
            names = []
        before = request.query_params.get("before")
        around = request.query_params.get("turn")
        anchor = request.query_params.get("anchor", "")
        if anchor.startswith("turn-"):
            around = anchor.removeprefix("turn-")

        def turn_key(turn):
            return turn["node_id"] if turn.get("boundary") else turn["timeline_id"]

        # Initial history is active-only. Pagination resolves its cursor by walking
        # backwards, stopping as soon as the requested window is available.
        while True:
            messages = []
            for segment, entries in segments:
                for line, message in enumerate(entries, 1):
                    message["anchor"] = (
                        f"message-archive-{segment}-{line}"
                        if segment
                        else f"message-{line}"
                    )
                    message["timeline_segment"] = segment
                    messages.append(message)
            data = {**data, "messages": messages}
            grouped = turns(data, records, {}, include_empty_runs=number is None)
            for index, (segment, entries) in enumerate(segments[:-1]):
                newer = segments[index + 1][1]
                if not any(
                    m.get("kind") == "compaction"
                    and (m.get("archive") or "").endswith("." + segment)
                    for m in newer
                ):
                    grouped.append(
                        {
                            "boundary": True,
                            "manual": True,
                            "segment": segment,
                            "content": "",
                            "date": entries[-1]["ts"]
                            if entries
                            else "Date unavailable",
                            "count": len(entries),
                            "archive_number": segment,
                            "at": timestamp(entries[-1]["ts"]) if entries else 0,
                            "node_id": "seam-archive-" + segment,
                        }
                    )
            segment_order = {segment: i for i, (segment, _) in enumerate(segments)}
            grouped.sort(
                key=lambda turn: (
                    segment_order[turn.get("segment")],
                    bool(turn.get("manual")),
                    turn["at"],
                )
            )
            if anchor.startswith("message-"):
                around = next(
                    (
                        m["run_id"]
                        + (
                            "-archive-" + m["timeline_segment"]
                            if m.get("timeline_segment")
                            else ""
                        )
                        for m in messages
                        if m["anchor"] == anchor
                    ),
                    around,
                )
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
                        if not t.get("boundary")
                        and (t["timeline_id"] == around or t["run"]["id"] == around)
                    ),
                    None,
                )
                if at is not None:
                    positions = [
                        i
                        for i in range(at, len(grouped))
                        if not grouped[i].get("boundary")
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
            found = (
                (not before or any(turn_key(t) == before for t in grouped))
                and (
                    not around
                    or any(
                        not t.get("boundary")
                        and (t["timeline_id"] == around or t["run"]["id"] == around)
                        for t in grouped
                    )
                )
                and (
                    not anchor.startswith("message-")
                    or any(m["anchor"] == anchor for m in messages)
                )
            )
            initial = not before and not around and not anchor
            if initial and active_empty and len(segments) > 1:
                # Only the newest seam is needed to explain an empty active file.
                selected = [
                    t
                    for t in grouped
                    if (
                        t.get("boundary") and t.get("archive_number") == segments[-2][0]
                    )
                    or (not t.get("boundary") and t.get("segment") is None)
                ]
                start = len(grouped) - len(selected)
                break
            if (
                not names
                or (initial and not active_empty)
                or (not initial and found and remaining == 0)
            ):
                break
            name = names.pop()
            segment = name.rsplit(".", 1)[1]
            entries = await api(
                request,
                "GET",
                f"/api/sessions/{quote(identity, safe='')}/archives/{segment}",
            )
            segments.insert(0, (segment, entries))
        ids = {t["run"]["id"] for t in selected if not t.get("boundary")}
        data["earlier"] = (
            turn_key(selected[0]) if (start or names) and selected else None
        )
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
        evidence = dict(zip(sorted(with_tools), logs, strict=True))
        if number is None:
            # Loaded segments form a suffix. Mid-run rotation can leave earlier
            # calls in an unloaded archive; tool logs still cover the entire run.
            for run_id, log in evidence.items():
                completed = sum(
                    m["role"] == "tool" for m in messages if m["run_id"] == run_id
                )
                log["tool_offset"] = max(0, len(log.get("tools", [])) - completed)
        hydrated = turns(
            {**data, "messages": [m for m in data["messages"] if m["run_id"] in ids]},
            [r for r in records if r["id"] in ids],
            evidence,
            include_empty_runs=number is None,
        )
        indexed = {t["timeline_id"]: t for t in hydrated if not t.get("boundary")}
        return data, [
            t if t.get("boundary") else indexed[t["timeline_id"]] for t in selected
        ]

    async def chat_context(request, data, history, error=None):
        all_sessions, state, catalog, children = await asyncio.gather(
            api(request, "GET", "/api/sessions?roots_only=true&exclude_source=cron"),
            health(request),
            api(request, "GET", "/api/tools"),
            api(
                request,
                "GET",
                "/api/sessions?"
                + urlencode(
                    {
                        "parent_session": data["id"],
                        "status": "all",
                        "page": "true",
                        "offset": request.query_params.get("child_offset", "0"),
                    }
                ),
            ),
        )
        return {
            "children": children,
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
            return RedirectResponse(
                return_path(request.query_params.get("next")), 303, headers=HEADERS
            )
        return render(
            request,
            "login.html",
            {"next": return_path(request.query_params.get("next"))},
        )

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
                {"error": error, "next": return_path(values.get("next"))},
                status,
                {"Retry-After": "60"} if status == 429 else None,
            )
        response = RedirectResponse(
            return_path(values.get("next")), 303, headers=HEADERS
        )
        # Unsafe cookie requests also pass the shared origin check middleware.
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

    from types import SimpleNamespace

    from solakit.ui import routes_jobs, routes_runs, routes_sessions

    ns = SimpleNamespace(**{k: v for k, v in locals().items() if k != "ns"})
    for module in (routes_sessions, routes_runs, routes_jobs):
        module.register(router, ns)
    return router
