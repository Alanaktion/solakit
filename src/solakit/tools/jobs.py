"""Chat operations use the same persistent scheduler as the operator API."""

import json
from typing import Any

from solakit.core.config import Job
from solakit.tools import ToolContext, ToolError, ToolResult, tool


def manager(ctx):
    if ctx.job_manager is None:
        raise ToolError("Scheduler is unavailable in this context")
    return ctx.job_manager


def permitted(ctx, job):
    if "jobs.manage.all" in ctx.config.profiles[ctx.profile].capabilities:
        return True
    return (
        job.profile == ctx.profile
        and job.bind_session == ctx.session.id
        and not any(c.id == job.bind_session for c in ctx.config.channels)
        and (job.deliver is None or job.deliver.mode == "silent")
    )


def existing(ctx, identity):
    try:
        job = manager(ctx).job(identity)
    except KeyError as exc:
        raise ToolError("Job not found or outside this chat's grants") from exc
    if not permitted(ctx, job):
        raise ToolError("Other profiles, sessions or delivery require jobs.manage.all")
    return job


def result(ctx, data):
    content = json.dumps(ctx.store.redactor.redact(data), ensure_ascii=False)
    content = content.replace("<", "\\u003c").replace(">", "\\u003e")
    return ToolResult(
        content='<external-content source="jobs" encoding="json">\n'
        + content
        + "\n</external-content>"
    )


def authorize(ctx, job):
    if not permitted(ctx, job):
        raise ToolError("Other profiles, sessions or delivery require jobs.manage.all")


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
    except ValueError as exc:
        raise ToolError(
            "Invalid job definition, disabled job or scheduler shutting down"
        ) from exc


@tool(name="jobs.list", profiles=["*"])
async def list_jobs(ctx: ToolContext) -> ToolResult:
    """List jobs for this chat/profile; jobs.manage.all allows every job."""
    jobs = manager(ctx)
    return result(
        ctx, [jobs.get(job.id) for job in jobs.jobs.values() if permitted(ctx, job)]
    )


@tool(name="jobs.create", profiles=["*"], writes=["jobs.manage"])
async def create(
    ctx: ToolContext,
    id: str,
    task: str,
    cron: str | None = None,
    run_at: str | None = None,
    timezone: str | None = None,
    enabled: bool = False,
    profile: str | None = None,
    bind_session: str | None = None,
    deliver: dict[str, Any] | None = None,
    sub_session: bool = False,
    polling: bool = False,
    catch_up: bool = False,
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
    jobs.manage.all. A stable ID prevents duplicate creation on retry.
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
        )
    except ValueError as exc:
        raise ToolError("Invalid job definition") from exc
    if not permitted(ctx, job):
        raise ToolError("Other profiles, sessions or delivery require jobs.manage.all")
    return await operation(ctx, "create", job)


@tool(name="jobs.update", profiles=["*"], writes=["jobs.manage"])
async def update(ctx: ToolContext, id: str, changes: dict[str, Any]) -> ToolResult:
    """Edit job fields (cron, run_at, timezone, profile, task, bind_session, deliver,
    enabled, sub_session, polling, catch_up). ID is immutable;
    use deliver=null to clear it.
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
        raise ToolError("Invalid job changes") from exc
    if not permitted(ctx, proposed):
        raise ToolError("Other profiles, sessions or delivery require jobs.manage.all")
    return await operation(ctx, "update", id, patch)


@tool(name="jobs.run", profiles=["*"], writes=["jobs.run"])
async def run(ctx: ToolContext, id: str) -> ToolResult:
    """Queue an enabled job once without waiting, including in this chat.

    Does not enable the job or change its schedule. Active jobs cannot overlap.
    The queued job runs after the current turn if bound to the same session.
    Running a one-time job early consumes it; it will not fire again at run_at.
    """
    existing(ctx, id)
    return await operation(ctx, "trigger", id)


@tool(name="jobs.pause", profiles=["*"], writes=["jobs.manage"])
async def pause(ctx: ToolContext, id: str, paused: bool = True) -> ToolResult:
    """Pause/resume future firings; does not cancel active work or enable a job."""
    existing(ctx, id)
    await operation(ctx, "pause", id, paused)
    return result(ctx, manager(ctx).get(id))


@tool(name="jobs.delete", profiles=["*"], writes=["jobs.manage"])
async def delete(ctx: ToolContext, id: str) -> ToolResult:
    """Delete an idle job; retain all prior run evidence."""
    existing(ctx, id)
    return await operation(ctx, "delete", id)
