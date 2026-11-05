"""Memory tools use only the session-scoped facade supplied by execution."""

import json

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


@tool(name="memory.remember", profiles=PROFILES)
async def remember(ctx: ToolContext, text: str) -> ToolResult:
    """Stage an exact note for this run's normal shared-memory commit.

    Returns staged note/run IDs, not a claim that storage or extraction succeeded.
    Requires enabled shared memory. Empty or over-limit notes are refused whole;
    completion is tracked by the run's existing memory/extraction status.
    """
    if ctx.memory_client is None or ctx.session.memory_scope != "shared":
        raise ToolError("Memory notes require enabled shared memory")
    if not text.strip() or len(text) > ctx.config.memory.remember_max_chars:
        raise ToolError("Memory note is empty or exceeds remember_max_chars")
    if ctx.stage_memory is None:
        raise ToolError("Memory staging is unavailable in this context")
    try:
        note = await ctx.stage_memory(text)
    except MemoryUnavailable as exc:
        raise ToolError(str(exc)) from exc
    return ToolResult(
        content=json.dumps(
            {
                "status": "staged",
                "note_id": note.id,
                "run_id": note.run_id,
                "message": (
                    "Staged for this run's normal commit; not yet stored or extracted."
                ),
            }
        )
    )
