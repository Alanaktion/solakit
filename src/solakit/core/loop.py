"""FIFO session queues feeding one bounded, cancellable execution path."""

import asyncio
import logging
import traceback
from dataclasses import dataclass

from solakit.core.compaction import (
    Compactor,
    ContextLimit,
)
from solakit.core.config import Config
from solakit.core.hints import failure_hint
from solakit.core.io import durable
from solakit.core.media import ImageRef, Media
from solakit.core.memory import MemoryClient, MemoryUnavailable
from solakit.core.model import ModelClient, ModelUnavailable
from solakit.core.prompt import read_turn
from solakit.core.recall import recall_query, trivial_turn
from solakit.core.runtime_ops import RuntimeOps
from solakit.core.schemas import (
    Message,
    Origin,
    Run,
    RunStatus,
    SessionArchived,
    now,
)
from solakit.core.step_loop import StepLoopMixin
from solakit.core.steps import (
    max_output,
    valid_history,
)
from solakit.core.store import Store
from solakit.tools import Registry
from solakit.tools.disclosure import ToolView


@dataclass
class Pending:
    run: Run
    text: str
    future: asyncio.Future[Run]
    task: asyncio.Task | None = None
    cancellation: asyncio.Task | None = None
    stream: bool = False
    preview: dict | None = None
    parent_run: str | None = None
    tool_view: ToolView | None = None


class Execution(RuntimeOps, StepLoopMixin):
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
        output_schema: dict | None = None,
        output_render: str | None = None,
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
                output_schema=output_schema,
                output_render=output_render,
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
        output_schema: dict | None = None,
        output_render: str | None = None,
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
                if any(p.run.session == session_id for p in self.pending.values()):
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
                output_schema=output_schema,
                output_render=output_render,
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
                self.workers[session_id] = asyncio.create_task(
                    self._worker(session_id, queue)
                )
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
        assert caller is not None
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
                ancestor: Pending | None = parent
                ancestors_cancelled = False
                while ancestor is not None:
                    ancestors_cancelled |= ancestor.cancellation is not None
                    ancestor = (
                        self.pending.get(ancestor.parent_run)
                        if ancestor.parent_run
                        else None
                    )
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
                async with self.submit_lock:
                    if not pending.future.done():
                        pending.future.set_result(pending.run)
                    self.pending.pop(pending.run.id, None)
                    self.session_locks[session_id].release()
                    self._release_session_lock(session_id)

    def _release_session_lock(self, session_id: str) -> None:
        if session_id not in self.queues and not any(
            pending.run.session == session_id for pending in self.pending.values()
        ):
            self.session_locks.pop(session_id, None)

    async def _work_pending(self, pending: Pending) -> None:
        if pending.cancellation is not None:
            await asyncio.shield(pending.future)
            return
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

    async def _worker(self, session_id: str, queue: asyncio.Queue) -> None:
        while True:
            pending = await queue.get()
            try:
                await self._work_pending(pending)
            except Exception as exc:
                if not pending.future.done():
                    pending.future.set_exception(exc)
            finally:
                async with self.submit_lock:
                    self.pending.pop(pending.run.id, None)
                    queue.task_done()
                    if queue.empty():
                        self.queues.pop(session_id, None)
                        self.workers.pop(session_id, None)
                        self._release_session_lock(session_id)
            if queue.empty():
                return

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

    async def _perform_locked(self, pending: Pending) -> None:  # noqa: C901
        run = pending.run
        task = asyncio.current_task()
        assert task is not None
        previous = self.performing.get(task)
        self.performing[task] = pending
        try:
            # Refresh against current grants after queueing, retaining the admission
            # ceiling. Profile effects/tiers/resources and budgets remain unchanged.
            run.effective_tools = self.registry.effective_tools(
                run.profile, run.effective_tools
            )
            assert pending.tool_view is not None
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
                    recall = await self.memory.scoped(session, run.profile).search(
                        query
                    )
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
                        run.memory_commit = await self.memory.scoped(
                            session, run.profile
                        ).commit(self.store, run.id, new_messages)
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
                            self.store.tool_log,
                            run.id,
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
            for queue in list(self.queues.values()):
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
