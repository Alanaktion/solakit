"""Read-only grounding in actual running services, independent of model guesses."""

import json

from solakit.tools import ToolContext, ToolResult, tool


@tool(name="runtime.status", profiles=["*"])
async def status(ctx: ToolContext) -> ToolResult:
    """Report live model, channels, jobs and tools granted to this profile."""
    return ToolResult(content=json.dumps(ctx.runtime(), ensure_ascii=False))
