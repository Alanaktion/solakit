"""Fixed operations for operator-trusted Git checkouts."""

import asyncio
import os
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, BinaryIO

from pydantic import Field, JsonValue

from solakit.records import ErrorCode
from solakit.sdk import ToolArguments, ToolError, ToolResult
from solakit.tools.files import DIRECTORY, GuardedFiles
from solakit.tools.process import DIRECTORY_LAUNCHER, open_pipe, read_pipe, reap_group

if TYPE_CHECKING:
    from solakit.config import EffectiveProfile
    from solakit.sdk import ToolContext
    from solakit.tools.dispatch import Invocation

MAX_STATUS_BYTES = 16 * 1024
MAX_DIFF_BYTES = 32 * 1024
MAX_CHECK_BYTES = 1024 * 1024
STATUS = (
    "status",
    "--porcelain=v1",
    "-z",
    "--no-renames",
    "--untracked-files=all",
    "--ignore-submodules=all",
)
DIFF = (
    "diff",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--no-renames",
    "--ignore-submodules=all",
)
GIT = (
    "git",
    "--no-pager",
    "--no-optional-locks",
    "--no-replace-objects",
    "--literal-pathspecs",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.quotePath=true",
    "-c",
    "maintenance.auto=false",
    "-c",
    "gc.auto=0",
    "-c",
    "submodule.recurse=false",
    "-c",
    "remote.origin.mirror=false",
)
IN_PROGRESS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "REBASE_HEAD",
    "rebase-apply",
    "rebase-merge",
    "sequencer",
    "BISECT_START",
)

FilePath = Annotated[str, Field(min_length=1, max_length=4096)]


class GitTarget(ToolArguments):
    repository: Annotated[str, Field(min_length=1, max_length=128)]


class GitDiff(GitTarget):
    path: FilePath | None = None


class GitPublish(GitTarget):
    paths: Annotated[list[FilePath], Field(min_length=1, max_length=128)]
    message: Annotated[str, Field(min_length=1, max_length=8192, pattern=r"\S")]


def _deny(message: str) -> ToolError:
    return ToolError(message, ErrorCode.TOOL_DENIED)


def _walk_error(error: OSError) -> None:
    raise error


def _paths(raw: bytes) -> list[str]:
    return [os.fsdecode(entry) for entry in raw.split(b"\0")[:-1]]


