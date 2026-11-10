"""Create-only Astro blog/dream content and receipted, exact-file publication."""

import asyncio
import json
import os
import re
from datetime import date as calendar_date
from datetime import datetime, time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sola_packs.support import IDENTIFIER, external
from sola_packs.support.evidence import Operation

from solakit.sdk import (
    ToolContext,
    ToolError,
    ToolResult,
    durable,
    execute,
    repository_lock,
    sync_directory,
    tool,
)

SOLA_SDK = ">=1,<2"


DIRECTORIES = {"post": "src/content/blog", "dream": "src/content/dreams"}
COVERS = "src/assets/blog-images"
MAX_FILES = 2000
MAX_CONTENT = 1_000_000
TOTAL_TIMEOUT = 600


class Cover(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    workflow: str
    operation_id: str
    image_index: int = Field(default=0, ge=0, le=99)


def _slug(value):
    if len(value) > 100 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        raise ToolError("Slug must use lowercase letters, digits and single hyphens")
    return value


def _instant(ctx, value, *, historical=False):
    zone = ZoneInfo(ctx.config.timezone)
    try:
        if value is None:
            return datetime.now(zone)
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, calendar_date):
            parsed = datetime.combine(value, time(), zone)
        elif len(value) == 10:
            day = calendar_date.fromisoformat(value)
            if day.isoformat() != value:
                raise ValueError("noncanonical date")
            parsed = datetime.combine(day, time(), zone)
        else:
            parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            if not historical:
                raise ValueError("timezone required")
            parsed = parsed.replace(tzinfo=zone)
        return parsed
    except (ValueError, TypeError, AttributeError) as exc:
        raise ToolError(
            "Date must be YYYY-MM-DD or an ISO timestamp with timezone"
        ) from exc


def _path(ctx, git, relative, *, write=False):
    candidate = git.root / relative
    # Deny even in-repository symlink aliases: filenames must keep their identity.
    if candidate.is_symlink() or any(
        parent.is_symlink()
        for parent in candidate.parents
        if parent != git.root and parent.is_relative_to(git.root)
    ):
        raise ToolError("Blog paths must not use symlinks")
    target = ctx.path(str(candidate), write=write)
    if not target.is_relative_to(git.root):
        raise ToolError("Blog path escapes repository")
    return target


def _entry(kind, identifier):
    if kind not in DIRECTORIES:
        raise ToolError("Kind must be post or dream")
    if kind == "post":
        _slug(identifier)
    else:
        try:
            if calendar_date.fromisoformat(identifier).isoformat() != identifier:
                raise ValueError("noncanonical date")
        except ValueError as exc:
            raise ToolError("Dream identifier must be YYYY-MM-DD") from exc
    return f"{DIRECTORIES[kind]}/{identifier}.md"


def _frontmatter(text):
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ToolError("Entry has no YAML frontmatter")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ToolError("Frontmatter exceeds 16384 characters or is incomplete")
    try:
        value = yaml.safe_load("".join(lines[1:end]))
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        raise ToolError("Invalid YAML frontmatter") from exc
    if not isinstance(value, dict):
        raise ToolError("Frontmatter must be a mapping")
    return value


async def _metadata(ctx, target):
    result = await ctx.fs.read(target, max_chars=16384)
    return _frontmatter(result.content)


def _cover_path(ctx, git, metadata):
    reference = metadata.get("coverImage")
    if reference is None:
        return None
    if not isinstance(reference, str):
        raise ToolError("Cover must be a relative local image reference")
    prefix = "../../assets/blog-images/"
    name = reference.removeprefix(prefix)
    if (
        not reference.startswith(prefix)
        or Path(name).name != name
        or name in {"", ".", ".."}
    ):
        raise ToolError("Cover must name one file in src/assets/blog-images")
    relative = f"{COVERS}/{name}"
    target = _path(ctx, git, relative, write=True)
    if not target.is_file():
        raise ToolError("Referenced cover is missing")
    return relative


