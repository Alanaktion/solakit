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
    offset: Annotated[
        int, Field(ge=1, description="First line to read (1-based) for windows.")
    ] = 1,
    limit: Annotated[
        int | None, Field(ge=1, le=5000, description="Maximum lines to return.")
    ] = None,
) -> ToolResult:
    """Read a UTF-8 operator file inside profile filesystem scopes.

    A file that fits is returned whole. A large file, or a call with offset or
    limit, returns numbered lines and a footer with the next offset; call again
    with that offset to continue. Prefer a granted domain tool for its data; do
    not inspect runtime configuration or credentials to discover resources."""
    whole = None
    if offset == 1 and limit is None:
        whole = await ctx.fs.read(path)
        if whole.content_complete:
            return whole
    window = await ctx.fs.window(path, offset, limit)
    if window["cut"] and whole is not None:
        return whole  # One overlong line: a counted character truncation.
    if window["cut"]:
        window["text"] += "\n[line cut to fit the output limit]"
    footer = f"[lines {window['start']}-{window['end']} of {window['total']}"
    footer += f"; next offset={window['next']}]" if window["next"] else "; end of file]"
    return ToolResult(content=window["text"] + "\n" + footer)


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
    overwrite: Annotated[
        bool, Field(description="Replace an existing file instead of failing.")
    ] = False,
) -> ToolResult:
    """Create a UTF-8 file. Existing files are refused unless overwrite is true."""
    existed = overwrite and (await ctx.fs.exists(path))
    target = await ctx.fs.write(path, content, overwrite=overwrite)
    verb = "Replaced" if existed else "Created"
    return ToolResult(
        content=f"{verb} {target} ({len(content.encode())} bytes)",
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
    """Replace one unique match of old with new.

    Exact text is preferred; if none matches, whole lines are matched ignoring
    trailing whitespace and line endings. Zero or multiple matches fail."""
    report: dict = {}
    target = await ctx.fs.edit(path, old, new, report=report)
    how = " (whitespace-insensitive)" if report.get("mode") == "whitespace" else ""
    return ToolResult(
        content=(
            f"Edited {target}: replaced lines {report.get('start')}-"
            f"{report.get('end')}{how}"
        ),
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


@tool(
    summary="Find files by name pattern below a directory",
    keywords=("file", "find", "glob", "locate", "name"),
    name="files.find",
    profiles=["*"],
    next_hint="files.read(path=...) or files.grep(query=..., path=...)",
)
async def find(
    ctx: ToolContext,
    pattern: Annotated[
        str,
        Field(
            description=(
                "Basename glob anywhere (e.g. *.toml), or root-relative path glob "
                "(e.g. docs/*.md). * and ? stay within a directory; "
                "** spans directories."
            )
        ),
    ],
    path: Annotated[str, Field(description="Directory to search below.")] = ".",
    limit: Annotated[int, Field(ge=1, le=200, description="Maximum results.")] = 50,
) -> ToolResult:
    """Find files recursively by glob, returning relative paths and sizes.

    Path patterns are relative to path: docs/*.md matches direct files only;
    docs/**/*.md includes nested files. Hidden directories and symlinks are skipped.
    Use files.grep to search contents and files.read to open a result."""
    result = await ctx.fs.find(pattern, path, limit)
    return ToolResult(content=json.dumps(result, ensure_ascii=False))


@tool(
    summary="Search file contents for text, with line context",
    keywords=("file", "grep", "search", "contents", "text", "where"),
    name="files.grep",
    profiles=["*"],
    next_hint="files.read(path=..., offset=<line>) to see more",
)
async def grep(
    ctx: ToolContext,
    query: Annotated[str, Field(min_length=1, description="Text to find (no case).")],
    path: Annotated[str, Field(description="File or directory to search.")] = ".",
    glob: Annotated[
        str | None, Field(description="Only files matching this glob, e.g. *.py.")
    ] = None,
    regex: Annotated[
        bool, Field(description="Treat query as a regular expression.")
    ] = False,
    context: Annotated[
        int, Field(ge=0, le=5, description="Lines of context around each match.")
    ] = 2,
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum matches.")] = 30,
) -> ToolResult:
    """Search text files recursively; lines read path:line: text (matches) and
    path-line- text (context). Skips binary files, symlinks and lines over 4096
    characters; matching has a 100 ms cumulative budget."""
    result = await ctx.fs.grep(
        query, path, glob=glob, regex=regex, context=context, limit=limit
    )
    text = "\n".join(result["lines"]) or "No matches."
    if result["more"]:
        text += "\n[search incomplete; narrow the search or inspect skipped long lines]"
    return ToolResult(content=text)
