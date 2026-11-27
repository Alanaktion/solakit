"""Durable tool attempts, authority revalidation, guarded services and bounded results."""

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from pydantic import JsonValue, ValidationError

from solakit.config import ConfigError, EffectiveProfile, WebFetchSettings, restrict_to_admission
from solakit.records import (
    Artifact,
    ErrorCode,
    Failure,
    Origin,
    Run,
    RunState,
    SessionMode,
    ToolCall,
    ToolReceipt,
    new_id,
    utc_now,
)
from solakit.redaction import Redactor
from solakit.sdk import ToolContext, ToolError, ToolResult
from solakit.storage.evidence import CorruptionError, StorageError, canonical, digest, read_artifact
from solakit.storage.search import search
from solakit.storage.store import Store
from solakit.tools.files import GuardedFiles
from solakit.tools.mcp import McpCall, McpClient, McpList
from solakit.tools.registry import Registry
from solakit.tools.web_fetch import CloudflareMarkdown, WebFetch
from solakit.tools.web_search import BraveSearch, WebSearch

if TYPE_CHECKING:
    from solakit.adapters.memory import MemoryService
    from solakit.engine.jobs import JobService
    from solakit.engine.run_loop import RunLoop
    from solakit.tools.blog import BlogService
    from solakit.tools.images import ImageService

MAX_ARGUMENT_BYTES = 65536
MAX_ARTIFACT_BYTES = 1024 * 1024
WORKER_TOOLS = frozenset(
    {
        "files.read",
        "files.list",
        "files.search",
        "files.glob",
        "files.grep",
        "web.search",
        "web.fetch",
        "git.status",
        "git.diff",
        "blog.list",
        "blog.read",
        "dreams.list",
        "dreams.read",
        "letters.read",
        "transcripts.search",
        "runtime.status",
    }
)


def receipt_content(
    status: str,
    effect: str,
    result: JsonValue,
    failure: Failure | None,
    truncated: JsonValue = None,
) -> bytes:
    content: dict[str, JsonValue] = {
        "status": status,
        "effect": effect,
        "result": result,
        "failure": failure.model_dump(mode="json") if failure else None,
    }
    if truncated is not None:
        content["truncated"] = truncated
    return canonical(content)


