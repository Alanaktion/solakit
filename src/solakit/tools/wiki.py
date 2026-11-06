"""Wikipedia REST summaries with Wikidata identity."""

from solakit.tools import ToolContext, ToolResult, tool
from solakit.tools._read_api import invoke


@tool(
    name="wiki.summary",
    profiles=["*"],
    summary="Read a Wikipedia summary and its Wikidata entity ID.",
    keywords=("wikipedia", "wikidata", "summary", "encyclopedia"),
)
async def summary(ctx: ToolContext, title: str, language: str = "en") -> ToolResult:
    """Read a MediaWiki REST summary plus Wikidata ID; not full Wikidata claims."""
    return await invoke(ctx, "summary", title=title, language=language)
