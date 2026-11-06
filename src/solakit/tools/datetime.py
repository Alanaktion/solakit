"""Exact calendar and timezone tools."""

import json

from solakit.tools import ToolContext, ToolResult, tool
from solakit.tools import _arithmetic_calendar as calendar


@tool(
    name="datetime.convert",
    profiles=["*"],
    summary="Convert timestamps between IANA time zones with DST.",
    keywords=("date", "time", "timezone", "convert", "dst"),
)
def convert(
    ctx: ToolContext,
    value: str,
    to_zone: str,
    from_zone: str | None = None,
    fold: int | None = None,
) -> ToolResult:
    """Convert ISO timestamps. Naive times require from_zone; DST repeats need fold."""
    return ToolResult(
        content=json.dumps(calendar.convert(value, to_zone, from_zone, fold))
    )


@tool(
    name="datetime.add_days",
    profiles=["*"],
    summary="Add or subtract calendar days from an ISO date.",
    keywords=("date", "days", "before", "after", "calendar"),
)
def add_days(ctx: ToolContext, value: str, days: int) -> ToolResult:
    """Add calendar days to YYYY-MM-DD; negative days compute dates before it."""
    return ToolResult(content=json.dumps(calendar.add_days(value, days)))


@tool(
    name="datetime.weekday",
    profiles=["*"],
    summary="Find the day of the week for an ISO date.",
    keywords=("date", "weekday", "calendar", "day of week"),
)
def weekday(ctx: ToolContext, value: str) -> ToolResult:
    """Return weekday name and ISO number (Monday=1) for YYYY-MM-DD."""
    return ToolResult(content=json.dumps(calendar.weekday(value)))
