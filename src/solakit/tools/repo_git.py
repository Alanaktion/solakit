"""Generic Git tool wrappers; subprocess and scope logic live in services."""

from typing import Annotated

from pydantic import Field

from solakit.tools import ToolContext, ToolResult, tool


@tool(
    summary="Inspect the working tree and index of a named repository",
    keywords=("git", "repository", "status", "changes"),
    name="repo_git.status",
    profiles=["*"],
)
async def status(
    ctx: ToolContext,
    repository: Annotated[
        str,
        Field(
            description="Configured repository alias, not an arbitrary filesystem path."
        ),
    ],
) -> ToolResult:
    """Return short Git status for a named repository within read scopes."""
    return await ctx.git(repository).status()


@tool(
    summary="Read staged and unstaged repository changes",
    keywords=("git", "diff", "changes", "patch"),
    name="repo_git.diff",
    profiles=["*"],
)
async def diff(
    ctx: ToolContext,
    repository: Annotated[
        str,
        Field(
            description="Configured repository alias, not an arbitrary filesystem path."
        ),
    ],
) -> ToolResult:
    """Show staged and unstaged changes against HEAD; no external diff drivers."""
    return await ctx.git(repository).diff()


@tool(
    summary="Read recent commits from a named repository",
    keywords=("git", "log", "commits", "history"),
    name="repo_git.log",
    profiles=["*"],
)
async def log(
    ctx: ToolContext,
    repository: Annotated[
        str,
        Field(
            description="Configured repository alias, not an arbitrary filesystem path."
        ),
    ],
    count: Annotated[
        int, Field(description="Maximum number of results to return.")
    ] = 10,
) -> ToolResult:
    """Show up to 100 recent commits for a named repository."""
    return await ctx.git(repository).log(count)


@tool(
    summary="Fast-forward a clean named repository",
    keywords=("git", "pull", "update", "repository"),
    name="repo_git.pull",
    profiles=["*"],
    writes=["filesystem"],
)
async def pull(
    ctx: ToolContext,
    repository: Annotated[
        str,
        Field(
            description="Configured repository alias, not an arbitrary filesystem path."
        ),
    ],
) -> ToolResult:
    """Fast-forward a clean repository inside profile write roots."""
    return await ctx.git(repository).pull()


@tool(
    summary="Commit exact named files and push the commit",
    keywords=("git", "commit", "push", "publish"),
    name="repo_git.commit_push",
    profiles=["*"],
    writes=["filesystem"],
    effect="push",
)
async def commit_push(
    ctx: ToolContext,
    repository: Annotated[
        str,
        Field(
            description="Configured repository alias, not an arbitrary filesystem path."
        ),
    ],
    paths: Annotated[
        list[str],
        Field(
            description=(
                "Exact repository-relative file paths to commit; no "
                "directories, globs or .git paths."
            )
        ),
    ],
    message: Annotated[
        str, Field(description="Git commit message describing the named file changes.")
    ],
) -> ToolResult:
    """Commit named files and push; needs write reach and the push effect."""
    return await ctx.git(repository).commit_push(paths, message)