class Dispatcher:
    def __init__(
        self,
        store: Store,
        registry: Registry,
        current: Callable[[EffectiveProfile], EffectiveProfile],
        *,
        redactor: Redactor | None = None,
    ) -> None:
        self.store, self.registry, self.current = store, registry, current
        self.redactor = redactor or Redactor()
        self.web_search = BraveSearch(self.redactor)
        self.web_fetch = CloudflareMarkdown(self.redactor)
        self.mcp = McpClient()
        self.memory: MemoryService | None = None
        self.jobs: JobService | None = None
        self.runs: RunLoop | None = None
        self.images: ImageService | None = None
        self.blog: BlogService | None = None
        self._checkout_locks: dict[tuple[int, int], asyncio.Lock] = {}
        package = Path(__file__).resolve().parents[1]
        checkout = package.parent.parent
        self.protected: tuple[Path, ...] = (
            checkout if (checkout / "pyproject.toml").is_file() else package,
        )
        self.protected += tuple(Path(path).resolve().parent for path in registry.code_roots)

    def checkout_lock(self, root: Path) -> asyncio.Lock:
        info = root.stat()
        key = (info.st_dev, info.st_ino)
        if key not in self._checkout_locks:
            self._checkout_locks[key] = asyncio.Lock()
        return self._checkout_locks[key]

    def letters_inbox_allowed(self, run: Run) -> bool:
        return run.origin == Origin.INTERACTIVE or (
            run.occurrence_id is not None
            and self.store.occurrence(run.occurrence_id).definition.get("workspace_roots")
            is not None
        )

    def bind(self, run_id: str) -> ToolSession:
        admitted = self.store.admitted_profile(run_id)
        run = self.store.run(run_id)
        if (run.origin, run.profile, self.store.session(run.session_id).mode) != (
            admitted.origin,
            admitted.name,
            admitted.mode,
        ):
            raise ValueError("run differs from its admitted authority")
        if run.state != RunState.RUNNING or run.started_at is None:
            raise ValueError("only running work may dispatch tools")
        return ToolSession(self, run_id, admitted)

    def profile(self, run: Run, admitted: EffectiveProfile) -> EffectiveProfile:
        profile = restrict_to_admission(self.current(admitted), admitted)
        topic = self.store.session(run.session_id)
        if run.parent_tool_call_id is None and run.origin != Origin.DELEGATED:
            if topic.parent_session_id is not None or topic.origin == Origin.DELEGATED:
                raise ConfigError("Delegated topic requires child lineage")
            return profile
        link = self.store.delegation(run.parent_tool_call_id) if run.parent_tool_call_id else None
        if link is None or (link.child_run_id, link.child_session_id, link.session_id) != (
            run.id,
            run.session_id,
            topic.parent_session_id,
        ):
            raise ConfigError("Delegated run lacks its parent link")
        parent = self.store.run(link.run_id)
        if parent.origin != Origin.INTERACTIVE or parent.state != RunState.RUNNING:
            raise ConfigError("Delegated run requires an active interactive parent")
        parent_admitted = self.store.admitted_profile(parent.id)
        ceiling = restrict_to_admission(self.current(parent_admitted), parent_admitted)
        if "delegate.run" not in ceiling.tools or ceiling.delegation_worker != profile.name:
            raise ConfigError("Parent delegation grant has changed")
        profile = restrict_to_admission(ceiling, profile)
        tools = frozenset(
            name
            for name in profile.tools & WORKER_TOOLS
            if (tool := self.registry.tools.get(name)) is not None and not tool.consequential
        )
        budgets = profile.budgets.model_copy(
            update={
                "model_calls": min(
                    profile.budgets.model_calls,
                    ceiling.budgets.model_calls
                    - self.store.model_index(parent.id)
                    - self.store.child_attempts(parent.id, "request")
                    + self.store.model_index(run.id)
                    - 1,
                ),
                "tool_calls": min(
                    profile.budgets.tool_calls,
                    ceiling.budgets.tool_calls
                    - self.store.tool_attempts(parent.id)
                    - self.store.child_attempts(parent.id, "tool")
                    + self.store.tool_attempts(run.id),
                ),
            }
        )
        return profile.model_copy(update={"tools": tools, "budgets": budgets, "memory_mode": "off"})


