"""Host-user shell tools with configuration-driven access."""

import json
import math
from typing import Annotated

from pydantic import Field

from solakit.services.process import execute
from solakit.tools import ToolContext, ToolError, ToolResult, tool


async def run_process(ctx, argv, directory, timeout_s):
    if "shell" not in ctx.config.profiles[ctx.profile].effects:
        raise ToolError("Missing effect: shell")
    if (
        not argv
        or len(argv) > 128
        or sum(map(len, argv)) > 32000
        or any("\x00" in arg for arg in argv)
    ):
        raise ToolError(
            "Command must have 1–128 arguments, no NULs and at most 32000 characters"
        )
    if not math.isfinite(timeout_s) or not 0 < timeout_s <= 300:
        raise ToolError("Timeout must be greater than zero and at most 300 seconds")
    directory = ctx.path(directory, write=True)
    if not directory.is_dir():
        raise ToolError("Working directory must exist")
    profile = ctx.config.profiles[ctx.profile]
    try:
        result = await execute(
            argv,
            directory,
            min(timeout_s, profile.tool_timeout_s),
            profile.tool_output_max_chars,
        )
    except OSError as exc:
        raise ToolError("Command could not be started") from exc
    return ToolResult(
        content=json.dumps(result, ensure_ascii=False),
        error=result["exit_code"] != 0 or result["timed_out"],
        content_complete=result["stdout_complete"] and result["stderr_complete"],
    )


@tool(
    summary="Execute literal argv without shell expansion within cwd scopes",
    keywords=("shell", "command", "terminal"),
    name="shell.exec",
    profiles=["*"],
    writes=["filesystem"],
    effect="shell",
)
async def exec_command(
    ctx: ToolContext,
    argv: Annotated[
        list[str],
        Field(
            description=(
                "Executable then literal arguments. Pipes, redirects, globs and ~ "
                "are not interpreted; use shell.run for those."
            )
        ),
    ],
    directory: Annotated[
        str,
        Field(
            description=(
                "Working directory inside profile write scopes; omit for the "
                "configured server.cwd."
            )
        ),
    ] = ".",
    timeout_s: Annotated[
        float,
        Field(
            gt=0,
            le=300,
            description=(
                "Maximum seconds, additionally bounded by the profile tool timeout."
            ),
        ),
    ] = 30,
) -> ToolResult:
    """Run literal argv (no shell) with host-user access and configured grants.

    For pipes, redirects or globs use shell.run. Use only when no granted domain
    tool covers the task. Do not use shell to
    bypass a domain error or discover runtime configuration or credentials."""
    return await run_process(ctx, argv, directory, timeout_s)


@tool(
    summary="Run a /bin/sh command with shell expansion within cwd scopes",
    keywords=("shell", "command", "terminal"),
    name="shell.run",
    profiles=["*"],
    writes=["filesystem"],
    effect="shell",
)
async def run(
    ctx: ToolContext,
    command: Annotated[
        str,
        Field(
            description=(
                "Nonempty /bin/sh script; use shell.exec when shell syntax "
                "is unnecessary."
            )
        ),
    ],
    directory: Annotated[
        str,
        Field(
            description=(
                "Working directory inside profile write scopes; omit for the "
                "configured server.cwd."
            )
        ),
    ] = ".",
    timeout_s: Annotated[
        float,
        Field(
            gt=0,
            le=300,
            description=(
                "Maximum seconds, additionally bounded by the profile tool timeout."
            ),
        ),
    ] = 30,
) -> ToolResult:
    """Run a /bin/sh script with host-user access and configured grants.

    Use shell.exec for literal argv; use this for shell syntax. Use only when no
    granted domain tool covers the task. Do not bypass domain errors or discover
    runtime configuration or credentials."""
    if not command.strip():
        raise ToolError("Shell script must be nonempty")
    return await run_process(ctx, ["/bin/sh", "-c", command], directory, timeout_s)
