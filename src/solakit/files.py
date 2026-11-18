"""Bounded descriptor-relative file operations for local macOS/Linux workspaces."""

import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from solakit.config import EffectiveProfile
from solakit.evidence import digest
from solakit.records import ErrorCode, new_id
from solakit.sdk import ToolError

MAX_FILE_BYTES = 1024 * 1024
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _deny() -> ToolError:
    return ToolError("Path is outside permitted file access", ErrorCode.TOOL_DENIED)


# shortcut: local scans are synchronous; offload if large workspaces stall the event loop.
def _git_roots(roots: tuple[Path, ...]) -> set[Path]:
    """Also protect gitdir/commondir targets outside a worktree's .git entry."""
    markers: set[Path] = set()
    protected: set[Path] = set()

    def inaccessible(error: OSError) -> None:
        raise _deny()

    for root in roots:
        markers.update(parent / ".git" for parent in (root, *root.parents))
        for directory, children, files in os.walk(root, followlinks=False, onerror=inaccessible):
            directories = {name.casefold() for name in children}
            if "head" in {name.casefold() for name in files} and {"objects", "refs"} <= directories:
                protected.add(Path(directory))
            markers.update(
                Path(directory) / name for name in (*children, *files) if name.casefold() == ".git"
            )
            children[:] = [
                name
                for name in children
                if name.casefold() != ".git" and not (Path(directory) / name).is_symlink()
            ]
    for marker in markers:
        if not marker.exists():
            continue
        target = marker.resolve()
        protected.add(target)
        if target.is_file():
            with target.open("rb") as stream:
                content = stream.read(4097)
            if len(content) > 4096:
                raise _deny()
            if content.startswith(b"gitdir:"):
                target = (marker.parent / os.fsdecode(content[7:].strip())).resolve()
                protected.add(target)
        common = target / "commondir"
        if target.is_dir() and common.is_file():
            with common.open("rb") as stream:
                content = stream.read(4097)
            if len(content) > 4096:
                raise _deny()
            protected.add((target / os.fsdecode(content.strip())).resolve())
    return protected


