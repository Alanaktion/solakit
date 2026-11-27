"""Bounded headless execution; SQLite owns queues, this service owns live tasks."""

import asyncio
import math
import re
from collections.abc import Sequence
from functools import partial
from typing import TYPE_CHECKING

from pydantic import JsonValue

from solakit.adapters.memory import MemoryService
from solakit.adapters.model import ModelAdapter, ModelError
from solakit.config import ConfigError, EffectiveProfile, restrict_to_admission
from solakit.engine.compaction import Compactor, select
from solakit.engine.lineage import current_run, deny_autonomous_successor
from solakit.engine.prompts import ContextLimit, PromptInput, PromptTurn, assemble
from solakit.records import (
    RUN_TRANSITIONS,
    Artifact,
    CompactionBoundary,
    ErrorCode,
    Failure,
    Message,
    ModelStep,
    Origin,
    Record,
    RecoveryNote,
    Run,
    RunState,
    SessionMode,
    ToolCall,
    ToolReceipt,
    utc_now,
)
from solakit.sdk import ToolError
from solakit.storage.evidence import StorageError, canonical, digest
from solakit.storage.store import Store
from solakit.tools.dispatch import WORKER_TOOLS, Dispatcher, receipt_content
from solakit.tools.images import ImageUploads

if TYPE_CHECKING:
    from solakit.adapters.vision import VisionAdapter
    from solakit.engine.jobs import JobService

_ACTIONS = (
    r"read|write|save|create|generate|publish|search|recall|remember|list|show|check|inspect|use"
    r"|delegate"
)
_NAMED_ENTRY = r"(?:(?:a|the|this|that|new)\s+)*(?:files?|directory|folder)\b"


class QueueFull(RuntimeError):
    pass


