"""Memory tools use only the session-scoped facade supplied by execution."""

import json
from typing import Annotated

from pydantic import Field

from solakit.core.memory import MemoryUnavailable
from solakit.tools import ToolContext, ToolError, ToolResult, tool

PROFILES = ["*"]


@tool(
    summary="Semantic recall: facts, decisions, preferences",
    keywords=("recall", "semantic", "facts", "preferences"),
    name="memory.search",
    profiles=PROFILES,
    next_hint="memory.read(uri=...) for a full record",
)
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
        raise ToolError("Memory is disabled for this session; answer without it")
    try:
        return ToolResult(content=await ctx.memory_client.search(query))
    except MemoryUnavailable as exc:
        raise ToolError(str(exc)) from exc


@tool(
    summary="Read a memory resource by URI",
    keywords=("memory", "resource", "uri"),
    name="memory.read",
    profiles=PROFILES,
)
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
    """Open the full memory behind a memory.search result by its canonical URI.

    Only URIs inside configured shared memory roots are readable."""
    if ctx.memory_client is None:
        raise ToolError("Memory is disabled for this session; answer without it")
    try:
        return ToolResult(content=await ctx.memory_client.read(uri))
    except MemoryUnavailable as exc:
        raise ToolError(str(exc)) from exc


@tool(
    summary="Stage an exact note for persistent memory",
    keywords=("remember", "note", "save"),
    name="memory.remember",
    profiles=PROFILES,
)
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
    """Keep a durable fact, decision or preference for future conversations.

    Not for one-offs, secrets or facts only this chat needs (use notes.set).
    Stages the note for this run's shared-memory commit and returns staged note/run
    IDs, not a claim that storage or extraction succeeded. Requires enabled shared
    memory. Empty or over-limit notes are refused whole."""
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
