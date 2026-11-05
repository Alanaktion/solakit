"""Scope-checked text filesystem operations shared by built-ins and packs."""

import fnmatch
import heapq
import os
import stat
import tempfile
import threading
from pathlib import Path

from solakit.core.io import durable
from solakit.core.store import sync_directory
from solakit.tools import ToolError, ToolResult

# Serialize read/modify/replace operations within the one-worker runtime.
_LOCK = threading.RLock()
_EDIT_LIMIT = 4_000_000


class Filesystem:
    def __init__(self, ctx):
        self.ctx = ctx

    def _read(self, path, limit):
        target = self.ctx.path(str(path))
        descriptor = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ToolError("Only regular files can be read")
            with os.fdopen(
                descriptor, "r", encoding="utf-8", newline="", closefd=False
            ) as stream:
                return stream.read(limit + 1), metadata
        finally:
            os.close(descriptor)

    async def read(self, path, *, max_chars: int | None = None) -> ToolResult:
        limit = (
            self.ctx.config.profiles[self.ctx.profile].tool_output_max_chars
            if max_chars is None
            else max_chars
        )
        if type(limit) is not int or not 1 <= limit <= _EDIT_LIMIT:
            raise ToolError("Read limit must be between 1 and 4000000 characters")
        try:
            content, _ = await durable(self._read, path, limit)
        except (OSError, UnicodeError) as exc:
            raise ToolError("Cannot read requested file") from exc
        return ToolResult(content=content, content_complete=len(content) <= limit)

    async def write(self, path, content: str) -> Path:
        def create():
            with _LOCK:
                target = self.ctx.path(str(path), write=True)
                with target.open("x", encoding="utf-8") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                sync_directory(target.parent)
                return target

        try:
            return await durable(create)
        except OSError as exc:
            raise ToolError(
                "Cannot create requested file (it may already exist)"
            ) from exc

    async def edit(self, path, old: str, new: str) -> Path:
        if not old:
            raise ToolError("Replacement text must be nonempty")

        def replace():
            with _LOCK:
                target = self.ctx.path(str(path), write=True)
                content, metadata = self._read(target, _EDIT_LIMIT)
                if len(content) > _EDIT_LIMIT:
                    raise ToolError("File is too large for exact replacement")
                first = content.find(old)
                if first < 0 or content.find(old, first + 1) >= 0:
                    raise ToolError("Replacement must match exactly once")
                descriptor, name = tempfile.mkstemp(
                    prefix=".solakit-", dir=target.parent
                )
                try:
                    with os.fdopen(
                        descriptor, "w", encoding="utf-8", newline=""
                    ) as stream:
                        os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
                        stream.write(content.replace(old, new, 1))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, target)
                    sync_directory(target.parent)
                finally:
                    Path(name).unlink(missing_ok=True)
                return target

        try:
            return await durable(replace)
        except (OSError, UnicodeError) as exc:
            raise ToolError("Cannot edit requested file") from exc

    async def append(self, path, content: str) -> Path:
        def append_text():
            with _LOCK:
                target = self.ctx.path(str(path), write=True)
                descriptor = os.open(
                    target, os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK | os.O_NOFOLLOW
                )
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise ToolError("Only regular files can be appended")
                    with os.fdopen(
                        descriptor, "a", encoding="utf-8", newline="", closefd=False
                    ) as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                finally:
                    os.close(descriptor)
                return target

        try:
            return await durable(append_text)
        except OSError as exc:
            raise ToolError("Cannot append to requested file") from exc

    async def entries(
        self, path, limit: int = 100, *, newest: bool = False, pattern: str = "*"
    ) -> tuple[list[dict], bool]:
        if not 1 <= limit <= 500:
            raise ToolError("Directory limit must be between 1 and 500")

        def listing():
            directory = self.ctx.path(str(path))

            def permitted(children):
                for child in children:
                    if not fnmatch.fnmatchcase(child.name, pattern):
                        continue
                    try:
                        self.ctx.path(child.path)
                        metadata = child.stat()
                    except (ToolError, OSError):
                        continue
                    yield {
                        "name": child.name,
                        "directory": child.is_dir(),
                        "modified_ns": metadata.st_mtime_ns,
                    }

            with os.scandir(directory) as children:
                if newest:
                    entries = heapq.nlargest(
                        limit + 1,
                        permitted(children),
                        key=lambda item: (item["modified_ns"], item["name"]),
                    )
                else:
                    entries = heapq.nsmallest(
                        limit + 1, permitted(children), key=lambda item: item["name"]
                    )
            return entries[:limit], len(entries) > limit

        try:
            return await durable(listing)
        except OSError as exc:
            raise ToolError("Cannot list requested directory") from exc

    async def mkdir(self, path) -> Path:
        def create():
            with _LOCK:
                target = self.ctx.path(str(path), write=True)
                target.mkdir(parents=True, exist_ok=True)
                return target

        try:
            return await durable(create)
        except OSError as exc:
            raise ToolError("Cannot create requested directory") from exc
