"""Run literal argv with a guarded working directory and bounded process ownership."""

import asyncio
import os
import sys
from contextlib import ExitStack
from tempfile import gettempdir
from typing import TYPE_CHECKING, Annotated, BinaryIO

from pydantic import Field, field_validator

from solakit.process import DIRECTORY_LAUNCHER, kill_group, open_pipe, read_pipe, reap_group
from solakit.records import ErrorCode
from solakit.sdk import ToolArguments, ToolError, ToolResult

if TYPE_CHECKING:
    from solakit.dispatch import Invocation
    from solakit.files import GuardedFiles

MAX_STREAM_BYTES = 16 * 1024


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


async def _capture(pipe: BinaryIO) -> tuple[str, bool]:
    retained = bytearray()
    truncated = False
    async with read_pipe(pipe) as stream:
        while chunk := await stream.read(8192):
            remaining = MAX_STREAM_BYTES - len(retained)
            retained.extend(chunk[:remaining])
            truncated |= len(chunk) > remaining
    return retained.decode("utf-8", errors="replace"), truncated


async def execute_shell(invocation: Invocation, args: ShellExec, files: GuardedFiles) -> ToolResult:
    if invocation.call.name != "shell.exec":
        raise ToolError("Shell execution requires its named tool", ErrorCode.TOOL_DENIED)
    invocation.uncertain_advice = ". Inspect the workspace before another attempt."
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8", "TMPDIR": gettempdir()}

    with ExitStack() as pipes:
        stdout_read, stdout_write = pipes.enter_context(open_pipe())
        stderr_read, stderr_write = pipes.enter_context(open_pipe())
        status_read, status_write = pipes.enter_context(open_pipe())

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
                        DIRECTORY_LAUNCHER,
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
                kill_group(process.pid)
                group_closed = True
        finally:
            if not group_closed:
                if invocation.effect == "none" and not spawn.done():
                    spawn.cancel()
                await reap_group(spawn)

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