class ToolSession:
    def __init__(self, dispatcher: Dispatcher, run_id: str, admitted: EffectiveProfile) -> None:
        self.dispatcher, self.run_id, self.admitted = dispatcher, run_id, admitted
        self.lock = asyncio.Lock()
        run = dispatcher.store.run(run_id)
        assert run.started_at is not None
        elapsed = max(0.0, (utc_now() - run.started_at).total_seconds())
        self.started = time.monotonic() - elapsed

    def schemas(
        self, *, deferred: frozenset[str] = frozenset()
    ) -> tuple[dict[str, JsonValue], ...]:
        return self.dispatcher.registry.visible(self.eligible(), deferred=deferred)

    def eligible(self) -> frozenset[str]:
        run = self.dispatcher.store.run(self.run_id)
        profile = self.dispatcher.profile(run, self.admitted)
        tools = self.dispatcher.registry.eligible(profile)
        if run.origin == Origin.DELEGATED and (
            self.dispatcher.store.tool_attempts(run.id) >= profile.budgets.tool_calls
        ):
            return frozenset()
        if "letters.inbox" in tools and not self.dispatcher.letters_inbox_allowed(run):
            tools -= {"letters.inbox"}
        return tools

    async def dispatch(self, call: ToolCall) -> ToolReceipt:
        async with self.lock:
            return await self._dispatch(call)

    async def _dispatch(self, call: ToolCall) -> ToolReceipt:
        store, registry = self.dispatcher.store, self.dispatcher.registry
        if call.run_id != self.run_id:
            raise ValueError("tool call belongs to another run")
        # Store validates model-step/session/origin lineage and rejects duplicate attempts.
        store.append(call)
        invocation = Invocation(self, call)
        status: Literal["succeeded", "failed", "denied", "cancelled", "uncertain"] = "succeeded"
        failure = None
        result: JsonValue = None
        artifact = None
        truncated = False
        cancelled = False
        result_bytes = 0
        result_sha256 = None
        try:
            profile = invocation.authority()
            arguments = canonical(call.arguments)
            if len(arguments) > MAX_ARGUMENT_BYTES:
                raise ToolError("Tool arguments exceed 64 KiB", ErrorCode.INVALID_INPUT)
            tool = registry.tools[call.name]
            args = tool.arguments.model_validate_json(arguments)
            context = ToolContext(
                call.session_id,
                call.run_id,
                profile.origin,
                profile.mode,
                GuardedFiles(
                    invocation.authority,
                    self.dispatcher.protected,
                    invocation.begin_effect,
                    invocation.confirm_effect,
                    invocation.record_artifact,
                ),
                invocation,
                invocation.begin_effect,
                invocation.confirm_effect,
            )
            remaining = profile.budgets.active_seconds - (time.monotonic() - self.started)
            async with asyncio.timeout(min(profile.budgets.tool_timeout_seconds, remaining)):
                outcome = await tool.handler(context, args)
            if not isinstance(outcome, ToolResult):
                raise ToolError("Tool did not return ToolResult")
            if invocation.effect == "uncertain":
                raise ToolError(
                    "Tool returned without confirming its effect", ErrorCode.EFFECT_UNCERTAIN
                )
            if call.name == "delegate.run":
                result = outcome.data
                assert isinstance(result, dict)
                if result["status"] != "completed":
                    status = "failed"
                    failure = Failure(
                        code=ErrorCode.TOOL_FAILED,
                        message=f"Delegation {result['status']}",
                    )
                report = result.get("report")
                if isinstance(report, str):
                    safe_report = self.dispatcher.redactor.redact(report)
                    result["report"] = safe_report
                    cap = profile.budgets.tool_result_bytes
                    if len(receipt_content(status, "none", result, failure)) > cap:
                        result["report"] = ""
                        overhead = len(receipt_content(status, "none", result, failure))
                        room = max(0, (cap - overhead) // 6)
                        result["report"] = safe_report[:room]
                        truncated = True
                encoded = canonical(result)
                result_bytes, result_sha256 = len(encoded), digest(encoded)
            else:
                safe = (
                    outcome.data
                    if invocation.redacted_result
                    else self.dispatcher.redactor.value(outcome.data)
                )
                raw = canonical(safe).decode() if not isinstance(safe, str) else safe
                encoded = raw.encode("utf-8")
                result_bytes, result_sha256 = len(encoded), digest(encoded)
                cap = invocation.authority().budgets.tool_result_bytes
                if invocation.redacted_result and len(encoded) > cap:
                    raise ToolError(
                        "Structured result exceeds the current budget", ErrorCode.BUDGET_EXCEEDED
                    )
                if len(encoded) > cap:
                    marker = "[truncated]" if cap >= 11 else "!"
                    result = (
                        encoded[: max(0, cap - len(marker))].decode("utf-8", errors="ignore")
                        + marker
                    )
                    truncated = True
                    suffix = b"\n[artifact truncated]" if len(encoded) > MAX_ARTIFACT_BYTES else b""
                    retained = encoded[: MAX_ARTIFACT_BYTES - len(suffix)]
                    retained = retained.decode("utf-8", errors="ignore").encode() + suffix
                    artifact = Artifact.model_validate(store.artifact(new_id(), retained))
                else:
                    result = raw
        except asyncio.CancelledError:
            cancelled = True
            status = "cancelled"
            failure = Failure(code=ErrorCode.CANCELLED, message="Tool execution cancelled")
        except TimeoutError:
            status = "failed"
            failure = Failure(code=ErrorCode.TIMEOUT, message="Tool execution timed out")
        except ValidationError:
            status = "failed"
            failure = Failure(
                code=ErrorCode.INVALID_INPUT, message="Arguments do not match the tool schema"
            )
        except StorageError as error:
            status = "failed"
            failure = Failure(
                code=ErrorCode.CORRUPTION
                if isinstance(error, CorruptionError)
                else ErrorCode.STORAGE_FAILED,
                message="Tool storage is unavailable or corrupt; inspect before retry",
            )
        except ConfigError:
            status = "denied"
            failure = Failure(
                code=ErrorCode.CONFIGURATION, message="Current configuration requires new admission"
            )
        except ToolError as error:
            status = (
                "denied"
                if error.code in (ErrorCode.TOOL_DENIED, ErrorCode.BUDGET_EXCEEDED)
                else "failed"
            )
            failure = Failure(
                code=error.code, message=self.dispatcher.redactor.redact(str(error))[:1024]
            )
        except Exception:
            status = "failed"
            failure = Failure(
                code=ErrorCode.TOOL_FAILED, message="Tool failed; inspect its intent and receipt"
            )
        finally:
            invocation.closed = True
        if call.name == "delegate.run" and result is None and (link := store.delegation(call.id)):
            result = store.delegation_result(link, include_report=False)
            encoded = canonical(result)
            result_bytes, result_sha256 = len(encoded), digest(encoded)
        artifact = invocation.artifact or artifact
        if invocation.effect == "uncertain":
            status = "uncertain"
            message = (
                failure.message if failure else "Tool effect is uncertain; reconcile before retry"
            )
            failure = Failure(
                code=ErrorCode.EFFECT_UNCERTAIN,
                message=message + invocation.uncertain_advice,
            )
        else:
            if failure is not None and call.name in (
                "git.pull",
                "git.publish",
                "letters.inbox",
                "letters.send",
                "letters.reply",
            ):
                failure = failure.model_copy(
                    update={"message": failure.message + invocation.uncertain_advice}
                )
            if status == "denied" and invocation.effect == "confirmed":
                status = "failed"
        receipt = ToolReceipt(
            origin=call.origin,
            session_id=call.session_id,
            run_id=call.run_id,
            tool_call_id=call.id,
            status=status,
            effect=invocation.effect,
            result=result,
            failure=failure,
            artifact=artifact,
            truncated=truncated,
            result_bytes=result_bytes,
            result_sha256=result_sha256,
        )
        store.append(receipt)
        if call.name == "images.generate":
            store.fault("image_receipt")
        if cancelled:
            raise asyncio.CancelledError
        return receipt


class Invocation:
    """A context expires with its call, even if a pack retains it for later."""

    def __init__(self, session: ToolSession, call: ToolCall) -> None:
        self.dispatcher, self.admitted = session.dispatcher, session.admitted
        self.run_id, self.started, self.call = session.run_id, session.started, call
        self.effect: Literal["none", "confirmed", "uncertain"] = "none"
        self.uncertain_advice = ""
        self.call_started = time.monotonic()
        self.closed = False
        self.artifact: Artifact | None = None
        self.redacted_result = False

    def authority(self, service: str = "") -> EffectiveProfile:
        if self.closed:
            raise ToolError("Tool context is no longer active", ErrorCode.TOOL_DENIED)
        store, registry = self.dispatcher.store, self.dispatcher.registry
        run = store.run(self.run_id)
        profile = self.dispatcher.profile(run, self.admitted)
        tool = registry.tools.get(self.call.name)
        if (
            run.state != RunState.RUNNING
            or tool is None
            or tool.name not in registry.eligible(profile)
        ):
            raise ToolError("Tool is outside current run authority", ErrorCode.TOOL_DENIED)
        if (
            time.monotonic() - self.started >= profile.budgets.active_seconds
            or store.tool_attempts(self.run_id) + store.child_attempts(self.run_id, "tool")
            > profile.budgets.tool_calls
        ):
            raise ToolError("Tool run budget exhausted", ErrorCode.BUDGET_EXCEEDED)
        if time.monotonic() - self.call_started >= profile.budgets.tool_timeout_seconds:
            raise ToolError("Tool execution timed out", ErrorCode.TIMEOUT)
        if service in ("files.write", "effect"):
            if run.parent_tool_call_id is not None:
                raise ToolError("Delegated work is read-only", ErrorCode.TOOL_DENIED)
            if not tool.consequential:
                raise ToolError("Tool did not declare writes", ErrorCode.TOOL_DENIED)
        if tool.name == "letters.inbox" and not self.dispatcher.letters_inbox_allowed(run):
            raise ToolError(
                "Letters inbox requires explicit job workspace roots", ErrorCode.TOOL_DENIED
            )
        if (
            service == "files.write"
            and tool.name != "memory.remember"
            and run.occurrence_id is not None
            and store.occurrence(run.occurrence_id).definition.get("workspace_roots") is None
        ):
            raise ToolError(
                "Background file writes require an explicit job artifact root",
                ErrorCode.TOOL_DENIED,
            )
        if (
            tool.consequential
            and self.dispatcher.jobs is not None
            and not self.dispatcher.jobs.effect_allowed(run, tool.name)
        ):
            raise ToolError("Outbound effect deferred by quiet hours", ErrorCode.TOOL_DENIED)
        return profile

    async def delegate(self, task: str) -> ToolResult:
        profile = self.authority()
        if self.call.name != "delegate.run" or self.dispatcher.runs is None:
            raise ToolError("Delegation is outside current authority", ErrorCode.TOOL_DENIED)
        remaining = min(
            profile.budgets.active_seconds - (time.monotonic() - self.started),
            profile.budgets.tool_timeout_seconds - (time.monotonic() - self.call_started),
        )
        return ToolResult(
            data=await self.dispatcher.runs.delegate(self.call, profile, task, remaining)
        )

    def begin_effect(self, credentials: tuple[str, ...] = ()) -> None:
        self.authority("effect")
        self.dispatcher.redactor.register(*credentials)
        self.effect = "uncertain"

    def _authorize_web_search(self) -> str | None:
        profile = self.authority()
        if self.call.name != "web.search":
            raise ToolError("Web search requires its named tool", ErrorCode.TOOL_DENIED)
        return profile.web_search.api_key_env

    async def search_web(self, args: WebSearch) -> ToolResult:
        return ToolResult(
            data=await self.dispatcher.web_search.search(args, self._authorize_web_search)
        )

    def _authorize_web_fetch(self) -> WebFetchSettings:
        profile = self.authority()
        if self.call.name != "web.fetch" or profile.web_fetch is None:
            raise ToolError("Web fetching requires its named tool", ErrorCode.TOOL_DENIED)
        return profile.web_fetch

    async def fetch_web(self, args: WebFetch) -> ToolResult:
        return ToolResult(
            data=await self.dispatcher.web_fetch.fetch(args, self._authorize_web_fetch)
        )

    async def mcp(self, args: McpList | McpCall) -> ToolResult:
        server = self.authority().mcp_servers.get(args.server)
        if server is None:
            raise ToolError("Unknown MCP server", ErrorCode.INVALID_INPUT)
        secrets = {name: os.environ.get(env, "") for name, env in server.headers_env.items()}
        if any(not value.strip() for value in secrets.values()):
            raise ToolError("MCP server credential is missing", ErrorCode.CONFIGURATION)
        self.dispatcher.redactor.register(*secrets.values())
        headers = server.headers | secrets
        if isinstance(args, McpList):
            params: dict[str, JsonValue] = {"cursor": args.cursor} if args.cursor else {}
            return ToolResult(
                data=await self.dispatcher.mcp.request(server.url, headers, "tools/list", params)
            )
        result = await self.dispatcher.mcp.request(
            server.url,
            headers,
            "tools/call",
            {"name": args.tool, "arguments": args.arguments},
            self.begin_effect,
        )
        self.confirm_effect()
        if result.get("isError") is True:
            content = result.get("content")
            text = " ".join(
                text
                for part in (content if isinstance(content, list) else [])
                if isinstance(part, dict) and isinstance(text := part.get("text"), str)
            )
            raise ToolError("MCP tool error: " + (text or "no details"))
        return ToolResult(data=result)

    def confirm_effect(self) -> None:
        if self.closed:
            raise ToolError("Tool context is no longer active", ErrorCode.TOOL_DENIED)
        if self.effect != "uncertain":
            raise ToolError("Effect must be started before confirmation")
        self.effect = "confirmed"

    def record_artifact(self, raw: bytes) -> None:
        self.authority("files.write")
        self.artifact = Artifact.model_validate(
            self.dispatcher.store.artifact(
                new_id(), self.dispatcher.redactor.redact(raw.decode()).encode()
            )
            | {"media_type": "text/plain"}
        )

    async def generate_image(self, prompt: str, width: int, height: int, seed: int) -> JsonValue:
        self.authority("files.write")
        if self.dispatcher.images is None:
            raise ToolError("ComfyUI image generation is unavailable")
        from solakit.tools.builtins import GenerateImage

        args = GenerateImage(
            prompt=prompt,
            width=width,
            height=height,
            seed=seed,
        )
        generation = await self.dispatcher.images.generate(
            self.call, args, self.begin_effect, self.confirm_effect
        )
        self.artifact = generation.artifact
        return self.dispatcher.images.result(generation)

    async def blog(self, action: str, arguments: dict[str, JsonValue]) -> JsonValue:
        profile = self.authority()
        if action != self.call.name or action not in {
            "blog.list",
            "blog.read",
            "blog.create",
            "blog.publish",
            "dreams.list",
            "dreams.read",
            "dreams.create",
        }:
            raise ToolError("Content service requires its named tool", ErrorCode.TOOL_DENIED)
        service = self.dispatcher.blog
        if service is None:
            raise ToolError("Blog service is unavailable")
        if profile.blog_repository is None:
            raise ToolError("Configure blog.repository before using blog or dream tools")
        args = self.dispatcher.registry.tools[action].arguments.model_validate_json(
            canonical(arguments)
        )
        from solakit.tools.builtins import BlogCreate

        cover = None
        if isinstance(args, BlogCreate) and args.cover_artifact_id is not None:
            from solakit.tools.images import MAX_IMAGE_BYTES

            scope = "run_id" if profile.mode == SessionMode.EPHEMERAL else "session_id"
            identity = self.run_id if scope == "run_id" else self.call.session_id
            row = self.dispatcher.store.db.execute(
                f"SELECT id FROM entities WHERE kind='receipt' AND {scope}=? "
                "AND json_extract(body,'$.artifact.path')=? LIMIT 1",
                (identity, f"artifacts/{args.cover_artifact_id}"),
            ).fetchone()
            if row is None:
                raise ToolError(
                    "Cover artifact is outside this content scope", ErrorCode.TOOL_DENIED
                )
            receipt = self.dispatcher.store._load("receipt", row[0], ToolReceipt)
            reference = receipt.artifact
            if (
                reference is None
                or reference.media_type != "image/png"
                or reference.length > MAX_IMAGE_BYTES
            ):
                raise ToolError("Cover must be a bounded generated PNG", ErrorCode.INVALID_INPUT)
            cover = read_artifact(self.dispatcher.store.root, reference)
        files = GuardedFiles(
            self.authority,
            self.dispatcher.protected,
            self.begin_effect,
            self.confirm_effect,
            self.record_artifact,
        )
        return await service.execute(
            action, args, files, self.dispatcher.checkout_lock(profile.blog_repository), cover=cover
        )

    def transcript_search(self, query: str, *, limit: int = 20) -> JsonValue:
        profile = self.authority()
        cross_session = profile.mode == SessionMode.SHARED and profile.origin == Origin.INTERACTIVE
        results = search(
            profile.data_dir,
            query,
            limit=limit,
            kind="message",
            shared_only=cross_session,
            session_id=None if cross_session else self.call.session_id,
            run_id=self.run_id if profile.mode == SessionMode.EPHEMERAL else None,
        )
        # Search is disposable: authority must come from verified session evidence.
        if cross_session and any(
            self.dispatcher.store.session(str(item["session_id"])).mode != SessionMode.SHARED
            for item in results
        ):
            raise CorruptionError("search session projection differs from evidence")
        return cast(JsonValue, results)

    def status(self) -> JsonValue:
        self.authority()
        return {
            "run_id": self.run_id,
            "state": self.dispatcher.store.run(self.run_id).state.value,
            "tool_attempts": self.dispatcher.store.tool_attempts(self.run_id),
            "search": self.dispatcher.store.search_status,
            "images": self.dispatcher.images.failure if self.dispatcher.images else "unavailable",
            "memory": self.dispatcher.memory.status() if self.dispatcher.memory else "unavailable",
            "background": {
                "paused": self.dispatcher.jobs.control().paused,
                "failure": self.dispatcher.jobs.failure,
            }
            if self.dispatcher.jobs
            else None,
        }

    async def recall(self, query: str) -> JsonValue:
        profile = self.authority()
        if profile.mode != SessionMode.SHARED or profile.memory_mode == "off":
            raise ToolError("Memory is disabled for this run", ErrorCode.TOOL_DENIED)
        if self.dispatcher.memory is None:
            raise ToolError("Semantic memory is unavailable", ErrorCode.MEMORY_UNAVAILABLE)
        run = self.dispatcher.store.run(self.run_id)
        observation = await self.dispatcher.memory.recall(run, profile, query)
        if observation.status != "ok":
            raise ToolError("Semantic memory recall is degraded", ErrorCode.MEMORY_UNAVAILABLE)
        return observation.model_dump(mode="json")

    def remember(self, content: str) -> JsonValue:
        profile = self.authority("files.write")
        if profile.mode != SessionMode.SHARED or profile.memory_mode == "off":
            raise ToolError("Memory is disabled for this run", ErrorCode.TOOL_DENIED)
        if self.dispatcher.memory is None:
            raise ToolError("Semantic memory is unavailable", ErrorCode.MEMORY_UNAVAILABLE)
        from solakit.adapters.memory import MemoryError

        try:
            self.dispatcher.memory.adapter.check(profile)
        except MemoryError:
            raise ToolError(
                "Semantic memory is unavailable", ErrorCode.MEMORY_UNAVAILABLE
            ) from None
        self.begin_effect()
        note = self.dispatcher.memory.stage(self.run_id, self.call.id, content)
        self.confirm_effect()
        return {"status": "staged", "note_id": note.id, "run_id": note.run_id}

    def jobs(self, action: str, arguments: dict[str, JsonValue]) -> JsonValue:
        profile = self.authority()
        if "jobs." + action not in profile.tools or self.dispatcher.jobs is None:
            raise ToolError("Job operation is outside current authority", ErrorCode.TOOL_DENIED)
        if action != "list" and profile.origin != Origin.INTERACTIVE:
            raise ToolError("Autonomous work cannot manage wake-ups", ErrorCode.TOOL_DENIED)
        if action != "list":
            self.begin_effect()
        try:
            result = self.dispatcher.jobs.invoke(
                action,
                arguments,
                self.dispatcher.store.run(self.run_id),
                profile,
            )
        except ValueError, KeyError:
            # Validation precedes mutations; a failed store still retains uncertainty.
            if self.dispatcher.store.failed:
                raise StorageError("Job mutation failed; recover before retry") from None
            self.effect = "none"
            raise ToolError("Invalid job operation or target", ErrorCode.INVALID_INPUT) from None
        if action != "list":
            self.confirm_effect()
        return result
