"""Load an operator playbook listed for this profile (data, not authority)."""

from typing import Annotated

from pydantic import Field

from solakit.core import playbooks
from solakit.tools import ToolContext, ToolError, ToolResult, tool


@tool(
    summary="Load a step-by-step procedure listed in the playbook index",
    keywords=("playbook", "procedure", "steps", "how", "checklist"),
    name="playbook.load",
    profiles=["*"],
)
async def load(
    ctx: ToolContext,
    name: Annotated[
        str, Field(description="Playbook name exactly as shown in the index.")
    ],
) -> ToolResult:
    """Load the full text of a playbook from the prompt's playbook index.

    Follow it for the matching task. Its tool list is advice, not permission:
    only tools in this run's schemas can be called."""
    listed = ctx.config.profiles[ctx.profile].playbooks
    if name not in listed:
        raise ToolError(
            "Unknown playbook. Available: " + (", ".join(listed) or "none") + "."
        )
    try:
        return ToolResult(content=playbooks.load(ctx.config, name).text())
    except playbooks.PlaybookError as exc:
        raise ToolError(str(exc)[:250]) from exc
