"""Read-only GitHub API tools; no notification acknowledgements or writes."""

from solakit.tools import ToolContext, ToolResult, tool
from solakit.tools._read_api import invoke


@tool(
    name="github.notifications",
    profiles=["*"],
    summary="Read the authenticated user's GitHub notifications.",
    keywords=("github", "notifications", "inbox"),
)
async def notifications(ctx: ToolContext, limit: int = 30) -> ToolResult:
    """Read one batch of 1–100 GitHub notifications without marking them read."""
    return await invoke(ctx, "notifications", limit=limit)


@tool(
    name="github.pr_status",
    profiles=["*"],
    summary="Read a pull request, commit status, and CI checks.",
    keywords=("github", "pr", "pull request", "status", "ci"),
)
async def pr_status(ctx: ToolContext, owner: str, repo: str, n: int) -> ToolResult:
    """Read PR metadata, combined commit status and the first page of CI check runs."""
    return await invoke(ctx, "pr_status", owner=owner, repo=repo, n=n)


@tool(
    name="github.latest_release",
    profiles=["*"],
    summary="Read a repository's latest published release.",
    keywords=("github", "release", "version", "changelog"),
)
async def latest_release(ctx: ToolContext, owner: str, repo: str) -> ToolResult:
    """Read the latest published GitHub release; drafts and prereleases are excluded."""
    return await invoke(ctx, "latest_release", owner=owner, repo=repo)