async def _list(ctx, repository, kind, limit, offset):
    if not 1 <= limit <= 100 or not 0 <= offset <= MAX_FILES:
        raise ToolError("Limit must be 1–100; offset must be 0–2000")
    git = ctx.git(repository)
    directory = _path(ctx, git, DIRECTORIES[kind])

    def names():
        if not directory.exists():
            return []
        found = []
        with os.scandir(directory) as children:
            for child in children:
                if child.name.endswith(".md") and child.is_file(follow_symlinks=False):
                    found.append(child.name)
                    if len(found) > MAX_FILES:
                        raise ToolError(
                            "Collection exceeds 2000 entries; use filesystem tools"
                        )
        return found

    entries = []
    for name in await durable(names):
        target = _path(ctx, git, f"{DIRECTORIES[kind]}/{name}")
        item = {"filename": name, "identifier": name[:-3]}
        try:
            metadata = await _metadata(ctx, target)
            instant = _instant(ctx, metadata.get("date"), historical=True)
            if metadata.get("date") is None:
                raise ToolError("Missing content date")
            item.update(
                date=instant.isoformat(), draft=metadata.get("draft", False) is True
            )
            if kind == "post":
                for key in ("title", "description"):
                    if isinstance(metadata.get(key), str):
                        item[key] = metadata[key][:2000]
                tags = metadata.get("tags", [])
                item["tags"] = (
                    [v[:100] for v in tags[:50] if isinstance(v, str)]
                    if isinstance(tags, list)
                    else []
                )
            else:
                alignment = metadata.get("align", "center")
                item["align"] = (
                    alignment if alignment in ("left", "center", "right") else "center"
                )
            timestamp = instant.timestamp()
        except ToolError as exc:
            item["metadata_error"] = str(exc)
            timestamp = float("-inf")
        entries.append((timestamp, name, item))
    entries.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    result = {
        "entries": [v[2] for v in entries[offset : offset + limit]],
        "more": offset + limit < len(entries),
        "total": len(entries),
    }
    return ToolResult(content=external(f"blog:{repository}:{kind}", result))


@tool(
    name="blog.list_posts",
    profiles=["*"],
    summary="List blog posts by content date, including drafts",
)
async def list_posts(
    ctx: ToolContext, repository: str, limit: int = 10, offset: int = 0
) -> ToolResult:
    """List bounded post metadata newest first; repository is a configured alias."""
    return await _list(ctx, repository, "post", limit, offset)


@tool(
    name="blog.list_dreams",
    profiles=["*"],
    summary="List dreams by content date, including drafts",
)
async def list_dreams(
    ctx: ToolContext, repository: str, limit: int = 10, offset: int = 0
) -> ToolResult:
    """List bounded dream metadata newest first; repository is a configured alias."""
    return await _list(ctx, repository, "dream", limit, offset)


async def _read(ctx, repository, kind, identifier):
    git = ctx.git(repository)
    target = _path(ctx, git, _entry(kind, identifier))
    result = await ctx.fs.read(target)
    return ToolResult(
        content=external(f"blog:{repository}:{kind}:{identifier}", result.content),
        content_complete=result.content_complete,
    )


@tool(
    name="blog.read_post",
    profiles=["*"],
    summary="Read a blog post's literal Markdown and frontmatter",
)
async def read_post(ctx: ToolContext, repository: str, slug: str) -> ToolResult:
    """Read one post by its filename slug, with explicitly bounded output."""
    return await _read(ctx, repository, "post", slug)


@tool(
    name="blog.read_dream",
    profiles=["*"],
    summary="Read a dream's literal Markdown and frontmatter",
)
async def read_dream(ctx: ToolContext, repository: str, date: str) -> ToolResult:
    """Read one dream by its YYYY-MM-DD filename date, with bounded output."""
    return await _read(ctx, repository, "dream", date)


async def _build(ctx, git, operation):
    operation.record("building")
    result = await execute(
        ["npm", "run", "build"],
        git.root,
        min(300, ctx.config.profiles[ctx.profile].tool_timeout_s),
        16000,
    )
    # Diagnostics remain private evidence, masked before persistence.
    operation.record("built", build=ctx.store.redactor.redact(result))
    if result["exit_code"] != 0 or result["timed_out"]:
        raise ToolError(
            "Blog build failed; files remain locally, no commit or push performed"
        )


