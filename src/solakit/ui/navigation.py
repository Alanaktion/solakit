"""Chat navigation and compatibility routes, using only the authenticated API."""

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.routing import APIRoute

from solakit.ui.context import UIContext

SECTIONS = (
    ("/settings/system", "System"),
    ("/settings/jobs", "Jobs"),
    ("/settings/tools", "Tools & profiles"),
    ("/settings/sessions", "Sessions"),
    ("/settings/evidence/runs", "Evidence"),
    ("/settings/search", "Search"),
)
LEGACY = {
    "/operations": "/settings/jobs",
    "/model-health": "/settings/system",
    "/tools": "/settings/tools",
    "/prompt": "/settings/evidence/prompt",
    "/runs": "/settings/evidence/runs",
    "/search": "/settings/search",
}


def health_indicator(state: dict) -> dict:
    model, storage, memory = (
        state.get(k, "unknown") for k in ("model", "storage", "memory")
    )
    color = "amber"
    if model == "unavailable" or storage not in {"ok", "unknown"}:
        color = "red"
    elif model == "ready" and storage == "ok" and memory in {"ready", "disabled"}:
        color = "green"
    failing = state.get("jobs_failing", 0)
    lagging = state.get("memory_lagging", 0)
    if color == "green" and (failing or lagging):
        color = "amber"
    extra = (
        f" · {failing} jobs failing · {lagging} extractions delayed"
        if failing or lagging
        else ""
    )
    return {
        "color": color,
        "label": (
            f"Model {model} · storage {storage} · "
            f"memory {memory} (startup check){extra}"
        ),
    }


def run_duration(run: dict) -> str:
    if not run.get("finished_at"):
        return (
            "In progress"
            if run.get("status") in {"queued", "running"}
            else "Not measured"
        )
    seconds = max(
        0,
        (
            datetime.fromisoformat(run["finished_at"])
            - datetime.fromisoformat(run["created_at"])
        ).total_seconds(),
    )
    return f"{seconds:.1f}s"


async def topic_context(request: Request, api) -> dict:
    """Page sidebar work, including title hydration, to keep API reads bounded."""
    from solakit.ui.routes import APIError

    try:
        offset = int(request.query_params.get("topic_offset", "0"))
        archived_offset = int(request.query_params.get("archived_offset", "0"))
        if min(offset, archived_offset) < 0:
            raise ValueError
    except ValueError:
        raise APIError(422, "Topic offset must be nonnegative.") from None

    async def catalog(status, at, **extra):
        return await api(
            request,
            "GET",
            "/api/sessions?"
            + urlencode(
                {
                    "page": "true",
                    "status": status,
                    "limit": 20,
                    "offset": at,
                    **extra,
                }
            ),
        )

    # Pinned chats are fetched on their own so paging recents can never push a
    # pinned (or channel-bound) session out of the list.
    pinned, active, archived = await asyncio.gather(
        api(
            request,
            "GET",
            "/api/sessions?"
            + urlencode(
                {"page": "true", "status": "active", "pinned": "true", "limit": 100}
            ),
        ),
        catalog(
            "active",
            offset,
            roots_only="true",
            exclude_source="cron",
            pinned="false",
        ),
        catalog("archived", archived_offset, roots_only="true", exclude_source="cron"),
    )
    semaphore = asyncio.Semaphore(6)

    async def hydrate(item):
        async with semaphore:
            item["topic_title"] = item.get("title") or item["id"]
            records = await api(
                request,
                "GET",
                "/api/runs?"
                + urlencode(
                    {
                        "session_id": item["id"],
                        "descending": "true",
                        "limit": 1,
                        "include_archived": "true",
                    }
                ),
            )
            item["last_run_status"] = records[0]["status"] if records else None
            preview = (
                records[0].get("rendered_output") or records[0].get("input_message")
                if records
                else None
            )
            if preview:
                prefix = "" if records[0].get("rendered_output") else "You: "
                item["preview"] = prefix + " ".join(preview.split())[:90]
            if item.get("title"):
                return
            earliest = await api(
                request,
                "GET",
                "/api/runs?"
                + urlencode(
                    {
                        "session_id": item["id"],
                        "limit": 1,
                        "include_archived": "true",
                    }
                ),
            )
            first = earliest[0].get("input_message") if earliest else None
            if earliest and not first:
                # Run inspection uses indexed message offsets across archives;
                # never hydrate entire archives merely to label the sidebar.
                run_id = quote(earliest[0]["id"], safe="")
                detail = await api(request, "GET", f"/api/runs/{run_id}")
                first = next(
                    (
                        m.get("content")
                        for m in detail["messages"]
                        if m["role"] == "user" and m.get("content")
                    ),
                    None,
                )
            if first:
                item["topic_title"] = " ".join(first.split())[:64]

    await asyncio.gather(
        *(
            hydrate(item)
            for item in pinned["sessions"] + active["sessions"] + archived["sessions"]
        )
    )

    def page_url(key, value):
        return (
            request.url.path
            + "?"
            + urlencode(
                {
                    **request.query_params,
                    key: value,
                }
            )
        )

    return {
        "pinned_topics": pinned["sessions"],
        "sessions": active["sessions"],
        "archived_topics": archived["sessions"],
        "topic_previous": page_url("topic_offset", max(0, offset - 20))
        if offset
        else None,
        "topic_next": page_url("topic_offset", offset + 20)
        if offset + 20 < active["total"]
        else None,
        "archived_previous": page_url("archived_offset", max(0, archived_offset - 20))
        if archived_offset
        else None,
        "archived_next": page_url("archived_offset", archived_offset + 20)
        if archived_offset + 20 < archived["total"]
        else None,
    }


