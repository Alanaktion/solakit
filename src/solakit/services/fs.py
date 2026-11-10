"""Scope-checked text filesystem operations shared by built-ins and packs."""

import asyncio
import fnmatch
import heapq
import os
import stat
import tempfile
import threading
import time
from pathlib import Path

import regex as bounded_regex

from solakit.core.io import durable
from solakit.core.store import sync_directory
from solakit.tools import ToolError, ToolResult

# Serialize read/modify/replace operations within the one-worker runtime.
_LOCK = threading.RLock()
_EDIT_LIMIT = 4_000_000


class _SearchMatcher:
    def __init__(self, query: str, regex: bool):
        try:
            self.pattern = bounded_regex.compile(
                query if regex else bounded_regex.escape(query),
                bounded_regex.I | bounded_regex.V0,
            )
        except bounded_regex.error as exc:
            raise ToolError("Invalid regular expression") from exc
        self.remaining = 0.1

    def matches(self, line: str) -> bool:
        started = time.monotonic()
        try:
            result = self.pattern.search(line, timeout=max(self.remaining, 0.000001))
        except TimeoutError as exc:
            raise ToolError("Regex search exceeded its matching time budget") from exc
        self.remaining -= time.monotonic() - started
        if self.remaining <= 0:
            raise ToolError("Regex search exceeded its matching time budget")
        return result is not None


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
        if max_chars is not None and (
            type(limit) is not int or not 1 <= limit <= _EDIT_LIMIT
        ):
            raise ToolError("Read limit must be between 1 and 4000000 characters")
        try:
            content, _ = await asyncio.to_thread(self._read, path, limit)
        except (OSError, UnicodeError) as exc:
            raise ToolError("Cannot read requested file") from exc
        return ToolResult(content=content, content_complete=len(content) <= limit)

    async def exists(self, path) -> bool:
        def check():
            return self.ctx.path(str(path), write=True).exists()

        try:
            return await asyncio.to_thread(check)
        except (OSError, ToolError):
            return False

    async def window(self, path, offset: int = 1, limit: int | None = None) -> dict:
        """Line window of a UTF-8 file: numbered lines plus position facts."""
        try:
            content, _ = await asyncio.to_thread(self._read, path, _EDIT_LIMIT)
        except (OSError, UnicodeError) as exc:
            raise ToolError("Cannot read requested file") from exc
        if len(content) > _EDIT_LIMIT:
            raise ToolError("File is too large to window")
        lines = content.splitlines()
        total = len(lines)
        if offset > max(total, 1):
            raise ToolError(f"Offset is past the end of the file ({total} lines)")
        budget = self.ctx.config.profiles[self.ctx.profile].tool_output_max_chars
        budget = max(64, budget - 160)  # Leave room for the footer.
        shown, used, cut = [], 0, False
        for number in range(offset, total + 1):
            if limit is not None and len(shown) >= limit:
                break
            line = f"{number:>6}\t{lines[number - 1]}"
            if used + len(line) + 1 > budget and shown:
                break
            cut |= len(line) > budget
            shown.append(line[:budget])
            used += len(line) + 1
        end = offset + len(shown) - 1
        return {
            "text": "\n".join(shown),
            "start": offset,
            "end": end,
            "total": total,
            "next": end + 1 if end < total else None,
            "cut": cut,
        }

    async def write(self, path, content: str, *, overwrite: bool = False) -> Path:
        def create():
            with _LOCK:
                target = self.ctx.path(str(path), write=True)
                if overwrite and target.exists():
                    if target.is_symlink() or not target.is_file():
                        raise ToolError("Only regular files can be overwritten")
                    mode = stat.S_IMODE(target.stat().st_mode)
                    descriptor, name = tempfile.mkstemp(
                        prefix=".solakit-", dir=target.parent
                    )
                    try:
                        with os.fdopen(
                            descriptor, "w", encoding="utf-8", newline=""
                        ) as stream:
                            os.fchmod(stream.fileno(), mode)
                            stream.write(content)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(name, target)
                    finally:
                        Path(name).unlink(missing_ok=True)
                else:
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
                "Cannot create requested file (it may already exist; pass "
                "overwrite=true to replace it)"
            ) from exc

    async def edit(self, path, old: str, new: str, *, report=None) -> Path:
        if not old:
            raise ToolError("Replacement text must be nonempty")

        def replace():
            with _LOCK:
                target = self.ctx.path(str(path), write=True)
                content, metadata = self._read(target, _EDIT_LIMIT)
                if len(content) > _EDIT_LIMIT:
                    raise ToolError("File is too large for exact replacement")
                first = content.find(old)
                if first >= 0 and content.find(old, first + 1) >= 0:
                    raise ToolError(
                        "Replacement must match exactly once; it matches more "
                        "than once, so include more surrounding lines in old"
                    )
                if first >= 0:
                    updated = content.replace(old, new, 1)
                    line = content.count("\n", 0, first) + 1
                    if report is not None:
                        report.update(
                            mode="exact",
                            start=line,
                            end=line + old.count("\n"),
                        )
                else:
                    updated, start, end = whitespace_replace(content, old, new)
                    if report is not None:
                        report.update(mode="whitespace", start=start, end=end)
                descriptor, name = tempfile.mkstemp(
                    prefix=".solakit-", dir=target.parent
                )
                try:
                    with os.fdopen(
                        descriptor, "w", encoding="utf-8", newline=""
                    ) as stream:
                        os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
                        stream.write(updated)
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

        stopped = threading.Event()

        def listing():
            directory = self.ctx.path(str(path))

            def permitted(children):
                for child in children:
                    if stopped.is_set():
                        return
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
            return await asyncio.to_thread(listing)
        except OSError as exc:
            raise ToolError("Cannot list requested directory") from exc
        finally:
            stopped.set()

    def _walk(self, path, max_files=5000, *, stopped=None):
        """Scope-checked regular files below path; symlinks are never followed."""
        root = self.ctx.path(str(path))
        if root.is_file():
            yield root, root.name
            return
        seen = 0
        for directory, dirs, files in os.walk(root, followlinks=False):
            if stopped is not None and stopped.is_set():
                return
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(files):
                if stopped is not None and stopped.is_set():
                    return
                candidate = Path(directory) / name
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                try:
                    self.ctx.path(str(candidate))
                except ToolError:
                    continue
                seen += 1
                if seen > max_files:
                    yield None, None
                    return
                yield candidate, str(candidate.relative_to(root))

    async def find(self, pattern: str, path=".", limit: int = 50) -> dict:
        stopped = threading.Event()

        def search():
            matches, more = [], False
            for file, relative in self._walk(path, stopped=stopped):
                if file is None:
                    more = True
                    break
                target = relative if "/" in pattern else file.name
                # "**/" also matches zero directories, as in shell globstar.
                if fnmatch.fnmatch(target, pattern) or (
                    "**/" in pattern
                    and fnmatch.fnmatch(target, pattern.replace("**/", ""))
                ):
                    if len(matches) >= limit:
                        more = True
                        break
                    info = file.stat()
                    matches.append(
                        {
                            "path": relative,
                            "bytes": info.st_size,
                            "modified": int(info.st_mtime),
                        }
                    )
            return {"matches": matches, "more": more}

        try:
            return await asyncio.to_thread(search)
        except OSError as exc:
            raise ToolError("Cannot search requested directory") from exc
        finally:
            stopped.set()

    async def grep(  # noqa: C901
        self,
        query: str,
        path=".",
        *,
        glob: str | None = None,
        regex: bool = False,
        context: int = 2,
        limit: int = 30,
    ) -> dict:
        stopped = threading.Event()

        def search():
            matcher = _SearchMatcher(query, regex)
            hits, lines_out, more = 0, [], False
            for file, relative in self._walk(path, stopped=stopped):
                if file is None:
                    more = True
                    break
                if glob and not fnmatch.fnmatch(
                    relative if "/" in glob else file.name, glob
                ):
                    continue
                try:
                    if file.stat().st_size > 2_000_000:
                        continue
                    text, _ = self._read(file, 2_000_000)
                except (OSError, UnicodeError, ToolError):
                    continue
                if "\0" in text[:4096]:
                    continue
                lines = text.splitlines()
                shown = set()
                for index, line in enumerate(lines):
                    if stopped.is_set():
                        return {"lines": lines_out, "matches": hits, "more": True}
                    if len(line) > 4096:
                        more = True
                        continue
                    if not matcher.matches(line):
                        continue
                    if hits >= limit:
                        return {"lines": lines_out, "matches": hits, "more": True}
                    hits += 1
                    for near in range(
                        max(0, index - context), min(len(lines), index + context + 1)
                    ):
                        if near in shown:
                            continue
                        shown.add(near)
                        mark = ":" if near == index else "-"
                        lines_out.append(
                            f"{relative}{mark}{near + 1}{mark} {lines[near][:300]}"
                        )
            return {"lines": lines_out, "matches": hits, "more": more}

        try:
            return await asyncio.to_thread(search)
        except OSError as exc:
            raise ToolError("Cannot search requested directory") from exc
        finally:
            stopped.set()

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