class RunFailure(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        self.reason = Failure(code=code, message=message)


def requested_tools(text: str, names: Sequence[str]) -> set[str]:
    # shortcut: English imperative matching is conservative; expand only with evaluated cases.
    text = re.sub(r"\b(?:do not|don't|never)\b[^.;\n]*", "", text, flags=re.I)
    file_target = bool(
        re.search(
            r"\b[\w-]+\.[a-zA-Z0-9]{1,8}\b|(?:/|\\)[\w-]+|"
            rf"\b(?:read|write|save|create|list|copy|rename|move)\s+{_NAMED_ENTRY}",
            text,
        )
    )
    text = text.strip().lower()
    prefix = r"^(?:please\s+)?(?:(?:can|could|would|will) you\s+|i (?:want|need) you to\s+)?"
    web_page = (
        re.match(
            prefix + r"(?:fetch|read|load|open)\s+(?:(?:the\s+)?(?:web\s?page|url)\s+(?:at\s+)?)?"
            r'["“<]?https?://[^\s"”>]+["”>]?',
            text,
        )
        if "web.fetch" in names
        else None
    )
    if web_page:
        text = text[web_page.end() :]
    text = re.sub(r'```.*?```|"[^"]*"|“[^”]*”', "", text, flags=re.S).strip()
    file_command = re.match(prefix + r"(copy|rename|move|mkdir)\b", text)
    if not web_page and not file_command and not re.match(prefix + rf"(?:{_ACTIONS})\b", text):
        return set()
    if re.match(prefix + r"show\b.*\b(?:how to|example|meaning)\b", text):
        return set()
    actions = set(re.findall(rf"\b(?:{_ACTIONS})\b", text))
    creates_directory = re.match(
        prefix + r"create\s+(?:(?:a|the|new)\s+)*(?:directory|folder)\b", text
    )
    if file_command:
        verb = file_command[1]
        actions.add("rename" if verb == "move" else verb)
    if creates_directory:
        actions.add("mkdir")
    if "mkdir" in actions:
        file_target = True
    if "save" in actions or ("create" in actions and not creates_directory):
        actions.add("write")
    image_target = bool(re.search(r"\b(?:images?|photos?|pictures?|illustration)\b", text))
    if "create" in actions and image_target:
        actions.add("generate")
    explicit = {
        name
        for name in names
        if re.search(rf"(?<![\w.-]){re.escape(name.lower())}(?![\w-]|\.[\w-])", text)
    }
    if web_page:
        explicit.add("web.fetch")
    if "delegate.run" in names and re.match(prefix + r"(?:use\s+)?delegate\b", text):
        return {"delegate.run"}
    # A named tool resolves shared verbs such as files.search/transcripts.search.
    actions -= {part for name in explicit for part in re.split(r"[_.-]", name)}
    web_target = bool(
        re.search(r"\bsearch\s+(?:(?:the|for)\s+)?(?:web|internet|online|public sources?)\b", text)
    )
    return {
        name
        for name in names
        if (actions.intersection(re.split(r"[_.-]", name)) or name in explicit)
        and (name != "web.search" or name in explicit or web_target)
        and (
            name not in {"files.search", "transcripts.search"} or name in explicit or not web_target
        )
        and (not ({"file", "files"} & set(re.split(r"[_.-]", name))) or file_target or name in text)
        and (
            not ({"image", "images"} & set(re.split(r"[_.-]", name)))
            or image_target
            or name in text
        )
        and (
            not name.startswith("jobs.") or bool(re.search(r"\b(?:jobs?|wakes?|schedule)\b", text))
        )
        and (
            not name.startswith("blog.")
            or name in explicit
            or bool(re.search(r"\b(?:blogs?|posts?)\b", text))
        )
        and (not name.startswith("mcp.") or name in explicit or bool(re.search(r"\bmcp\b", text)))
        and (
            not name.startswith("dreams.")
            or name in explicit
            or bool(re.search(r"\b(?:dreams?)\b", text))
        )
        and (
            not name.startswith("letters.")
            or name in explicit
            or bool(re.search(r"\b(?:letters?|correspondence)\b(?!\.[\w-])", text))
        )
    }


TRUNCATION_NOTE = "Result truncated to fit the context budget; request a narrower range or page."
MIN_PROJECTED_RESULT_BYTES = 1024


def render_receipt(receipt: ToolReceipt, cap: int) -> str:
    """Render valid receipt JSON within cap, telling the model when its result was cut."""
    raw = receipt_content(receipt.status, receipt.effect, receipt.result, receipt.failure)
    if len(raw) <= cap and not receipt.truncated:
        return raw.decode()
    result = receipt.result
    encoded = (result if isinstance(result, str) else canonical(result).decode()).encode()
    original = max(receipt.result_bytes, len(encoded)) if receipt.truncated else len(encoded)
    budget = len(encoded)
    while budget >= 0:
        shown = encoded[:budget].decode(errors="ignore")
        size = len(shown.encode())
        notice: JsonValue = {
            "shown_bytes": size,
            "original_bytes": original,
            "note": TRUNCATION_NOTE,
        }
        content = receipt_content(
            receipt.status, receipt.effect, shown, receipt.failure, truncated=notice
        )
        if len(content) <= cap:
            return content.decode()
        # JSON escaping makes size nonlinear, so shrink proportionally; strictly decreasing.
        budget = min(size - 1, size * cap // len(content))
    if len(raw) <= cap:
        return raw.decode()
    # The cap cannot hold even an empty structured notice.
    marker = b"[truncated]" if cap >= 11 else b"!"
    return raw[: cap - len(marker)].decode(errors="ignore") + marker.decode()


def records(store: Store, run_id: str) -> list[Record]:
    result: list[Record] = []
    after = 0
    while page := store.run_records(run_id, after=after, limit=100):
        result.extend(record for _, record in page)
        after = page[-1][0]
    return result


def project_run(
    store: Store, run_id: str, cap: int, older_cap: int | None = None
) -> tuple[PromptTurn, ...]:
    """Project execution evidence, pairing even rejected/unexecuted proposals honestly.

    older_cap renders results before the latest tool batch smaller so a long run still fits.
    """
    source = records(store, run_id)
    # A retried attempt contributes only settled exchanges; partial replies are discarded.
    superseded = store.superseded(run_id)
    latest = next(
        (r.id for r in reversed(source) if isinstance(r, ModelStep) and r.tool_calls), None
    )
    calls = {r.id: r for r in source if isinstance(r, ToolCall)}
    receipts = {
        (calls[r.tool_call_id].model_step_id, calls[r.tool_call_id].provider_tool_call_id): r
        for r in source
        if isinstance(r, ToolReceipt)
    }
    turns = []
    for record in source:
        if isinstance(record, Message) and record.role == "user":
            turns.append(
                PromptTurn(
                    source_id=record.id,
                    session_id=record.session_id,
                    role="user",
                    content=record.content,
                    image=record.image if record.image_route != "vision" else None,
                )
            )
        elif isinstance(record, ModelStep):
            if record.purpose == "compaction":
                continue
            if record.purpose == "vision":
                if not record.failure:
                    turns.append(
                        PromptTurn(
                            source_id=record.id,
                            session_id=record.session_id,
                            role="user",
                            content=canonical(
                                {"untrusted_visual_observation": record.content}
                            ).decode(),
                        )
                    )
                continue
            proposals = record.tool_calls
            if superseded:
                proposals = tuple(
                    p for p in proposals if (record.id, p.provider_tool_call_id) in receipts
                )
            if not proposals and (superseded or not record.content):
                continue
            turns.append(
                PromptTurn(
                    source_id=record.id,
                    session_id=record.session_id,
                    role="assistant",
                    content=record.content,
                    tool_calls=proposals,
                )
            )
            for proposal in proposals:
                receipt = receipts.get((record.id, proposal.provider_tool_call_id))
                turns.append(
                    PromptTurn(
                        source_id=receipt.id if receipt else record.id,
                        session_id=record.session_id,
                        role="tool",
                        tool_call_id=proposal.provider_tool_call_id,
                        content=render_receipt(
                            receipt,
                            cap if older_cap is None or record.id == latest else older_cap,
                        )
                        if receipt
                        else "Not executed. Inspect the recovery evidence and correct the call.",
                    )
                )
        elif isinstance(record, RecoveryNote) and not superseded and not store.imported_run(run_id):
            turns.append(
                PromptTurn(
                    source_id=record.id,
                    session_id=record.session_id,
                    role="system",
                    content=record.content,
                )
            )
    run = store.run(run_id)
    if not superseded and run.state not in RUN_TRANSITIONS and run.state != RunState.COMPLETED:
        turns.append(
            PromptTurn(
                source_id=run.id,
                session_id=run.session_id,
                role="system",
                content=f"Previous run outcome: {run.state}. "
                + (f"{run.reason.code}: {run.reason.message}" if run.reason else ""),
            )
        )
    return tuple(turns)


class RunLoop:
    def __init__(
        self,
        store: Store,
        model: ModelAdapter,
        dispatcher: Dispatcher,
        *,
        max_pending: int = 64,
        max_active: int = 8,
        deferred: frozenset[str] = frozenset(),
        memory: MemoryService | None = None,
        vision: VisionAdapter | None = None,
    ) -> None:
        if max_pending < 1 or max_active < 1:
            raise ValueError("queue and active bounds must be positive")
        self.store, self.model, self.dispatcher = store, model, dispatcher
        dispatcher.runs = self
        self.memory = memory
        self.vision = vision
        self.uploads = ImageUploads(store.root)
        self.jobs: JobService | None = None
        self.max_pending, self.max_active, self.deferred = max_pending, max_active, deferred
        self.active: dict[str, asyncio.Task[None]] = {}
        self.sessions: set[str] = set()
        self.cancelling: set[str] = set()
        self.delegating: set[str] = set()
        self.failure: BaseException | None = None
        self.accepting = False
        self.stopping = False
        self.changed = asyncio.Event()
        self.worker: asyncio.Task[None] | None = None

    def _notify(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    async def start(self) -> None:
        if self.worker is not None:
            return
        self.uploads.clean()
        self.failure = None
        self.stopping = False
        self.accepting = True
        self.worker = asyncio.create_task(self._pump(), name="sola-run-queue")

    async def stop(self) -> None:
        self.accepting = False
        self.stopping = True
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        tasks = tuple(self.active.values())
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._notify()

    def submit(
        self,
        session_id: str,
        *,
        profile: EffectiveProfile,
        content: str,
        request_id: str,
        image: Artifact | None = None,
        image_source: tuple[str, str] | None = None,
        upload_id: str | None = None,
        upload: bytes | None = None,
    ) -> Run:
        deny_autonomous_successor()
        if not self.accepting or self.failure is not None:
            raise RuntimeError("run service unavailable")
        existing = self.store.request(request_id)
        if existing is not None:
            admission = self.store.admission_profile(existing.id)
        else:
            if len(self.store.queued_runs()) >= self.max_pending:
                raise QueueFull("pending admission limit reached")
            if (
                profile.data_dir != self.store.root
                or profile.mode != self.store.session(session_id).mode
                or (profile.model_pin, profile.model_base_url)
                != (self.model.profile.model_pin, self.model.profile.model_base_url)
            ):
                raise ConfigError("profile differs from the owning runtime/session")
            effective = restrict_to_admission(self.dispatcher.current(profile), profile)
            if (image is not None or upload_id is not None) and not (
                effective.vision or effective.model_image_inputs
            ):
                raise ConfigError("image inputs are unavailable at admission")
            admission = effective.model_dump(mode="json")
        try:
            run = self.store.admit(
                session_id,
                request_id=request_id,
                content=content,
                profile=profile.name,
                admission=admission,
                origin=profile.origin,
                image=image,
                image_source=image_source,
                upload_id=upload_id,
                upload=upload,
            )
        except BaseException as error:
            if self.store.failed:
                self._fault(error)
            raise
        self._notify()
        if upload_id is not None and upload is not None:
            self.uploads.discard(session_id, upload_id)
        return run

    def _retry_admission(self, run: Run) -> dict[str, object]:
        """The frozen admission of a failed reply that may be retried, or ValueError."""
        if (
            run.state != RunState.FAILED
            or run.origin != Origin.INTERACTIVE
            or run.parent_tool_call_id is not None
            or self.store.imported_run(run.id)
            or self.store.session(run.session_id).archived
            or self.store.runs(run.session_id, before=0, limit=1)[0][1].id != run.id
        ):
            raise ValueError("only the latest failed local reply can be retried")
        for attempt in self.store.retry_chain(run.id):
            source = records(self.store, attempt.id)
            summaries = {r.summary_message_id for r in source if isinstance(r, CompactionBoundary)}
            if any(
                isinstance(r, Message) and r.role == "assistant" and r.id not in summaries
                for r in source
            ):
                raise ValueError("a reply was already saved")
            receipts = {r.tool_call_id: r for r in source if isinstance(r, ToolReceipt)}
            if any(
                r.id not in receipts or receipts[r.id].effect == "uncertain"
                for r in source
                if isinstance(r, ToolCall)
            ):
                raise ValueError("a tool outcome is uncertain or missing")
        admitted = self.store.admitted_profile(run.id)
        self.dispatcher.profile(run, admitted)
        if (admitted.model_pin, admitted.model_base_url) != (
            self.model.profile.model_pin,
            self.model.profile.model_base_url,
        ):
            raise ConfigError("runtime/model identity changed; a new admission is required")
        return self.store.admission_profile(run.id)

    def retryable(self, session_id: str) -> str | None:
        latest = self.store.runs(session_id, before=0, limit=1)
        if not latest:
            return None
        try:
            self._retry_admission(latest[0][1])
        except ValueError, ConfigError:
            return None
        return latest[0][1].id

    def retry(self, run_id: str) -> Run:
        """Synchronous, so concurrent requests share one successor."""
        deny_autonomous_successor()
        if not self.accepting or self.failure is not None:
            raise RuntimeError("run service unavailable")
        failed = self.store.run(run_id)
        request_id = digest(canonical(["retry", run_id]))[:32]
        if existing := self.store.request(request_id):
            if existing.retry_of != run_id:
                raise ValueError("retry identity belongs to another submission")
            return existing
        admission = self._retry_admission(failed)
        if len(self.store.queued_runs()) >= self.max_pending:
            raise QueueFull("pending admission limit reached")
        original = records(self.store, self.store.retry_chain(run_id)[0].id)
        content = next(r.content for r in original if isinstance(r, Message) and r.role == "user")
        try:
            run = self.store.admit(
                failed.session_id,
                request_id=request_id,
                content=content,
                profile=failed.profile,
                admission=admission,
                retry_of=run_id,
            )
        except BaseException as error:
            if self.store.failed:
                self._fault(error)
            raise
        self._notify()
        return run

    def cancel(self, run_id: str) -> Run:
        caller = current_run.get()
        if caller is not None and (
            caller.origin == Origin.DELEGATED or caller.parent_tool_call_id is not None
        ):
            raise ToolError("Delegated work cannot cancel runs", ErrorCode.TOOL_DENIED)
        run = self.store.run(run_id)
        if run.state == RunState.QUEUED:
            run = self.store.transition_run(
                run_id,
                RunState.CANCELLED,
                reason=Failure(code=ErrorCode.CANCELLED, message="Cancelled before execution"),
            )
        elif run.state == RunState.RUNNING and run_id not in self.cancelling:
            self.cancelling.add(run_id)
            self.active[run_id].cancel()
        self._notify()
        return run

    async def delegate(
        self, call: ToolCall, parent: EffectiveProfile, brief: str, remaining: float
    ) -> JsonValue:
        deny_autonomous_successor()
        caller = current_run.get()
        if (
            caller is None
            or caller.id != call.run_id
            or caller.origin != Origin.INTERACTIVE
            or parent.delegation_worker is None
        ):
            raise ToolError("Delegation requires an interactive parent", ErrorCode.TOOL_DENIED)
        if not brief.strip():
            raise ToolError("Provide a self-contained task brief", ErrorCode.INVALID_INPUT)
        if link := self.store.delegation(call.id):
            child = self.store.run(link.child_run_id)
            if child.state in RUN_TRANSITIONS:
                return {"status": "busy"}
            return self.store.delegation_result(link)
        if caller.id in self.delegating or not self.accepting or self.failure is not None:
            return {"status": "busy"}
        self._start_ready()
        if len(self.active) >= self.max_active:
            return {"status": "busy"}
        model_calls = (
            parent.budgets.model_calls
            - self.store.model_index(caller.id)
            - self.store.child_attempts(caller.id, "request")
            - 1
        )
        tool_calls = (
            parent.budgets.tool_calls
            - self.store.tool_attempts(caller.id)
            - self.store.child_attempts(caller.id, "tool")
        )
        if model_calls <= 0 or remaining <= 0 or parent.budgets.tool_result_bytes < 512:
            raise ToolError("Insufficient budget for a child and report", ErrorCode.BUDGET_EXCEEDED)
        worker = self.dispatcher.current(
            parent.model_copy(
                update={
                    "name": parent.delegation_worker,
                    "mode": SessionMode.ISOLATED,
                    "origin": Origin.DELEGATED,
                }
            )
        )
        if (worker.name, worker.origin, worker.mode) != (
            parent.delegation_worker,
            Origin.DELEGATED,
            SessionMode.ISOLATED,
        ):
            raise ConfigError("Delegation resolver returned a different worker")
        worker = restrict_to_admission(parent, worker)
        tools = worker.tools & WORKER_TOOLS
        worker = worker.model_copy(
            update={
                "tools": tools,
                "granted_tools": tools,
                "memory_mode": "off",
                "budgets": worker.budgets.model_copy(
                    update={
                        "model_calls": min(worker.budgets.model_calls, model_calls),
                        "tool_calls": min(worker.budgets.tool_calls, tool_calls),
                        "active_seconds": min(worker.budgets.active_seconds, math.ceil(remaining)),
                    }
                ),
            }
        )
        self.delegating.add(caller.id)
        try:
            child = self.store.admit(
                None,
                request_id=call.id,
                content=brief,
                profile=worker.name,
                admission=worker.model_dump(mode="json"),
                origin=Origin.DELEGATED,
                parent_tool_call_id=call.id,
            )
            task = self._start_run(child)
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                self.cancel(child.id)
                # Shield settlement even if another cancellation arrives during cleanup.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                raise
            link = self.store.delegation(call.id)
            assert link is not None
            return self.store.delegation_result(link)
        finally:
            self.delegating.discard(caller.id)

    async def wait(self, run_id: str, *, timeout: float | None = None) -> Run:
        async with asyncio.timeout(timeout):
            while True:
                changed = self.changed
                run = self.store.run(run_id)
                if run.state not in RUN_TRANSITIONS:
                    return run
                if (
                    caller := current_run.get()
                ) is not None and caller.session_id == run.session_id:
                    return run
                if self.failure is not None:
                    raise StorageError("run worker failed; stop and recover") from self.failure
                if self.stopping:
                    return run
                await changed.wait()

    def _fault(self, error: BaseException) -> None:
        self.failure = error
        self.accepting = False
        for task in self.active.values():
            if task is not asyncio.current_task() and not task.cancelling():
                task.cancel()
        self._notify()

    def _finished(self, run: Run, task: asyncio.Task[None]) -> None:
        self.active.pop(run.id)
        self.sessions.remove(run.session_id)
        self.cancelling.discard(run.id)
        if not task.cancelled() and (error := task.exception()) is not None:
            self._fault(error)
        self._notify()

    def _start_run(self, run: Run) -> asyncio.Task[None]:
        self.sessions.add(run.session_id)
        task = asyncio.create_task(self._execute(run), name=f"sola-run-{run.id}")
        self.active[run.id] = task
        task.add_done_callback(partial(self._finished, run))
        return task

    def _start_ready(self) -> None:
        for run in self.store.queued_runs():
            if len(self.active) >= self.max_active:
                break
            if run.session_id in self.sessions or run.parent_tool_call_id is not None:
                continue
            if self.jobs is not None and run.occurrence_id is not None:
                if not self.jobs.can_start(run):
                    continue
                background = sum(
                    self.store.run(identity).origin in (Origin.SCHEDULED, Origin.PROACTIVE)
                    for identity in self.active
                )
                if background >= self.jobs.policy().background.max_active:
                    continue
            self._start_run(run)

    async def _pump(self) -> None:
        try:
            while True:
                changed = self.changed
                if self.failure is not None:
                    return
                self._start_ready()
                await changed.wait()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fault(error)

    async def _execute(self, queued: Run) -> None:
        lineage_token = current_run.set(queued)
        try:
            if self.store.run(queued.id).state != RunState.QUEUED:
                return
            admitted = self.store.admitted_profile(queued.id)
            self.dispatcher.profile(queued, admitted)
            if self.jobs is not None:
                self.jobs.validate_execution(queued)
            if self.store.session(queued.session_id).archived:
                raise RunFailure(ErrorCode.INVALID_INPUT, "Session was archived before execution")
            run = self.store.transition_run(queued.id, RunState.RUNNING)
            async with asyncio.timeout(admitted.budgets.active_seconds):
                await self._cycle(run, admitted)
        except asyncio.CancelledError:
            state = RunState.CANCELLED if queued.id in self.cancelling else RunState.INTERRUPTED
            self.store.transition_run(
                queued.id,
                state,
                reason=Failure(
                    code=ErrorCode.CANCELLED
                    if state == RunState.CANCELLED
                    else ErrorCode.PROCESS_INTERRUPTED,
                    message="Run cancelled" if state == RunState.CANCELLED else "Runtime stopped",
                ),
            )
        except Exception as error:
            if self.store.failed or isinstance(error, StorageError):
                self._fault(error)
                return
            if isinstance(error, RunFailure):
                reason = error.reason
            elif isinstance(error, TimeoutError):
                reason = Failure(code=ErrorCode.BUDGET_EXCEEDED, message="Active time exhausted")
            elif isinstance(error, ConfigError):
                reason = Failure(code=ErrorCode.CONFIGURATION, message="Admission no longer valid")
            elif isinstance(error, ModelError):
                reason = Failure(code=error.code, message=str(error))
            elif isinstance(error, ContextLimit):
                reason = Failure(code=ErrorCode.CONTEXT_LIMIT, message=str(error))
            else:
                reason = Failure(
                    code=ErrorCode.INVALID_MODEL_RESPONSE,
                    message="Run failed; inspect recorded requests, replies and receipts",
                )
            self.store.transition_run(queued.id, RunState.FAILED, reason=reason)
        finally:
            current_run.reset(lineage_token)

    def _skip(self, call: ToolCall, prior: ToolReceipt) -> ToolReceipt:
        """Record a repeated proposal without dispatch, pointing at the earlier receipt."""
        result: JsonValue = {
            "note": "Not dispatched: an earlier attempt already made this exact call.",
            "receipt_id": prior.id,
            "status": prior.status,
            "effect": prior.effect,
            "result": prior.result,
        }
        encoded = canonical(result)
        self.store.append(call)
        receipt = ToolReceipt(
            origin=call.origin,
            session_id=call.session_id,
            run_id=call.run_id,
            tool_call_id=call.id,
            status="succeeded",
            result=result,
            result_bytes=len(encoded),
            result_sha256=digest(encoded),
        )
        self.store.append(receipt)
        return receipt

    async def _observe(self, run: Run, admitted: EffectiveProfile, user: Message) -> None:
        current = self.dispatcher.profile(run, admitted)
        if current.budgets.model_calls < 2:
            raise RunFailure(ErrorCode.BUDGET_EXCEEDED, "Image recognition needs two model calls")
        if (
            self.vision is None
            or admitted.vision is None
            or self.vision.profile.vision != admitted.vision
            or self.dispatcher.current(admitted).vision != admitted.vision
        ):
            raise RunFailure(ErrorCode.VISION_UNAVAILABLE, "Pinned vision service is unavailable")
        try:
            step = await self.vision.observe(run, user, current.budgets.output_tokens)
        except ModelError as error:
            failure = Failure(code=error.code, message=str(error))
        else:
            if step.failure is None:
                return
            failure = step.failure
        code, message = {
            ErrorCode.TIMEOUT: (ErrorCode.VISION_TIMEOUT, "Vision analysis timed out"),
            ErrorCode.MODEL_MISMATCH: (
                ErrorCode.VISION_MODEL_MISMATCH,
                "Vision service did not return the pinned model",
            ),
            ErrorCode.INVALID_MODEL_RESPONSE: (
                ErrorCode.INVALID_VISION_RESPONSE,
                "Vision service returned an invalid observation",
            ),
        }.get(failure.code, (ErrorCode.VISION_UNAVAILABLE, "Pinned vision service is unavailable"))
        raise RunFailure(code, message)

    async def _cycle(self, run: Run, admitted: EffectiveProfile) -> None:
        def current(admission: EffectiveProfile) -> EffectiveProfile:
            profile = self.dispatcher.profile(run, admission)
            if run.parent_tool_call_id is not None:
                # The operator's global identity and curated memory stay out of worker prompts.
                return profile.model_copy(update={"soul_file": None, "curated_memory_file": None})
            return profile

        tools = self.dispatcher.bind(run.id)
        chain = self.store.retry_chain(run.id)
        source = records(self.store, chain[0].id)
        original = next(r for r in source if isinstance(r, Message) and r.role == "user")
        user = original.model_copy(update={"run_id": run.id})
        # Calls an earlier attempt already settled, keyed by exact name and arguments.
        settled: dict[bytes, ToolReceipt] = {}
        analyzed = False
        done: set[str] = set()
        for attempt in chain[:-1]:
            earlier = records(self.store, attempt.id)
            calls = {r.id: r for r in earlier if isinstance(r, ToolCall)}
            for record in earlier:
                if isinstance(record, ModelStep):
                    analyzed |= record.purpose == "vision" and not record.failure
                elif isinstance(record, ToolReceipt) and (
                    record.status == "succeeded" or record.effect == "confirmed"
                ):
                    call_made = calls[record.tool_call_id]
                    key = canonical([call_made.name, call_made.arguments])
                    settled.setdefault(key, record)
                    done.add(call_made.name)
        first_index = 1
        if user.image_route == "vision" and not analyzed:
            await self._observe(run, admitted, user)
            first_index += 1
        selection = None
        if admitted.mode != SessionMode.EPHEMERAL:
            selection = select(self.store, run, admitted.budgets.tool_result_bytes)

        def project(cap: int, older_cap: int | None = None) -> tuple[PromptTurn, ...]:
            return tuple(
                turn
                for attempt in chain
                for turn in project_run(self.store, attempt.id, cap, older_cap)
            )

        recall = None
        if self.memory and run.parent_tool_call_id is None:
            history = selection.turns if selection else ()
            query = (
                "\n".join(turn.content for turn in history if turn.role in ("user", "assistant"))[
                    -2000:
                ]
                + "\n"
                + user.content
            )
            observation = await self.memory.recall(run, current(admitted), query[-1000:])
            recall = canonical(observation.model_dump(mode="json")).decode()
            if admitted.mode != SessionMode.SHARED or admitted.memory_mode == "off":
                recall = None
            if admitted.memory_mode == "required" and observation.status != "ok":
                raise RunFailure(
                    ErrorCode.MEMORY_UNAVAILABLE, "Required memory recall is unavailable"
                )
        elif admitted.memory_mode == "required":
            raise RunFailure(ErrorCode.MEMORY_UNAVAILABLE, "Required memory service is unavailable")
        hidden = self.deferred
        attempted = set(done)
        requested = requested_tools(user.content, tuple(self.dispatcher.registry.tools))
        if not requested:
            requested = requested_tools(
                user.content, ("files.read", "files.write", "images.generate", "publish_note")
            )
        counts = {"argument_repair": 0, "schema_disclosure": 0, "narration": 0}
        sticky: Failure | None = None
        unrepaired: set[str] = set()
        started = asyncio.get_running_loop().time()

        def recover(step: ModelStep, kind: str, content: str) -> None:
            if counts[kind] >= 1:
                raise RunFailure(ErrorCode.INVALID_MODEL_RESPONSE, f"{kind} recovery exhausted")
            counts[kind] += 1
            note = RecoveryNote.model_validate(
                {
                    "origin": run.origin,
                    "session_id": run.session_id,
                    "run_id": run.id,
                    "model_step_id": step.id,
                    "kind": kind,
                    "content": content,
                }
            )
            self.store.append(note)

        index = first_index
        while index <= admitted.budgets.model_calls:
            profile = current(admitted)
            child_calls = self.store.child_attempts(run.id, "request")
            if (
                index + child_calls > profile.budgets.model_calls
                or asyncio.get_running_loop().time() - started >= profile.budgets.active_seconds
            ):
                raise RunFailure(ErrorCode.BUDGET_EXCEEDED, "Run budget exhausted")
            report_only = child_calls > 0 and index + child_calls == profile.budgets.model_calls
            projected = project(profile.budgets.tool_result_bytes)
            eligible = frozenset() if report_only else tools.eligible()
            frozen = PromptInput(
                at=utc_now(),
                input=user,
                continuation=projected[1:],
                enable_thinking=False,
                recall=recall if profile.memory_mode != "off" else None,
                tools=self.dispatcher.registry.visible(eligible, deferred=hidden),
                tool_catalog=tuple(sorted(hidden & eligible)),
                disclosure="deferred-experiment-v1" if self.deferred else "eager-v1",
            )
            if selection and selection.result_cap != profile.budgets.tool_result_bytes:
                selection = select(self.store, run, profile.budgets.tool_result_bytes)
            if index == first_index and selection and selection.runs:
                prompt, index, selection = await Compactor(self.store, self.model, current).prepare(
                    profile, run, frozen, index, selection
                )
                if index != first_index:
                    # Summary inference yielded control. Recheck grants and budgets before chat.
                    continue
            else:
                older_cap = profile.budgets.tool_result_bytes
                while True:
                    try:
                        prompt = assemble(profile, selection.apply(frozen) if selection else frozen)
                        break
                    except ContextLimit:
                        # Shrink earlier results in this run before failing; evidence is unchanged.
                        older_cap //= 2
                        if older_cap < MIN_PROJECTED_RESULT_BYTES:
                            raise
                        shrunk = project(profile.budgets.tool_result_bytes, older_cap)
                        frozen = frozen.model_copy(update={"continuation": shrunk[1:]})
            async with asyncio.timeout(
                profile.budgets.active_seconds - (asyncio.get_running_loop().time() - started)
            ):
                result = await self.model.complete(prompt, run, index)
            index += 1
            step = result.step
            if step.failure:
                if step.tool_calls and step.failure.code == ErrorCode.INVALID_MODEL_RESPONSE:
                    unrepaired.update(call.name for call in step.tool_calls)
                    recover(
                        step,
                        "argument_repair",
                        "No calls from the malformed batch executed. Correct the arguments "
                        "against the tool schemas and submit complete calls.",
                    )
                    continue
                raise RunFailure(step.failure.code, step.failure.message)
            if result.calls:
                if report_only:
                    raise RunFailure(
                        ErrorCode.BUDGET_EXCEEDED, "Final model call was reserved for the report"
                    )
                eligible = tools.eligible()
                undisclosed = {call.name for call in result.calls} & hidden & eligible
                if undisclosed:
                    hidden -= undisclosed
                    recover(
                        step,
                        "schema_disclosure",
                        "No calls from this batch executed. The requested authorized schemas "
                        "are now disclosed; submit complete calls using them.",
                    )
                    continue
                invalid = False
                delegated = False
                for proposed in result.calls:
                    profile = current(admitted)
                    if (
                        self.store.tool_attempts(run.id) + self.store.child_attempts(run.id, "tool")
                        >= profile.budgets.tool_calls
                        or asyncio.get_running_loop().time() - started
                        >= profile.budgets.active_seconds
                    ):
                        if delegated:
                            # The child can use the remaining tool budget before its parent reply.
                            break
                        raise RunFailure(ErrorCode.BUDGET_EXCEEDED, "Tool budget exhausted")
                    call = ToolCall(
                        origin=run.origin,
                        session_id=run.session_id,
                        run_id=run.id,
                        model_step_id=step.id,
                        provider_tool_call_id=proposed.provider_tool_call_id,
                        name=proposed.name,
                        arguments=proposed.arguments,
                    )
                    declared = self.dispatcher.registry.tools.get(call.name)
                    prior = settled.get(canonical([call.name, call.arguments]))
                    if (
                        prior is None
                        and self.jobs is not None
                        and declared is not None
                        and declared.consequential
                        and call.name in tools.eligible()
                    ):
                        await self.jobs.wait_for_effect(run, call.name)
                    if prior is None:
                        receipt = await tools.dispatch(call)
                    else:
                        receipt = self._skip(call, prior)
                    delegated |= call.name == "delegate.run"
                    attempted.add(call.name)
                    self.store.append(
                        Message(
                            origin=run.origin,
                            session_id=run.session_id,
                            run_id=run.id,
                            role="tool",
                            tool_call_id=call.id,
                            content=render_receipt(receipt, profile.budgets.tool_result_bytes),
                        )
                    )
                    if receipt.status == "uncertain":
                        assert receipt.failure is not None
                        raise RunFailure(receipt.failure.code, receipt.failure.message)
                    if receipt.failure:
                        if (
                            receipt.failure.code == ErrorCode.MEMORY_UNAVAILABLE
                            and profile.memory_mode != "required"
                        ):
                            continue
                        if (
                            receipt.failure.code == ErrorCode.INVALID_INPUT
                            and receipt.effect == "none"
                        ):
                            invalid = True
                            unrepaired.add(call.name)
                        else:
                            sticky = receipt.failure
                    elif receipt.status == "succeeded":
                        unrepaired.discard(call.name)
                if invalid:
                    recover(
                        step, "argument_repair", "Correct rejected arguments using the schemas."
                    )
            else:
                pending = requested - attempted
                refusal = bool(
                    re.search(r"\b(?:cannot|can't|unable|unavailable|denied)\b", step.content, re.I)
                )
                if pending and (not refusal or counts["narration"]):
                    authorized = tools.eligible()
                    available = pending & authorized
                    if not available:
                        raise RunFailure(
                            ErrorCode.TOOL_DENIED,
                            "Requested work was narrated without an authorized tool",
                        )
                    hidden -= available
                    recover(
                        step,
                        "narration",
                        "Requested work remains unattempted. Use the authorized tools "
                        + ", ".join(sorted(available))
                        + " now, or report failure. No execution or success is implied by prose.",
                    )
                    continue
                if pending:
                    sticky = Failure(
                        code=ErrorCode.TOOL_FAILED,
                        message="Requested work remains unexecuted; model reported refusal",
                    )
                if unrepaired:
                    raise RunFailure(
                        ErrorCode.INVALID_MODEL_RESPONSE, "Malformed calls were not repaired"
                    )
                if self.jobs is not None and run.occurrence_id is not None:
                    self.jobs.finish(run, step.content, sticky)
                else:
                    self.store.append(
                        Message(
                            origin=run.origin,
                            session_id=run.session_id,
                            run_id=run.id,
                            role="assistant",
                            content=step.content,
                        )
                    )
                    self.store.transition_run(
                        run.id,
                        RunState.FAILED if sticky else RunState.COMPLETED,
                        reason=sticky,
                    )
                if self.memory:
                    self.memory.changed.set()
                return
            await asyncio.sleep(0)
        raise RunFailure(ErrorCode.BUDGET_EXCEEDED, "Model call budget exhausted")
