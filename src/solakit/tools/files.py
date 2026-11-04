"""Thin wrappers around scope-checked Python filesystem services."""

import json

from solakit.tools import ToolContext, ToolResult, tool


@tool(name="files.read", profiles=["*"])
async def read(ctx: ToolContext, path: str) -> ToolResult:
    """Read a bounded UTF-8 regular file inside profile filesystem scopes."""
    return await ctx.fs.read(path)


@tool(name="files.write", profiles=["*"], writes=["filesystem"])
async def write(ctx: ToolContext, path: str, content: str) -> ToolResult:
    """Create a UTF-8 file; never overwrite an existing file."""
    target = await ctx.fs.write(path, content)
    return ToolResult(content=str(target), artifacts=[str(target)])


@tool(name="files.edit", profiles=["*"], writes=["filesystem"])
async def edit(ctx: ToolContext, path: str, old: str, new: str) -> ToolResult:
    """Replace one exact nonempty match; fail on zero or multiple matches."""
    target = await ctx.fs.edit(path, old, new)
    return ToolResult(content=str(target), artifacts=[str(target)])


@tool(name="files.append", profiles=["*"], writes=["filesystem"])
async def append(ctx: ToolContext, path: str, content: str) -> ToolResult:
    """Append text to an existing regular file inside profile write scopes."""
    target = await ctx.fs.append(path, content)
    return ToolResult(content=str(target), artifacts=[str(target)])


@tool(name="files.list", profiles=["*"])
async def list_files(ctx: ToolContext, path: str, limit: int = 100) -> ToolResult:
    """List up to 500 directory entries inside profile scopes, without recursion."""
    entries, more = await ctx.fs.entries(path, limit)
    return ToolResult(
        content=json.dumps({"entries": entries, "more": more}, ensure_ascii=False)
    )
