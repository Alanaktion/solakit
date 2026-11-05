"""Serial delegated work with private transcripts and report-only results."""

import asyncio
import json
from typing import Annotated

from pydantic import Field

from solakit.core.io import durable
from solakit.core.schemas import Origin, RunStatus, Session
from solakit.tools import ToolContext, ToolError, ToolResult, tool


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
    return ToolResult(
        content='<external-content source="delegate" encoding="json">\n'
        + content
        + "\n</external-content>",
        error=timed_out or run is None or run.status != RunStatus.completed,
    )


@tool(name="delegate.run", profiles=["*"], manages_timeout=True)
async def run(
    ctx: ToolContext,
    task: Annotated[str, Field(min_length=1, max_length=32000)],
    profile: str,
    session: str | None = None,
) -> ToolResult:
    """Run a self-contained brief in a fresh ephemeral child session.

    Only delegation.allowed_profiles may be used. Supply session to continue an
    idle, unarchived child of this conversation. The child sees its own history,
    not this conversation; return includes child session/run IDs, status and only
    its final report on success. Full child work remains in its own transcript.
    """
    if not task.strip():
        raise ToolError("Delegation requires a nonempty task")
    if profile not in ctx.config.delegation.allowed_profiles:
        raise ToolError("Profile is not allowed for delegation")
    if ctx.nested_submit is None:
        raise ToolError("Nested execution is unavailable in this context")
    child = (
        Session(id=session)
        if session is not None
        else Session(
            parent_session=ctx.session.id,
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
        async with asyncio.timeout(ctx.config.delegation.timeout_s):
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
