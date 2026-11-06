"""Stateless RSS and Atom reads."""

from solakit.tools import ToolContext, ToolResult, tool
from solakit.tools._read_api import invoke


@tool(
    name="feeds.check",
    profiles=["*"],
    summary="Read recent entries from an RSS or Atom feed.",
    keywords=("rss", "atom", "feed", "news", "updates"),
)
async def check(ctx: ToolContext, url: str, limit: int = 20) -> ToolResult:
    """Read up to 100 untrusted feed entries; retain IDs to track updates."""
    return await invoke(ctx, "check_feed", url=url, limit=limit)
