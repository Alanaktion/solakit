"""UI routes (sessions), registered by build_ui_router."""

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

from solakit.ui.context import UIContext
from solakit.ui.markdown import markdown, preview_text
from solakit.ui.view import timestamp, turns

TURN_WINDOW = 20
ROOT = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))
TEMPLATES.env.filters["markdown"] = markdown


def register(router: APIRouter, ns: UIContext) -> None:  # noqa: C901
    from solakit.ui.routes import (  # noqa: F401
        HEADERS,
        APIError,
    )

    logged_in = ns.logged_in
    login_redirect = ns.login_redirect
    render = ns.render
    form = ns.form
    api = ns.api
    all_runs = ns.all_runs
    health = ns.health
    session_page = ns.session_page
    conversation = ns.conversation
    chat_context = ns.chat_context

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
            page = render(
                request,
                "fragments/message_tail.html",
                {"turns": history, "session": {**data, "active_empty_archived": False}},
            )
            return JSONResponse(
                {
                    "html": bytes(page.body).decode(),
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
    async def conversation_events(request: Request, identity: str):  # noqa: C901
        if not logged_in(request):
            return Response(status_code=401, headers=HEADERS)
        try:
            records = await all_runs(
                request,
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
                        catalog = await all_runs(
                            request,
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
                        request,
                        "fragments/message_tail.html",
                        {"turns": history, "session": data},
                    )
                    snapshot = json.dumps(
                        {
                            "run_id": current["id"],
                            "created_at": current["created_at"],
                            "html": bytes(page.body).decode(),
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
