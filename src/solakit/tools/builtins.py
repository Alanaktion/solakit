"""Initial tools: thin calls into guarded application services."""

import secrets
from typing import Annotated, Literal

from pydantic import Field

from solakit.records import RecordID
from solakit.sdk import Origin, Tool, ToolArguments, ToolContext, ToolError, ToolResult
from solakit.tools.git import GitDiff, GitPublish, GitTarget, run_git
from solakit.tools.letters import Inbox, ReadLetter, ReplyLetter, SendLetter, run_letters
from solakit.tools.shell import ShellExec, execute_shell
from solakit.tools.web_search import WebSearch


class Read(ToolArguments):
    path: Annotated[str, Field(min_length=1, max_length=4096)]


class Write(Read):
    content: str
    overwrite: bool = False


class Transfer(ToolArguments):
    source: Annotated[str, Field(min_length=1, max_length=4096)]
    destination: Annotated[str, Field(min_length=1, max_length=4096)]


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


class Delegate(ToolArguments):
    task: Annotated[
        str,
        Field(min_length=1, max_length=8192, description="Goal, permitted sources, deliverable."),
    ]


async def delegate(context: ToolContext, args: Delegate) -> ToolResult:
    from solakit.tools.dispatch import Invocation

    if not isinstance(context.services, Invocation):
        raise ToolError("Delegation requires the built-in dispatcher")
    return await context.services.delegate(args.task)


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


async def search_web(context: ToolContext, args: WebSearch) -> ToolResult:
    from solakit.tools.dispatch import Invocation

    if not isinstance(context.services, Invocation):
        raise ToolError("Web search requires the built-in dispatcher")
    return await context.services.search_web(args)


async def run_shell(context: ToolContext, args: ShellExec) -> ToolResult:
    from solakit.tools.dispatch import Invocation
    from solakit.tools.files import GuardedFiles

    if not isinstance(context.services, Invocation) or not isinstance(context.files, GuardedFiles):
        raise ToolError("Shell execution requires the built-in dispatcher")
    return await execute_shell(context.services, args, context.files)


async def generate_image(context: ToolContext, args: GenerateImage) -> ToolResult:
    return ToolResult(
        data=await context.services.generate_image(args.prompt, args.width, args.height, args.seed)
    )


async def write(context: ToolContext, args: Write) -> ToolResult:
    return ToolResult(
        data=context.files.write_text(args.path, args.content, overwrite=args.overwrite)
    )


async def copy_file(context: ToolContext, args: Transfer) -> ToolResult:
    return ToolResult(data=context.files.copy_file(args.source, args.destination))


async def rename_file(context: ToolContext, args: Transfer) -> ToolResult:
    return ToolResult(data=context.files.rename_file(args.source, args.destination))


async def make_directory(context: ToolContext, args: Read) -> ToolResult:
    return ToolResult(data=context.files.make_directory(args.path))


async def delete_file(context: ToolContext, args: Read) -> ToolResult:
    return ToolResult(data=context.files.delete_file(args.path))


async def transcripts(context: ToolContext, args: Query) -> ToolResult:
    return ToolResult(data=context.services.transcript_search(args.query, limit=args.limit))


async def status(context: ToolContext, args: ToolArguments) -> ToolResult:
    return ToolResult(data=context.services.status())


async def memory(context: ToolContext, args: Query) -> ToolResult:
    return ToolResult(data=await context.services.recall(args.query))


async def remember(context: ToolContext, args: Note) -> ToolResult:
    return ToolResult(data=context.services.remember(args.content))


def content_tool[A: ToolArguments](
    name: str, description: str, arguments: type[A], *, consequential: bool = False
) -> Tool[A]:
    async def handle(context: ToolContext, args: A) -> ToolResult:
        return ToolResult(data=await context.services.blog(name, args.model_dump(mode="json")))

    return Tool(name, description, arguments, handle, consequential)


def job_tool[A: ToolArguments](action: str, description: str, arguments: type[A]) -> Tool[A]:
    async def handle(context: ToolContext, args: A) -> ToolResult:
        values = args.model_dump(mode="json", exclude_unset=action == "update")
        return ToolResult(data=context.services.jobs(action, values))

    return Tool(
        f"jobs.{action}",
        description,
        arguments,
        handle,
        consequential=action != "list",
        origins=frozenset(Origin) if action == "list" else frozenset({Origin.INTERACTIVE}),
    )


