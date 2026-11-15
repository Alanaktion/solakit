"""Wikipedia REST summaries with Wikidata identity."""

from typing import Annotated

from pydantic import Field

from solakit.tools import ToolContext, ToolResult, tool
from solakit.tools._read_api import invoke


@tool(
    name="wiki.summary",
    profiles=["*"],
    summary="Quick Wikipedia overview of a topic, plus its Wikidata ID",
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
    """Get a quick encyclopedic overview of a person, place or topic from Wikipedia.

    Returns the article summary plus its Wikidata ID, not full Wikidata claims;
    use web.get_page for the whole article."""
    return await invoke(ctx, "summary", title=title, language=language)
