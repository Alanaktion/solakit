"""Bounded headless execution; SQLite owns queues, this service owns live tasks."""

import asyncio
import contextvars
import re
from collections.abc import Sequence
from functools import partial
from typing import TYPE_CHECKING

from solakit.compaction import Compactor, select
from solakit.config import ConfigError, EffectiveProfile, restrict_to_admission
from solakit.dispatch import Dispatcher
from solakit.evidence import StorageError, canonical
from solakit.lineage import current_run, deny_autonomous_successor
from solakit.memory import MemoryService
from solakit.model import ModelAdapter, ModelError
from solakit.prompts import ContextLimit, PromptInput, PromptTurn, assemble
from solakit.records import (
    RUN_TRANSITIONS,
    Artifact,
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
from solakit.store import Store

if TYPE_CHECKING:
    from solakit.jobs import JobService

_CURRENT_SESSION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sola_run_session", default=None
)
_ACTIONS = (
    r"read|write|save|create|generate|publish|search|recall|remember|list|show|check|inspect|use"
)


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
            r"\b(?:read|write|save|create|list)\s+(?:(?:a|the|this|that|new)\s+)?"
            r"(?:files?|directory|folder)\b",
            text,
        )
    )
    text = re.sub(r'```.*?```|"[^"]*"|“[^”]*”', "", text, flags=re.S).strip().lower()
    prefix = r"^(?:please\s+)?(?:(?:can|could|would|will) you\s+|i (?:want|need) you to\s+)?"
    if not re.match(prefix + rf"(?:{_ACTIONS})\b", text):
        return set()
    if re.match(prefix + r"show\b.*\b(?:how to|example|meaning)\b", text):
        return set()
    actions = set(re.findall(rf"\b(?:{_ACTIONS})\b", text))
    if "save" in actions or "create" in actions:
        actions.add("write")
    image_target = bool(re.search(r"\b(?:images?|photos?|pictures?|illustration)\b", text))
    if "create" in actions and image_target:
        actions.add("generate")
    explicit = {
        name
        for name in names
        if re.search(rf"(?<![\w.-]){re.escape(name.lower())}(?![\w-]|\.[\w-])", text)
    }
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
        and (
            not name.startswith("dreams.")
            or name in explicit
            or bool(re.search(r"\b(?:dreams?)\b", text))
        )
    }


def render_receipt(receipt: ToolReceipt, cap: int) -> str:
    text = canonical(
        {
            "status": receipt.status,
            "effect": receipt.effect,
            "result": receipt.result,
            "failure": receipt.failure.model_dump(mode="json") if receipt.failure else None,
        }
    ).decode()
    raw = text.encode()
    if len(raw) <= cap:
        return text
    marker = b"[truncated]" if cap >= 11 else b"!"
    return raw[: cap - len(marker)].decode(errors="ignore") + marker.decode()


def records(store: Store, run_id: str) -> list[Record]:
    result: list[Record] = []
    after = 0
    while page := store.run_records(run_id, after=after, limit=100):
        result.extend(record for _, record in page)
        after = page[-1][0]
    return result


