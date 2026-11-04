"""FIFO session queues feeding one bounded, cancellable execution path."""

import asyncio
import json
import logging
import traceback
from dataclasses import dataclass

from solakit.core.config import Config
from solakit.core.io import durable
from solakit.core.memory import MemoryClient, MemoryUnavailable
from solakit.core.model import ModelClient, ModelUnavailable
from solakit.core.prompt import read_prompt
from solakit.core.schemas import Message, Origin, Run, RunStatus, Session, now
from solakit.core.store import Store
from solakit.tools import Registry, ToolContext


@dataclass
class Pending:
    run: Run
    text: str
    future: asyncio.Future
    task: asyncio.Task | None = None
    cancellation: asyncio.Task | None = None


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
        self.memory = memory
        self.semaphore = asyncio.Semaphore(config.model.concurrency)
        self.queues: dict[str, asyncio.Queue] = {}
        self.workers: dict[str, asyncio.Task] = {}
        self.pending: dict[str, Pending] = {}
        self.submit_lock = asyncio.Lock()
        self.closing = False
        self._close_task: asyncio.Task | None = None

    async def submit(
        self,
        session_id: str,
        text: str,
        *,
        profile: str | None = None,
        origin: Origin | None = None,
    ) -> Run:
        async with self.submit_lock:
            if self.closing:
                raise ValueError("Runtime is shutting down")
            session = await durable(self.store.session, session_id)
            profile = profile or session.default_profile
            if profile not in self.config.profiles:
                raise ValueError("Unknown profile")
            run = Run(
                session=session_id,
                profile=profile,
                input_message=text,
                origin=origin or Origin(),
            )
            await durable(self.store.save_run, run)
            pending = Pending(run, text, asyncio.get_running_loop().create_future())
            self.pending[run.id] = pending
            queue = self.queues.setdefault(session_id, asyncio.Queue())
            queue.put_nowait(pending)
            if session_id not in self.workers:
                self.workers[session_id] = asyncio.create_task(self._worker(queue))
        # A client disconnect does not silently cancel an accepted run.
        return await asyncio.shield(pending.future)

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
        run = pending.run
        try:
            async with self.semaphore:
                run.status = RunStatus.running
                await durable(self.store.save_run, run)
                await self.model.preflight()
                session = await durable(self.store.session, run.session)
                if session.memory_scope == "isolated":
                    raise ValueError("Isolated memory is not implemented")
                recall = None
                if self.memory and session.memory_scope == "shared":
                    run.memory_status = "recalling"
                    recall = await self.memory.scoped(session).search(pending.text)
                    run.memory_status = "recalled"
                elif session.memory_scope == "ephemeral":
                    run.memory_status = "ephemeral"
                prompt = await durable(
                    read_prompt,
                    self.config,
                    profile=run.profile,
                    session=run.session,
                    origin=run.origin.job or run.origin.channel,
                    at=now(),
                    recall=recall,
                )
                run.prompt_sha256 = await durable(
                    self.store.write_prompt, run.id, prompt
                )
                await durable(self.store.save_run, run)
                await self._loop(run, session, prompt, pending.text)
                if self.memory and session.memory_scope == "shared":
                    history = await durable(self.store.transcript, session.id)
                    new_messages = [m for m in history if m["run_id"] == run.id]
                    if new_messages:
                        try:
                            run.memory_commit = await self.memory.scoped(
                                session
                            ).commit(self.store, run.id, new_messages)
                            run.memory_status = run.memory_commit["status"]
                        except MemoryUnavailable:
                            run.memory_status = "commit_failed_or_uncertain"
        except MemoryUnavailable:
            run.status = RunStatus.memory_unavailable
            run.memory_status = "recall_failed"
            run.error = "Memory recall failed; no session messages were added"
        except ModelUnavailable as exc:
            run.status = RunStatus.model_unavailable
            run.error = str(exc)
        except asyncio.CancelledError:
            run.status = RunStatus.cancelled
            run.error = "Cancelled by operator or shutdown"
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
            run.finished_at = now()
            await durable(self.store.save_run, run)

    async def _loop(self, run: Run, session: Session, prompt: str, text: str) -> None:
        cfg = self.config.profiles[run.profile]
        history = await durable(self.store.transcript, session.id)
        messages = [{"role": "system", "content": prompt}]
        # Interrupted tool groups stay in evidence but cannot form invalid context.
        context = valid_history(history)
        run.context_skipped_messages = len(history) - len(context)
        messages.extend(context)
        user = Message(role="user", content=text, origin=run.origin, run_id=run.id)
        messages.append(user.model_message())
        repairs = 0
        usage_complete = True
        ctx = ToolContext(
            session,
            run.profile,
            self.config,
            self.store,
            run.id,
            memory_client=(
                self.memory.scoped(session)
                if self.memory and session.memory_scope == "shared"
                else None
            ),
        )
        for step in range(cfg.max_steps):
            data = await self.model.complete(
                messages, self.registry.schemas(run.profile)
            )
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
            messages.append(assistant.model_message())
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
