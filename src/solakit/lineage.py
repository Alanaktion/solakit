"""Execution lineage survives callbacks and inherited asyncio task contexts."""

from contextvars import ContextVar

from solakit.records import ErrorCode, Origin, Run
from solakit.sdk import ToolError

current_run: ContextVar[Run | None] = ContextVar("sola_current_run", default=None)


def deny_autonomous_successor() -> None:
    run = current_run.get()
    if run is not None and (
        run.origin in (Origin.SCHEDULED, Origin.PROACTIVE, Origin.DELEGATED)
        or run.parent_tool_call_id is not None
    ):
        raise ToolError(
            "Autonomous work cannot create or change wake-ups or admit descendants",
            ErrorCode.TOOL_DENIED,
        )
