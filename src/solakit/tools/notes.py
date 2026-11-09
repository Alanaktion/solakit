"""Small per-session facts the model keeps between turns (opt-in module)."""

import json
from typing import Annotated

from pydantic import Field

from solakit.core.io import durable
from solakit.core.schemas import now
from solakit.tools import ToolContext, ToolError, ToolResult, tool

KEY = Annotated[
    str,
    Field(
        pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$",
        description="Short lowercase key such as repo_path.",
    ),
]
MAX_NOTES = 50


@tool(
    summary="Save a small fact for this conversation (shown each turn)",
    keywords=("note", "remember", "save", "fact", "session"),
    name="notes.set",
    profiles=["*"],
)
async def set_note(
    ctx: ToolContext,
    key: KEY,
    value: Annotated[
        str | None,
        Field(max_length=500, description="Value; null deletes the note."),
    ],
) -> ToolResult:
    """Save or delete a short fact for this conversation only, such as a path or
    preference. Notes appear in each turn and survive compaction; they are not
    long-term memory."""
    notes = await durable(ctx.store.session_notes, ctx.session.id)
    if value is not None and key not in notes and len(notes) >= MAX_NOTES:
        raise ToolError(f"At most {MAX_NOTES} notes; delete one first.")
    await durable(
        ctx.store.log_session_note,
        ctx.session.id,
        {"key": key, "value": value, "run_id": ctx.run_id, "ts": now().isoformat()},
    )
    return ToolResult(content=f"{'Saved' if value is not None else 'Deleted'} {key}.")


@tool(
    summary="List this conversation's saved notes",
    keywords=("note", "list", "facts", "session"),
    name="notes.list",
    profiles=["*"],
)
async def list_notes(ctx: ToolContext) -> ToolResult:
    """All notes saved in this conversation."""
    notes = await durable(ctx.store.session_notes, ctx.session.id)
    values = {key: event["value"] for key, event in notes.items()}
    return ToolResult(content=json.dumps(values, ensure_ascii=False))
