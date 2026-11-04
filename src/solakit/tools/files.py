"""Narrow filesystem tools with per-call resolved-path checks."""

from solakit.core.io import durable
from solakit.tools import ToolContext, ToolError, ToolResult, tool

PROFILES = ["interactive", "maintenance", "dream", "blog", "inbox", "letters"]


@tool(name="files.read", profiles=PROFILES)
async def read(ctx: ToolContext, path: str) -> ToolResult:
    """Read a UTF-8 file inside the profile's configured read or write roots."""
    target = ctx.path(path)
    try:
        # Bound the read itself, not only the returned text.
        def bounded_read():
            with target.open("r", encoding="utf-8") as stream:
                return stream.read(
                    ctx.config.profiles[ctx.profile].tool_output_max_chars + 1
                )

        content = await durable(bounded_read)
    except (OSError, UnicodeError) as exc:
        raise ToolError("Cannot read requested file") from exc
    return ToolResult(content=content)


@tool(name="files.write", profiles=PROFILES, writes=["filesystem"])
async def write(ctx: ToolContext, path: str, content: str) -> ToolResult:
    """Create a new UTF-8 file inside a configured write root; never overwrite."""
    target = ctx.path(path, write=True)
    try:

        def create():
            with target.open("x", encoding="utf-8") as stream:
                stream.write(content)

        await durable(create)
    except OSError as exc:
        raise ToolError("Cannot create requested file") from exc
    return ToolResult(content=str(target))
