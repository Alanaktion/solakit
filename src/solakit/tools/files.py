"""Bounded descriptor-relative file operations for local macOS/Linux workspaces."""

from __future__ import annotations

import ctypes
import os
import stat
import sys
from collections.abc import Callable, Generator, Iterator
from contextlib import closing, contextmanager
from glob import translate
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import JsonValue
from pydantic_core import SchemaError, SchemaValidator, core_schema

from solakit.records import ErrorCode, new_id
from solakit.sdk import ToolError
from solakit.storage.evidence import digest

if TYPE_CHECKING:
    from solakit.config import EffectiveProfile

MAX_FILE_BYTES = 1024 * 1024
MAX_SCAN_ENTRIES = 2000
MAX_SCAN_BYTES = 16 * MAX_FILE_BYTES
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_MUTATIONS = frozenset(
    {
        "files.write",
        "files.append",
        "files.edit",
        "files.copy",
        "files.rename",
        "files.mkdir",
        "files.delete",
    }
)
RENAME_EXCL = 0x00000004  # macOS
RENAME_NOREPLACE = 1  # Linux


def _deny() -> ToolError:
    return ToolError("Path is outside permitted file access", ErrorCode.TOOL_DENIED)


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeError:
        raise ToolError("File is not UTF-8 text", ErrorCode.INVALID_INPUT) from None


def _matcher(pattern: str) -> Callable[[str], bool]:
    # The Rust engine keeps model-supplied patterns from backtracking on the event loop.
    try:
        validator = SchemaValidator(
            core_schema.str_schema(pattern=pattern, regex_engine="rust-regex")
        )
    except SchemaError as error:
        raise ToolError(f"Invalid pattern: {error}", ErrorCode.INVALID_INPUT) from None
    return validator.isinstance_python


