"""Reversible session retirement; immutable transcripts and their search stay put."""

import asyncio
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from solakit.core.io import durable
from solakit.core.schemas import now
from solakit.core.search import timestamp
from solakit.core.store import safe_id


class LifecycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    archived: bool


class ArchiveBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ids: list[str] = Field(default_factory=list, max_length=500)
    before: str | None = None
    inactive_days: int | None = Field(default=None, ge=1, le=36500)
    q: str = Field(default="", max_length=200)
    profile: str | None = None
    expected_activity: dict[str, str] = Field(default_factory=dict, max_length=500)
    dry_run: bool = True


class SessionLifecycle:
    def __init__(self, config, execution, jobs):
        self.config, self.execution, self.jobs = config, execution, jobs
        self.store = execution.store

    def protected(self, identity):
        if any(
            p.run.session == identity and not p.future.done()
            for p in self.execution.pending.values()
        ):
            return "Session has queued or running work"
        if any(c.enabled and c.session == identity for c in self.config.channels):
            return "Session is bound to an enabled channel"
        for job in self.jobs.jobs.values():
            state = self.jobs.states[job.id]
            binding = next(
                (c.session for c in self.config.channels if c.id == job.bind_session),
                job.bind_session,
            )
            if binding == identity and (
                state.active or (job.enabled and not state.consumed)
            ):
                return "Session is used by an enabled or active job"
        return None

    async def _complete(self, operation):
        task = asyncio.create_task(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def change(self, identity, archived):
        return await self._complete(self._change(identity, archived))

    async def _change(self, identity, archived):
        safe_id(identity)
        # Scheduler admission takes its lock before execution admission. Match
        # that ordering so protections cannot change while metadata is published.
        async with self.jobs.lock, self.execution.submit_lock:
            if self.execution.closing or self.jobs.closing:
                raise ValueError("Runtime is shutting down")
            session = await durable(self.store.session, identity)
            if archived and session.archived_at is None:
                reason = self.protected(identity)
                if reason:
                    raise ValueError(reason)
            return (
                await durable(self.store.set_session_archived, identity, archived)
            ).model_dump(mode="json")

    async def batch(self, request, *, archived=True):
        return await self._complete(self._batch(request, archived))

    async def _batch(self, request, archived):
        if request.before and request.inactive_days is not None:
            raise ValueError("Choose before or inactive_days")
        cutoff = (
            timestamp(request.before)
            if request.before
            else (
                timestamp(now() - timedelta(days=request.inactive_days))
                if request.inactive_days
                else None
            )
        )
        if not request.ids and cutoff is None:
            raise ValueError("Choose session IDs or an inactivity cutoff")
        if not archived and not request.ids:
            raise ValueError("Choose session IDs to restore")
        for identity in request.ids:
            safe_id(identity)
        for value in request.expected_activity.values():
            timestamp(value)
        async with self.jobs.lock, self.execution.submit_lock:
            if self.execution.closing or self.jobs.closing:
                raise ValueError("Runtime is shutting down")

            async def pages():
                if request.ids:
                    catalog = await durable(
                        self.store.list_sessions,
                        status="all",
                        ids=list(dict.fromkeys(request.ids)),
                        limit=500,
                    )
                    yield catalog["sessions"]
                    return
                offset = 0
                while True:
                    catalog = await durable(
                        self.store.list_sessions,
                        status="active",
                        q=request.q,
                        before=cutoff,
                        profile=request.profile,
                        limit=500,
                        offset=offset,
                    )
                    yield catalog["sessions"]
                    offset += len(catalog["sessions"])
                    if offset >= catalog["total"] or not catalog["sessions"]:
                        return

            eligible, skipped, found, more = [], [], set(), False
            async for rows in pages():
                for item in rows:
                    if len(eligible) == 500:
                        more = True
                        break
                    identity = item["id"]
                    found.add(identity)
                    reason = self.protected(identity) if archived else None
                    if bool(item.get("archived_at")) == archived:
                        reason = (
                            "Session is already archived"
                            if archived
                            else "Session is already active"
                        )
                    if cutoff and timestamp(item["last_activity_at"]) >= cutoff:
                        reason = "Session has newer activity"
                    expected = request.expected_activity.get(identity)
                    if expected and timestamp(item["last_activity_at"]) != timestamp(
                        expected
                    ):
                        reason = "Session changed since preview"
                    if reason:
                        skipped.append({"id": identity, "reason": reason})
                    else:
                        eligible.append(item)
                if more:
                    break
            skipped.extend(
                {"id": identity, "reason": "Session not found"}
                for identity in dict.fromkeys(request.ids)
                if identity not in found
            )
            if not request.dry_run:
                for item in eligible:
                    updated = await durable(
                        self.store.set_session_archived, item["id"], archived
                    )
                    item["archived_at"] = (
                        updated.archived_at.isoformat() if updated.archived_at else None
                    )
            return {
                "dry_run": request.dry_run,
                "before": cutoff,
                "sessions": eligible,
                "archived": len(eligible) if not request.dry_run and archived else 0,
                "restored": len(eligible)
                if not request.dry_run and not archived
                else 0,
                "skipped": skipped,
                "more": more,
            }
