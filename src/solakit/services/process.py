"""Bounded POSIX process execution for built-in low-level tools and trusted packs."""

import asyncio
import codecs
import os
import signal
from pathlib import Path

DRAIN_GRACE_S = 5


async def execute(
    argv: list[str],
    directory: Path,
    timeout: float,  # noqa: ASYNC109 - bounds and kills the process group
    limit: int,
    *,
    environment: dict[str, str] | None = None,
) -> dict:
    # Deliberately avoid inheriting backend/API credentials. This is hygiene,
    # not isolation: host-user shell commands can still access host files.
    environment = (
        dict(environment)
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

    captured: dict[str, list[str]] = {"stdout": [], "stderr": []}

    async def drain(name, stream):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        kept, size, retained = captured[name], 0, 0
        while True:
            chunk = await stream.read(65536)
            text = decoder.decode(chunk, final=not chunk)
            size += len(text)
            prefix = text[: max(0, limit - retained)]
            if prefix:
                kept.append(prefix)
                retained += len(prefix)
            if not chunk:
                return "".join(kept), size <= limit

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
            return [("".join(captured[name]), False) for name in ("stdout", "stderr")]

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