def whitespace_replace(content: str, old: str, new: str) -> tuple[str, int, int]:
    """Replace whole lines matching ``old`` ignoring trailing whitespace/CRLF.

    Small models often reproduce text without exact trailing spaces or line
    endings. Only a unique run of complete lines qualifies; the file keeps its
    own line-ending style. Returns (content, first line, last line)."""
    wanted = [line.rstrip() for line in old.strip("\r\n").splitlines()]
    if not wanted or not any(wanted):
        raise ToolError("Replacement must match exactly once")
    lines = content.splitlines(keepends=True)
    normalized = [line.rstrip() for line in lines]
    size = len(wanted)
    hits = [
        index
        for index in range(len(lines) - size + 1)
        if normalized[index : index + size] == wanted
    ]
    if len(hits) != 1:
        raise ToolError(
            "Replacement must match exactly once"
            + ("; include more surrounding lines" if hits else "")
        )
    start = hits[0]
    ending = "\r\n" if lines[start].endswith("\r\n") else "\n"
    replacement = new.replace("\r\n", "\n").replace("\n", ending)
    last = lines[start + size - 1]
    if last.endswith(("\n", "\r")) and not replacement.endswith(ending):
        replacement += ending
    updated = "".join(lines[:start]) + replacement + "".join(lines[start + size :])
    return updated, start + 1, start + size
