"""Alias-bound Git services with literal paths, bounded output and write checks."""

import asyncio
import os
import signal
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from solakit.tools import ToolError, ToolResult

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


@asynccontextmanager
async def repository_lock(root):
    # Also serialize service users in sync packs that run their own event loop.
    # Nonblocking acquisition avoids stranded background-thread acquisitions on
    # cancellation. Keys are configured repository roots, never model paths.
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(root, threading.Lock())
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.01)
    try:
        yield
    finally:
        lock.release()


class Git:
    def __init__(self, ctx, repository: str, *, collection: str | None = None):
        self.ctx, self.alias, self.collection = ctx, repository, collection
        configured = ctx.config.repositories.get(repository)
        if configured is None:
            raise ToolError("Unknown repository")
        self.root = configured.expanduser().resolve()
        self.directory = self.root
        if collection is not None:
            resource = ctx.config.collections.get(collection)
            if resource is None or resource.repository != repository:
                raise ToolError("Unknown collection")
            self.directory = (self.root / resource.directory).resolve()
            if not self.directory.is_relative_to(self.root):
                raise ToolError("Collection directory escapes repository")
        ctx.path(str(self.directory))

    async def _run(self, *arguments) -> ToolResult:
        limit = max(
            65536, self.ctx.config.profiles[self.ctx.profile].tool_output_max_chars * 4
        )
        environment = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        process = await asyncio.create_subprocess_exec(
            "git",
            "--literal-pathspecs",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(self.root),
            *arguments,
            env=environment,
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def drain(stream, capture):
            output, size = bytearray(), 0
            while chunk := await stream.read(65536):
                size += len(chunk)
                if capture:
                    output.extend(chunk[: max(0, limit - len(output))])
            return bytes(output), size <= limit

        stdout = asyncio.create_task(drain(process.stdout, True))
        stderr = asyncio.create_task(drain(process.stderr, False))
        try:
            async with asyncio.timeout(
                self.ctx.config.profiles[self.ctx.profile].tool_timeout_s
            ):
                await process.wait()
                (output, complete), _ = await asyncio.gather(stdout, stderr)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            await asyncio.gather(stdout, stderr, return_exceptions=True)
            raise
        if process.returncode:
            raise ToolError(f"Git {arguments[0]} failed (exit {process.returncode})")
        return ToolResult(
            content=output.decode("utf-8", errors="replace"), content_complete=complete
        )

    async def _text(self, *arguments) -> str:
        result = await self._run(*arguments)
        if not result.content_complete:
            raise ToolError("Git metadata exceeded the output limit")
        return result.content.removesuffix("\n")

    async def _ready(self):
        actual = await self._text("rev-parse", "--show-toplevel")
        if Path(actual).resolve() != self.root:
            raise ToolError("Repository alias must name the Git worktree root")
        self.ctx.path(str(self.directory))

    def _view(self):
        return (
            []
            if self.collection is None
            else [str(self.directory.relative_to(self.root))]
        )

    def _write_access(self):
        self.ctx.path(str(self.directory), write=True)

    def _write_paths(self, paths):
        self._write_access()
        if not paths or len(paths) > 100:
            raise ToolError("Name between 1 and 100 files")
        checked = []
        for name in paths:
            relative = Path(name)
            if (
                not name
                or relative.is_absolute()
                or ".." in relative.parts
                or any(part.casefold() == ".git" for part in relative.parts)
                or not relative.parts
                or "\x00" in name
            ):
                raise ToolError(
                    "Git paths must be literal repository-relative file names"
                )
            target = self.ctx.path(str(self.root / relative), write=True)
            if not target.is_relative_to(self.directory) or target.is_dir():
                raise ToolError("Named file is outside the resource or is a directory")
            checked.append(str(relative))
        return list(dict.fromkeys(checked))

    async def status(self) -> ToolResult:
        await self._ready()
        return await self._run(
            "status", "--short", "--untracked-files=all", "--", *self._view()
        )

    async def diff(self) -> ToolResult:
        await self._ready()
        return await self._run(
            "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--", *self._view()
        )

    async def log(self, count: int = 10) -> ToolResult:
        if not 1 <= count <= 100:
            raise ToolError("Log count must be between 1 and 100")
        await self._ready()
        return await self._run(
            "log",
            f"-{count}",
            "--no-show-signature",
            "--format=%h %aI %s",
            "--",
            *self._view(),
        )

    async def pull(self) -> ToolResult:
        if self.collection is not None:
            raise ToolError("Pull requires whole-repository write access")
        self._write_access()
        async with repository_lock(self.root):
            await self._ready()
            if (await self.status()).content:
                raise ToolError("Pull requires a clean worktree and index")
            return await self._run("pull", "--ff-only", "--no-rebase")

    def _publish_access(self):
        self._write_access()
        if "push" not in self.ctx.config.profiles[self.ctx.profile].effects:
            raise ToolError("Missing effect: push")

    async def _upstream(self):
        branch = await self._text("branch", "--show-current")
        if not branch:
            raise ToolError("Publishing requires a checked-out branch")
        remote = await self._text("config", "--get", f"branch.{branch}.remote")
        merge = await self._text("config", "--get", f"branch.{branch}.merge")
        if not merge.startswith("refs/heads/"):
            raise ToolError("Publishing requires a configured upstream branch")
        return remote, merge

    async def _commit(self, paths, message):
        paths = self._write_paths(paths)
        if not message.strip() or len(message) > 2000 or "\x00" in message:
            raise ToolError(
                "Commit message must be nonempty and at most 2000 characters"
            )
        await self._ready()
        if await self._text("diff", "--cached", "--name-only"):
            raise ToolError("Commit requires an initially empty index")
        await self._run("add", "--all", "--", *paths)
        await self._run("commit", "--only", "-m", message, "--", *paths)
        return await self._text("rev-parse", "HEAD")

    async def commit(self, paths: list[str], message: str) -> str:
        """Commit only named files; no implicit push. Return the commit identity."""
        async with repository_lock(self.root):
            return await self._commit(paths, message)

    async def _push(self, commit):
        self._publish_access()
        await self._ready()
        if commit != await self._text("rev-parse", "HEAD"):
            raise ToolError(
                "Publish identity is no longer HEAD; inspect before retrying"
            )
        remote, merge = await self._upstream()
        unpublished = (await self._text("rev-list", "@{upstream}..HEAD")).splitlines()
        if unpublished != [commit]:
            raise ToolError("Push must publish exactly one new commit")
        changed = await self._text(
            "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit
        )
        self._write_paths([path for path in changed.split("\x00") if path])
        try:
            await self._run(
                "push", "--porcelain", "--no-follow-tags", "--", remote, f"HEAD:{merge}"
            )
        except (ToolError, TimeoutError) as exc:
            raise ToolError(
                f"Commit {commit} exists locally; push failed or is uncertain. "
                "Inspect remote before retrying."
            ) from exc
        return ToolResult(content=f"Published commit {commit}")

    async def push(self, commit: str) -> ToolResult:
        """Push one checked commit; revalidate grants and all its changed paths."""
        async with repository_lock(self.root):
            return await self._push(commit)

    async def commit_push(self, paths: list[str], message: str) -> ToolResult:
        self._publish_access()
        async with repository_lock(self.root):
            await self._ready()
            await self._upstream()
            if await self._text("rev-list", "@{upstream}..HEAD"):
                raise ToolError(
                    "Unpublished commits already exist; reconcile before publishing"
                )
            if await self._text("rev-list", "HEAD..@{upstream}"):
                raise ToolError("Upstream is ahead; pull before publishing")
            commit = await self._commit(paths, message)
            return await self._push(commit)
