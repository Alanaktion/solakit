"""Fixed Markdown collections and build/commit/push workflow for the configured blog."""

import asyncio
import json
import os
import signal
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import JsonValue

from solakit.config import checkout_reserved
from solakit.files import MAX_FILE_BYTES, GuardedFiles
from solakit.images import MAX_IMAGE_BYTES, validate_png
from solakit.records import ErrorCode
from solakit.sdk import ToolArguments, ToolError
from solakit.tools import BlogCreate, ContentPage, ContentTarget, DreamCreate

CONTENT_PATHS = (
    "src/content/blog/*.md",
    "src/content/dreams/*.md",
    "src/assets/blog-images/",
)


def content_date(value: str | date | None, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    try:
        if value is None:
            return datetime.now(zone)
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            value = datetime.combine(value, time())
        return value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)
    except ValueError, TypeError:
        raise ToolError(
            "Content date must be an ISO date or timestamp", ErrorCode.INVALID_INPUT
        ) from None


def frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        raise ToolError("Content is missing YAML frontmatter", ErrorCode.INVALID_INPUT)
    for index, line in enumerate(lines[1:], 1):
        if line.rstrip() == "---":
            try:
                metadata = yaml.safe_load("".join(lines[1:index]))
                if not isinstance(metadata, dict) or any(
                    not isinstance(key, str) for key in metadata
                ):
                    raise ValueError
                json.dumps(metadata, default=str, allow_nan=False)
            except yaml.YAMLError, ValueError, TypeError, RecursionError:
                raise ToolError("Invalid YAML frontmatter", ErrorCode.INVALID_INPUT) from None
            return metadata, "".join(lines[index + 1 :])
    raise ToolError("Content is missing its frontmatter delimiter", ErrorCode.INVALID_INPUT)


def markdown(metadata: dict[str, Any], body: str) -> str:
    return "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n" + body