def project_run(store: Store, run_id: str, cap: int) -> tuple[PromptTurn, ...]:
    """Project execution evidence, pairing even rejected/unexecuted proposals honestly."""
    source = records(store, run_id)
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
                    image=record.image,
                )
            )
        elif isinstance(record, ModelStep):
            if record.purpose == "compaction":
                continue
            if not record.content and not record.tool_calls:
                continue
            turns.append(
                PromptTurn(
                    source_id=record.id,
                    session_id=record.session_id,
                    role="assistant",
                    content=record.content,
                    tool_calls=record.tool_calls,
                )
            )
            for proposal in record.tool_calls:
                receipt = receipts.get((record.id, proposal.provider_tool_call_id))
                turns.append(
                    PromptTurn(
                        source_id=receipt.id if receipt else record.id,
                        session_id=record.session_id,
                        role="tool",
                        tool_call_id=proposal.provider_tool_call_id,
                        content=render_receipt(receipt, cap)
                        if receipt
                        else "Not executed. Inspect the recovery evidence and correct the call.",
                    )
                )
        elif isinstance(record, RecoveryNote):
            turns.append(
                PromptTurn(
                    source_id=record.id,
                    session_id=record.session_id,
                    role="system",
                    content=record.content,
                )
            )
    run = store.run(run_id)
    if run.state not in RUN_TRANSITIONS and run.state != RunState.COMPLETED:
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
    ) -> None:
        if max_pending < 1 or max_active < 1:
            raise ValueError("queue and active bounds must be positive")
        self.store, self.model, self.dispatcher = store, model, dispatcher
        self.memory = memory
        self.jobs: JobService | None = None
        self.max_pending, self.max_active, self.deferred = max_pending, max_active, deferred
        self.active: dict[str, asyncio.Task[None]] = {}
        self.sessions: set[str] = set()
        self.cancelling: set[str] = set()
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
            )
        except BaseException as error:
            if self.store.failed:
                self._fault(error)
            raise
        self._notify()
        return run

    def cancel(self, run_id: str) -> Run:
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

    async def wait(self, run_id: str, *, timeout: float | None = None) -> Run:
        async with asyncio.timeout(timeout):
            while True:
                changed = self.changed
                run = self.store.run(run_id)
                if run.state not in RUN_TRANSITIONS:
                    return run
                if _CURRENT_SESSION.get() == run.session_id:
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

    async def _pump(self) -> None:
        try:
            while True:
                changed = self.changed
                if self.failure is not None:
                    return
                for run in self.store.queued_runs():
                    if len(self.active) >= self.max_active:
                        break
                    if run.session_id in self.sessions:
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
                    self.sessions.add(run.session_id)
                    task = asyncio.create_task(self._execute(run), name=f"sola-run-{run.id}")
                    self.active[run.id] = task
                    task.add_done_callback(partial(self._finished, run))
                await changed.wait()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fault(error)

    def _profile(self, admitted: EffectiveProfile) -> EffectiveProfile:
        return restrict_to_admission(self.dispatcher.current(admitted), admitted)

    async def _execute(self, queued: Run) -> None:
        token = _CURRENT_SESSION.set(queued.session_id)
        lineage_token = current_run.set(queued)
        try:
            if self.store.run(queued.id).state != RunState.QUEUED:
                return
            admitted = EffectiveProfile.model_validate_json(
                canonical(self.store.admission_profile(queued.id))
            )
            self._profile(admitted)
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
            _CURRENT_SESSION.reset(token)

    async def _cycle(self, run: Run, admitted: EffectiveProfile) -> None:
        tools = self.dispatcher.bind(run.id)
        source = records(self.store, run.id)
        user = next(r for r in source if isinstance(r, Message) and r.role == "user")
        history: tuple[PromptTurn, ...] = ()
        selection = None
        if admitted.mode != SessionMode.EPHEMERAL:
            selection = select(self.store, run, admitted.budgets.tool_result_bytes)
            history = selection.turns
        recall = None
        if self.memory:
            query = (
                "\n".join(turn.content for turn in history if turn.role in ("user", "assistant"))[
                    -2000:
                ]
                + "\n"
                + user.content
            )
            observation = await self.memory.recall(run, self._profile(admitted), query[-1000:])
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
        attempted: set[str] = set()
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

        index = 1
        while index <= admitted.budgets.model_calls:
            profile = self._profile(admitted)
            if (
                index > profile.budgets.model_calls
                or asyncio.get_running_loop().time() - started >= profile.budgets.active_seconds
            ):
                raise RunFailure(ErrorCode.BUDGET_EXCEEDED, "Run budget exhausted")
            current = project_run(self.store, run.id, profile.budgets.tool_result_bytes)
            frozen = PromptInput(
                at=utc_now(),
                input=user,
                prior_turns=tuple(history),
                continuation=current[1:],
                enable_thinking=False,
                recall=recall if profile.memory_mode != "off" else None,
                tools=tools.schemas(deferred=hidden),
                tool_catalog=tuple(sorted(hidden & tools.eligible())),
                disclosure="deferred-experiment-v1" if self.deferred else "eager-v1",
                summary=selection.summary if selection else None,
                context_boundary=selection.boundary.through_message_id
                if selection and selection.boundary
                else None,
                compaction=selection.provenance if selection else None,
            )
            if index == 1:
                _, index = await Compactor(self.store, self.model, self._profile).prepare(
                    profile,
                    run,
                    frozen,
                    index,
                )
                profile = self._profile(admitted)
                if selection is not None:
                    selection = select(self.store, run, profile.budgets.tool_result_bytes)
                    history = selection.turns
                if index > profile.budgets.model_calls:
                    raise RunFailure(ErrorCode.BUDGET_EXCEEDED, "Model call budget exhausted")
                frozen = frozen.model_copy(
                    update={
                        "prior_turns": history,
                        "summary": selection.summary if selection else None,
                        "context_boundary": selection.boundary.through_message_id
                        if selection and selection.boundary
                        else None,
                        "compaction": selection.provenance if selection else None,
                        "recall": recall if profile.memory_mode != "off" else None,
                        "tools": tools.schemas(deferred=hidden),
                        "tool_catalog": tuple(sorted(hidden & tools.eligible())),
                    }
                )
                prompt = assemble(profile, frozen)
            else:
                prompt = assemble(profile, frozen)
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
                for proposed in result.calls:
                    profile = self._profile(admitted)
                    if (
                        self.store.tool_attempts(run.id) >= profile.budgets.tool_calls
                        or asyncio.get_running_loop().time() - started
                        >= profile.budgets.active_seconds
                    ):
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
                    if (
                        self.jobs is not None
                        and declared is not None
                        and declared.consequential
                        and call.name in tools.eligible()
                    ):
                        await self.jobs.wait_for_effect(run, call.name)
                    receipt = await tools.dispatch(call)
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