def register_pin(router: APIRouter, ns: UIContext) -> None:
    from solakit.ui.routes import HEADERS, APIError

    @router.post("/sessions/{identity}/pin")
    async def pin_topic(request: Request, identity: str):
        if not ns.logged_in(request):
            return ns.login_redirect(request)
        try:
            values = await ns.form(request, 4096)
            if values.get("pinned") not in {"1", "0", "default"}:
                raise APIError(422, "Choose pin, unpin or default.")
            await ns.api(
                request,
                "POST",
                f"/api/sessions/{quote(identity, safe='')}/pin",
                {
                    "pinned": None
                    if values["pinned"] == "default"
                    else values["pinned"] == "1"
                },
            )
        except APIError as exc:
            return ns.render(request, "error.html", {"error": exc.detail}, exc.status)
        target = values.get("return_to", "")
        if (
            not target.startswith("/")
            or target.startswith("//")
            or "\\" in target
            or any(ord(char) <= 32 for char in target)
        ):
            target = f"/sessions/{quote(identity, safe='')}"
        return RedirectResponse(target, 303, headers=HEADERS)


def register_navigation(router: APIRouter, ns: UIContext) -> None:
    from solakit.ui.routes import HEADERS, APIError

    @router.get("/settings/sessions")
    async def managed_sessions(request: Request):
        if not ns.logged_in(request):
            return ns.login_redirect(request)
        try:
            return await ns.session_page(request)
        except APIError as exc:
            return ns.render(request, "error.html", {"error": exc.detail}, exc.status)

    @router.get("/sessions/{identity}/details")
    async def topic_details(request: Request, identity: str):
        if not ns.logged_in(request):
            return JSONResponse({"detail": "Sign in again."}, 401, headers=HEADERS)
        try:
            session, recent, children = await asyncio.gather(
                ns.api(request, "GET", f"/api/sessions/{quote(identity, safe='')}"),
                ns.api(
                    request,
                    "GET",
                    "/api/runs?"
                    + urlencode(
                        {
                            "session_id": identity,
                            "descending": "true",
                            "limit": 10,
                            "include_archived": "true",
                        }
                    ),
                ),
                ns.api(
                    request,
                    "GET",
                    "/api/sessions?"
                    + urlencode(
                        {
                            "parent_session": identity,
                            "status": "all",
                            "page": "true",
                            "offset": request.query_params.get("child_offset", "0"),
                        }
                    ),
                ),
            )
            return ns.render(
                request,
                "fragments/topic_details.html",
                {
                    "session": session,
                    "recent_runs": recent,
                    "children": children,
                    "archive_number": request.query_params.get("archive"),
                },
            )
        except APIError as exc:
            return JSONResponse({"detail": exc.detail}, exc.status, headers=HEADERS)

    @router.get("/settings")
    async def settings(request: Request):
        return RedirectResponse("/settings/system", 308, headers=HEADERS)

    @router.get("/ui/health")
    async def indicator(request: Request):
        if not ns.logged_in(request):
            return JSONResponse({"detail": "Sign in again."}, 401, headers=HEADERS)
        try:
            state, jobs, runs = await asyncio.gather(
                ns.health(request),
                ns.api(request, "GET", "/api/jobs"),
                ns.api(
                    request,
                    "GET",
                    "/api/runs?descending=true&limit=100&include_archived=true",
                ),
            )
            state["jobs_failing"] = sum(
                job.get("last_status")
                not in {None, "completed", "queued", "running", "cancelled"}
                for job in jobs
            )
            threshold = datetime.now(UTC) - timedelta(minutes=5)
            state["memory_lagging"] = sum(
                run.get("memory_status") in {"failed", "status_lost"}
                or (
                    run.get("memory_status") in {"accepted", "pending"}
                    and datetime.fromisoformat(run["created_at"]) < threshold
                )
                for run in runs
            )
        except APIError:
            state = {}
        return JSONResponse(health_indicator(state), headers=HEADERS)

    register_pin(router, ns)
    register_legacy(router, ns)


def register_legacy(router: APIRouter, ns: UIContext) -> None:
    from solakit.ui.routes import HEADERS

    def redirect_to(target):
        async def legacy(request: Request):
            if not ns.logged_in(request):
                return ns.login_redirect(request)
            path = target
            for key, value in request.path_params.items():
                path = path.replace("{" + key + "}", quote(str(value), safe=""))
            if request.url.query:
                path += "?" + request.url.query
            return RedirectResponse(path, 308, headers=HEADERS)

        return legacy

    # Existing POST URLs remain callable; redirects are only for read navigation.
    for route in list(router.routes):
        if not isinstance(route, APIRoute):
            continue
        for old, new in LEGACY.items():
            if route.path != new and not route.path.startswith(new + "/"):
                continue
            path = (
                old
                + ("/jobs" if old == "/operations" and route.path != new else "")
                + route.path[len(new) :]
            )
            if "GET" in (route.methods or set()):
                router.add_api_route(
                    path,
                    redirect_to(route.path),
                    methods=["GET"],
                    include_in_schema=False,
                )
            if "POST" in (route.methods or set()):
                router.add_api_route(
                    path
                    + ("/jobs" if old == "/operations" and route.path == new else ""),
                    route.endpoint,
                    methods=["POST"],
                    include_in_schema=False,
                )
