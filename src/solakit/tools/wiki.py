"""Wikipedia REST summaries with Wikidata identity."""

from typing import Annotated

from pydantic import Field

from solakit.tools import ToolContext, ToolResult, tool
from solakit.tools._read_api import invoke


@tool(
    name="wiki.summary",
    profiles=["*"],
    summary="Read a Wikipedia summary and its Wikidata entity ID.",
    keywords=("wikipedia", "wikidata", "summary", "encyclopedia"),
)
async def summary(
    ctx: ToolContext,
    title: Annotated[
        str,
        Field(description="Wikipedia article title, not a URL; spaces are allowed."),
    ],
    language: Annotated[
        str,
        Field(
            description=(
                "Wikipedia language code, for example en or zh-min-nan; not a "
                "locale name."
            )
        ),
    ] = "en",
) -> ToolResult:
    """Read a MediaWiki REST summary plus Wikidata ID; not full Wikidata claims."""
    return await invoke(ctx, "summary", title=title, language=language)
