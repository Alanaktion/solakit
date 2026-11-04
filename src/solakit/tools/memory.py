"""Memory tools use only the session-scoped facade supplied by execution."""

from solakit.core.memory import MemoryUnavailable
from solakit.tools import ToolContext, ToolError, ToolResult, tool

PROFILES = ["*"]


@tool(name="memory.search", profiles=PROFILES)
async def search(ctx: ToolContext, query: str) -> ToolResult:
    """Search configured shared memory roots."""
    if ctx.memory_client is None:
        raise ToolError("Memory is disabled for this session")
    try:
        return ToolResult(content=await ctx.memory_client.search(query))
    except MemoryUnavailable as exc:
        raise ToolError(str(exc)) from exc


@tool(name="memory.read", profiles=PROFILES)
async def read(ctx: ToolContext, uri: str) -> ToolResult:
    """Read a canonical URI inside configured shared memory roots."""
    if ctx.memory_client is None:
        raise ToolError("Memory is disabled for this session")
    try:
        return ToolResult(content=await ctx.memory_client.read(uri))
    except MemoryUnavailable as exc:
        raise ToolError(str(exc)) from exc