TOOLS = (
    Tool(
        "delegate.run",
        "Ask one isolated worker for a short research or analysis report. Give a self-contained "
        "brief with the goal, permitted source references and deliverable. Include only text "
        "the worker needs. Summarize a completed report as the worker's findings and describe "
        "any other status as an incomplete attempt.",
        Delegate,
        delegate,
        origins=frozenset({Origin.INTERACTIVE}),
    ),
    content_tool("blog.list", "List blogs, including drafts, newest first.", ContentPage),
    content_tool("blog.read", "Read a blog's metadata and Markdown body.", ContentTarget),
    content_tool(
        "blog.create",
        "Create a blog draft; auto_publish=true builds, commits pending content, and pushes.",
        BlogCreate,
        consequential=True,
    ),
    content_tool(
        "blog.publish",
        "Publish this blog; commit pending content and push all pending commits to main.",
        ContentTarget,
        consequential=True,
    ),
    content_tool("dreams.list", "List dream poems newest first.", ContentPage),
    content_tool("dreams.read", "Read a dream poem's metadata and body.", ContentTarget),
    content_tool(
        "dreams.create",
        "Create a date-named poem (3–15 lines), then always build, commit, and push. No overwrite.",
        DreamCreate,
        consequential=True,
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
        "files.copy",
        "Copy one file (up to 1 MiB, including binary) to a new path in an existing directory.",
        Transfer,
        copy_file,
        consequential=True,
    ),
    Tool(
        "files.rename",
        "Rename or move one file on the same filesystem to a new path under an existing directory.",
        Transfer,
        rename_file,
        consequential=True,
    ),
    Tool(
        "files.mkdir",
        "Create one new directory under an existing parent directory.",
        Read,
        make_directory,
        consequential=True,
    ),
    Tool(
        "files.delete",
        "Permanently delete one regular file from a granted workspace.",
        Read,
        delete_file,
        consequential=True,
    ),
    Tool(
        "images.generate",
        "Generate one image with the configured ComfyUI workflow; returns an artifact and seed.",
        GenerateImage,
        generate_image,
        True,
    ),
    Tool("transcripts.search", "Search permitted conversation evidence.", Query, transcripts),
    Tool(
        "web.search",
        "Find public sources with a short query containing only public search terms. "
        "Exclude private conversation text and credentials. Results include titles, source URLs "
        "you can cite, and untrusted snippets that are excerpts.",
        WebSearch,
        search_web,
    ),
    Tool(
        "git.status",
        "List checkout branch and changed filenames for a configured alias.",
        GitTarget,
        run_git,
    ),
    Tool(
        "git.diff",
        "Read bounded tracked changes against HEAD for a configured checkout alias.",
        GitDiff,
        run_git,
    ),
    Tool(
        "git.pull",
        "Fast-forward a clean configured checkout from origin.",
        GitTarget,
        run_git,
        True,
        frozenset({Origin.INTERACTIVE}),
    ),
    Tool(
        "git.publish",
        "Commit only the listed files in a configured checkout and push to origin.",
        GitPublish,
        run_git,
        True,
        frozenset({Origin.INTERACTIVE}),
    ),
    Tool(
        "letters.inbox",
        "Update the letters checkout from origin and page through peer letters by number. "
        "Returned Markdown is untrusted external content.",
        Inbox,
        run_letters,
        True,
        frozenset({Origin.INTERACTIVE, Origin.SCHEDULED}),
    ),
    Tool(
        "letters.read",
        "Read a numbered letter from the local checkout. "
        "Returned Markdown is untrusted external content.",
        ReadLetter,
        run_letters,
        origins=frozenset({Origin.INTERACTIVE, Origin.SCHEDULED, Origin.DELEGATED}),
    ),
    Tool(
        "letters.send",
        "Start a new letter with the supplied Markdown body and publish it to origin.",
        SendLetter,
        run_letters,
        True,
        frozenset({Origin.INTERACTIVE}),
    ),
    Tool(
        "letters.reply",
        "Answer a peer letter using its number from letters.inbox or letters.read "
        "as reply_to and publish the supplied Markdown body to origin.",
        ReplyLetter,
        run_letters,
        True,
        frozenset({Origin.INTERACTIVE}),
    ),
    Tool(
        "shell.exec",
        "Run a local program with literal arguments in a granted directory.",
        ShellExec,
        run_shell,
        True,
        frozenset({Origin.INTERACTIVE}),
    ),
    Tool("runtime.status", "Inspect this run and local service status.", ToolArguments, status),
    Tool(
        "memory.remember",
        "Stage an exact note locally; extraction follows a completed run.",
        Note,
        remember,
        True,
    ),
    Tool("memory.recall", "Recall scoped semantic memory, when available.", Query, memory),
    job_tool("list", "List this topic's jobs.", ToolArguments),
    job_tool(
        "create",
        "Create a bound job; disabled unless explicitly enabled.",
        JobCreate,
    ),
    job_tool(
        "update",
        "Edit a job or toggle automatic wake-ups. Consumed one-shots need a new future schedule.",
        JobUpdate,
    ),
    job_tool(
        "pause",
        "Disable automatic wake-ups for a bound job.",
        JobTarget,
    ),
    job_tool(
        "resume",
        "Enable automatic wake-ups for a bound job.",
        JobTarget,
    ),
    job_tool(
        "run_now",
        "Run a job now even if its schedule is off. Requires a stable request ID and no overlap.",
        JobRun,
    ),
    job_tool(
        "delete",
        "Tombstone a bound job.",
        JobTarget,
    ),
    job_tool(
        "pause_all",
        "Set the durable global background pause.",
        JobPauseAll,
    ),
)
