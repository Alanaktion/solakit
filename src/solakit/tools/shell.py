"""Host-user shell tools with configuration-driven access."""

import json
import math

from solakit.services.process import execute
from solakit.tools import ToolContext, ToolError, ToolResult, tool


async def run_process(ctx, argv, directory, timeout_s):
    if "shell.exec" not in ctx.config.profiles[ctx.profile].capabilities:
        raise ToolError("Missing capability: shell.exec")
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


@tool(name="shell.exec", profiles=["*"], writes=["filesystem", "shell.exec"])
async def exec_command(
    ctx: ToolContext, argv: list[str], directory: str, timeout_s: float = 30
) -> ToolResult:
    """Run literal argv with host-user access and configured grants."""
    return await run_process(ctx, argv, directory, timeout_s)


@tool(name="shell.run", profiles=["*"], writes=["filesystem", "shell.exec"])
async def run(
    ctx: ToolContext, command: str, directory: str, timeout_s: float = 30
) -> ToolResult:
    """Run a /bin/sh script with host-user access and configured grants."""
    if not command.strip():
        raise ToolError("Shell script must be nonempty")
    return await run_process(ctx, ["/bin/sh", "-c", command], directory, timeout_s)
