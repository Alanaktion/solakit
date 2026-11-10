"""UI routes (runs), registered by build_ui_router."""

import asyncio
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from solakit.core.schemas import RunStatus
from solakit.ui.context import UIContext
from solakit.ui.markdown import markdown
from solakit.ui.view import turns

TURN_WINDOW = 20
ROOT = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))
TEMPLATES.env.filters["markdown"] = markdown


def register(router: APIRouter, ns: UIContext) -> None:  # noqa: C901
    from solakit.ui.routes import (  # noqa: F401
        HEADERS,
        APIError,
        status_digest,
    )

    logged_in = ns.logged_in
    login_redirect = ns.login_redirect
    render = ns.render
    api = ns.api
    schema_views = ns.schema_views
    health = ns.health

    @router.get("/settings/evidence/prompt")
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

    @router.get("/settings/evidence/runs")
    async def runs_page(request: Request):
        if not logged_in(request):
            return login_redirect(request)
        try:
            session_filter = request.query_params.get("session", "")
            status_filter = request.query_params.get("status", "")
            try:
                offset = int(request.query_params.get("offset", "0"))
                if offset < 0:
                    raise ValueError
            except ValueError:
                raise APIError(422, "Offset must be nonnegative.") from None
            params = {"limit": 101, "offset": offset, "descending": "true"}
            if session_filter:
                params["session_id"] = session_filter
            if status_filter:
                params["status"] = status_filter
            records, state = await asyncio.gather(
                api(request, "GET", "/api/runs?" + urlencode(params)),
                health(request),
            )
            statuses = sorted(
                {value.value for value in RunStatus}
                | ({status_filter} if status_filter else set())
            )

            def page_url(at):
                return "/settings/evidence/runs?" + urlencode(
                    {"session": session_filter, "status": status_filter, "offset": at}
                )

            previous = page_url(max(0, offset - 100)) if offset else None
            following = page_url(offset + 100) if len(records) > 100 else None
            records = records[:100]
            return render(
                request,
                "runs.html",
                {
                    "runs": records,
                    "previous": previous,
                    "next": following,
                    "statuses": statuses,
                    "health": state,
                    "session_filter": session_filter,
                    "status_filter": status_filter,
                },
            )
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.get("/settings/evidence/runs/{identity}")
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

    @router.post("/settings/evidence/runs/{identity}/memory/refresh")
    async def refresh_memory(request: Request, identity: str):
        if not logged_in(request):
            return login_redirect(request)
        try:
            await api(
                request, "POST", f"/api/runs/{quote(identity, safe='')}/memory/refresh"
            )
            return RedirectResponse(
                f"/settings/evidence/runs/{quote(identity, safe='')}",
                303,
                headers=HEADERS,
            )
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.post("/settings/evidence/runs/{identity}/cancel")
    async def cancel_run(request: Request, identity: str):
        if not logged_in(request):
            return login_redirect(request)
        try:
            target = f"/settings/evidence/runs/{quote(identity, safe='')}"
            if request.query_params.get("return_to_session") == "true":
                record = await api(
                    request, "GET", f"/api/runs/{quote(identity, safe='')}"
                )
                target = f"/sessions/{quote(record['session'], safe='')}"
            await api(request, "POST", f"/api/runs/{quote(identity, safe='')}/cancel")
            return RedirectResponse(target, 303, headers=HEADERS)
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.get("/settings/system")
    async def model_health(request: Request):
        if not logged_in(request):
            return login_redirect(request)
        try:
            window = request.query_params.get("window", "100")
            if not window.isdigit():
                raise APIError(422, "Window must be a number of runs.")
            stats = await api(request, "GET", f"/api/stats/model?window={window}")
        except APIError as exc:
            return render(request, "error.html", {"error": exc.detail}, exc.status)
        return render(
            request,
            "model_health.html",
            {"stats": stats, "health": await health(request)},
        )

    @router.get("/settings/tools")
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
