"""Read-only GitHub API tools; no notification acknowledgements or writes."""

from typing import Annotated

from pydantic import Field

from solakit.tools import ToolContext, ToolResult, tool
from solakit.tools._read_api import invoke


@tool(
    name="github.notifications",
    profiles=["*"],
    summary="Read the authenticated user's GitHub notifications.",
    keywords=("github", "notifications", "inbox"),
)
async def notifications(
    ctx: ToolContext,
    limit: Annotated[
        int, Field(description="Maximum number of entries to return.", ge=1, le=100)
    ] = 30,
) -> ToolResult:
    """Read one batch of 1–100 GitHub notifications without marking them read."""
    return await invoke(ctx, "notifications", limit=limit)


@tool(
    name="github.pr_status",
    profiles=["*"],
    summary="Read a pull request, commit status, and CI checks.",
    keywords=("github", "pr", "pull request", "status", "ci"),
)
async def pr_status(
    ctx: ToolContext,
    owner: Annotated[
        str,
        Field(
            description="GitHub repository owner login or organization, without a URL."
        ),
    ],
    repo: Annotated[
        str, Field(description="GitHub repository name, without owner or URL.")
    ],
    n: Annotated[
        int, Field(description="Positive pull request number, not an issue URL.", ge=1)
    ],
) -> ToolResult:
    """Read PR metadata, combined commit status and the first page of CI check runs."""
    return await invoke(ctx, "pr_status", owner=owner, repo=repo, n=n)


@tool(
    name="github.latest_release",
    profiles=["*"],
    summary="Read a repository's latest published release.",
    keywords=("github", "release", "version", "changelog"),
)
async def latest_release(
    ctx: ToolContext,
    owner: Annotated[
        str,
        Field(
            description="GitHub repository owner login or organization, without a URL."
        ),
    ],
    repo: Annotated[
        str, Field(description="GitHub repository name, without owner or URL.")
    ],
) -> ToolResult:
    """Read the latest published GitHub release; drafts and prereleases are excluded."""
    return await invoke(ctx, "latest_release", owner=owner, repo=repo)
