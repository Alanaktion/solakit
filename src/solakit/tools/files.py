"""Thin wrappers around scope-checked Python filesystem services."""

import json
from typing import Annotated

from pydantic import Field

from solakit.tools import ToolContext, ToolResult, tool


@tool(
    summary="Read a local file within filesystem scopes",
    keywords=("file", "read", "disk"),
    name="files.read",
    profiles=["*"],
)
async def read(
    ctx: ToolContext,
    path: Annotated[
        str,
        Field(
            description=(
                "File or directory path inside this profile’s filesystem scopes."
            )
        ),
    ],
) -> ToolResult:
    """Read a bounded UTF-8 operator file inside profile filesystem scopes.

    Prefer a granted domain tool for its data; do not inspect runtime configuration
    or credentials to discover resources. Use the domain discovery tool instead."""
    return await ctx.fs.read(path)


@tool(
    summary="Create a new local file within write scopes",
    keywords=("file", "create", "new", "save"),
    name="files.write",
    profiles=["*"],
    writes=["filesystem"],
)
async def write(
    ctx: ToolContext,
    path: Annotated[
        str,
        Field(
            description=(
                "File or directory path inside this profile’s filesystem scopes."
            )
        ),
    ],
    content: Annotated[str, Field(description="Exact UTF-8 text to write.")],
) -> ToolResult:
    """Create a UTF-8 file; never overwrite an existing file."""
    target = await ctx.fs.write(path, content)
    return ToolResult(
        content=f"Created {target} ({len(content.encode())} bytes)",
        artifacts=[str(target)],
    )


@tool(
    summary="Replace exact matching text in a local file",
    keywords=("file", "edit", "replace"),
    name="files.edit",
    profiles=["*"],
    writes=["filesystem"],
)
async def edit(
    ctx: ToolContext,
    path: Annotated[
        str,
        Field(
            description=(
                "File or directory path inside this profile’s filesystem scopes."
            )
        ),
    ],
    old: Annotated[
        str, Field(description="Exact nonempty text occurring once in the file.")
    ],
    new: Annotated[
        str, Field(description="Replacement text for the unique old match.")
    ],
) -> ToolResult:
    """Replace one exact nonempty match; fail on zero or multiple matches."""
    target = await ctx.fs.edit(path, old, new)
    return ToolResult(
        content=f"Edited {target}: replaced 1 match ({len(old)} → {len(new)} chars)",
        artifacts=[str(target)],
    )


@tool(
    summary="Append text to a local file",
    keywords=("file", "append"),
    name="files.append",
    profiles=["*"],
    writes=["filesystem"],
)
async def append(
    ctx: ToolContext,
    path: Annotated[
        str,
        Field(
            description=(
                "File or directory path inside this profile’s filesystem scopes."
            )
        ),
    ],
    content: Annotated[str, Field(description="Exact UTF-8 text to write.")],
) -> ToolResult:
    """Append text to an existing regular file inside profile write scopes."""
    target = await ctx.fs.append(path, content)
    return ToolResult(
        content=f"Appended {len(content.encode())} bytes to {target}",
        artifacts=[str(target)],
    )


@tool(
    summary="List a local directory",
    keywords=("file", "directory", "list"),
    name="files.list",
    profiles=["*"],
)
async def list_files(
    ctx: ToolContext,
    path: Annotated[
        str,
        Field(
            description=(
                "File or directory path inside this profile’s filesystem scopes."
            )
        ),
    ],
    limit: Annotated[
        int, Field(ge=1, le=500, description="Maximum directory entries to return.")
    ] = 100,
) -> ToolResult:
    """List up to 500 operator-file entries inside profile scopes, without recursion.

    Prefer granted domain tools for their data and resource discovery. Do not browse
    runtime configuration or credentials to work around a failed domain call."""
    entries, more = await ctx.fs.entries(path, limit)
    return ToolResult(
        content=json.dumps({"entries": entries, "more": more}, ensure_ascii=False)
    )
