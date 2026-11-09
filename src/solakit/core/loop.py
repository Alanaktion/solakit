"""FIFO session queues feeding one bounded, cancellable execution path."""

import asyncio
import hashlib
import json
import logging
import traceback
from dataclasses import dataclass

from solakit.core.aging import age_tool_results
from solakit.core.archival import archive_session
from solakit.core.compaction import (
    Compactor,
    ContextLimit,
    request_bytes,
    retain_recent_tools,
)
from solakit.core.config import Config
from solakit.core.hints import failure_hint
from solakit.core.io import durable
from solakit.core.media import ImageRef, Media
from solakit.core.memory import MemoryClient, MemoryUnavailable
from solakit.core.model import ModelClient, ModelUnavailable, ReasoningExhausted
from solakit.core.prompt import read_turn
from solakit.core.recall import recall_query, trivial_turn
from solakit.core.schemas import (
    MemoryNote,
    Message,
    Origin,
    Run,
    RunStatus,
    Session,
    SessionArchived,
    now,
)
from solakit.core.store import Store, plan_state
from solakit.tools import Registry, ToolContext, render_for_model
from solakit.tools.disclosure import DISCLOSURE_STEPS, ToolView


@dataclass
class Pending:
    run: Run
    text: str
    future: asyncio.Future
    task: asyncio.Task | None = None
    cancellation: asyncio.Task | None = None
    stream: bool = False
    preview: dict | None = None
    parent_run: str | None = None
    tool_view: ToolView | None = None