class GitCheckout:
    """Shared checkout checks and commands used inside the caller's grant and lock."""

    def __init__(self, root: Path, branch: str, files: GuardedFiles, *, write: bool) -> None:
        self.root, self.files, self.write = root, files, write
        self.ref = f"refs/heads/{branch}"
        self.identity: tuple[int, ...] | None = None

    async def validate(self) -> None:
        from solakit.config import same_path

        root, metadata = self.root, self.root / ".git"
        profile = self.files.authority("files.write" if self.write else "files.read")
        if any(
            same_path(root, parent)
            for p in self.files.protected_paths(profile)
            for parent in (p, *p.parents)
        ):
            raise _deny("Checkout contains protected runtime, source, or prompt paths")
        with self.files.command_directory(str(root), write=self.write) as fd:
            self.identity = self.checkout_identity(fd)
        if any(os.path.lexists(parent / ".git") for parent in root.parents):
            raise _deny("Nested checkouts are unavailable")
        for directory, children, names in os.walk(root, onerror=_walk_error):
            path = Path(directory)
            if path == root:
                if ".git" not in children:
                    raise _deny("Configured checkout must own its Git metadata")
                children.remove(".git")
            elif any(name.casefold() == ".git" for name in (*children, *names)):
                raise _deny("Checkout contains a nested repository")
        for directory, children, names in os.walk(metadata, onerror=_walk_error):
            path = Path(directory)
            if any(not stat.S_ISDIR((path / child).lstat().st_mode) for child in children):
                raise _deny("Aliased Git metadata directories are unavailable")
            for name in names:
                self.files._regular((path / name).lstat())
        for name in ("commondir", "objects/info/alternates", "objects/info/http-alternates"):
            if os.path.lexists(metadata / name):
                raise _deny("Checkout uses shared Git metadata")
        code, top, truncated = await self.command("rev-parse", "--show-toplevel")
        if code or truncated or top.removesuffix(b"\n") != os.fsencode(root):
            raise _deny("Configured directory must be an ordinary Git checkout root")
        code, directory_bytes, truncated = await self.command("rev-parse", "--absolute-git-dir")
        if code or truncated or directory_bytes.removesuffix(b"\n") != os.fsencode(metadata):
            raise _deny("Configured checkout must own its Git metadata")

    @staticmethod
    def checkout_identity(fd: int) -> tuple[int, ...]:
        git_fd = os.open(".git", DIRECTORY, dir_fd=fd)
        try:
            root, metadata = os.fstat(fd), os.fstat(git_fd)
            return root.st_dev, root.st_ino, metadata.st_dev, metadata.st_ino
        finally:
            os.close(git_fd)

    async def path(self, raw: str, *, supplied: bool = True) -> str:
        if (
            (supplied and any(ch in raw for ch in "*?["))
            or "\0" in raw
            or any(part in ("", ".", "..") or part.casefold() == ".git" for part in raw.split("/"))
        ):
            raise _deny("Use a literal repository-relative filename")
        with self.files._parent(str(self.root / raw), write=self.write, protect_git=True) as (
            parent,
            name,
        ):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None:
                self.files._regular(info)
        if info is None:
            tracked = await self.checked(
                "ls-files", "-z", "--", raw, phase="checking a missing path"
            )
            if not self.write and not tracked:
                tree = await self.checked(
                    "ls-tree", "-z", "HEAD", "--", raw, phase="checking a deleted file"
                )
                if tree.startswith((b"100644 blob ", b"100755 blob ")):
                    tracked = tree.partition(b"\t")[2]
            if _paths(tracked) != [raw]:
                raise _deny("Missing filenames must be tracked files with an existing parent")
        return raw

    async def command(
        self,
        *argv: str,
        limit: int = MAX_CHECK_BYTES,
        effect: Invocation | None = None,
        confirm: bool = True,
    ) -> tuple[int, bytes, bool]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(
            LC_ALL="C",
            GIT_TERMINAL_PROMPT="0",
            GIT_ASKPASS="/usr/bin/false",
            SSH_ASKPASS="/usr/bin/false",
            SSH_ASKPASS_REQUIRE="never",
            GIT_ALLOW_PROTOCOL="file:git:http:https:ssh",
        )

        started = False

        async def launch(stdout: BinaryIO) -> asyncio.subprocess.Process:
            nonlocal started
            with self.files.command_directory(str(self.root), write=self.write) as fd:
                if self.checkout_identity(fd) != self.identity:
                    raise _deny("Checkout identity changed during the tool call")
                previous = effect.effect if effect is not None else "none"
                if effect is not None:
                    effect.begin_effect()
                try:
                    started = True
                    return await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-I",
                        "-S",
                        "-c",
                        DIRECTORY_LAUNCHER,
                        str(fd),
                        "-1",
                        *GIT,
                        *argv,
                        env=env,
                        pass_fds=(fd,),
                        start_new_session=True,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                except OSError:
                    if effect is not None:
                        effect.effect = previous
                    raise ToolError("Git could not be started") from None

        output = bytearray()
        truncated = False
        with open_pipe() as (reader, writer):
            spawn = asyncio.create_task(launch(writer))
            try:
                # Shield until the process handle is available, even if dispatch is cancelled.
                process = await asyncio.shield(spawn)
                writer.close()
                async with read_pipe(reader) as stream:
                    while chunk := await stream.read(65536):
                        remaining = limit - len(output)
                        output.extend(chunk[:remaining])
                        truncated = len(chunk) > remaining
                        if truncated:
                            break
                code = await process.wait() if not truncated else -1
            finally:
                if not started and not spawn.done():
                    spawn.cancel()
                await reap_group(spawn)
        if (
            effect is not None
            and confirm
            and (
                (argv[0] != "push" and not truncated)
                or code == 0
                or any(
                    line.startswith(b"!\t")
                    and line.split(b"\t")[-1].startswith((b"[rejected]", b"[remote rejected]"))
                    for line in output.splitlines()
                )
            )
        ):
            effect.confirm_effect()
        return code, bytes(output), truncated

    async def checked(
        self,
        *argv: str,
        phase: str,
        effect: Invocation | None = None,
        confirm: bool = True,
        limit: int = MAX_CHECK_BYTES,
    ) -> bytes:
        code, output, truncated = await self.command(
            *argv, effect=effect, confirm=confirm, limit=limit
        )
        if truncated:
            raise ToolError(f"Git output limit exceeded while {phase}", ErrorCode.BUDGET_EXCEEDED)
        if code:
            raise ToolError(f"Git failed while {phase}")
        return output

    async def status(self) -> ToolResult:
        branch_code, branch, cut = await self.command("symbolic-ref", "--quiet", "HEAD")
        if branch_code not in (0, 1) or cut:
            raise ToolError("Git could not read the current branch")
        code, output, cut = await self.command(*STATUS, limit=MAX_STATUS_BYTES)
        if code and not cut:
            raise ToolError("Git could not read checkout status")
        staged: list[JsonValue] = []
        unstaged: list[JsonValue] = []
        untracked: list[JsonValue] = []
        for record in output.split(b"\0")[:-1]:
            path = record[3:].decode(errors="replace")
            if record.startswith(b"?? "):
                untracked.append(path)
            else:
                if record[:1] != b" ":
                    staged.append(path)
                if record[1:2] != b" ":
                    unstaged.append(path)
        return ToolResult(
            data={
                "branch": branch.removeprefix(b"refs/heads/")
                .removesuffix(b"\n")
                .decode(errors="replace")
                if not branch_code
                else None,
                "staged": staged,
                "unstaged": unstaged,
                "untracked": untracked,
                "truncated": cut,
            }
        )

    async def diff(self, path: str | None) -> ToolResult:
        selected = (await self.path(path),) if path is not None else ()
        if not selected:
            changed = await self.checked(
                *DIFF, "--name-only", "-z", "HEAD", "--", phase="listing changed files"
            )
            for name in _paths(changed):
                await self.path(name, supplied=False)
        code, diff, truncated = await self.command(
            *DIFF, "HEAD", "--", *selected, limit=MAX_DIFF_BYTES
        )
        if code and not truncated:
            raise ToolError("Git could not read checkout diff")
        return ToolResult(data={"diff": diff.decode(errors="replace"), "truncated": truncated})

    async def head(self, ref: str) -> str:
        return (
            (
                await self.checked(
                    "rev-parse", "--verify", f"{ref}^{{commit}}", phase="reading commit"
                )
            )
            .strip()
            .decode("ascii")
        )

    async def clean(self, *, pull: bool) -> None:
        if any(os.path.lexists(self.root / ".git" / name) for name in IN_PROGRESS):
            raise _deny("Finish the existing Git operation before mutating the checkout")
        index = await self.checked("ls-files", "--stage", "-v", "-z", phase="checking the index")
        if any(
            not entry.startswith(b"H ") or entry.startswith(b"H 160000")
            for entry in index.split(b"\0")[:-1]
        ):
            raise _deny("Checkout index contains submodules, conflicts, or hidden changes")
        status = await self.checked(*STATUS, phase="checking checkout cleanliness")
        if any(pull or entry[:1] not in (b" ", b"?") for entry in status.split(b"\0")[:-1]):
            raise _deny(
                "Pull requires a clean checkout" if pull else "Publish requires an empty index"
            )

    async def verify_branch(self) -> None:
        symbolic = await self.checked("symbolic-ref", "HEAD", phase="checking current branch")
        if symbolic.removesuffix(b"\n") != os.fsencode(self.ref):
            raise _deny("Checkout is on a different branch")

    async def origin_url(self, *, publishing: bool) -> bytes:
        fetch = await self.checked("remote", "get-url", "--all", "origin", phase="checking origin")
        if publishing:
            push = await self.checked(
                "remote", "get-url", "--push", "--all", "origin", phase="checking push origin"
            )
            if fetch != push or len(fetch.splitlines()) != 1:
                raise _deny("Origin must have one matching fetch and push destination")
        return fetch

    async def fetch_origin(self, effect: Invocation | None = None, *, confirm: bool = True) -> str:
        await self.checked(
            "fetch",
            "--no-tags",
            "--no-prune",
            "--no-prune-tags",
            "--no-recurse-submodules",
            "--refmap=",
            "origin",
            self.ref,
            phase="fetching origin",
            effect=effect,
            confirm=confirm,
        )
        return await self.head("FETCH_HEAD")

    async def fast_forward(
        self, old: str, upstream: str, effect: Invocation, *, confirm: bool = True
    ) -> str:
        if old != upstream:
            await self.checked(
                "merge-base", "--is-ancestor", old, upstream, phase="checking fast-forward ancestry"
            )
            await self.checked(
                "merge",
                "--ff-only",
                "--no-autostash",
                "--no-overwrite-ignore",
                upstream,
                phase="fast-forwarding to origin",
                effect=effect,
                confirm=confirm,
            )
        new = await self.head("HEAD")
        if new != upstream:
            raise ToolError("Local HEAD differs from the fetched branch")
        return new

    async def mutate(self, invocation: Invocation, args: GitTarget) -> ToolResult:
        invocation.uncertain_advice = ". Last verified Git phase: checkout validated."
        await self.verify_branch()
        pull = not isinstance(args, GitPublish)
        await self.clean(pull=pull)
        old = await self.head("HEAD")
        if isinstance(args, GitPublish):
            if "\0" in args.message or len(set(args.paths)) != len(args.paths):
                raise ToolError(
                    "Use distinct filenames and a NUL-free message", ErrorCode.INVALID_INPUT
                )
            for path in args.paths:
                await self.path(path)
            await self.origin_url(publishing=True)
        upstream = await self.fetch_origin(invocation)
        invocation.uncertain_advice = ". Last verified Git phase: upstream fetched."
        await self.clean(pull=pull)
        if await self.head("HEAD") != old:
            raise _deny("Local HEAD changed during synchronization")
        if isinstance(args, GitPublish):
            if old != upstream:
                raise _deny("Publish requires local HEAD to match the freshly fetched branch")
            invocation.uncertain_advice = ". Last verified Git phase: upstream matched local HEAD."
            for path in args.paths:
                await self.path(path)
            await self.checked(
                "add",
                "-A",
                "--",
                *args.paths,
                phase="staging selected files",
                effect=invocation,
            )
            invocation.uncertain_advice = ". Last verified Git phase: selected files staged."
            await self.checked(
                "commit",
                "--only",
                "-m",
                args.message,
                "--",
                *args.paths,
                phase="committing selected files",
                effect=invocation,
            )
            commit = await self.head("HEAD")
            invocation.uncertain_advice = (
                f". Last verified Git phase: local commit {commit}. Inspect before retrying."
            )
            await self.checked(
                "push",
                "--porcelain",
                "--no-follow-tags",
                "--recurse-submodules=no",
                "origin",
                f"{commit}:{self.ref}",
                phase="pushing the local commit",
                effect=invocation,
            )
            invocation.uncertain_advice = f". Last verified Git phase: pushed commit {commit}."
            return ToolResult(data={"commit": commit, "push": "succeeded"})
        new = await self.fast_forward(old, upstream, invocation)
        invocation.uncertain_advice = f". Last verified Git phase: synchronized at commit {new}."
        return ToolResult(data={"old_commit": old, "new_commit": new})


