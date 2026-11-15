"""Exact calendar and timezone tools."""

import json
from typing import Annotated
from zoneinfo import ZoneInfoNotFoundError

from pydantic import Field

from solakit.tools import ToolContext, ToolError, ToolResult, tool
from solakit.tools import _arithmetic_calendar as calendar


@tool(
    name="datetime.convert",
    profiles=["*"],
    summary="Convert timestamps between IANA time zones with DST.",
    keywords=("date", "time", "timezone", "convert", "dst"),
)
def convert(
    ctx: ToolContext,
    value: Annotated[
        str,
        Field(
            description=(
                "ISO timestamp with an offset, or a naive timestamp paired with "
                "from_zone."
            )
        ),
    ],
    to_zone: Annotated[
        str,
        Field(
            description="Destination IANA time zone, for example America/Denver or UTC."
        ),
    ],
    from_zone: Annotated[
        str | None,
        Field(
            description=(
                "IANA source zone for a naive timestamp; omit when the "
                "timestamp already has an offset."
            )
        ),
    ] = None,
    fold: Annotated[
        int | None,
        Field(
            description=(
                "For repeated DST local times, 0 selects the first occurrence "
                "and 1 the second."
            ),
            ge=0,
            le=1,
        ),
    ] = None,
) -> ToolResult:
    """Convert a time between time zones, e.g. what 15:00 in Tokyo is in London.

    ISO timestamps. Naive times require from_zone; DST repeats need fold."""
    try:
        result = calendar.convert(value, to_zone, from_zone, fold)
    except ZoneInfoNotFoundError as exc:
        zone = exc.args[0].removeprefix("No time zone found with key ")
        raise ToolError(
            f"Unknown time zone: {zone}; use an IANA name such as Europe/London"
        ) from None
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    return ToolResult(content=json.dumps(result))


@tool(
    name="datetime.add_days",
    profiles=["*"],
    summary="Add or subtract calendar days from an ISO date.",
    keywords=("date", "days", "before", "after", "calendar"),
)
def add_days(
    ctx: ToolContext,
    value: Annotated[str, Field(description="ISO calendar date in YYYY-MM-DD form.")],
    days: Annotated[
        int, Field(description="Calendar days to add; negative values subtract days.")
    ],
) -> ToolResult:
    """Find the date N calendar days before or after a YYYY-MM-DD date.

    Negative days count backwards. Use this instead of counting days mentally."""
    try:
        result = calendar.add_days(value, days)
    except (ValueError, OverflowError) as exc:
        raise ToolError(str(exc)) from None
    return ToolResult(content=json.dumps(result))


@tool(
    name="datetime.weekday",
    profiles=["*"],
    summary="Find the day of the week for an ISO date.",
    keywords=("date", "weekday", "calendar", "day of week"),
)
def weekday(
    ctx: ToolContext,
    value: Annotated[str, Field(description="ISO calendar date in YYYY-MM-DD form.")],
) -> ToolResult:
    """Find the weekday of a YYYY-MM-DD date instead of guessing it.

    Returns the weekday name and ISO number (Monday=1)."""
    try:
        result = calendar.weekday(value)
    except (ValueError, OverflowError) as exc:
        raise ToolError(str(exc)) from None
    return ToolResult(content=json.dumps(result))