class GuardedFiles:
    def __init__(
        self,
        authority: Callable[[str], EffectiveProfile],
        protected: tuple[Path, ...],
        begin_effect: Callable[[tuple[str, ...]], None],
        confirm_effect: Callable[[], None],
        record_artifact: Callable[[bytes], None] | None = None,
    ) -> None:
        self.authority = authority
        self.protected = protected
        self.begin_effect = begin_effect
        self.confirm_effect = confirm_effect
        self.record_artifact = record_artifact

    @contextmanager
    def _parent(self, raw: str, *, write: bool) -> Iterator[tuple[int, str]]:
        profile = self.authority("files.write" if write else "files.read")
        if not raw or "\x00" in raw or ".." in Path(raw).parts:
            raise _deny()
        path = Path(raw)
        if not path.is_absolute():
            path = profile.workspace_roots[0] / path if profile.workspace_roots else path
        # macOS /tmp and /var may themselves be aliases; configured roots are canonical.
        if not path.is_absolute() or not any(
            path.is_relative_to(root) for root in profile.workspace_roots
        ):
            raise _deny()
        protected = set(self.protected) | {profile.data_dir}
        protected.update(profile.fragments)
        protected.update(p for p in (profile.soul_file, profile.curated_memory_file) if p)
        if write:
            protected |= _git_roots(profile.workspace_roots)
            if any(part.casefold() == ".git" for part in path.parts):
                raise _deny()
        if any(path == root or path.is_relative_to(root) for root in protected):
            raise _deny()
        identities = set()
        for root in protected:
            try:
                info = root.stat()
            except FileNotFoundError:
                continue
            identities.add((info.st_dev, info.st_ino))

        def check(info: os.stat_result) -> None:
            if (info.st_dev, info.st_ino) in identities:
                raise _deny()

        fd = os.open("/", DIRECTORY)
        try:
            for part in path.parts[1:-1]:
                child = os.open(part, DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
                check(os.fstat(fd))
            try:
                check(os.stat(path.name, dir_fd=fd, follow_symlinks=False))
            except FileNotFoundError:
                pass
            yield fd, path.name
        except OSError:
            raise _deny() from None
        finally:
            os.close(fd)

    @staticmethod
    def _regular(info: os.stat_result) -> None:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _deny()

    def read_text(self, path: str) -> str:
        with self._parent(path, write=False) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                self._regular(os.fstat(stream.fileno()))
                raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ToolError("File exceeds 1 MiB read limit", ErrorCode.BUDGET_EXCEEDED)
        try:
            return raw.decode("utf-8")
        except UnicodeError:
            raise ToolError("File is not UTF-8 text", ErrorCode.INVALID_INPUT) from None

    def list_directory(self, path: str, *, limit: int = 100) -> JsonValue:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("Invalid listing limit", ErrorCode.INVALID_INPUT)
        entries: list[dict[str, JsonValue]] = []
        truncated = False
        with self._parent(path, write=False) as (parent, name):
            fd = os.open(name, DIRECTORY, dir_fd=parent)
            try:
                with os.scandir(fd) as children:
                    for index, child in enumerate(children):
                        if index >= 2000:
                            truncated = True
                            break
                        info = child.stat(follow_symlinks=False)
                        if child.is_symlink() or not (
                            stat.S_ISDIR(info.st_mode)
                            or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
                        ):
                            continue
                        try:
                            with self._parent(str(Path(path) / child.name), write=False):
                                pass
                        except ToolError:
                            continue
                        entries.append(
                            {
                                "name": child.name,
                                "kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
                                "bytes": info.st_size,
                            }
                        )
            finally:
                os.close(fd)
        entries.sort(key=lambda entry: str(entry["name"]))
        return cast(
            JsonValue, {"entries": entries[:limit], "truncated": truncated or len(entries) > limit}
        )

    def search_text(self, path: str, query: str, *, limit: int = 20) -> JsonValue:
        if not query or len(query) > 4096 or type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("Invalid search query or limit", ErrorCode.INVALID_INPUT)
        matches: list[JsonValue] = []
        pending = [path]
        scanned = total_bytes = visited = skipped = 0
        truncated = False
        while pending:
            directory = pending.pop()
            with self._parent(directory, write=False) as (parent, name):
                fd = os.open(name, DIRECTORY, dir_fd=parent)
                try:
                    with os.scandir(fd) as children:
                        for child in children:
                            visited += 1
                            if visited > 2000:
                                truncated = True
                                break
                            target = str(Path(directory) / child.name)
                            if child.is_symlink():
                                skipped += 1
                                continue
                            try:
                                with self._parent(target, write=False):
                                    pass
                                if child.is_dir(follow_symlinks=False):
                                    pending.append(target)
                                    continue
                                info = child.stat(follow_symlinks=False)
                                self._regular(info)
                                if total_bytes + info.st_size > 16 * MAX_FILE_BYTES:
                                    truncated = True
                                    break
                                total_bytes += info.st_size
                                text = self.read_text(target)
                            except ToolError, OSError:
                                skipped += 1
                                continue
                            scanned += 1
                            for number, line in enumerate(text.splitlines(), 1):
                                if query in line:
                                    matches.append(
                                        {"path": target, "line": number, "text": line[:1000]}
                                    )
                                    if len(matches) >= limit:
                                        truncated = True
                                        break
                            if truncated:
                                break
                finally:
                    os.close(fd)
            if truncated:
                break
        return {
            "matches": matches,
            "truncated": truncated,
            "scanned_files": scanned,
            "skipped": skipped,
        }

    def write_text(
        self, path: str, content: str, *, overwrite: bool = False
    ) -> dict[str, JsonValue]:
        raw = content.encode("utf-8")
        if len(raw) > MAX_FILE_BYTES:
            raise ToolError("File exceeds 1 MiB write limit", ErrorCode.BUDGET_EXCEEDED)
        with self._parent(path, write=True) as (parent, name):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                self._regular(info)
                if not overwrite:
                    raise ToolError(
                        "File exists; overwrite must be explicit", ErrorCode.INVALID_INPUT
                    )
            temporary = f".sola-{new_id()}.tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.begin_effect(())
                if overwrite:
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                else:
                    os.link(
                        temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False
                    )
                self.confirm_effect()
                os.fsync(parent)
                if self.record_artifact is not None:
                    self.record_artifact(raw)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return {"path": path, "bytes": len(raw), "sha256": digest(raw)}
