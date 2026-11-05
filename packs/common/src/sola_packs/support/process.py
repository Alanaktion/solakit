"""Bounded POSIX process execution for trusted low-level packs."""

import asyncio
import os
import signal


async def execute(argv, directory, timeout, limit):
    # Deliberately avoid inheriting backend/API credentials. This is hygiene,
    # not isolation: host-user shell commands can still access host files.
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in {"PATH", "HOME", "LANG", "TMPDIR", "TERM"} or name.startswith("LC_")
    }
    environment.setdefault("PATH", "/usr/bin:/bin")
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=directory,
        env=environment,
        start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def drain(stream):
        kept, size = bytearray(), 0
        while chunk := await stream.read(65536):
            size += len(chunk)
            kept.extend(chunk[: max(0, limit - len(kept))])
        return kept.decode("utf-8", errors="replace"), size <= limit

    stdout = asyncio.create_task(drain(process.stdout))
    stderr = asyncio.create_task(drain(process.stderr))
    timed_out = False
    try:
        async with asyncio.timeout(timeout):
            await process.wait()
            out, err = await asyncio.shield(asyncio.gather(stdout, stderr))
    except BaseException as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        if not isinstance(exc, TimeoutError):
            await asyncio.gather(stdout, stderr, return_exceptions=True)
            raise
        timed_out = True
    if timed_out:
        out, err = await asyncio.gather(stdout, stderr)
    return {
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout_complete": out[1],
        "stderr_complete": err[1],
        "stdout": out[0],
        "stderr": err[0],
    }