async def _finish(ctx, git, transaction, operation, paths, kind, identifier):
    await _build(ctx, git, operation)
    if not (await git.changed_paths()).issubset(set(paths)):
        raise ToolError(
            "Unrelated worktree changes appeared; inspect before publishing"
        )
    operation.record("committing", paths=paths)
    commit = await transaction.commit(paths, f"Publish {kind}: {identifier}")
    operation.record("committed", commit=commit, paths=paths)
    operation.record("pushing", commit=commit, paths=paths)
    await transaction.push(commit)
    return {"status": "published", "commit": commit, "paths": paths}


def _operation(ctx, git, operation_id, request):
    if not IDENTIFIER.fullmatch(operation_id):
        raise ToolError("A stable operation ID is required")
    return Operation(ctx, "blog", [str(git.root), operation_id], request)


def _previous(operation):
    if operation.current:
        if operation.current["stage"] == "completed":
            return ToolResult(content=json.dumps(operation.current["result"]))
        raise ToolError(
            "Blog operation failed, interrupted or uncertain; inspect receipts "
            "and repository before a new operation"
        )
    return None


def _failure(operation):
    previous = operation.current or {}
    # Keep commit identity even if cancellation interrupted push.
    operation.record(
        "interrupted_or_failed",
        previous_stage=previous.get("stage"),
        commit=previous.get("commit"),
    )


def _text(value, name, limit):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or "\x00" in value
    ):
        raise ToolError(f"{name} must contain 1–{limit} characters without NUL")


async def _write(ctx, repository, operation_id, kind, data, publish):
    git = ctx.git(repository)
    request = {"action": "write", "kind": kind, "data": data, "publish": publish}
    async with asyncio.timeout(TOTAL_TIMEOUT):
        with _operation(ctx, git, operation_id, request) as operation:
            prior = _previous(operation)
            if prior:
                return prior
            operation.record("prepared")
            try:
                if publish:
                    async with git.publication() as transaction:
                        if await git.changed_paths():
                            raise ToolError(
                                "Writing and publishing requires a clean worktree"
                            )
                        paths, identifier = await _create(
                            ctx, git, kind, data, operation
                        )
                        result = await _finish(
                            ctx, git, transaction, operation, paths, kind, identifier
                        )
                else:
                    async with repository_lock(git.root):
                        paths, identifier = await _create(
                            ctx, git, kind, data, operation
                        )
                        result = {"status": "saved", "paths": paths}
                operation.record("completed", result=result)
                return ToolResult(
                    content=json.dumps(result),
                    artifacts=[str(git.root / p) for p in paths],
                )
            except BaseException:
                _failure(operation)
                raise


async def _create(ctx, git, kind, data, operation):
    instant = _instant(ctx, data["date"])
    identifier = data["slug"] if kind == "post" else instant.date().isoformat()
    relative = _entry(kind, identifier)
    target = _path(ctx, git, relative, write=True)
    if target.exists():
        raise ToolError("Entry already exists; it will not be overwritten")
    _text(data["body"], "Body", MAX_CONTENT)
    metadata = {"date": instant.isoformat()}
    paths = [relative]
    cover_data = None
    if kind == "post":
        _text(data["title"], "Title", 300)
        _text(data["description"], "Description", 2000)
        tags = data["tags"]
        if len(tags) > 50 or any(
            not isinstance(v, str) or not v.strip() or len(v) > 100 or "\x00" in v
            for v in tags
        ):
            raise ToolError(
                "Tags must be at most 50 nonempty strings of at most 100 characters"
            )
        metadata = {
            "title": data["title"],
            "description": data["description"],
            **metadata,
            "draft": data["draft"],
            "tags": tags,
        }
        if data["cover"] is not None:
            cover = Cover.model_validate(data["cover"])
            cover_relative = f"{COVERS}/{identifier}-cover.png"
            cover_target = _path(ctx, git, cover_relative, write=True)
            if cover_target.exists():
                raise ToolError("Cover already exists; it will not be overwritten")
            try:
                from sola_packs.image import completed_png
            except ImportError as exc:
                raise ToolError(
                    "Install solakit-packs-blog[image] for generated covers"
                ) from exc
            cover_data = await completed_png(
                ctx, cover.workflow, cover.operation_id, cover.image_index
            )
            metadata["coverImage"] = f"../../assets/blog-images/{identifier}-cover.png"
            paths.append(cover_relative)
    elif data["align"] is not None:
        if data["align"] not in ("left", "center", "right"):
            raise ToolError("Alignment must be left, center or right")
        metadata["align"] = data["align"]
    content = (
        "---\n"
        + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + data["body"]
    )
    operation.record("creating", paths=paths)
    if cover_data is not None:
        await ctx.fs.mkdir(cover_target.parent)

        def write_cover():
            actual = _path(ctx, git, paths[1], write=True)
            with actual.open("xb") as stream:
                stream.write(cover_data)
                stream.flush()
                os.fsync(stream.fileno())
            sync_directory(actual.parent)

        await durable(write_cover)
    await ctx.fs.mkdir(target.parent)
    await ctx.fs.write(target, content)
    operation.record("created", paths=paths)
    return paths, identifier


