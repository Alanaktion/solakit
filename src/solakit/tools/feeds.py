"""Stateless RSS and Atom reads."""

from typing import Annotated

from pydantic import Field

from solakit.tools import ToolContext, ToolResult, tool
from solakit.tools._read_api import invoke


@tool(
    name="feeds.check",
    profiles=["*"],
    summary="Read recent entries from an RSS or Atom feed.",
    keywords=("rss", "atom", "feed", "news", "updates"),
)
async def check(
    ctx: ToolContext,
    url: Annotated[
        str,
        Field(
            description=(
                "Public HTTP(S) URL; credentials, private addresses and "
                "nonstandard ports are refused."
            )
        ),
    ],
    limit: Annotated[
        int, Field(description="Maximum number of entries to return.", ge=1, le=100)
    ] = 20,
    if_none_match: Annotated[
        str | None,
        Field(
            description=(
                "Previously returned ETag, verbatim; omit for an unconditional "
                "feed read."
            )
        ),
    ] = None,
    if_modified_since: Annotated[
        str | None,
        Field(
            description=(
                "Previously returned Last-Modified header, verbatim; omit for "
                "an unconditional read."
            )
        ),
    ] = None,
) -> ToolResult:
    """Read up to 100 untrusted feed entries; retain IDs to track updates."""
    return await invoke(
        ctx,
        "check_feed",
        url=url,
        limit=limit,
        if_none_match=if_none_match,
        if_modified_since=if_modified_since,
    )
