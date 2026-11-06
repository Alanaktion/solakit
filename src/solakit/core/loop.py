"""FIFO session queues feeding one bounded, cancellable execution path."""

import asyncio
import json
import logging
import traceback
from dataclasses import dataclass

from solakit.core.archival import archive_session
from solakit.core.compaction import Compactor, ContextLimit, request_bytes
from solakit.core.config import Config
from solakit.core.io import durable
from solakit.core.media import ImageRef, Media
from solakit.core.memory import MemoryClient, MemoryUnavailable
from solakit.core.model import ModelClient, ModelUnavailable
from solakit.core.prompt import read_prompt
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
from solakit.core.store import Store
from solakit.tools import Registry, ToolContext


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
        self.compactor = Compactor(store, memory)
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
                if profile not in self.config.delegation.allowed_profiles:
                    raise ValueError("Profile is not allowed for delegation")
                if any(
                    p.run.session == session_id and not p.future.done()
                    for p in self.pending.values()
                ):
                    raise ValueError("Child session has queued or running work")
            run = Run(
                session=session_id,
                profile=profile,
                input_message=text,
                input_images=images or [],
                effective_tools=self.registry.effective_tools(profile, tools),
                origin=origin or Origin(),
            )
            await durable(self.store.save_run, run)
            pending = Pending(run, text, asyncio.get_running_loop().create_future())
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
    ) -> Run:
        pending = await self.enqueue(
            session_id,
            text,
            profile=profile,
            origin=origin,
            stream=stream,
            images=images,
            tools=tools,
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
            task = asyncio.create_task(self.compactor.compact(session))
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
            # ceiling. Profile capabilities/scopes and budgets remain unchanged.
            run.effective_tools = self.registry.effective_tools(
                run.profile, run.effective_tools
            )
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
            prompt = await durable(
                read_prompt,
                self.config,
                profile=run.profile,
                session=run.session,
                origin=run.origin.job or run.origin.channel,
                at=now(),
                recall=recall,
            )
            if history is None:
                history = await durable(self.store.transcript, session.id)
            tools = self.registry.schemas(run.profile, run.effective_tools)
            preview = [
                {"role": "system", "content": prompt},
                *valid_history(history),
                Message(
                    role="user",
                    content=pending.text,
                    images=run.input_images,
                    origin=run.origin,
                    run_id=run.id,
                ).model_message(),
            ]
            if (
                request_bytes(
                    self.config.model.model_id,
                    preview,
                    tools,
                    self.config.model.max_tokens,
                )
                > self.config.profiles[run.profile].context_budget
            ):
                await self._compact_run(run, session, "before_turn", 0)
            run.tool_schema_sha256 = await durable(
                self.store.write_tool_schemas, run.id, tools
            )
            run.prompt_sha256 = await durable(self.store.write_prompt, run.id, prompt)
            await durable(self.store.save_run, run)
            await self._loop(run, session, prompt, pending.text)
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
                await durable(self.store.save_run, run)
            finally:
                if previous is None:
                    self.performing.pop(task, None)
                else:
                    self.performing[task] = previous

    async def _loop(self, run: Run, session: Session, prompt: str, text: str) -> None:
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
        )
        messages.append(user.model_message())
        repairs = 0
        usage_complete = True
        ctx = ToolContext(
            session,
            run.profile,
            self.config,
            self.store,
            run.id,
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
        for step in range(cfg.max_steps):
            if self.pending[run.id].cancellation is not None:
                raise asyncio.CancelledError
            tools = self.registry.schemas(run.profile, run.effective_tools)
            if (
                request_bytes(
                    self.config.model.model_id,
                    messages,
                    tools,
                    self.config.model.max_tokens,
                )
                > cfg.context_budget
            ):
                if not cfg.mid_run_compaction or step == 0:
                    raise ContextLimit(
                        "Request exceeds conservative context byte budget"
                    )
                # The previous iteration persisted every tool result. Never rotate
                # between an assistant call envelope and its matching result group.
                await self._compact_run(run, session, "mid_run", step)
                active = await durable(self.store.transcript, session.id)
                messages = [
                    {"role": "system", "content": prompt},
                    *valid_history(active),
                    user.model_message(),
                ]
                if (
                    request_bytes(
                        self.config.model.model_id,
                        messages,
                        tools,
                        self.config.model.max_tokens,
                    )
                    > cfg.context_budget
                ):
                    raise ContextLimit(
                        "Compacted request still exceeds context byte budget"
                    )
            pending = self.pending[run.id]

            if pending.stream:
                active = await durable(self.store.transcript, session.id)
                baseline = sum(
                    m["role"] == "assistant"
                    and m.get("kind", "message") == "message"
                    and m["run_id"] == run.id
                    for m in active
                )

                async def preview(content):
                    pending.preview = {
                        "step": step,
                        "baseline": baseline,
                        "content": content,
                    }

                data = await self.model.complete(messages, tools, on_text=preview)
            else:
                data = await self.model.complete(messages, tools)
            if step == 0:
                await durable(self.store.append_message, session.id, user)
            run.steps = step + 1
            usage = data.get("usage")
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
            assistant = Message(
                role="assistant",
                content=raw.get("content"),
                tool_calls=raw.get("tool_calls"),
                origin=run.origin,
                run_id=run.id,
            )
            await durable(self.store.append_message, session.id, assistant)
            pending.preview = None
            messages.append(assistant.model_message())
            if data["choices"][0].get("finish_reason") == "length":
                # Keep partial output as evidence, but never execute a potentially
                # truncated tool call or mark a token-limited answer complete.
                run.status = RunStatus.budget_exceeded
                run.error = (
                    "Model output reached model.max_tokens "
                    f"({self.config.model.max_tokens}); response may be incomplete."
                )
                return
            calls = assistant.tool_calls or []
            if not calls:
                run.status = RunStatus.completed
                return
            exhausted = False
            for call in calls:
                function = call["function"]
                if exhausted:
                    content = json.dumps(
                        {"error": True, "content": "Repair budget exhausted"}
                    )
                else:
                    result = await self.registry.dispatch(
                        function["name"], function["arguments"], ctx
                    )
                    if pending.cancellation is not None:
                        raise asyncio.CancelledError
                    for artifact in result.artifacts:
                        verified = ctx.path(artifact)
                        if str(verified) not in run.artifacts:
                            run.artifacts.append(str(verified))
                    content = result.model_dump_json()
                    if result.error and result.repairable:
                        repairs += 1
                        exhausted = repairs > cfg.repair_attempts
                tool_message = Message(
                    role="tool",
                    content=content,
                    tool_call_id=call["id"],
                    origin=run.origin,
                    run_id=run.id,
                )
                await durable(self.store.append_message, session.id, tool_message)
                messages.append(tool_message.model_message())
            await durable(self.store.save_run, run)
            if exhausted:
                run.status = RunStatus.tool_error
                return
        run.status = RunStatus.budget_exceeded

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