class BlogService:
    @staticmethod
    def repository(files: GuardedFiles, *, write: bool) -> Path:
        profile = files.authority("files.write" if write else "files.read")
        root = profile.blog_repository
        if root is None:
            raise ToolError("Configure blog.repository before using blog or dream tools")
        if write and checkout_reserved(root, profile.git_reserved):
            raise ToolError("Checkout is reserved for letters", ErrorCode.TOOL_DENIED)
        if not any(root.is_relative_to(parent) for parent in profile.workspace_roots):
            raise ToolError("Blog repository is outside granted roots", ErrorCode.TOOL_DENIED)
        # Check the checkout through the same descriptor traversal and protected-path rules.
        with files._parent(str(root / "package.json"), write=write) as (parent, name):
            if write:
                files._regular(os.stat(name, dir_fd=parent, follow_symlinks=False))
        return root

    async def execute(
        self,
        action: str,
        args: ToolArguments,
        files: GuardedFiles,
        lock: asyncio.Lock,
        *,
        cover: bytes | None = None,
    ) -> JsonValue:
        async with lock:
            write = action.endswith((".create", ".publish"))
            root = self.repository(files, write=write)
            timezone = files.authority("").background.timezone
            collection = "dreams" if action.startswith("dreams.") else "blog"
            directory = root / "src/content" / collection
            if isinstance(args, ContentPage):
                return self.list_entries(directory, args, files, timezone)
            if isinstance(args, ContentTarget) and not isinstance(args, BlogCreate):
                path = directory / f"{args.slug}.md"
                metadata, body = frontmatter(files.read_text(str(path)))
                if action.endswith(".read"):
                    return {
                        "slug": args.slug,
                        "metadata": json.loads(json.dumps(metadata, default=str)),
                        "body": body,
                    }
                metadata["draft"] = False
                files.write_text(str(path), markdown(metadata, body), overwrite=True)
                return await self.publish(root, path, files)
            if not isinstance(args, DreamCreate):
                raise ToolError("Unsupported content operation", ErrorCode.INVALID_INPUT)
            if not args.body.strip():
                raise ToolError("Content body must contain text", ErrorCode.INVALID_INPUT)
            at = content_date(args.date, timezone)
            slug = args.slug if isinstance(args, BlogCreate) else at.date().isoformat()
            path = directory / f"{slug}.md"
            # Reject duplicates before copying a cover; failed dream publication is manual recovery.
            with files._parent(str(path), write=True) as (parent, name):
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ToolError(f"Content already exists: {path}", ErrorCode.INVALID_INPUT)
            metadata = {"date": at.isoformat()}
            if isinstance(args, BlogCreate):
                if (
                    not args.title.strip()
                    or not args.description.strip()
                    or any(not tag.strip() for tag in args.tags)
                ):
                    raise ToolError(
                        "Blog title, description, and tags must contain text",
                        ErrorCode.INVALID_INPUT,
                    )
                if args.cover_path is not None and args.cover_artifact_id is not None:
                    raise ToolError(
                        "Choose cover_path or cover_artifact_id", ErrorCode.INVALID_INPUT
                    )
                metadata = {
                    "title": args.title,
                    "description": args.description,
                    "date": at.isoformat(),
                    "draft": not args.auto_publish,
                    "tags": list(args.tags),
                }
                if args.cover_path is not None:
                    cover = files.read_bytes(args.cover_path, limit=MAX_IMAGE_BYTES)
                if cover is not None:
                    validate_png(cover, int.from_bytes(cover[16:20]), int.from_bytes(cover[20:24]))
                    image = root / "src/assets/blog-images" / f"{slug}-cover.png"
                    files.write_bytes(str(image), cover, limit=MAX_IMAGE_BYTES)
                    metadata["coverImage"] = f"../../assets/blog-images/{image.name}"
            elif not 3 <= sum(bool(line.strip()) for line in args.body.splitlines()) <= 15:
                raise ToolError("Dreams require 3–15 nonempty lines", ErrorCode.INVALID_INPUT)
            try:
                files.write_text(str(path), markdown(metadata, "\n" + args.body.rstrip() + "\n"))
            except ToolError as error:
                raise ToolError(
                    f"Create failed for {path}; inspect any saved cover: {error}", error.code
                ) from None
            if isinstance(args, BlogCreate) and not args.auto_publish:
                return {"slug": slug, "path": str(path), "draft": True, "published": False}
            return await self.publish(root, path, files)

    @staticmethod
    def list_entries(
        directory: Path, args: ContentPage, files: GuardedFiles, timezone: str
    ) -> JsonValue:
        entries: list[tuple[float, str, dict[str, JsonValue]]] = []
        total = 0
        with files._parent(str(directory / ".listing"), write=False) as (parent, _):
            with os.scandir(parent) as children:
                for index, child in enumerate(children):
                    if index >= 2000:
                        raise ToolError(
                            "Content listing exceeds 2000 entries", ErrorCode.BUDGET_EXCEEDED
                        )
                    if not child.name.endswith(".md") or child.is_symlink():
                        continue
                    total += child.stat(follow_symlinks=False).st_size
                    if total > 16 * MAX_FILE_BYTES:
                        raise ToolError("Content listing exceeds 16 MiB", ErrorCode.BUDGET_EXCEEDED)
                    metadata, _ = frontmatter(files.read_text(str(directory / child.name)))
                    at = content_date(metadata.get("date"), timezone)
                    item: dict[str, JsonValue] = {
                        "slug": Path(child.name).stem,
                        "date": at.isoformat(),
                        "draft": metadata.get("draft", False),
                    }
                    if "title" in metadata:
                        item["title"] = metadata["title"]
                    entries.append((at.timestamp(), child.name, item))
        entries.sort(key=lambda item: (item[0], item[1]), reverse=True)
        end = args.offset + args.limit
        return {
            "entries": [item[2] for item in entries[args.offset : end]],
            "next_offset": end if end < len(entries) else None,
        }

    async def command(
        self, root: Path, files: GuardedFiles, *argv: str, capture: bool = False
    ) -> tuple[int, str]:
        self.repository(files, write=True)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=root,
                env=os.environ | {"GIT_TERMINAL_PROMPT": "0"},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError:
            raise ToolError(
                f"Cannot start {argv[0]}; install it and check the server PATH"
            ) from None
        assert process.stdout is not None
        output = bytearray()
        try:
            while chunk := await process.stdout.read(65536):
                output.extend(chunk)
                if capture and len(output) > MAX_FILE_BYTES:
                    raise ToolError("Git path listing exceeds 1 MiB", ErrorCode.BUDGET_EXCEEDED)
                if not capture and len(output) > 4096:
                    del output[:-4096]
            code = await process.wait()
        finally:
            # Include grandchildren (npm/Astro/Git hooks), and always reap the owned process.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        return code, output.decode("utf-8", errors="strict" if capture else "replace")

    async def publish(self, root: Path, path: Path, files: GuardedFiles) -> JsonValue:
        phase, commit = "repository", None
        try:
            code, output = await self.command(root, files, "git", "rev-parse", "--show-toplevel")
            if code or Path(output.strip()) != root:
                raise ToolError("Configured blog directory must be the Git checkout root")
            phase = "build"
            code, output = await self.command(root, files, "npm", "run", "build")
            if code:
                raise ToolError(f"Build failed: {output[-600:]}")
            phase = "stage"
            files.begin_effect(())
            code, _ = await self.command(
                root, files, "git", "add", "--", str(path.relative_to(root))
            )
            if code:
                raise ToolError("Cannot stage selected content; check ignored paths")
            # List exact changed paths: Git treats an unmatched wildcard pathspec as an error.
            code, output = await self.command(
                root,
                files,
                "git",
                "ls-files",
                "--modified",
                "--deleted",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                *CONTENT_PATHS,
                capture=True,
            )
            if code:
                raise ToolError("Cannot inspect pending content")
            paths = tuple(dict.fromkeys(output.rstrip("\0").split("\0"))) if output else ()
            if paths:
                code, _ = await self.command(root, files, "git", "add", "-A", "--", *paths)
                if code:
                    raise ToolError("Cannot stage pending content")
            files.confirm_effect()
            # Include already staged content, while retaining unrelated staged edits.
            code, output = await self.command(
                root,
                files,
                "git",
                "diff",
                "--no-renames",
                "--cached",
                "--name-only",
                "-z",
                "--",
                *CONTENT_PATHS,
                capture=True,
            )
            if code:
                raise ToolError("Cannot inspect staged content")
            paths = tuple(output.rstrip("\0").split("\0")) if output else ()
            if paths:
                phase = "commit"
                files.begin_effect(())
                code, output = await self.command(
                    root,
                    files,
                    "git",
                    "commit",
                    "--only",
                    "-m",
                    f"Publish {path.parent.name}: {path.stem}",
                    "--",
                    *paths,
                )
                if code:
                    raise ToolError(f"Commit failed: {output[-600:]}")
                files.confirm_effect()
            code, output = await self.command(root, files, "git", "rev-parse", "HEAD")
            if code:
                raise ToolError("Cannot identify the publication commit")
            commit = output.strip()
            phase = "push"
            files.begin_effect(())
            code, output = await self.command(root, files, "git", "push", "origin", "HEAD:main")
            if code:
                # Git diagnostics can echo credential-bearing remote URLs.
                raise ToolError(
                    f"Git push failed (exit {code}); inspect the remote and credentials"
                )
            files.confirm_effect()
            return {
                "slug": path.stem,
                "path": str(path),
                "draft": False,
                "published": True,
                "commit": commit,
            }
        except ToolError as error:
            raise ToolError(
                f"Publication stopped at {phase}; saved content: {path}; "
                f"commit: {commit or 'not recorded'}. {error}",
                error.code,
            ) from None
