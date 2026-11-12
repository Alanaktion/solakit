"""Operator session operations and memory note staging, mixed into Execution.

They share Execution's admission lock, pending map and compactor."""

import asyncio

from solakit.core.archival import archive_session
from solakit.core.contracts import ExecutionServices
from solakit.core.io import durable
from solakit.core.memory import MemoryUnavailable
from solakit.core.schemas import MemoryNote, Run, RunStatus, Session, SessionArchived


class RuntimeOps(ExecutionServices):
    async def stage_memory_note(self, text: str) -> MemoryNote:
        task = asyncio.current_task()
        assert task is not None
        pending = self.performing.get(task)
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

            async def compact():
                if self.compactor.strategy(session, memory=self.memory) == "local":
                    # Explicit local compaction uses one inference slot.
                    async with self.semaphore:
                        return await self.compactor.compact(
                            session,
                            memory=self.memory,
                            profile=session.default_profile,
                        )
                return await self.compactor.compact(
                    session, memory=self.memory, profile=session.default_profile
                )

            task = asyncio.create_task(compact())
            try:
                manifest = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Finish accepted remote work/rotation before admission or shutdown
                # can proceed; a disconnected operator does not orphan a compaction.
                await asyncio.gather(task, return_exceptions=True)
                raise
            return {"session": session_id, **manifest}

    async def _compact_run(
        self, run: Run, session: Session, trigger: str, step: int
    ) -> None:
        async def perform():
            manifest = await self.compactor.compact(
                session, memory=self.memory, profile=run.profile
            )
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
