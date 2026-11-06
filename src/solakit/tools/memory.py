"""Memory tools use only the session-scoped facade supplied by execution."""

import json
from typing import Annotated

from pydantic import Field

from solakit.core.memory import MemoryUnavailable
from solakit.tools import ToolContext, ToolError, ToolResult, tool

PROFILES = ["*"]


@tool(name="memory.search", profiles=PROFILES)
async def search(
    ctx: ToolContext,
    query: Annotated[
        str,
        Field(
            description=(
                "Semantic query about remembered facts, decisions or preferences."
            )
        ),
    ],
) -> ToolResult:
    """Search long-term semantic memory for facts, decisions and preferences.

    For exact conversation evidence use sessions.search. For current external
    data use its domain tool. Use returned canonical URIs with memory.read."""
    if ctx.memory_client is None:
        raise ToolError("Memory is disabled for this session")
    try:
        return ToolResult(content=await ctx.memory_client.search(query))
    except MemoryUnavailable as exc:
        raise ToolError(str(exc)) from exc


@tool(name="memory.read", profiles=PROFILES)
async def read(
    ctx: ToolContext,
    uri: Annotated[
        str,
        Field(
            description=(
                "Canonical URI returned by memory.search, inside "
                "configured shared roots."
            )
        ),
    ],
) -> ToolResult:
    """Read a canonical URI inside configured shared memory roots."""
    if ctx.memory_client is None:
        raise ToolError("Memory is disabled for this session")
    try:
        return ToolResult(content=await ctx.memory_client.read(uri))
    except MemoryUnavailable as exc:
        raise ToolError(str(exc)) from exc


@tool(name="memory.remember", profiles=PROFILES)
async def remember(
    ctx: ToolContext,
    text: Annotated[
        str,
        Field(
            description=(
                "Exact durable note to stage; staging does not confirm "
                "storage or extraction."
            )
        ),
    ],
) -> ToolResult:
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
