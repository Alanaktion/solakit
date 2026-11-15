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
    """Check a named repository for uncommitted or untracked changes, e.g. before a
    commit or pull.

    Returns short Git status within read scopes."""
    return await ctx.git(repository).status()


@tool(
    summary="See what changed in a named repository before committing",
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
    """See exactly what changed in a named repository, e.g. to review before committing.

    Shows staged and unstaged changes against HEAD; no external diff drivers."""
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
    """See a named repository's recent history, up to 100 commits."""
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
    """Bring a clean named repository up to date with its remote (fast-forward only).

    The repository must be inside profile write roots."""
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
    """Save and publish work: commit exactly the named files and push.

    Needs write reach and the push effect."""
    return await ctx.git(repository).commit_push(paths, message)