@tool(
    name="blog.write_post",
    profiles=["*"],
    writes=["filesystem"],
    manages_timeout=True,
    summary="Create a blog post, build, commit and push by default",
)
async def write_post(
    ctx: ToolContext,
    repository: str,
    operation_id: str,
    slug: str,
    title: str,
    description: str,
    body: str,
    date: str | None = None,
    tags: list[str] = [],  # noqa: B006 - immutable tool schema default
    draft: bool = False,
    cover: Cover | None = None,
    publish: bool = True,
) -> ToolResult:
    """Create a new Astro post; publish=False saves only. Never overwrite.

    Cover references a completed image operation (workflow, operation_id, image_index).
    Dates accept YYYY-MM-DD or a timestamp with timezone; omit for local now.
    """
    data = {
        "slug": slug,
        "title": title,
        "description": description,
        "body": body,
        "date": date,
        "tags": tags,
        "draft": draft,
        "cover": cover.model_dump() if isinstance(cover, Cover) else cover,
    }
    return await _write(ctx, repository, operation_id, "post", data, publish)


@tool(
    name="blog.write_dream",
    profiles=["*"],
    writes=["filesystem"],
    manages_timeout=True,
    summary="Create a dated dream, build, commit and push by default",
)
async def write_dream(
    ctx: ToolContext,
    repository: str,
    operation_id: str,
    body: str,
    date: str | None = None,
    align: Literal["left", "center", "right"] | None = None,
    publish: bool = True,
) -> ToolResult:
    """Create one dream per date with optional alignment; publish=False saves only.

    Dates accept YYYY-MM-DD or a timestamp with timezone; omit for local now.
    """
    return await _write(
        ctx,
        repository,
        operation_id,
        "dream",
        {"body": body, "date": date, "align": align},
        publish,
    )


@tool(
    name="blog.publish",
    profiles=["*"],
    writes=["filesystem"],
    effect="push",
    manages_timeout=True,
    summary="Build, commit and push one saved post or dream and its cover",
)
async def publish(
    ctx: ToolContext,
    repository: str,
    operation_id: str,
    kind: Literal["post", "dream"],
    identifier: str,
) -> ToolResult:
    """Publish a saved entry by post slug or dream YYYY-MM-DD."""
    git = ctx.git(repository)
    relative = _entry(kind, identifier)
    request = {"action": "publish", "kind": kind, "identifier": identifier}
    async with asyncio.timeout(TOTAL_TIMEOUT):
        with _operation(ctx, git, operation_id, request) as operation:
            prior = _previous(operation)
            if prior:
                return prior
            operation.record("prepared")
            try:
                async with git.publication() as transaction:
                    target = _path(ctx, git, relative, write=True)
                    metadata = await _metadata(ctx, target)
                    paths = [relative]
                    if kind == "post":
                        cover = _cover_path(ctx, git, metadata)
                        if cover:
                            paths.append(cover)
                    if not (await git.changed_paths()).issubset(set(paths)):
                        raise ToolError(
                            "Only the selected entry and cover may have local changes"
                        )
                    operation.record("selected", paths=paths)
                    result = await _finish(
                        ctx, git, transaction, operation, paths, kind, identifier
                    )
                    operation.record("completed", result=result)
                    return ToolResult(content=json.dumps(result))
            except BaseException:
                _failure(operation)
                raise
