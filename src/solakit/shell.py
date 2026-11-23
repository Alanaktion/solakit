"""Run literal argv with a guarded working directory and bounded process ownership."""

import asyncio
import os
import signal
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from tempfile import gettempdir
from typing import TYPE_CHECKING, Annotated, BinaryIO

from pydantic import Field, field_validator

from solakit.records import ErrorCode
from solakit.sdk import ToolArguments, ToolError, ToolResult

if TYPE_CHECKING:
    from solakit.dispatch import Invocation
    from solakit.files import GuardedFiles

MAX_STREAM_BYTES = 16 * 1024

# subprocess accepts only path-based cwd. Use the verified descriptor across exec, including
# on macOS where /dev/fd cannot be used as cwd. The status pipe closes on successful exec.
_LAUNCHER = """\
import os
import sys

directory, status = map(int, sys.argv[1:3])
os.set_inheritable(status, False)
try:
    os.fchdir(directory)
    os.close(directory)
    argv = sys.argv[3:]
    os.execvpe(argv[0], argv, os.environ)
except (OSError, ValueError):
    os.write(status, b"!")
    os._exit(126)
"""


class ShellExec(ToolArguments):
    argv: Annotated[list[str], Field(min_length=1, max_length=128)]
    cwd: Annotated[str, Field(min_length=1, max_length=4096)] | None = None

    @field_validator("argv")
    @classmethod
    def bounded_argv(cls, argv: list[str]) -> list[str]:
        if not argv[0] or any("\x00" in arg for arg in argv):
            raise ValueError("argv requires an executable and NUL-free arguments")
        if sum(len(os.fsencode(arg)) + 1 for arg in argv) > 32 * 1024:
            raise ValueError("argv exceeds 32 KiB")
        return argv


@contextmanager
def _pipe() -> Iterator[tuple[BinaryIO, BinaryIO]]:
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "rb", buffering=0) as reader,
        os.fdopen(write_fd, "wb", buffering=0) as writer,
    ):
        yield reader, writer


async def _capture(pipe: BinaryIO) -> tuple[str, bool]:
    loop = asyncio.get_running_loop()
    stream = asyncio.StreamReader()
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stream), pipe)
    retained = bytearray()
    truncated = False
    try:
        while chunk := await stream.read(8192):
            remaining = MAX_STREAM_BYTES - len(retained)
            retained.extend(chunk[:remaining])
            truncated |= len(chunk) > remaining
    finally:
        transport.close()
    return retained.decode("utf-8", errors="replace"), truncated


async def _reap(spawn: asyncio.Task[asyncio.subprocess.Process]) -> None:
    try:
        process = await spawn
    except Exception, asyncio.CancelledError:
        # The invocation is already handling the launch outcome.
        return
    _kill_group(process.pid)
    await process.wait()


def _kill_group(pid: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


async def execute_shell(invocation: Invocation, args: ShellExec, files: GuardedFiles) -> ToolResult:
    if invocation.call.name != "shell.exec":
        raise ToolError("Shell execution requires its named tool", ErrorCode.TOOL_DENIED)
    invocation.uncertain_advice = ". Inspect the workspace before another attempt."
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8", "TMPDIR": gettempdir()}

    with ExitStack() as pipes:
        stdout_read, stdout_write = pipes.enter_context(_pipe())
        stderr_read, stderr_write = pipes.enter_context(_pipe())
        status_read, status_write = pipes.enter_context(_pipe())

        async def launch() -> asyncio.subprocess.Process:
            invocation.begin_effect()
            with ExitStack() as directory:
                try:
                    fd = directory.enter_context(files.command_directory(args.cwd or "."))
                except Exception:
                    invocation.effect = "none"
                    raise
                status = status_write.fileno()
                try:
                    return await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-I",
                        "-S",
                        "-c",
                        _LAUNCHER,
                        str(fd),
                        str(status),
                        *args.argv,
                        pass_fds=(fd, status),
                        env=env,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=stdout_write,
                        stderr=stderr_write,
                        start_new_session=True,
                    )
                except OSError:
                    invocation.effect = "none"
                    raise ToolError("Local program could not be started") from None

        spawn = asyncio.create_task(launch())
        group_closed = False
        try:
            process = await asyncio.shield(spawn)
            stdout_write.close()
            stderr_write.close()
            status_write.close()
            async with asyncio.TaskGroup() as streams:
                stdout_task = streams.create_task(_capture(stdout_read))
                stderr_task = streams.create_task(_capture(stderr_read))
                exit_code = await process.wait()
                _kill_group(process.pid)
                group_closed = True
        finally:
            if not group_closed:
                if invocation.effect == "none" and not spawn.done():
                    spawn.cancel()
                # Cancellation must not lose a process handle while asyncio is still spawning it.
                cleanup = asyncio.create_task(_reap(spawn))
                interrupted = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        interrupted = True
                cleanup.result()
                if interrupted:
                    raise asyncio.CancelledError

        if status_read.read(1):
            invocation.effect = "none"
            raise ToolError("Local program could not be started")

    invocation.confirm_effect()
    stdout, stdout_truncated = stdout_task.result()
    stderr, stderr_truncated = stderr_task.result()
    if exit_code:
        diagnostic = invocation.dispatcher.redactor.redact((stderr or stdout).strip())[:500]
        suffix = f": {diagnostic}" if diagnostic else ""
        raise ToolError(f"Local program exited with code {exit_code}{suffix}")
    return ToolResult(
        data={
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
    )
