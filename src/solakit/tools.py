"""Initial tools: thin calls into guarded application services."""

from typing import Annotated, Literal

from pydantic import Field

from solakit.records import RecordID
from solakit.sdk import Origin, Tool, ToolArguments, ToolContext, ToolResult


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


class JobCreate(ToolArguments):
    task: str
    schedule: str
    profile: str = "default"
    origin: Literal["scheduled", "proactive"] = "scheduled"
    timezone: str = "America/Denver"
    enabled: bool = False
    tools: tuple[str, ...] | None = None
    workspace_roots: tuple[str, ...] | None = None
    budgets: dict[str, int] = Field(default_factory=dict)
    delivery: Literal["session", "none"] = "session"


class JobTarget(ToolArguments):
    job_id: RecordID


class JobUpdate(JobTarget):
    task: str | None = None
    schedule: str | None = None
    timezone: str | None = None
    profile: str | None = None
    enabled: bool | None = None
    tools: tuple[str, ...] | None = None
    workspace_roots: tuple[str, ...] | None = None
    budgets: dict[str, int] | None = None
    delivery: Literal["session", "none"] | None = None


class JobRun(JobTarget):
    request_id: RecordID


class JobPauseAll(ToolArguments):
    paused: bool


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


async def jobs(context: ToolContext, args: ToolArguments) -> ToolResult:
    if isinstance(args, JobCreate):
        action = "create"
    elif isinstance(args, JobUpdate):
        action = "update"
    elif isinstance(args, JobRun):
        action = "run_now"
    elif isinstance(args, JobPauseAll):
        action = "pause_all"
    else:
        action = "list"
    return ToolResult(
        data=context.services.jobs(
            action, args.model_dump(mode="json", exclude_unset=isinstance(args, JobUpdate))
        )
    )


async def pause_job(context: ToolContext, args: JobTarget) -> ToolResult:
    return ToolResult(data=context.services.jobs("pause", args.model_dump(mode="json")))


async def resume_job(context: ToolContext, args: JobTarget) -> ToolResult:
    return ToolResult(data=context.services.jobs("resume", args.model_dump(mode="json")))


async def delete_job(context: ToolContext, args: JobTarget) -> ToolResult:
    return ToolResult(data=context.services.jobs("delete", args.model_dump(mode="json")))


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
    Tool("jobs.list", "List this topic's jobs.", ToolArguments, jobs),
    Tool(
        "jobs.create",
        "Create a bound job; disabled unless explicitly enabled.",
        JobCreate,
        jobs,
        True,
        frozenset({Origin.INTERACTIVE}),
    ),
    Tool(
        "jobs.update",
        "Edit a job; consumed one-shots require a new future schedule.",
        JobUpdate,
        jobs,
        True,
        frozenset({Origin.INTERACTIVE}),
    ),
    Tool(
        "jobs.pause",
        "Pause a bound job.",
        JobTarget,
        pause_job,
        True,
        frozenset({Origin.INTERACTIVE}),
    ),
    Tool(
        "jobs.resume",
        "Enable/resume a bound job.",
        JobTarget,
        resume_job,
        True,
        frozenset({Origin.INTERACTIVE}),
    ),
    Tool(
        "jobs.run_now",
        "Run a job now with a stable request ID; no overlap.",
        JobRun,
        jobs,
        True,
        frozenset({Origin.INTERACTIVE}),
    ),
    Tool(
        "jobs.delete",
        "Tombstone a bound job.",
        JobTarget,
        delete_job,
        True,
        frozenset({Origin.INTERACTIVE}),
    ),
    Tool(
        "jobs.pause_all",
        "Set the durable global background pause.",
        JobPauseAll,
        jobs,
        True,
        frozenset({Origin.INTERACTIVE}),
    ),
)
