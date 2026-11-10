"""Bounded POSIX process execution for built-in low-level tools and trusted packs."""

import asyncio
import os
import signal
from pathlib import Path

DRAIN_GRACE_S = 5


async def execute(
    argv: list[str],
    directory: Path,
    timeout: float,
    limit: int,
    *,
    environment: dict[str, str] | None = None,
) -> dict:
    # Deliberately avoid inheriting backend/API credentials. This is hygiene,
    # not isolation: host-user shell commands can still access host files.
    environment = (
        environment
        if environment is not None
        else {
            name: value
            for name, value in os.environ.items()
            if name in {"PATH", "HOME", "LANG", "TMPDIR", "TERM"}
            or name.startswith("LC_")
        }
    )
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

    captured = {"stdout": bytearray(), "stderr": bytearray()}

    async def drain(name, stream):
        kept, size = captured[name], 0
        while chunk := await stream.read(65536):
            size += len(chunk)
            kept.extend(chunk[: max(0, limit - len(kept))])
        return kept.decode("utf-8", errors="replace"), size <= limit

    async def settle():
        # A descendant that left the killed process group can keep the pipes
        # open indefinitely. Bound the final drain and report what was kept.
        try:
            async with asyncio.timeout(DRAIN_GRACE_S):
                out, err, _ = await asyncio.gather(stdout, stderr, process.wait())
                return out, err
        except TimeoutError:
            for task in (stdout, stderr):
                task.cancel()
            await asyncio.gather(stdout, stderr, return_exceptions=True)
            return [
                (captured[name].decode("utf-8", errors="replace"), False)
                for name in ("stdout", "stderr")
            ]

    stdout = asyncio.create_task(drain("stdout", process.stdout))
    stderr = asyncio.create_task(drain("stderr", process.stderr))
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
        if not isinstance(exc, TimeoutError):
            await settle()
            raise
        timed_out = True
    if timed_out:
        out, err = await settle()
    return {
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout_complete": out[1],
        "stderr_complete": err[1],
        "stdout": out[0],
        "stderr": err[0],
    }
