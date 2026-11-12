"""Chat operations use the same persistent scheduler as the operator API."""

from typing import Annotated, Any

from pydantic import Field

from solakit.core.config import Job, session_readable
from solakit.jobs.schemas import JobToolsError
from solakit.tools import ToolContext, ToolError, ToolResult, tool, tool_json


def manager(ctx):
    if ctx.job_manager is None:
        raise ToolError("Scheduler is unavailable in this context")
    return ctx.job_manager


def session_isolated(ctx, job) -> bool:
    """A job's profile reads its bound session's history, so isolation applies."""
    try:
        owner = ctx.store.session(job.bind_session).default_profile
    except (FileNotFoundError, ValueError):
        return False
    return not session_readable(ctx.config.profiles, job.profile, owner)


def permitted(ctx, job):
    if session_isolated(ctx, job):
        return False
    if "operate" in ctx.config.profiles[ctx.profile].effects:
        return True
    return (
        job.profile == ctx.profile
        and job.bind_session == ctx.session.id
        and not any(c.id == job.bind_session for c in ctx.config.channels)
        and (job.deliver is None or job.deliver.mode == "silent")
    )


def denied(ctx, job) -> ToolError:
    if session_isolated(ctx, job):
        return ToolError("Session isolation forbids this profile on that session")
    return ToolError("Other profiles, sessions or delivery require the operate effect")


def existing(ctx, identity):
    try:
        job = manager(ctx).job(identity)
    except KeyError as exc:
        raise ToolError("Job not found or outside this chat's grants") from exc
    if not permitted(ctx, job):
        raise denied(ctx, job)
    return job


def result(ctx, data):
    return tool_json(ctx, data, "jobs")


def invalid(exc: Exception, prefix: str = "Invalid job definition") -> ToolError:
    """Name what was wrong using validator messages (never submitted values)."""
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        reasons = []
        for issue in exc.errors(include_input=False, include_url=False)[:3]:
            field = ".".join(str(p) for p in issue["loc"]) or "job"
            message = issue["msg"].removeprefix("Value error, ")
            reasons.append(f"{field}: {message}"[:120])
        if reasons:
            return ToolError(f"{prefix}: " + "; ".join(reasons))
    return ToolError(prefix)


def parse_when(when: str) -> tuple[str | None, str | None]:
    """Five-field cron or an ISO date and time; never ambiguous."""
    from datetime import datetime

    value = when.strip()
    if len(value.split()) == 5 and not any(c in value for c in "T:"):
        return value, None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise ToolError(
            "when must be a five-field cron such as '0 8 * * 1-5' or an ISO date "
            "and time such as '2026-12-01T09:00'"
        ) from None
    if "T" not in value and " " not in value:
        raise ToolError("A one-time when needs a time, e.g. '2026-12-01T09:00'")
    return None, value


def authorize(ctx, job):
    # Mutation callbacks run under the scheduler lock, after any wait.
    if "operate" not in ctx.config.profiles[ctx.profile].effects:
        raise ToolError("Job mutation requires effect 'operate'")


async def operation(ctx, name, *args):
    try:
        return result(
            ctx,
            await getattr(manager(ctx), name)(
                *args, authorize=lambda job: authorize(ctx, job)
            ),
        )
    except (FileExistsError, RuntimeError) as exc:
        raise ToolError(str(exc)) from exc
    except KeyError as exc:
        raise ToolError("Job not found") from exc
    except JobToolsError as exc:
        raise ToolError(str(exc)) from exc
    except ValueError as exc:
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            raise invalid(exc) from exc
        raise ToolError(
            "Invalid job definition, disabled job or scheduler shutting down"
            + (f": {str(exc)[:150]}" if str(exc) else "")
        ) from exc


@tool(
    summary="List scheduled tasks visible to this profile",
    keywords=("schedule", "cron", "list"),
    name="jobs.list",
    profiles=["*"],
)
async def list_jobs(ctx: ToolContext) -> ToolResult:
    """List jobs for this chat/profile; the operate effect allows every job."""
    jobs = manager(ctx)
    return result(
        ctx, [jobs.get(job.id) for job in jobs.jobs.values() if permitted(ctx, job)]
    )


