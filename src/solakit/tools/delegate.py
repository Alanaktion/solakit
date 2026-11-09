"""Serial delegated work with private transcripts and report-only results."""

import asyncio
import json
from typing import Annotated

from pydantic import Field

from solakit.core.io import durable
from solakit.core.schemas import Origin, RunStatus, Session
from solakit.tools import ToolContext, ToolError, ToolResult, tool, tool_json


def receipt(child, run, *, timed_out=False, report=None):
    data = {
        "session_id": child.id,
        "run_id": run.id if run else None,
        "status": str(run.status) if run else "not_started",
    }
    if timed_out:
        data["timed_out"] = True
        data["note"] = (
            "Timed out; side effects may have occurred. Inspect child evidence."
        )
    elif run and run.status == RunStatus.completed:
        data["report"] = report
    content = json.dumps(data, ensure_ascii=False)
    content = content.replace("<", "\\u003c").replace(">", "\\u003e")
    result = ToolResult(
        content='<external-content source="delegate" encoding="json">\n'
        + content
        + "\n</external-content>",
        error=timed_out or run is None or run.status != RunStatus.completed,
    )
    result._meta["receipt"] = data
    return result


TASK = Annotated[
    str,
    Field(
        description=(
            "Self-contained worker brief, 1\u201332000 characters; the child "
            "does not see this conversation."
        ),
        min_length=1,
        max_length=32000,
    ),
]


@tool(
    summary="Delegate a self-contained task and receive only the final report",
    keywords=("delegate", "worker", "subtask", "report"),
    name="delegate.run",
    profiles=["*"],
    manages_timeout=True,
)
async def run(
    ctx: ToolContext,
    task: TASK,
    profile: Annotated[
        str | None,
        Field(description="Worker profile; omit to use the current profile."),
    ] = None,
) -> ToolResult:
    """Run a brief in a fresh private child session; returns its final report.

    Use for long reading or multi-step side work whose details this conversation
    does not need. Do simple operations directly instead."""
    return await delegate(ctx, task, profile, None)


@tool(
    summary="Send a follow-up brief to an earlier delegated child session",
    keywords=("delegate", "worker", "continue", "follow-up"),
    name="delegate.continue",
    profiles=["*"],
    manages_timeout=True,
)
async def continue_child(
    ctx: ToolContext,
    session: Annotated[
        str, Field(description="session_id from an earlier delegate.run receipt.")
    ],
    task: TASK,
) -> ToolResult:
    """Continue an idle child of this conversation with its own history."""
    return await delegate(ctx, task, None, session)


MAP_LIMIT = 8


@tool(
    summary="Run the same brief over a short list of items, one child each",
    keywords=("delegate", "map", "each", "batch", "summarize", "list"),
    name="delegate.map",
    profiles=["*"],
    manages_timeout=True,
)
async def map_items(
    ctx: ToolContext,
    task_template: Annotated[
        str,
        Field(
            min_length=1,
            max_length=8000,
            description="Brief containing {item}, e.g. 'Summarize {item} in 5 lines'.",
        ),
    ],
    items: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=2000)]],
        Field(min_length=1, max_length=MAP_LIMIT, description="Up to 8 items."),
    ],
    profile: Annotated[
        str | None,
        Field(description="Worker profile; omit to use the current profile."),
    ] = None,
) -> ToolResult:
    """Delegate one private child per item, serially, and return their reports
    in order. Use it to read many documents or results without loading them all
    into this conversation. The whole map shares delegation.timeout_s."""
    if "{item}" not in task_template:
        raise ToolError("task_template must contain {item}")
    reports = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ctx.config.delegation.timeout_s
    for item in items:
        remaining = deadline - loop.time()
        if remaining <= 0:
            reports.append({"item": item, "status": "not_started", "timed_out": True})
            continue
        brief = task_template.replace("{item}", item)
        result = await delegate(ctx, brief, profile, None, timeout=remaining)
        payload = result._meta["receipt"]
        reports.append(
            {
                "item": item,
                "status": payload.get("status"),
                "report": payload.get("report"),
                "session_id": payload.get("session_id"),
                **({"timed_out": True} if payload.get("timed_out") else {}),
            }
        )
    return tool_json(ctx, {"results": reports}, "delegate")


async def delegate(ctx, task, profile, session, *, timeout=None) -> ToolResult:
    """Child sees its own history, not this conversation; the receipt carries
    child session/run IDs, status and only the final report on success."""
    if not task.strip():
        raise ToolError("Delegation requires a nonempty task")
    if session is not None and profile is None:
        # A continuation keeps the child's own profile.
        try:
            profile = (await durable(ctx.store.session, session)).default_profile
        except (FileNotFoundError, ValueError) as exc:
            raise ToolError("Child session not found") from exc
    profile = ctx.profile if profile is None else profile
    allowed = ctx.config.delegation.allowed_profiles
    if profile != ctx.profile and profile not in allowed:
        raise ToolError(
            f"Profile {profile[:40]!r} is not allowed for delegation. Omit profile "
            "to use the current one"
            + (f", or choose: {', '.join(allowed)}" if allowed else "")
            + "."
        )
    if ctx.nested_submit is None:
        raise ToolError("Nested execution is unavailable in this context")
    child = (
        Session(id=session)
        if session is not None
        else Session(
            parent_session=ctx.session.id,
            source="delegate",
            default_profile=profile,
            memory_scope="ephemeral",
            title=task.splitlines()[0][:200],
        )
    )
    child_run = None

    def started(run):
        nonlocal child_run
        child_run = run

    try:
        # Own the timeout so the receipt retains IDs and honest terminal status.
        # Generic tool_timeout_s must not shorten this bounded nested operation.
        async with asyncio.timeout(timeout or ctx.config.delegation.timeout_s):
            if session is not None:
                try:
                    child = await durable(ctx.store.session, session)
                except (FileNotFoundError, ValueError) as exc:
                    raise ToolError("Child session not found") from exc
                if (
                    child.parent_session != ctx.session.id
                    or child.archived_at is not None
                    or child.memory_scope != "ephemeral"
                    or child.bound_channel is not None
                ):
                    raise ToolError(
                        "Continue only your own unarchived ephemeral children"
                    )
            if session is None:
                await durable(ctx.store.create_session, child)
            child_run = await ctx.nested_submit(
                child.id,
                task,
                profile=profile,
                origin=Origin(
                    channel="delegate",
                    channel_id=ctx.session.id,
                    external_id=ctx.run_id,
                ),
                on_started=started,
            )
            if child_run.status == RunStatus.completed:
                evidence = await durable(ctx.store.run, child_run.id)
                report = next(
                    (
                        message.get("content") or ""
                        for message in reversed(evidence["messages"])
                        if message["role"] == "assistant"
                        and not message.get("tool_calls")
                    ),
                    "",
                )
                return receipt(child, child_run, report=report)
            return receipt(child, child_run)
    except TimeoutError:
        # submit_nested finalizes accepted children before propagating timeout.
        return receipt(child, child_run, timed_out=True)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
