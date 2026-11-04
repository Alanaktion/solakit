"""Read-only repository primitive; repository identity always comes from config."""

import asyncio

from solakit.tools import ToolContext, ToolError, ToolResult, tool


@tool(name="repo_git.status", profiles=["interactive", "maintenance"])
async def status(ctx: ToolContext, repository: str) -> ToolResult:
    """Return short git status for a named repository within profile read scopes."""
    if repository not in ctx.config.repositories:
        raise ToolError("Unknown repository")
    path = ctx.path(str(ctx.config.repositories[repository]))
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(path),
        "status",
        "--short",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await process.communicate()
    except BaseException:
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ToolError("Repository status failed")
    return ToolResult(content=stdout.decode("utf-8", errors="replace"))
