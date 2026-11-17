"""Durable tool attempts, authority revalidation, guarded services and bounded results."""

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from pydantic import JsonValue, ValidationError

from solakit.config import ConfigError, EffectiveProfile, restrict_to_admission
from solakit.evidence import CorruptionError, StorageError, canonical, digest
from solakit.files import GuardedFiles
from solakit.records import (
    Artifact,
    ErrorCode,
    Failure,
    Origin,
    RunState,
    SessionMode,
    ToolCall,
    ToolReceipt,
    new_id,
    utc_now,
)
from solakit.redaction import Redactor
from solakit.registry import Registry, RegistryError
from solakit.sdk import ToolContext, ToolError, ToolResult
from solakit.search import search
from solakit.store import Store
from solakit.tools import TOOLS

if TYPE_CHECKING:
    from solakit.jobs import JobService
    from solakit.memory import MemoryService

MAX_ARGUMENT_BYTES = 65536
MAX_ARTIFACT_BYTES = 1024 * 1024


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
        self.memory: MemoryService | None = None
        self.jobs: JobService | None = None
        package = Path(__file__).resolve().parent
        checkout = package.parent.parent
        self.protected: tuple[Path, ...] = (
            checkout if (checkout / "pyproject.toml").is_file() else package,
        )
        self.protected += tuple(Path(path).resolve().parent for path in registry.code_roots)

    def bind(self, run_id: str) -> ToolSession:
        admitted = EffectiveProfile.model_validate_json(
            canonical(self.store.admission_profile(run_id))
        )
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
        profile = restrict_to_admission(self.dispatcher.current(self.admitted), self.admitted)
        return frozenset(
            name
            for name in profile.tools
            if name in self.dispatcher.registry.tools
            and profile.origin in self.dispatcher.registry.tools[name].origins
            and (not name.startswith("memory.") or profile.memory_mode != "off")
        )

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
            if len(canonical(call.arguments)) > MAX_ARGUMENT_BYTES:
                raise ToolError("Tool arguments exceed 64 KiB", ErrorCode.INVALID_INPUT)
            tool = registry.tools[call.name]
            args = tool.arguments.model_validate_json(canonical(call.arguments))
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
            safe = self.dispatcher.redactor.value(outcome.data)
            raw = canonical(safe).decode() if not isinstance(safe, str) else safe
            encoded = raw.encode("utf-8")
            result_bytes, result_sha256 = len(encoded), digest(encoded)
            cap = invocation.authority().budgets.tool_result_bytes
            if len(encoded) > cap:
                marker = "[truncated]" if cap >= 11 else "!"
                result = (
                    encoded[: max(0, cap - len(marker))].decode("utf-8", errors="ignore") + marker
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
        if invocation.effect == "uncertain":
            status = "uncertain"
            failure = Failure(
                code=ErrorCode.EFFECT_UNCERTAIN,
                message="Tool effect is uncertain; reconcile before retry",
            )
        elif status == "denied" and invocation.effect == "confirmed":
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
        if cancelled:
            raise asyncio.CancelledError
        return receipt


class Invocation:
    """A context expires with its call, even if a pack retains it for later."""

    def __init__(self, session: ToolSession, call: ToolCall) -> None:
        self.dispatcher, self.admitted = session.dispatcher, session.admitted
        self.run_id, self.started, self.call = session.run_id, session.started, call
        self.effect: Literal["none", "confirmed", "uncertain"] = "none"
        self.call_started = time.monotonic()
        self.closed = False

    def authority(self, service: str = "") -> EffectiveProfile:
        if self.closed:
            raise ToolError("Tool context is no longer active", ErrorCode.TOOL_DENIED)
        store, registry = self.dispatcher.store, self.dispatcher.registry
        profile = restrict_to_admission(self.dispatcher.current(self.admitted), self.admitted)
        tool = registry.tools.get(self.call.name)
        if (
            store.run(self.run_id).state != RunState.RUNNING
            or tool is None
            or tool.name not in profile.tools
            or profile.origin not in tool.origins
        ):
            raise ToolError("Tool is outside current run authority", ErrorCode.TOOL_DENIED)
        if (
            time.monotonic() - self.started >= profile.budgets.active_seconds
            or store.tool_attempts(self.run_id) > profile.budgets.tool_calls
        ):
            raise ToolError("Tool run budget exhausted", ErrorCode.BUDGET_EXCEEDED)
        if time.monotonic() - self.call_started >= profile.budgets.tool_timeout_seconds:
            raise ToolError("Tool execution timed out", ErrorCode.TIMEOUT)
        if service in ("files.write", "effect") and not tool.consequential:
            raise ToolError("Tool did not declare writes", ErrorCode.TOOL_DENIED)
        run = store.run(self.run_id)
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
            and not self.dispatcher.jobs.effect_allowed(store.run(self.run_id), tool.name)
        ):
            raise ToolError("Outbound effect deferred by quiet hours", ErrorCode.TOOL_DENIED)
        return profile

    def begin_effect(self, credentials: tuple[str, ...] = ()) -> None:
        self.authority("effect")
        self.dispatcher.redactor.register(*credentials)
        self.effect = "uncertain"

    def confirm_effect(self) -> None:
        if self.closed:
            raise ToolError("Tool context is no longer active", ErrorCode.TOOL_DENIED)
        if self.effect != "uncertain":
            raise ToolError("Effect must be started before confirmation")
        self.effect = "confirmed"

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
        from solakit.memory import MemoryError

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


def load_registry(profile: EffectiveProfile) -> Registry:
    registry = Registry(tool for tool in TOOLS if tool.name in profile.loaded_tools)
    registry.load(profile.tool_modules)
    missing = profile.loaded_tools - registry.tools.keys()
    if missing:
        raise RegistryError("configured tools are not registered: " + ", ".join(sorted(missing)))
    return registry
