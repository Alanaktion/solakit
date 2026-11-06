"""Dated collections: one external pack for posts, dreams, poems or other entries."""

import fnmatch
import json
import re
from datetime import date as calendar_date
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from solakit.tools import ToolContext, ToolError, ToolResult, tool

DEFAULT_LAYOUT = "---\ntitle: {title}\ndate: {date}\n---\n\n{body}\n"


def filename_pattern(resource):
    return resource.config.filename.format(date="????-??-??", slug="*")


@tool(
    name="journal.create",
    profiles=["*"],
    writes=["filesystem"],
)
async def create(
    ctx: ToolContext,
    collection: str,
    slug: str,
    title: str,
    body: str,
    date: str | None = None,
) -> ToolResult:
    """Create one dated entry from a configured collection layout; never overwrite."""
    resource = ctx.collection(collection)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or len(slug) > 100:
        raise ToolError("Slug must use lowercase letters, digits and single hyphens")
    if not title.strip() or len(title) > 300:
        raise ToolError("Title must be nonempty and at most 300 characters")
    day = date or datetime.now(ZoneInfo(ctx.config.timezone)).date().isoformat()
    try:
        if calendar_date.fromisoformat(day).isoformat() != day:
            raise ValueError("noncanonical date")
    except ValueError as exc:
        raise ToolError("Date must be YYYY-MM-DD") from exc
    filename = resource.config.filename.format(date=day, slug=slug)
    target = resource.path(filename, write=True)
    layout = (
        DEFAULT_LAYOUT if resource.config.layout is None else resource.config.layout
    )
    text = layout.format(
        date=day,
        slug=slug,
        title=json.dumps(title, ensure_ascii=False),
        body=body,
    )
    await ctx.fs.mkdir(resource.root)
    target = await ctx.fs.write(target, text)
    return ToolResult(content=str(target), artifacts=[str(target)])


@tool(name="journal.list_recent", profiles=["*"])
async def list_recent(ctx: ToolContext, collection: str, limit: int = 10) -> ToolResult:
    """List matching entries ordered by modification time, newest first."""
    resource = ctx.collection(collection)
    if not resource.root.exists():
        return ToolResult(
            content=json.dumps({"collection": collection, "entries": [], "more": False})
        )
    entries, more = await ctx.fs.entries(
        resource.root,
        limit,
        newest=True,
        pattern=filename_pattern(resource),
    )
    return ToolResult(
        content=json.dumps({"collection": collection, "entries": entries, "more": more})
    )


@tool(name="journal.publish", profiles=["*"], writes=["filesystem"], effect="push")
async def publish(
    ctx: ToolContext,
    collection: str,
    paths: list[str],
    message: str,
) -> ToolResult:
    """Commit and push named collection entries; requires write and git.push grants."""
    resource = ctx.collection(collection)
    checked = []
    for name in paths:
        if Path(name).name != name or not fnmatch.fnmatchcase(
            name, filename_pattern(resource)
        ):
            raise ToolError("Publish requires matching collection entry filenames")
        resource.path(name, write=True)
        checked.append(str(resource.config.directory / name))
    return await resource.git().commit_push(checked, message)
