"""Generic Git tool wrappers; subprocess and scope logic live in services."""

from solakit.tools import ToolContext, ToolResult, tool


@tool(name="repo_git.status", profiles=["*"])
async def status(ctx: ToolContext, repository: str) -> ToolResult:
    """Return short Git status for a named repository within read scopes."""
    return await ctx.git(repository).status()


@tool(name="repo_git.diff", profiles=["*"])
async def diff(ctx: ToolContext, repository: str) -> ToolResult:
    """Show staged and unstaged changes against HEAD; no external diff drivers."""
    return await ctx.git(repository).diff()


@tool(name="repo_git.log", profiles=["*"])
async def log(ctx: ToolContext, repository: str, count: int = 10) -> ToolResult:
    """Show up to 100 recent commits for a named repository."""
    return await ctx.git(repository).log(count)


@tool(
    name="repo_git.pull",
    profiles=["*"],
    writes=["filesystem"],
)
async def pull(ctx: ToolContext, repository: str) -> ToolResult:
    """Fast-forward a clean named repository; requires its exact repo write scope."""
    return await ctx.git(repository).pull()


@tool(name="repo_git.commit_push", profiles=["*"], writes=["filesystem"], effect="push")
async def commit_push(
    ctx: ToolContext, repository: str, paths: list[str], message: str
) -> ToolResult:
    """Commit named files and push; requires repository write and git.push grants."""
    return await ctx.git(repository).commit_push(paths, message)
