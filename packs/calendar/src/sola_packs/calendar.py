"""Read-only, bounded queries of operator-exported ICS files."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import recurring_ical_events
from icalendar import Calendar as ICS
from pydantic import Field, field_validator
from sola_packs.support import Settings, external, resource

from solakit.core.io import durable
from solakit.core.search import timestamp
from solakit.tools import ToolContext, ToolError, ToolResult, tool


class Calendar(Settings):
    file: Path
    timezone: str = "UTC"
    max_bytes: int = Field(default=1_000_000, ge=1, le=4_000_000)

    @field_validator("file", mode="before")
    @classmethod
    def absolute(cls, value):
        value = Path(value)
        if not value.is_absolute():
            raise ValueError("Calendar export must be an absolute path")
        return value

    @field_validator("timezone")
    @classmethod
    def zone(cls, value):
        ZoneInfo(value)
        return value


def instant(value, zone):
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=zone)).astimezone(UTC)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), zone).astimezone(UTC)
    raise ValueError("Invalid calendar date")


def query(content, cfg, start, end, limit):
    calendar = ICS.from_ical(content)
    components = calendar.walk()
    if len(components) > 1000:
        raise ToolError("Calendar export exceeds its component limit")
    for event in components:
        if event.errors:
            raise ToolError("Calendar export contains invalid properties")
        for rule_name in ("RRULE", "EXRULE"):
            rule = event.get(rule_name)
            if rule and (
                not isinstance(rule, dict)
                or rule.get("FREQ")
                not in (["DAILY"], ["WEEKLY"], ["MONTHLY"], ["YEARLY"])
                or any(str(key).startswith("BY") for key in rule)
            ):
                # Avoid adversarial sub-daily expansion. Unsupported rules fail
                # explicitly rather than quietly reporting an incomplete agenda.
                raise ToolError("Calendar recurrence requires an unsupported expansion")
    zone = ZoneInfo(cfg.timezone)
    events = recurring_ical_events.of(calendar).between(
        start.astimezone(zone), end.astimezone(zone)
    )
    results = []
    for event in events:
        if event.get("STATUS") == "CANCELLED":
            continue
        begin = event.decoded("DTSTART")
        finish = event.decoded("DTEND", None)
        if finish is None:
            duration = event.decoded("DURATION", None)
            finish = begin + (
                duration
                or (timedelta(0) if isinstance(begin, datetime) else timedelta(days=1))
            )
        begin_utc, finish_utc = instant(begin, zone), instant(finish, zone)
        if finish_utc < begin_utc:
            raise ToolError("Calendar event ends before it starts")
        if begin_utc >= end or (finish_utc <= start and begin_utc < start):
            continue
        results.append(
            {
                "uid": str(event.get("UID", "")),
                "start": begin_utc.isoformat(),
                "end": finish_utc.isoformat(),
                "all_day": not isinstance(begin, datetime),
                "summary": str(event.get("SUMMARY", "")),
                "location": str(event.get("LOCATION", "")),
                "description": str(event.get("DESCRIPTION", "")),
            }
        )
    results.sort(key=lambda event: (event["start"], event["uid"]))
    return {"events": results[:limit], "has_more": len(results) > limit}


@tool(
    summary="Read calendar events within a bounded date window",
    keywords=("calendar", "events", "agenda", "schedule"),
    name="calendar.events",
    profiles=["*"],
)
async def events(
    ctx: ToolContext,
    calendar: Annotated[
        str, Field(description="Configured calendar alias granted to this profile.")
    ],
    after: Annotated[
        str,
        Field(
            description=(
                "Inclusive ISO timestamp with UTC offset for the event window."
            )
        ),
    ],
    before: Annotated[
        str,
        Field(
            description=(
                "Exclusive ISO timestamp with UTC offset; at most 31 days after after."
            )
        ),
    ],
    limit: Annotated[
        int, Field(description="Maximum number of entries to return.")
    ] = 50,
) -> ToolResult:
    """Read an ICS agenda in [after, before), at most 31 days; use ISO timestamps."""
    cfg = resource(ctx, "calendars", calendar, Calendar, "read")
    try:
        start, end = (
            datetime.fromisoformat(timestamp(after)),
            datetime.fromisoformat(timestamp(before)),
        )
    except ValueError:
        raise ToolError(
            "Calendar bounds require ISO timestamps with a timezone"
        ) from None
    if not 1 <= limit <= 200 or not timedelta(0) < end - start <= timedelta(days=31):
        raise ToolError("Calendar requires 1–200 results and at most 31 days")
    try:
        text = await ctx.fs.read(cfg.file, max_chars=cfg.max_bytes)
        if not text.content_complete or len(text.content.encode()) > cfg.max_bytes:
            raise ToolError("Calendar export exceeds its byte limit")
        result = await durable(query, text.content, cfg, start, end, limit)
    except ToolError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ToolError("Calendar export is unavailable or invalid") from exc
    return ToolResult(content=external("calendar:" + calendar, result))
