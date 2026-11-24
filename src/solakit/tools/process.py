"""Descriptor-based working directories and cleanup for owned subprocesses."""

import asyncio
import os
import signal
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from typing import BinaryIO

# subprocess takes a path for cwd. fchdir also works on macOS, where /dev/fd cannot be cwd.
DIRECTORY_LAUNCHER = """\
import os
import sys

directory, status = map(int, sys.argv[1:3])
if status >= 0:
    os.set_inheritable(status, False)
try:
    os.fchdir(directory)
    os.close(directory)
    argv = sys.argv[3:]
    os.execvpe(argv[0], argv, os.environ)
except (OSError, ValueError):
    if status >= 0:
        os.write(status, b"!")
    os._exit(126)
"""


def kill_group(pid: int) -> None:
    # Darwin may report EPERM when the group has only an exited, unreaped leader.
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


@contextmanager
def open_pipe() -> Iterator[tuple[BinaryIO, BinaryIO]]:
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "rb", buffering=0) as reader,
        os.fdopen(write_fd, "wb", buffering=0) as writer,
    ):
        yield reader, writer


@asynccontextmanager
async def read_pipe(pipe: BinaryIO) -> AsyncIterator[asyncio.StreamReader]:
    loop = asyncio.get_running_loop()
    stream = asyncio.StreamReader()
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stream), pipe)
    try:
        yield stream
    finally:
        transport.close()


async def _reap(spawn: asyncio.Task[asyncio.subprocess.Process]) -> None:
    try:
        process = await spawn
    except Exception, asyncio.CancelledError:
        return
    kill_group(process.pid)
    await process.wait()


async def reap_group(spawn: asyncio.Task[asyncio.subprocess.Process]) -> None:
    # Keep the handle through cancellation, including cancellation during spawn.
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
