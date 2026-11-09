"""A tiny in-run checklist persisted as append-only run evidence."""

import json
from typing import Annotated, Literal

from pydantic import Field

from solakit.core.io import durable
from solakit.core.store import plan_state
from solakit.tools import ToolContext, ToolError, ToolResult, tool

MAX_ITEMS = 20


async def _show(ctx: ToolContext) -> ToolResult:
    items = plan_state(await durable(ctx.store.plan_events, ctx.run_id))
    open_items = sum(item["status"] in ("todo", "doing") for item in items)
    return ToolResult(
        content=json.dumps({"items": items, "open": open_items}, ensure_ascii=False)
    )


@tool(
    summary="Write a checklist for a multi-step task (replaces any plan)",
    keywords=("plan", "checklist", "steps", "todo"),
    name="plan.set",
    profiles=["*"],
)
async def set_plan(
    ctx: ToolContext,
    items: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=200)]],
        Field(
            min_length=1,
            max_length=MAX_ITEMS,
            description="Ordered steps, one short sentence each.",
        ),
    ],
) -> ToolResult:
    """Record a checklist for a task with 3+ distinct steps; returns the plan.

    Update items with plan.update as you go. Skip this for simple requests."""
    await durable(ctx.store.log_plan, ctx.run_id, {"op": "set", "items": items})
    return await _show(ctx)


@tool(
    summary="Mark a checklist item doing, done or skipped",
    keywords=("plan", "checklist", "progress", "done"),
    name="plan.update",
    profiles=["*"],
)
async def update(
    ctx: ToolContext,
    id: Annotated[int, Field(ge=1, description="Item id from the plan.")],
    status: Annotated[
        Literal["todo", "doing", "done", "skipped"],
        Field(description="New status."),
    ],
    reason: Annotated[
        str | None,
        Field(max_length=200, description="Short reason, required for skipped."),
    ] = None,
) -> ToolResult:
    """Update one checklist item; returns the whole current plan."""
    items = plan_state(await durable(ctx.store.plan_events, ctx.run_id))
    if not any(item["id"] == id for item in items):
        raise ToolError("Unknown plan item id. Call plan.show to see the ids.")
    if status == "skipped" and not reason:
        raise ToolError("Give a short reason when skipping an item.")
    await durable(
        ctx.store.log_plan,
        ctx.run_id,
        {"op": "update", "id": id, "status": status, "reason": reason},
    )
    return await _show(ctx)


@tool(
    summary="Show the current checklist",
    keywords=("plan", "checklist", "status"),
    name="plan.show",
    profiles=["*"],
)
async def show(ctx: ToolContext) -> ToolResult:
    """Show the current checklist with statuses."""
    return await _show(ctx)
