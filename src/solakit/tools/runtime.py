"""Read-only grounding in actual running services, independent of model guesses."""

import json

from solakit.tools import ToolContext, ToolResult, tool


@tool(
    summary="Inspect runtime health, queues and current tool grants",
    keywords=("runtime", "health", "status", "queues"),
    name="runtime.status",
    profiles=["*"],
)
async def status(ctx: ToolContext) -> ToolResult:
    """Report live model, channels, jobs and tools granted to this profile.

    Diagnostic status only; use domain discovery tools for pack resource aliases."""
    return ToolResult(content=json.dumps(ctx.runtime(), ensure_ascii=False))