def _glob_matcher(pattern: str) -> Callable[[str], bool]:
    path = Path(pattern)
    if (
        not 1 <= len(pattern) <= 4096
        or path.is_absolute()
        or ".." in path.parts
        or "\x00" in pattern
    ):
        raise ToolError("Glob must be a relative path pattern", ErrorCode.INVALID_INPUT)
    return _matcher(
        r"\A" + translate(path.as_posix(), recursive=True, include_hidden=True, seps="/")
    )


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
    def _parent(
        self, raw: str, *, write: bool, protect_git: bool = False
    ) -> Iterator[tuple[int, str]]:
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
        protected = self.protected_paths(profile)
        if write or protect_git:
            protected |= _git_roots(profile.workspace_roots)
            if any(part.casefold() == ".git" for part in path.parts):
                raise _deny()
        if write:
            protected.update(profile.git_reserved)
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

    @staticmethod
    def _require_vacant(parent: int, name: str) -> None:
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise ToolError("Destination already exists", ErrorCode.INVALID_INPUT)

    def protected_paths(self, profile: EffectiveProfile) -> set[Path]:
        return (
            set(self.protected)
            | {profile.data_dir, *profile.fragments}
            | {p for p in (profile.soul_file, profile.curated_memory_file) if p is not None}
        )

    @contextmanager
    def command_directory(self, path: str, *, write: bool = True) -> Iterator[int]:
        with self._parent(path, write=write, protect_git=True) as (parent, name):
            fd = os.open(name or ".", DIRECTORY, dir_fd=parent)
            try:
                yield fd
            finally:
                os.close(fd)

    def read_text(self, path: str) -> str:
        return _decode(self.read_bytes(path, limit=MAX_FILE_BYTES))

    def read_bytes(self, path: str, *, limit: int, protect_git: bool = False) -> bytes:
        with self._parent(path, write=False, protect_git=protect_git) as (parent, name):
            raw, _ = self._read_at(parent, name, limit)
        return raw

    def _read_at(self, parent: int, name: str, limit: int) -> tuple[bytes, os.stat_result]:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            self._regular(info)
            if info.st_size > limit:
                raise ToolError("File exceeds read limit", ErrorCode.BUDGET_EXCEEDED)
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ToolError("File exceeds read limit", ErrorCode.BUDGET_EXCEEDED)
        return raw, info

    def copy_file(self, source: str, destination: str) -> dict[str, JsonValue]:
        raw = self.read_bytes(source, limit=MAX_FILE_BYTES, protect_git=True)
        return {"source": source, **self.write_bytes(destination, raw, limit=MAX_FILE_BYTES)}

    def rename_file(self, source: str, destination: str) -> dict[str, JsonValue]:
        with (
            self._parent(source, write=True) as (src_fd, src_name),
            self._parent(destination, write=True) as (dst_fd, dst_name),
        ):
            self._regular(os.stat(src_name, dir_fd=src_fd, follow_symlinks=False))
            self._require_vacant(dst_fd, dst_name)
            libc = ctypes.CDLL(None, use_errno=True)
            # Python's os.rename cannot atomically require a vacant destination.
            try:
                if sys.platform == "darwin":
                    rename, flags = libc.renameatx_np, RENAME_EXCL
                else:
                    rename, flags = libc.renameat2, RENAME_NOREPLACE
            except AttributeError:
                raise ToolError("Exclusive rename is unavailable on this platform") from None
            rename.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            src_bytes, dst_bytes = os.fsencode(src_name), os.fsencode(dst_name)
            self.begin_effect(())
            if rename(src_fd, src_bytes, dst_fd, dst_bytes, flags) != 0:
                error = ctypes.get_errno()
                raise ToolError(f"Rename failed: {os.strerror(error)}")
            self.confirm_effect()
            os.fsync(dst_fd)
            os.fsync(src_fd)
        return {"source": source, "path": destination}

    def make_directory(self, path: str) -> dict[str, JsonValue]:
        with self._parent(path, write=True) as (parent, name):
            self._require_vacant(parent, name)
            self.begin_effect(())
            os.mkdir(name, mode=0o700, dir_fd=parent)
            self.confirm_effect()
            os.fsync(parent)
        return {"path": path, "kind": "directory"}

    def delete_file(self, path: str) -> dict[str, JsonValue]:
        with self._parent(path, write=True) as (parent, name):
            self._regular(os.stat(name, dir_fd=parent, follow_symlinks=False))
            self.begin_effect(())
            os.unlink(name, dir_fd=parent)
            self.confirm_effect()
            os.fsync(parent)
        return {"path": path, "deleted": True}

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

    def _scan(
        self, path: str, *, allow_file: bool = False
    ) -> Generator[tuple[Path, os.stat_result] | None]:
        pending = [path]
        while pending:
            directory = pending.pop()
            with self._parent(directory, write=False) as (parent, name):
                if allow_file:
                    allow_file = False
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode):
                        self._regular(info)
                        yield Path(directory), info
                        return
                fd = os.open(name, DIRECTORY, dir_fd=parent)
                try:
                    with os.scandir(fd) as children:
                        for child in children:
                            target = Path(directory) / child.name
                            try:
                                info = child.stat(follow_symlinks=False)
                                if not stat.S_ISDIR(info.st_mode):
                                    self._regular(info)
                                with self._parent(str(target), write=False):
                                    pass
                            except ToolError, OSError:
                                yield None
                                continue
                            if stat.S_ISDIR(info.st_mode):
                                pending.append(str(target))
                            yield target, info
                finally:
                    os.close(fd)

    def glob_files(self, path: str, pattern: str, *, limit: int = 100) -> JsonValue:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("Invalid glob limit", ErrorCode.INVALID_INPUT)
        matches_pattern = _glob_matcher(pattern)
        base = Path(path)
        matches: list[JsonValue] = []
        truncated = False
        with closing(self._scan(path)) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_SCAN_ENTRIES:
                    truncated = True
                    break
                if entry is None:
                    continue
                target, info = entry
                if stat.S_ISREG(info.st_mode) and matches_pattern(
                    target.relative_to(base).as_posix()
                ):
                    if len(matches) == limit:
                        truncated = True
                        break
                    matches.append(str(target))
        return {"matches": matches, "truncated": truncated}

    def search_text(self, path: str, query: str, *, limit: int = 20) -> JsonValue:
        if not 1 <= len(query) <= 4096:
            raise ToolError("Invalid search query", ErrorCode.INVALID_INPUT)
        return self._search(path, lambda line: query in line, limit=limit)

    def grep_text(
        self,
        path: str,
        pattern: str,
        *,
        glob: str = "**/*",
        context: int = 2,
        limit: int = 20,
    ) -> JsonValue:
        if not 1 <= len(pattern) <= 4096 or type(context) is not int or not 0 <= context <= 10:
            raise ToolError("Invalid grep pattern or context", ErrorCode.INVALID_INPUT)
        return self._search(
            path, _matcher(pattern), limit=limit, glob_match=_glob_matcher(glob), context=context
        )

    def _search(
        self,
        path: str,
        matches_line: Callable[[str], bool],
        *,
        limit: int,
        glob_match: Callable[[str], bool] | None = None,
        context: int = 0,
    ) -> JsonValue:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("Invalid search limit", ErrorCode.INVALID_INPUT)
        base = Path(path)
        matches: list[JsonValue] = []
        scanned = total_bytes = skipped = 0
        truncated = False
        with closing(self._scan(path, allow_file=glob_match is not None)) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_SCAN_ENTRIES:
                    truncated = True
                    break
                if entry is None:
                    skipped += 1
                    continue
                target, info = entry
                if not stat.S_ISREG(info.st_mode):
                    continue
                if glob_match and not glob_match(
                    target.name if target == base else target.relative_to(base).as_posix()
                ):
                    continue
                if total_bytes + info.st_size > MAX_SCAN_BYTES:
                    truncated = True
                    break
                try:
                    available = min(MAX_FILE_BYTES, MAX_SCAN_BYTES - total_bytes)
                    raw = self.read_bytes(str(target), limit=available)
                    total_bytes += len(raw)
                    if glob_match and b"\x00" in raw:
                        skipped += 1
                        continue
                    lines = _decode(raw).splitlines()
                except ToolError:
                    skipped += 1
                    continue
                scanned += 1
                for index_in_file, line in enumerate(lines):
                    if not matches_line(line):
                        continue
                    match: dict[str, JsonValue] = {
                        "path": str(target),
                        "line": index_in_file + 1,
                        "text": line[:1000],
                    }
                    if context:
                        match["before"] = [
                            text[:1000]
                            for text in lines[max(0, index_in_file - context) : index_in_file]
                        ]
                        match["after"] = [
                            text[:1000]
                            for text in lines[index_in_file + 1 : index_in_file + 1 + context]
                        ]
                    matches.append(match)
                    if len(matches) >= limit:
                        truncated = True
                        break
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
        result = self.write_bytes(path, raw, limit=MAX_FILE_BYTES, overwrite=overwrite)
        if self.record_artifact is not None:
            self.record_artifact(raw)
        return result

    def append_text(self, path: str, content: str) -> dict[str, JsonValue]:
        if not content:
            raise ToolError("Append content must be nonempty", ErrorCode.INVALID_INPUT)
        return self._update_text(path, lambda text: text + content, create=True)

    def edit_text(self, path: str, old_text: str, new_text: str) -> dict[str, JsonValue]:
        if not old_text:
            raise ToolError("Old text must be nonempty", ErrorCode.INVALID_INPUT)

        def replace(text: str) -> str:
            start = text.find(old_text)
            if start == -1 or text.find(old_text, start + 1) != -1:
                raise ToolError("Old text must match exactly once", ErrorCode.INVALID_INPUT)
            return text[:start] + new_text + text[start + len(old_text) :]

        return self._update_text(path, replace)

    def _update_text(
        self, path: str, update: Callable[[str], str], *, create: bool = False
    ) -> dict[str, JsonValue]:
        with self._parent(path, write=True) as (parent, name):
            try:
                raw, original = self._read_at(parent, name, MAX_FILE_BYTES)
            except FileNotFoundError:
                if not create:
                    raise ToolError("File does not exist", ErrorCode.INVALID_INPUT) from None
                raw, original = b"", None
            raw = update(_decode(raw)).encode("utf-8")
            if len(raw) > MAX_FILE_BYTES:
                raise ToolError("File exceeds write limit", ErrorCode.BUDGET_EXCEEDED)
            self._write_at(parent, name, raw, overwrite=original is not None, original=original)
        return {"path": path, "bytes": len(raw), "sha256": digest(raw)}

    def write_bytes(
        self, path: str, raw: bytes, *, limit: int, overwrite: bool = False
    ) -> dict[str, JsonValue]:
        if len(raw) > limit:
            raise ToolError("File exceeds write limit", ErrorCode.BUDGET_EXCEEDED)
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
            self._write_at(parent, name, raw, overwrite=overwrite)
        return {"path": path, "bytes": len(raw), "sha256": digest(raw)}

    def _write_at(
        self,
        parent: int,
        name: str,
        raw: bytes,
        *,
        overwrite: bool,
        original: os.stat_result | None = None,
    ) -> None:
        temporary = f".sola-{new_id()}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self.begin_effect(())
            if original is not None:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (
                    current.st_dev != original.st_dev
                    or current.st_ino != original.st_ino
                    or current.st_size != original.st_size
                    or current.st_nlink != original.st_nlink
                    or current.st_mtime_ns != original.st_mtime_ns
                    or current.st_ctime_ns != original.st_ctime_ns
                ):
                    raise ToolError("File changed during update", ErrorCode.INVALID_INPUT)
            if overwrite:
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            else:
                os.link(
                    temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False
                )
            self.confirm_effect()
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
