"""Initial tools: thin calls into guarded application services."""

from typing import Annotated

from pydantic import Field

from solakit.sdk import Tool, ToolArguments, ToolContext, ToolResult


class Read(ToolArguments):
    path: Annotated[str, Field(min_length=1, max_length=4096)]


class Write(Read):
    content: str
    overwrite: bool = False


class Note(ToolArguments):
    content: Annotated[str, Field(min_length=1, max_length=2000)]


class Query(ToolArguments):
    query: Annotated[str, Field(min_length=1, max_length=4096)]
    limit: Annotated[int, Field(ge=1, le=100)] = 20


async def read(context: ToolContext, args: Read) -> ToolResult:
    return ToolResult(data=context.files.read_text(args.path))


async def write(context: ToolContext, args: Write) -> ToolResult:
    return ToolResult(
        data=context.files.write_text(args.path, args.content, overwrite=args.overwrite)
    )


async def transcripts(context: ToolContext, args: Query) -> ToolResult:
    return ToolResult(data=context.services.transcript_search(args.query, limit=args.limit))


async def status(context: ToolContext, args: ToolArguments) -> ToolResult:
    return ToolResult(data=context.services.status())


async def memory(context: ToolContext, args: Query) -> ToolResult:
    return ToolResult(data=await context.services.recall(args.query))


async def remember(context: ToolContext, args: Note) -> ToolResult:
    return ToolResult(data=context.services.remember(args.content))


TOOLS = (
    Tool("files.read", "Read a UTF-8 file in a granted workspace.", Read, read),
    Tool(
        "files.write",
        "Atomically write UTF-8; existing files require overwrite=true.",
        Write,
        write,
        True,
    ),
    Tool("transcripts.search", "Search permitted conversation evidence.", Query, transcripts),
    Tool("runtime.status", "Inspect this run and local service status.", ToolArguments, status),
    Tool(
        "memory.remember",
        "Stage an exact note locally; extraction follows a completed run.",
        Note,
        remember,
        True,
    ),
    Tool("memory.recall", "Recall scoped semantic memory, when available.", Query, memory),
)