async def run_git(context: ToolContext, args: GitTarget) -> ToolResult:
    from solakit.config import GIT_READ, GIT_WRITE, checkout_reserved
    from solakit.tools.dispatch import Invocation

    invocation, files = context.services, context.files
    if not isinstance(invocation, Invocation) or not isinstance(files, GuardedFiles):
        raise _deny("Git requires the built-in dispatcher")
    name = invocation.call.name
    if name not in GIT_READ | GIT_WRITE:
        raise _deny("Git requires its named tool")
    write = name in GIT_WRITE

    def checkout() -> GitCheckout:
        repo = invocation.authority().git_repositories.get(args.repository)
        if repo is None:
            raise _deny("Repository alias is outside current Git authority")

        def authority(service: str) -> EffectiveProfile:
            profile = invocation.authority(service)
            if profile.git_repositories.get(args.repository) != repo or (
                write and checkout_reserved(repo.path, profile.git_reserved)
            ):
                raise _deny("Repository alias is outside current Git authority")
            return profile

        current = invocation.dispatcher.current(invocation.admitted)
        guarded = GuardedFiles(
            authority,
            tuple(files.protected_paths(current)),
            files.begin_effect,
            files.confirm_effect,
            files.record_artifact,
        )
        return GitCheckout(repo.path, repo.branch, guarded, write=write)

    repo = checkout()
    async with invocation.dispatcher.checkout_lock(repo.root):
        repo = checkout()
        await repo.validate()
        if write:
            return await repo.mutate(invocation, args)
        if isinstance(args, GitDiff):
            return await repo.diff(args.path)
        return await repo.status()