@tool(
    summary="Schedule a recurring (cron) or one-time task in this chat",
    keywords=("schedule", "cron", "reminder", "create", "remind", "every"),
    name="jobs.schedule",
    profiles=["*"],
    effect="operate",
)
async def schedule(
    ctx: ToolContext,
    id: Annotated[
        str,
        Field(description="Short stable ID such as calendar-review; reuse on retry."),
    ],
    task: Annotated[
        str,
        Field(
            description=(
                "Self-contained instruction for the future run; may use {date}, "
                "{time}, {timezone}, {job_id}."
            )
        ),
    ],
    when: Annotated[
        str,
        Field(
            description=(
                "Five-field cron ('0 8 * * 1-5' = weekdays 08:00) or one ISO "
                "date-time ('2026-12-01T09:00') in the operator timezone."
            )
        ),
    ],
    enabled: Annotated[
        bool, Field(description="Start active; set true when asked to schedule.")
    ] = False,
) -> ToolResult:
    """Schedule a task for this chat and profile.

    when decides the kind: a cron expression repeats, an ISO date-time runs once
    (it catches up if the app was offline). Resolve relative times from the
    current time and timezone; "tomorrow morning" means 09:00 next local day.
    Use jobs.update for delivery, profile, tools or playbook settings."""
    cron, run_at = parse_when(when)
    try:
        job = Job(
            id=id,
            cron=cron,
            run_at=run_at,
            task=task,
            timezone=ctx.config.timezone,
            enabled=enabled,
            profile=ctx.profile,
            bind_session=ctx.session.id,
        )
    except ValueError as exc:
        raise invalid(exc) from exc
    if not permitted(ctx, job):
        raise denied(ctx, job)
    return await operation(ctx, "create", job)


@tool(
    summary="Deprecated: use jobs.schedule (and jobs.update for options)",
    keywords=("schedule", "cron", "reminder", "create"),
    name="jobs.create",
    profiles=["*"],
    effect="operate",
    deprecated="jobs.schedule",
)
async def create(
    ctx: ToolContext,
    id: Annotated[
        str,
        Field(
            description=(
                "Stable scheduled job ID; use the same ID to avoid duplicate creation."
            )
        ),
    ],
    task: Annotated[
        str,
        Field(
            description=(
                "Self-contained task; supports {date}, {time}, {timezone}, "
                "{job_id}. Escape literal braces as {{ and }}."
            )
        ),
    ],
    cron: Annotated[
        str | None,
        Field(
            description=(
                "Five-field cron schedule; supply exactly one of cron or run_at."
            )
        ),
    ] = None,
    run_at: Annotated[
        str | None,
        Field(
            description=(
                "One-time ISO date and time, with offset or interpreted in "
                "timezone; DST ambiguity requires an offset."
            )
        ),
    ] = None,
    timezone: Annotated[
        str | None,
        Field(description="IANA schedule timezone; omit to use the operator default."),
    ] = None,
    enabled: Annotated[
        bool,
        Field(description="Whether the job is enabled; creation defaults to false."),
    ] = False,
    profile: Annotated[
        str | None,
        Field(
            description=(
                "Configured execution profile; its tools and authority cannot "
                "be widened by this call."
            )
        ),
    ] = None,
    bind_session: Annotated[
        str | None,
        Field(
            description=(
                "Session ID or configured channel ID for execution; ephemeral "
                "creates an isolated session."
            )
        ),
    ] = None,
    deliver: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Delivery object with to and mode (final, artifact or silent); "
                "null clears delivery on update."
            )
        ),
    ] = None,
    sub_session: Annotated[
        bool,
        Field(
            description=(
                "Run in a fresh private child session; deliver only its final report."
            )
        ),
    ] = False,
    polling: Annotated[
        bool,
        Field(
            description="Mark this as a polling job; required when catch_up is true."
        ),
    ] = False,
    catch_up: Annotated[
        bool,
        Field(
            description=(
                "Run an unattempted overdue polling job once after restart; "
                "requires polling=true."
            )
        ),
    ] = False,
    tools: Annotated[
        list[str] | None,
        Field(
            description=(
                "Tool names or family patterns narrowing the profile; null "
                "inherits, [] exposes no tools."
            )
        ),
    ] = None,
    tool_disclosure: Annotated[
        str,
        Field(
            description=(
                "Schema mode: inherit uses the profile; eager sends all; "
                "deferred starts with discovery."
            )
        ),
    ] = "inherit",
    tool_resident: Annotated[
        list[str] | None,
        Field(
            description=(
                "Initially visible tools in deferred mode; null inherits, [] "
                "starts with discovery only."
            )
        ),
    ] = None,
) -> ToolResult:
    """Schedule a recurring (cron) or one-time (run_at) task in this conversation.

    Supply exactly one of cron (five fields) or run_at (ISO date and time).
    Resolve relative requests using the current run's time and configured timezone.
    For "tomorrow morning", use 09:00 next local day unless a time was specified.
    Set enabled=true when the user asks to schedule a check. Defaults otherwise
    are this chat/profile and disabled. run_at accepts an explicit UTC offset,
    or local time in timezone; ambiguous DST times require an explicit offset.
    One-time jobs run at most once, including manual triggers, and catch up if the
    app was offline. Changing task/enablement does not rearm an attempted job;
    choose a new future run_at to reschedule. Tasks accept only {date},
    {time}, {timezone}, {job_id} placeholders (escape other braces as {{ and }}).
    sub_session=true runs privately in a fresh child with the parent memory scope;
    only configured final delivery reaches the channel. No parent history is copied.
    catch_up requires polling=true. Other profiles/sessions/delivery require
    the operate effect. A stable ID prevents duplicate creation on retry.
    tool_disclosure accepts inherit/eager/deferred; tool_resident=null inherits
    profile residents, [] starts with discovery only in deferred mode.
    tools=null inherits profile tools; tools=[] exposes none; an explicit list
    must narrow profile grants and cannot add effects, resources or
    filesystem reach.
    """
    try:
        job = Job(
            id=id,
            cron=cron,
            run_at=run_at,
            task=task,
            timezone=timezone or ctx.config.timezone,
            enabled=enabled,
            profile=profile or ctx.profile,
            bind_session=bind_session or ctx.session.id,
            deliver=deliver,
            sub_session=sub_session,
            polling=polling,
            catch_up=catch_up,
            tools=tools,
            tool_disclosure=tool_disclosure,
            tool_resident=tool_resident,
        )
    except ValueError as exc:
        raise invalid(exc) from exc
    if not permitted(ctx, job):
        raise denied(ctx, job)
    return await operation(ctx, "create", job)