class Execution:
    def __init__(
        self,
        config: Config,
        store: Store,
        registry: Registry,
        model: ModelClient,
        memory: MemoryClient | None = None,
    ):
        self.config, self.store, self.registry, self.model = (
            config,
            store,
            registry,
            model,
        )
        self.model.media = Media(store.root)
        self.memory = memory
        self.compactor = Compactor(store, memory, model, config.compaction)
        self.semaphore = asyncio.Semaphore(config.model.concurrency)
        self.session_locks: dict[str, asyncio.Lock] = {}
        self.performing: dict[asyncio.Task, Pending] = {}
        self.queues: dict[str, asyncio.Queue] = {}
        self.workers: dict[str, asyncio.Task] = {}
        self.pending: dict[str, Pending] = {}
        self.submit_lock = asyncio.Lock()
        self.closing = False
        self._close_task: asyncio.Task | None = None

    async def enqueue(
        self,
        session_id: str,
        text: str,
        *,
        profile: str | None = None,
        origin: Origin | None = None,
        stream: bool = False,
        images: list[ImageRef] | None = None,
        tools: list[str] | None = None,
        tool_disclosure: str = "inherit",
        tool_resident: list[str] | None = None,
        playbook: str | None = None,
    ) -> Pending:
        # Admission (durable run + queue ownership) is indivisible even if an
        # asynchronous HTTP client disconnects while the run record is written.
        task = asyncio.create_task(
            self._enqueue(
                session_id,
                text,
                profile=profile,
                origin=origin,
                stream=stream,
                images=images,
                tools=tools,
                tool_disclosure=tool_disclosure,
                tool_resident=tool_resident,
                playbook=playbook,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _enqueue(
        self,
        session_id: str,
        text: str,
        *,
        profile: str | None = None,
        origin: Origin | None = None,
        stream: bool = False,
        images: list[ImageRef] | None = None,
        tools: list[str] | None = None,
        tool_disclosure: str = "inherit",
        tool_resident: list[str] | None = None,
        playbook: str | None = None,
        nested_parent: Pending | None = None,
        nested_task: asyncio.Task | None = None,
    ) -> Pending:
        async with self.submit_lock:
            if self.closing:
                raise ValueError("Runtime is shutting down")
            session = await durable(self.store.session, session_id)
            if session.archived_at:
                raise SessionArchived(
                    "Session is archived; restore it before continuing"
                )
            profile = profile or session.default_profile
            if profile not in self.config.profiles:
                raise ValueError("Unknown profile")
            if nested_parent is not None:
                if (
                    nested_parent.future.done()
                    or session.parent_session != nested_parent.run.session
                    or session.memory_scope != "ephemeral"
                    or session.bound_channel is not None
                ):
                    raise ValueError(
                        "Only own unbound ephemeral child sessions can be delegated"
                    )
                if (
                    profile != nested_parent.run.profile
                    and profile not in self.config.delegation.allowed_profiles
                ):
                    raise ValueError("Profile is not allowed for delegation")
                if any(
                    p.run.session == session_id and not p.future.done()
                    for p in self.pending.values()
                ):
                    raise ValueError("Child session has queued or running work")
            view = self.registry.view(
                profile, tools, tool_disclosure, tool_resident, strict=False
            )
            run = Run(
                session=session_id,
                profile=profile,
                input_message=text,
                input_images=images or [],
                access={
                    **self.registry.access_summary(profile),
                    "tools": list(view.effective),
                },
                effective_tools=list(view.effective),
                tool_view=view.metadata(),
                origin=origin or Origin(),
                playbook=playbook,
            )
            await durable(self.store.save_run, run)
            pending = Pending(run, text, asyncio.get_running_loop().create_future())
            pending.tool_view = view
            pending.stream = stream
            # Async API clients need not await the result; consume exceptions to
            # avoid orphan-future warnings without changing await semantics.
            pending.future.add_done_callback(
                lambda future: future.exception() if not future.cancelled() else None
            )
            if nested_parent is not None:
                # No child work exists under admission, so this lock is idle.
                # Reserve it before exposing the pending run to normal enqueues.
                await self.session_locks.setdefault(
                    session_id, asyncio.Lock()
                ).acquire()
                pending.parent_run = nested_parent.run.id
                pending.task = nested_task
            self.pending[run.id] = pending
            if nested_parent is not None:
                return pending
            queue = self.queues.setdefault(session_id, asyncio.Queue())
            queue.put_nowait(pending)
            if session_id not in self.workers:
                self.workers[session_id] = asyncio.create_task(self._worker(queue))
        return pending

    async def submit(
        self,
        session_id: str,
        text: str,
        *,
        profile: str | None = None,
        origin: Origin | None = None,
        stream: bool = False,
        images: list[ImageRef] | None = None,
        tools: list[str] | None = None,
        tool_disclosure: str = "inherit",
        tool_resident: list[str] | None = None,
    ) -> Run:
        pending = await self.enqueue(
            session_id,
            text,
            profile=profile,
            origin=origin,
            stream=stream,
            images=images,
            tools=tools,
            tool_disclosure=tool_disclosure,
            tool_resident=tool_resident,
        )
        # A client disconnect does not silently cancel an accepted run.
        return await asyncio.shield(pending.future)

    async def submit_nested(
        self,
        session_id: str,
        text: str,
        *,
        profile: str,
        origin: Origin,
        on_started=None,
    ) -> Run:
        """Run a private child serially inside the calling run's inference slot."""
        caller = asyncio.current_task()
        parent = self.performing.get(caller)
        if parent is None or parent.run.status != RunStatus.running:
            raise ValueError("Nested execution requires a running tool call")
        admission = asyncio.create_task(
            self._enqueue(
                session_id,
                text,
                profile=profile,
                origin=origin,
                nested_parent=parent,
                nested_task=caller,
            )
        )
        pending = None
        try:
            try:
                pending = await asyncio.shield(admission)
            except asyncio.CancelledError:
                # Finish durable admission and finalize the accepted child below.
                pending = await admission
                if on_started:
                    on_started(pending.run)
                raise
            if on_started:
                on_started(pending.run)
            await self._perform_locked(pending)
            return pending.run
        except asyncio.CancelledError:
            if pending is not None:
                pending.run.status = RunStatus.cancelled
                pending.run.error = (
                    "Cancelled by operator, delegation timeout or shutdown"
                )
                pending.run.finished_at = now()
                await durable(self.store.save_run, pending.run)
                # Explicit child cancellation can return a failure report to its
                # parent. Parent/shutdown cancellation must keep propagating.
                ancestor = parent
                ancestors_cancelled = False
                while ancestor is not None:
                    ancestors_cancelled |= ancestor.cancellation is not None
                    ancestor = self.pending.get(ancestor.parent_run)
                if pending.cancellation is not None and not ancestors_cancelled:
                    caller.uncancel()
                    return pending.run
            raise
        except Exception as exc:
            if pending is not None and not pending.future.done():
                pending.future.set_exception(exc)
            raise
        finally:
            if pending is not None:
                if not pending.future.done():
                    pending.future.set_result(pending.run)
                self.pending.pop(pending.run.id, None)
                self.session_locks[session_id].release()

    async def stage_memory_note(self, text: str) -> MemoryNote:
        pending = self.performing.get(asyncio.current_task())
        if pending is None or pending.run.status != RunStatus.running:
            raise MemoryUnavailable("Memory notes require a running tool call")
        session = await durable(self.store.session, pending.run.session)
        if self.memory is None or session.memory_scope != "shared":
            raise MemoryUnavailable("Memory notes require enabled shared memory")
        if not text.strip() or len(text) > self.config.memory.remember_max_chars:
            raise MemoryUnavailable(
                "Memory note is empty or exceeds remember_max_chars"
            )
        note = MemoryNote(
            run_id=pending.run.id,
            session_id=session.id,
            profile=pending.run.profile,
            text=text,
        )
        await durable(self.store.stage_memory_note, note)
        return note

    async def archive_session(self, session_id: str) -> dict:
        async with self.submit_lock:
            if self.closing:
                raise ValueError("Runtime is shutting down")
            session = await durable(self.store.session, session_id)
            if session.archived_at:
                raise SessionArchived(
                    "Session is archived; restore it before changing context"
                )
            if any(
                pending.run.session == session_id and not pending.future.done()
                for pending in self.pending.values()
            ):
                raise ValueError("Wait for queued or running turns before archiving")
            # Cancellation waits for rotation before releasing admission: no late
            # file operation can race a new turn or runtime shutdown.
            return await durable(archive_session, self.store, session_id)

    async def compact_session(self, session_id: str) -> dict:
        async with self.submit_lock:
            if self.closing:
                raise ValueError("Runtime is shutting down")
            session = await durable(self.store.session, session_id)
            if session.archived_at:
                raise SessionArchived(
                    "Session is archived; restore it before changing context"
                )
            if any(
                p.run.session == session_id and not p.future.done()
                for p in self.pending.values()
            ):
                raise ValueError("Wait for queued or running turns before compacting")
            self.compactor.memory = self.memory

            async def compact():
                if self.compactor.strategy(session) == "local":
                    # Explicit local compaction uses one inference slot.
                    async with self.semaphore:
                        return await self.compactor.compact(session)
                return await self.compactor.compact(session)

            task = asyncio.create_task(compact())
            try:
                manifest = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Finish accepted remote work/rotation before admission or shutdown
                # can proceed; a disconnected operator does not orphan a compaction.
                await asyncio.gather(task, return_exceptions=True)
                raise
            return {"session": session_id, **manifest}

    async def _compact_run(self, run, session, trigger, step):
        async def perform():
            self.compactor.memory = self.memory
            manifest = await self.compactor.compact(session)
            run.compaction = {**manifest, "trigger": trigger, "step": step}
            # Each rotation is retained in append-only run events.
            await durable(self.store.save_run, run)

        task = asyncio.create_task(perform())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Keep accepted rotation and its run attribution together, even when
            # cancellation arrives while the manifest is being committed.
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _worker(self, queue: asyncio.Queue) -> None:
        while True:
            pending = await queue.get()
            try:
                if pending.cancellation is not None:
                    await asyncio.shield(pending.future)
                    continue
                pending.task = asyncio.create_task(self._perform(pending))
                try:
                    await pending.task
                except asyncio.CancelledError:
                    # Cancellation before _perform's first instruction still finalizes.
                    pending.run.status = RunStatus.cancelled
                    pending.run.finished_at = now()
                    await durable(self.store.save_run, pending.run)
                if not pending.future.done():
                    pending.future.set_result(pending.run)
            except Exception as exc:
                if not pending.future.done():
                    pending.future.set_exception(exc)
            finally:
                self.pending.pop(pending.run.id, None)
                queue.task_done()

    async def _perform(self, pending: Pending) -> None:
        try:
            async with self.session_locks.setdefault(
                pending.run.session, asyncio.Lock()
            ):
                async with self.semaphore:
                    await self._perform_locked(pending)
        except asyncio.CancelledError:
            # Also finalize runs cancelled while waiting for either lock.
            pending.run.status = RunStatus.cancelled
            pending.run.error = "Cancelled by operator or shutdown"
            pending.run.finished_at = now()
            await durable(self.store.save_run, pending.run)

    async def _perform_locked(self, pending: Pending) -> None:
        run = pending.run
        task = asyncio.current_task()
        previous = self.performing.get(task)
        self.performing[task] = pending
        try:
            # Refresh against current grants after queueing, retaining the admission
            # ceiling. Profile effects/tiers/resources and budgets remain unchanged.
            run.effective_tools = self.registry.effective_tools(
                run.profile, run.effective_tools
            )
            pending.tool_view = self.registry.view(
                run.profile,
                run.effective_tools,
                pending.tool_view.mode,
                list(pending.tool_view.resident),
                strict=False,
            )
            run.tool_view = pending.tool_view.metadata()
            run.status = RunStatus.running
            await durable(self.store.save_run, run)
            await self.model.preflight()
            session = await durable(self.store.session, run.session)
            if session.memory_scope == "isolated":
                raise ValueError("Isolated memory is not implemented")
            recall, history = None, None
            if self.memory and session.memory_scope == "shared":
                if trivial_turn(pending.text, self.config.memory):
                    run.memory_status = run.recall_status = "skipped_trivial"
                else:
                    run.memory_status = run.recall_status = "recalling"
                    history = await durable(self.store.transcript, session.id)
                    query = recall_query(history, pending.text, self.config.memory)
                    recall = await self.memory.scoped(session).search(query)
                    run.memory_status = run.recall_status = "recalled"
                    run.recall = recall
            elif session.memory_scope == "ephemeral":
                run.memory_status = run.recall_status = "ephemeral"
            else:
                run.recall_status = "disabled"
            prompt, turn = await durable(
                read_turn,
                self.config,
                profile=run.profile,
                session=run.session,
                origin=run.origin.job or run.origin.channel,
                at=now(),
                recall=recall,
                tool_index=pending.tool_view.index_text,
                playbook=run.playbook,
                notes=await durable(self.store.session_notes, session.id),
            )
            if history is None:
                history = await durable(self.store.transcript, session.id)
            tools = pending.tool_view.schemas()
            preview = [
                {"role": "system", "content": prompt},
                *valid_history(history),
                Message(
                    role="user",
                    content=pending.text,
                    images=run.input_images,
                    origin=run.origin,
                    run_id=run.id,
                    turn_context=turn,
                ).model_message(),
            ]
            profile_cfg = self.config.profiles[run.profile]
            if self._over_budget(
                profile_cfg, preview, tools, max_output(self.config, profile_cfg), {}
            ):
                await self._compact_run(run, session, "before_turn", 0)
            if pending.tool_view.mode == "deferred":
                run.tool_catalog_sha256 = await durable(
                    self.store.write_tool_catalog,
                    run.id,
                    pending.tool_view.catalog_json,
                )
            run.tool_schema_sha256 = await durable(
                self.store.write_tool_schemas, run.id, tools
            )
            run.prompt_sha256 = await durable(self.store.write_prompt, run.id, prompt)
            if turn is not None:
                run.turn_context_sha256 = await durable(
                    self.store.write_turn_context, run.id, turn
                )
            await durable(self.store.save_run, run)
            await self._loop(run, session, prompt, pending.text, turn)
            if self.memory and session.memory_scope == "shared":
                # Mid-run compaction may have moved this run's early messages
                # to archives. Commit the whole original run, exactly once.
                evidence = await durable(self.store.run, run.id)
                new_messages = [
                    m
                    for m in evidence["messages"]
                    if m["run_id"] == run.id and m.get("kind", "message") == "message"
                ]
                new_messages.extend(
                    {
                        "role": "assistant",
                        "content": "[noted for memory]\n" + note["text"],
                    }
                    for note in evidence["memory_notes"]
                )
                if new_messages:
                    try:
                        run.memory_commit = await self.memory.scoped(session).commit(
                            self.store, run.id, new_messages
                        )
                        run.memory_status = run.memory_commit["status"]
                    except MemoryUnavailable:
                        run.memory_status = "commit_failed_or_uncertain"
        except ContextLimit as exc:
            run.status = RunStatus.context_limit
            run.error = str(exc)
        except MemoryUnavailable:
            run.status = RunStatus.memory_unavailable
            run.memory_status = run.recall_status = "recall_failed"
            run.error = "Memory recall failed; no session messages were added"
        except ModelUnavailable as exc:
            run.status = RunStatus.model_unavailable
            run.error = str(exc)
        except asyncio.CancelledError:
            run.status = RunStatus.cancelled
            run.error = "Cancelled by operator or shutdown"
            raise
        except Exception as exc:
            run.status = RunStatus.failed
            run.error = "Run failed; inspect configuration and retained evidence"
            diagnostic = {
                "exception_type": type(exc).__name__,
                "traceback": traceback.format_exc(),
            }
            try:
                await durable(self.store.log_diagnostic, run.id, diagnostic)
            except Exception:
                # Keep the original cause available even when storage itself fails.
                logging.getLogger(__name__).error(
                    "Run %s failed; diagnostic persistence failed: %s",
                    run.id,
                    self.store.redactor.redact(diagnostic),
                )
        finally:
            try:
                run.finished_at = now()
                if run.status not in (RunStatus.completed, RunStatus.cancelled):
                    try:
                        logs = await durable(
                            self.store._read,
                            self.store._path("runs", run.id, ".tools.jsonl"),
                        )
                    except (OSError, ValueError):
                        logs = []
                    run.hint = failure_hint(run, self.config, logs)
                await durable(self.store.save_run, run)
            finally:
                if previous is None:
                    self.performing.pop(task, None)
                else:
                    self.performing[task] = previous

    async def _loop(
        self, run: Run, session: Session, prompt: str, text: str, turn=None
    ) -> None:
        cfg = self.config.profiles[run.profile]
        history = await durable(self.store.transcript, session.id)
        messages = [{"role": "system", "content": prompt}]
        # Interrupted tool groups stay in evidence but cannot form invalid context.
        context = valid_history(history)
        run.context_skipped_messages = len(history) - len(context)
        messages.extend(context)
        user = Message(
            role="user",
            content=text,
            images=run.input_images,
            origin=run.origin,
            run_id=run.id,
            turn_context=turn,
        )
        messages.append(user.model_message())
        view = self.pending[run.id].tool_view
        loaded_tools = list(view.resident)
        step = work_steps = disclosure_steps = 0

        def activate(names):
            added = [
                n
                for n in dict.fromkeys(names)
                if n in view.effective and n not in loaded_tools
            ]
            if added:
                loaded_tools.extend(added)
                run.tool_view["activations"].append(
                    {"step": step + 1, "activated": added}
                )

        repairs = RepairBudget(cfg.repair_attempts, cfg.repair_ceiling)
        last_failure = None
        repeats: dict = {}
        outcomes: set = set()
        no_progress = 0
        reminded = False

        async def inject(kind: str, text: str, *, as_message: bool = False) -> None:
            """Request-only runtime notice; recorded as run evidence, never as a
            transcript message attributed to the user, assistant or a tool."""
            if as_message or messages[-1].get("role") != "tool":
                messages.append(
                    {
                        "role": "user",
                        "content": f"<runtime-notice>\n{text}\n</runtime-notice>",
                    }
                )
            else:
                messages[-1] = {
                    **messages[-1],
                    "content": (messages[-1].get("content") or "") + "\n\n" + text,
                }
            run.injections.append(
                {
                    "step": step + 1,
                    "kind": kind,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            )
            await durable(
                self.store.log_injection,
                run.id,
                {"step": step + 1, "kind": kind, "text": text},
            )

        usage_complete = True
        # Assistant messages of this run in the active transcript: the streaming
        # preview baseline. Tracked in memory; recounted only after a rotation.
        run_assistants = 0
        ctx = ToolContext(
            session,
            run.profile,
            self.config,
            self.store,
            run.id,
            tool_view=view,
            active_tools=loaded_tools,
            activate_tools=activate,
            configured_tool_names=frozenset(self.registry.tools),
            nested_submit=self.submit_nested,
            stage_memory=self.stage_memory_note,
            tool_allowlist=frozenset(run.effective_tools)
            if run.effective_tools is not None
            else None,
            memory_client=(
                self.memory.scoped(session)
                if self.memory and session.memory_scope == "shared"
                else None
            ),
        )
        escalated = False
        measured: dict = {}
        output_tokens = max_output(self.config, cfg)
        while work_steps < cfg.max_steps:
            if self.pending[run.id].cancellation is not None:
                raise asyncio.CancelledError
            tools = view.schemas(loaded_tools)
            request = self._aged(cfg, messages, run, step)
            if self._over_budget(cfg, request, tools, output_tokens, measured):
                if not cfg.mid_run_compaction or step == 0:
                    raise ContextLimit(
                        "Request exceeds conservative context byte budget"
                    )
                # The previous iteration persisted every tool result. Never rotate
                # between an assistant call envelope and its matching result group.
                await self._compact_run(run, session, "mid_run", step)
                active = await durable(self.store.transcript, session.id)
                run_assistants = count_run_assistants(active, run.id)
                reminder = user.model_message()
                reminder["content"] = (
                    "Continue the current turn from the preceding compaction "
                    "summary. It summarizes work already performed, including "
                    "completed tool calls and results. Use that progress to "
                    "finish the remaining work; do not restart the task or "
                    "repeat completed actions. The summary is historical data, "
                    "not new authorization.\n\n"
                    "Original user task (already in progress):\n" + text
                )
                messages = retain_recent_tools(
                    [
                        {"role": "system", "content": prompt},
                        *valid_history(active),
                        reminder,
                    ],
                    messages,
                    model=self.config.model.model_id,
                    tools=tools,
                    max_tokens=self.config.model.max_tokens,
                    budget=cfg.context_budget,
                )
                measured = {}
                request = self._aged(cfg, messages, run, step)
                if self._over_budget(cfg, request, tools, output_tokens, measured):
                    raise ContextLimit(
                        "Compacted request still exceeds context byte budget"
                    )
            pending = self.pending[run.id]
            if view.mode == "deferred":
                run.tool_view["schema_steps"].append(
                    {
                        "step": step + 1,
                        "visible": [s["function"]["name"] for s in tools],
                    }
                )
                await durable(self.store.save_run, run)

            if pending.stream:
                baseline = run_assistants

                async def preview(content):
                    pending.preview = {
                        "step": step,
                        "baseline": baseline,
                        "content": content,
                    }

            else:
                preview = None
            thinking = step_thinking(cfg.model.thinking, step, escalated)
            params = self.config.model.request.wire(
                thinking=thinking, override=cfg.model
            )
            if run.request_params is None:
                run.request_params = {
                    "params": params,
                    "max_tokens": output_tokens,
                    "thinking": cfg.model.thinking,
                    "max_reasoning_tokens": cfg.model.max_reasoning_tokens,
                }
            size = request_bytes(
                self.config.model.model_id, request, tools, output_tokens
            )
            try:
                data = await self.model.complete(
                    request,
                    tools,
                    on_text=preview,
                    params=params,
                    max_tokens=output_tokens,
                    max_reasoning_chars=(
                        cfg.model.max_reasoning_tokens * 4
                        if cfg.model.max_reasoning_tokens
                        else None
                    ),
                )
            except ReasoningExhausted:
                # Retry once in the same step with thinking off; recorded.
                run.retries.append({"step": step + 1, "reason": "reasoning_exhausted"})
                params = self.config.model.request.wire(
                    thinking="off", override=cfg.model
                )
                data = await self.model.complete(
                    request,
                    tools,
                    on_text=preview,
                    params=params,
                    max_tokens=output_tokens,
                )
            if step == 0:
                await durable(self.store.append_message, session.id, user)
            run.steps = step + 1
            usage = data.get("usage")
            if isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int):
                measured = {"tokens": usage["prompt_tokens"], "bytes": size}
                await durable(
                    self.store.record_calibration,
                    self.config.model.model_id,
                    size,
                    usage["prompt_tokens"],
                )
            if (
                usage_complete
                and isinstance(usage, dict)
                and all(
                    isinstance(usage.get(k), int)
                    for k in ("prompt_tokens", "completion_tokens")
                )
            ):
                if run.tokens is None:
                    run.tokens = {"prompt_tokens": 0, "completion_tokens": 0}
                for key in run.tokens:
                    run.tokens[key] += usage[key]
            else:
                # Any missing usage means the aggregate is unknown.
                run.tokens = None
                usage_complete = False
            raw = data["choices"][0]["message"]
            if raw.get("reasoning"):
                run.reasoning_steps += 1
                await durable(
                    self.store.log_reasoning,
                    run.id,
                    {"step": step + 1, "thinking": thinking, "text": raw["reasoning"]},
                )
            assistant = Message(
                role="assistant",
                content=raw.get("content"),
                tool_calls=raw.get("tool_calls"),
                origin=run.origin,
                run_id=run.id,
                tool_call_source=raw.get("tool_call_source"),
            )
            await durable(self.store.append_message, session.id, assistant)
            run_assistants += 1
            pending.preview = None
            messages.append(assistant.model_message())
            if data["choices"][0].get("finish_reason") == "length":
                # Keep partial output as evidence, but never execute a potentially
                # truncated tool call or mark a token-limited answer complete.
                run.status = RunStatus.budget_exceeded
                if (
                    not (raw.get("content") or "").strip()
                    and not raw.get("tool_calls")
                    and raw.get("reasoning")
                ):
                    run.error = (
                        "Thinking exhausted model.max_tokens "
                        f"({output_tokens}) before a visible reply; raise "
                        "max_tokens or set thinking = "
                        '"off" for this profile.'
                    )
                else:
                    run.error = (
                        f"Model output reached model.max_tokens ({output_tokens}); "
                        "response may be incomplete."
                    )
                return
            calls = assistant.tool_calls or []
            if not calls:
                if cfg.plan_reminder and not reminded:
                    open_items = [
                        item["text"]
                        for item in plan_state(
                            await durable(self.store.plan_events, run.id)
                        )
                        if item["status"] in ("todo", "doing")
                    ]
                    if open_items and work_steps + 1 < cfg.max_steps:
                        reminded = True
                        await inject(
                            "plan_reminder",
                            "You have unfinished plan items: "
                            + "; ".join(open_items[:5])
                            + ". Finish them, or mark them skipped with a reason, "
                            "then answer.",
                            as_message=True,
                        )
                        work_steps += 1
                        step += 1
                        run.tool_view["work_steps"] = work_steps
                        continue
                run.tool_view["work_steps"] = work_steps + 1
                run.status = RunStatus.completed
                return
            exhausted = False
            disclosure_only = view.mode == "deferred"
            new_information = False
            for call in calls:
                function = call["function"]
                name = function["name"]
                discovery = view.mode == "deferred" and (
                    name in ("tools.search", "tools.load")
                    or (
                        name in view.effective
                        and name not in loaded_tools
                        and self.registry.allowed(name, run.profile)
                    )
                )
                disclosure_only = disclosure_only and discovery
                if exhausted:
                    content = (
                        "Error: Repair budget exhausted"
                        if cfg.tool_result_format == "text"
                        else json.dumps(
                            {"error": True, "content": "Repair budget exhausted"}
                        )
                    )
                else:
                    result = await self.registry.dispatch(
                        function["name"], function["arguments"], ctx
                    )
                    for artifact in result.artifacts:
                        if artifact not in run.artifacts:
                            run.artifacts.append(artifact)
                    key = (result._meta.get("name"), result._meta.get("args_hash"))
                    if result.error:
                        if key == last_failure:
                            result.content += (
                                "\nYou already tried exactly this. Change the "
                                "approach or answer without the tool."
                            )
                        last_failure = key
                    else:
                        last_failure = None
                    outcome = hashlib.sha256(result.content.encode()).hexdigest()
                    seen = repeats.setdefault(key, {"count": 0, "outcome": None})
                    seen["count"] = (
                        seen["count"] + 1 if seen["outcome"] == outcome else 1
                    )
                    seen["outcome"] = outcome
                    new_information |= outcome not in outcomes
                    outcomes.add(outcome)
                    if seen["count"] >= 3:
                        run.repeated_calls += 1
                        result.content += (
                            f"\nNote: this exact call has run {seen['count']} "
                            "times with the same outcome."
                        )
                        if cfg.repair_ceiling is not None:
                            exhausted |= repairs.fail(key[0], "repeat")
                    if result.error and result.repairable:
                        escalated = True
                        exhausted |= repairs.fail(
                            result._meta.get("name"), result._meta.get("error_class")
                        )
                    elif not result.error:
                        repairs.success(result._meta.get("name"))
                    content = render_for_model(result, cfg.tool_result_format)
                tool_message = Message(
                    role="tool",
                    content=content,
                    tool_call_id=call["id"],
                    origin=run.origin,
                    run_id=run.id,
                )
                await durable(self.store.append_message, session.id, tool_message)
                messages.append(tool_message.model_message())
                # A tool that finished despite a cancellation request keeps its
                # completed result in history; no further calls are dispatched.
                if pending.cancellation is not None:
                    raise asyncio.CancelledError
            if disclosure_only:
                disclosure_steps += 1
            else:
                work_steps += 1
            run.tool_view["disclosure_steps"] = disclosure_steps
            run.tool_view["work_steps"] = work_steps
            step += 1
            await durable(self.store.save_run, run)
            if disclosure_steps >= DISCLOSURE_STEPS:
                run.status = RunStatus.tool_error
                run.error = "tool loading did not converge"
                return
            if exhausted:
                run.status = RunStatus.tool_error
                return
            no_progress = 0 if new_information else no_progress + 1
            remaining = cfg.max_steps - work_steps
            if cfg.step_notice_at and 0 < remaining <= cfg.step_notice_at:
                await inject(
                    "step_notice",
                    f"[Runtime: {remaining} step(s) left. Finish soon; give the "
                    "best answer you can.]",
                )
            elif cfg.step_notice_at and no_progress >= 2:
                no_progress = 0
                await inject(
                    "no_progress",
                    "[Runtime: the last two steps produced no new information. "
                    "Change approach or finish with the best answer you can.]",
                )
        if cfg.wrapup != "off":
            await inject(
                "wrapup",
                "The step budget is used up. Do not call tools. Give your best "
                "final answer now from what you already have, and say what is "
                "unfinished.",
                as_message=True,
            )
            try:
                data = await self.model.complete(
                    self._aged(cfg, messages, run, step),
                    tools,
                    params=self.config.model.request.wire(
                        thinking="off" if cfg.model.thinking else None,
                        override=cfg.model,
                    ),
                    max_tokens=output_tokens,
                    tool_choice="none",
                )
            except ModelUnavailable:
                # The honest outcome stands: the step budget was exceeded.
                run.status = RunStatus.budget_exceeded
                return
            raw = data["choices"][0]["message"]
            text = (raw.get("content") or "").strip()
            if text and not raw.get("tool_calls"):
                assistant = Message(
                    role="assistant",
                    content=raw.get("content"),
                    origin=run.origin,
                    run_id=run.id,
                )
                await durable(self.store.append_message, session.id, assistant)
                run.completed_via_wrapup = True
                if (
                    cfg.wrapup == "answer"
                    and data["choices"][0].get("finish_reason") != "length"
                ):
                    run.status = RunStatus.completed
                    return
        run.status = RunStatus.budget_exceeded

    def _aged(self, cfg, messages, run, step) -> list[dict]:
        if cfg.tool_result_aging != "auto":
            return messages
        request, stubbed = age_tool_results(
            messages, cfg.aging_keep_groups, cfg.aging_min_chars
        )
        if stubbed:
            entry = {"step": step + 1, "stubbed": stubbed}
            if not run.aging or run.aging[-1]["stubbed"] != stubbed:
                run.aging.append(entry)
        return request

    def _over_budget(self, cfg, messages, tools, output_tokens, measured) -> bool:
        """Byte guard always; calibrated token budget when context_tokens is set.

        The previous step's server-reported prompt_tokens is an exact floor for
        the next request within a run (the prompt only grows)."""
        size = request_bytes(self.config.model.model_id, messages, tools, output_tokens)
        if size > cfg.context_budget:
            return True
        if not cfg.context_tokens:
            return False
        sample = self.store.calibration(self.config.model.model_id)
        if not sample:
            return False
        ratio = sample["bytes_per_token"]
        estimate = size / ratio
        if measured:
            estimate = max(
                estimate, measured["tokens"] + (size - measured["bytes"]) / ratio
            )
        return (
            estimate + output_tokens > cfg.context_tokens
            or estimate > 0.85 * cfg.context_tokens
        )

    async def cancel(self, run_id: str) -> bool:
        pending = self.pending.get(run_id)
        if pending is None or pending.future.done():
            return False
        # Assign before yielding: concurrent callers share one durable operation.
        # Request cancellation must not interrupt cancellation of the actual run.
        if pending.cancellation is None:
            pending.cancellation = asyncio.create_task(self._cancel_pending(pending))
        await asyncio.shield(pending.cancellation)
        return True

    async def _cancel_pending(self, pending: Pending) -> None:
        if pending.task is not None:
            # Parent and nested child share a task. Shutdown may request both;
            # deliver cancellation once so durable finalization cannot be cut off.
            if not pending.task.cancelling():
                pending.task.cancel()
            await asyncio.shield(pending.future)
        else:
            pending.run.status = RunStatus.cancelled
            pending.run.finished_at = now()
            try:
                await durable(self.store.save_run, pending.run)
            except Exception as exc:
                if not pending.future.done():
                    pending.future.set_exception(exc)
                raise
            pending.future.set_result(pending.run)

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        # Wait for submissions already inside acceptance, then stop admission and
        # snapshot all accepted work under the same lock. No worker can appear late.
        async with self.submit_lock:
            self.closing = True
            accepted = list(self.pending)
        try:
            outcomes = await asyncio.gather(
                *(self.cancel(run_id) for run_id in accepted), return_exceptions=True
            )
            for queue in self.queues.values():
                await queue.join()
            failures = [
                outcome for outcome in outcomes if isinstance(outcome, Exception)
            ]
            if failures:
                raise ExceptionGroup(
                    "Run finalization failed during shutdown", failures
                )
        finally:
            for worker in self.workers.values():
                worker.cancel()
            await asyncio.gather(*self.workers.values(), return_exceptions=True)
            try:
                await self.model.close()
            finally:
                if self.memory:
                    await self.memory.close()


class RepairBudget:
    """Repairable-error accounting.

    Without a ceiling (default) this is the historical rule: more than
    ``attempts`` repairs in a run is exhaustion. With a ceiling, each tool +
    error class gets ``attempts`` repairs, the run total is capped by the
    ceiling, and a successful call of the tool that just failed refunds one."""

    def __init__(self, attempts: int, ceiling: int | None = None):
        self.attempts, self.ceiling = attempts, ceiling
        self.total = 0
        self.classes: dict[tuple, int] = {}
        self.last_failed: str | None = None

    def fail(self, tool: str | None, error_class: str | None) -> bool:
        self.total += 1
        if self.ceiling is None:
            return self.total > self.attempts
        key = (tool, error_class)
        self.classes[key] = self.classes.get(key, 0) + 1
        self.last_failed = tool
        return self.classes[key] > self.attempts or self.total > self.ceiling

    def success(self, tool: str | None) -> None:
        if self.ceiling is not None and tool == self.last_failed and self.total:
            self.total -= 1
        self.last_failed = None


def max_output(config: Config, profile) -> int:
    return profile.model.max_tokens or config.model.max_tokens


def step_thinking(mode: str | None, step: int, escalated: bool) -> str | None:
    """Per-step thinking. ``auto``: on for the fresh user message and after a
    repairable tool error; off for plain tool-result continuations."""
    if mode == "auto":
        return "on" if step == 0 or escalated else "off"
    return mode


def count_run_assistants(messages: list[dict], run_id: str) -> int:
    return sum(
        m["role"] == "assistant"
        and m.get("kind", "message") == "message"
        and m["run_id"] == run_id
        for m in messages
    )


def valid_history(history: list[dict]) -> list[dict]:
    """Include only complete assistant/tool groups; retain every source entry."""
    result = []
    index = 0
    while index < len(history):
        message = Message.model_validate(history[index])
        calls = message.tool_calls or []
        if calls:
            following = history[index + 1 : index + 1 + len(calls)]
            expected = [call["id"] for call in calls]
            actual = [
                item.get("tool_call_id")
                for item in following
                if item.get("role") == "tool"
            ]
            if expected == actual:
                result.append(message.model_message())
                result.extend(
                    Message.model_validate(item).model_message() for item in following
                )
                index += len(calls)
        elif message.role != "tool":
            result.append(message.model_message())
        index += 1
    return result
