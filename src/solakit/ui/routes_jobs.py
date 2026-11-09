"""UI routes (jobs), registered by build_ui_router."""

import asyncio
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import (
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates

from solakit.ui.markdown import markdown

TURN_WINDOW = 20
ROOT = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))
TEMPLATES.env.filters["markdown"] = markdown


def register(router, ns):  # noqa: C901
    from solakit.ui.routes import (  # noqa: F401
        HEADERS,
        APIError,
        job_local_time,
        same_job_local_time,
    )

    logged_in = ns.logged_in
    login_redirect = ns.login_redirect
    render = ns.render
    form = ns.form
    api = ns.api
    health = ns.health

    async def operations_page(request, error=None, status=200):
        jobs, channels, state, playbooks = await asyncio.gather(
            api(request, "GET", "/api/jobs"),
            api(request, "GET", "/api/channels"),
            health(request),
            api(request, "GET", "/api/playbooks"),
        )
        return render(
            request,
            "operations.html",
            {
                "jobs": jobs,
                "channels": channels,
                "health": state,
                "playbooks": playbooks,
                "error": error,
            },
            status,
        )

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