@tool(
    summary="Update a scheduled task and its tool policy",
    keywords=("schedule", "cron", "edit"),
    name="jobs.update",
    profiles=["*"],
    effect="operate",
)
async def update(
    ctx: ToolContext,
    id: Annotated[
        str,
        Field(
            description=(
                "Stable scheduled job ID; use the same ID to avoid duplicate creation."
            )
        ),
    ],
    changes: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Partial job fields to change; omit unchanged fields and use "
                "null to clear nullable values."
            )
        ),
    ],
) -> ToolResult:
    """Edit job fields (cron, run_at, timezone, profile, task, bind_session, deliver,
    enabled, sub_session, polling, catch_up, tools, playbook). ID is immutable;
    use deliver=null to clear it.
    tools=null restores profile inheritance; tools=[] disables all tools.
    Active jobs must finish before editing. Scheduling changes reset next fire.
    To switch schedule types, clear the old field with null and set the new one.
    A new future run_at rearms a one-time job; other edits do not.
    """
    old = existing(ctx, id)
    from solakit.jobs.schemas import JobUpdate

    try:
        patch = JobUpdate.model_validate(changes).model_dump(exclude_unset=True)
        proposed = Job.model_validate({**old.model_dump(), **patch})
    except ValueError as exc:
        raise invalid(exc, "Invalid job changes") from exc
    if not permitted(ctx, proposed):
        raise denied(ctx, proposed)
    return await operation(ctx, "update", id, patch)


@tool(
    summary="Queue a scheduled task immediately",
    keywords=("schedule", "cron", "run"),
    name="jobs.run",
    profiles=["*"],
    effect="operate",
)
async def run(
    ctx: ToolContext,
    id: Annotated[
        str,
        Field(
            description=(
                "Stable scheduled job ID; use the same ID to avoid duplicate creation."
            )
        ),
    ],
) -> ToolResult:
    """Queue an enabled job once without waiting, including in this chat.

    Does not enable the job or change its schedule. Active jobs cannot overlap.
    The queued job runs after the current turn if bound to the same session.
    Running a one-time job early consumes it; it will not fire again at run_at.
    """
    existing(ctx, id)
    return await operation(ctx, "trigger", id)


@tool(
    summary="Pause or resume a scheduled task",
    keywords=("schedule", "job", "pause", "resume"),
    name="jobs.pause",
    profiles=["*"],
    effect="operate",
)
async def pause(
    ctx: ToolContext,
    id: Annotated[
        str,
        Field(
            description=(
                "Stable scheduled job ID; use the same ID to avoid duplicate creation."
            )
        ),
    ],
    paused: Annotated[
        bool,
        Field(
            description=(
                "True pauses future firings; false resumes. Does not cancel "
                "active work or enable a disabled job."
            )
        ),
    ] = True,
) -> ToolResult:
    """Pause/resume future firings; does not cancel active work or enable a job."""
    existing(ctx, id)
    await operation(ctx, "pause", id, paused)
    return result(ctx, manager(ctx).get(id))


@tool(
    summary="Delete an idle scheduled task while retaining run evidence",
    keywords=("schedule", "job", "delete", "remove"),
    name="jobs.delete",
    profiles=["*"],
    effect="operate",
)
async def delete(
    ctx: ToolContext,
    id: Annotated[
        str,
        Field(
            description=(
                "Stable scheduled job ID; use the same ID to avoid duplicate creation."
            )
        ),
    ],
) -> ToolResult:
    """Delete an idle job; retain all prior run evidence."""
    existing(ctx, id)
    return await operation(ctx, "delete", id)
