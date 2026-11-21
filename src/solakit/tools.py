"""Initial tools: thin calls into guarded application services."""

import secrets
from typing import Annotated, Literal

from pydantic import Field

from solakit.records import RecordID
from solakit.sdk import Origin, Tool, ToolArguments, ToolContext, ToolResult


class Read(ToolArguments):
    path: Annotated[str, Field(min_length=1, max_length=4096)]


class Write(Read):
    content: str
    overwrite: bool = False


class ListFiles(ToolArguments):
    path: Annotated[str, Field(min_length=1, max_length=4096)] = "."
    limit: Annotated[int, Field(ge=1, le=100)] = 100


class SearchFiles(ListFiles):
    query: Annotated[str, Field(min_length=1, max_length=4096)]
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class GenerateImage(ToolArguments):
    prompt: Annotated[str, Field(min_length=1, max_length=16000)]
    width: Annotated[int, Field(ge=64, le=2048, multiple_of=16)] = 768
    height: Annotated[int, Field(ge=64, le=2048, multiple_of=16)] = 768
    seed: Annotated[int, Field(ge=0, le=18446744073709551615)] = Field(
        default_factory=lambda: secrets.randbits(52) + 2**52
    )


class Note(ToolArguments):
    content: Annotated[str, Field(min_length=1, max_length=2000)]


class Query(ToolArguments):
    query: Annotated[str, Field(min_length=1, max_length=4096)]
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class ContentPage(ToolArguments):
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    offset: Annotated[int, Field(ge=0, le=2000)] = 0


class ContentTarget(ToolArguments):
    slug: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=128)]


class DreamCreate(ToolArguments):
    body: Annotated[str, Field(min_length=1, max_length=48000)]
    date: Annotated[str, Field(min_length=1, max_length=64)] | None = None


class BlogCreate(ContentTarget, DreamCreate):
    title: Annotated[str, Field(min_length=1, max_length=256)]
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    tags: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    cover_path: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
    cover_artifact_id: RecordID | None = None
    auto_publish: bool = False


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


async def list_files(context: ToolContext, args: ListFiles) -> ToolResult:
    return ToolResult(data=context.files.list_directory(args.path, limit=args.limit))


async def search_files(context: ToolContext, args: SearchFiles) -> ToolResult:
    return ToolResult(data=context.files.search_text(args.path, args.query, limit=args.limit))


async def generate_image(context: ToolContext, args: GenerateImage) -> ToolResult:
    return ToolResult(
        data=await context.services.generate_image(args.prompt, args.width, args.height, args.seed)
    )


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


async def list_blogs(context: ToolContext, args: ContentPage) -> ToolResult:
    return ToolResult(data=await context.services.blog("blog.list", args.model_dump(mode="json")))


async def read_blog(context: ToolContext, args: ContentTarget) -> ToolResult:
    return ToolResult(data=await context.services.blog("blog.read", args.model_dump(mode="json")))


async def create_blog(context: ToolContext, args: BlogCreate) -> ToolResult:
    return ToolResult(data=await context.services.blog("blog.create", args.model_dump(mode="json")))


async def publish_blog(context: ToolContext, args: ContentTarget) -> ToolResult:
    return ToolResult(
        data=await context.services.blog("blog.publish", args.model_dump(mode="json"))
    )


async def list_dreams(context: ToolContext, args: ContentPage) -> ToolResult:
    return ToolResult(data=await context.services.blog("dreams.list", args.model_dump(mode="json")))


async def read_dream(context: ToolContext, args: ContentTarget) -> ToolResult:
    return ToolResult(data=await context.services.blog("dreams.read", args.model_dump(mode="json")))


async def create_dream(context: ToolContext, args: DreamCreate) -> ToolResult:
    return ToolResult(
        data=await context.services.blog("dreams.create", args.model_dump(mode="json"))
    )


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
    Tool("blog.list", "List blogs, including drafts, newest first.", ContentPage, list_blogs),
    Tool("blog.read", "Read a blog's metadata and Markdown body.", ContentTarget, read_blog),
    Tool(
        "blog.create",
        "Create a blog draft; auto_publish=true builds, commits pending content, and pushes.",
        BlogCreate,
        create_blog,
        True,
    ),
    Tool(
        "blog.publish",
        "Publish this blog; commit pending content and push all pending commits to main.",
        ContentTarget,
        publish_blog,
        True,
    ),
    Tool("dreams.list", "List dream poems newest first.", ContentPage, list_dreams),
    Tool("dreams.read", "Read a dream poem's metadata and body.", ContentTarget, read_dream),
    Tool(
        "dreams.create",
        "Create a date-named poem (3–15 lines), then always build, commit, and push. No overwrite.",
        DreamCreate,
        create_dream,
        True,
    ),
    Tool(
        "files.list",
        "List a granted workspace directory; bounded, no symlinks.",
        ListFiles,
        list_files,
    ),
    Tool(
        "files.search",
        "Search literal UTF-8 text under a granted directory.",
        SearchFiles,
        search_files,
    ),
    Tool("files.read", "Read a UTF-8 file in a granted workspace.", Read, read),
    Tool(
        "files.write",
        "Atomically write UTF-8; existing files require overwrite=true.",
        Write,
        write,
        True,
    ),
    Tool(
        "images.generate",
        "Generate one image with the configured ComfyUI workflow; returns an artifact and seed.",
        GenerateImage,
        generate_image,
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
