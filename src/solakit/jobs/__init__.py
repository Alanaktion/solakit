"""Persistent job cursors and operations; APScheduler supplies the wake-up/cron clock.

Definitions are authoritative SQLite records, with one-time TOML import.
Scheduling cursors remain JSONL evidence with a rebuildable SQLite index.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

from solakit.core.config import Config
from solakit.tools.disclosure import ToolView

if TYPE_CHECKING:
    from solakit.channels import Channels
    from solakit.core.loop import Execution

from string import Formatter
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from pydantic import BaseModel

from solakit.core.config import Delivery, Job
from solakit.core.io import durable
from solakit.core.schemas import Origin, Run, RunStatus, Session, now
from solakit.core.store import safe_id
from solakit.jobs.definitions import Definitions
from solakit.jobs.schemas import JobToolsError, JobUpdate

T = TypeVar("T")


def render_task(job: Job, at: datetime) -> str:
    local = at.astimezone(ZoneInfo(job.timezone))
    values = {
        "date": local.date().isoformat(),
        "time": local.time().isoformat(timespec="seconds"),
        "timezone": job.timezone,
        "job_id": job.id,
    }
    for _, field, spec, conversion in Formatter().parse(job.task):
        if field is not None and (field not in values or spec or conversion):
            raise ValueError("Task supports only {date}, {time}, {timezone}, {job_id}")
    rendered = job.task.format_map(values)
    if len(rendered) > 32000:
        raise ValueError("Expanded task exceeds 32000 characters")
    return rendered


class JobState(BaseModel):
    id: str
    schedule: str
    next_fire: datetime | None = None
    paused: bool = False
    active: bool = False
    last_run: str | None = None
    last_status: str | None = None
    consumed: bool = False


def schedule_key(job: Job) -> str:
    # Keep existing cron keys stable across upgrades.
    value = f"once:{job.run_at}" if job.run_at else f"{job.cron}:{job.timezone}"
    return hashlib.sha256(value.encode()).hexdigest()


def trigger_for(job: Job) -> CronTrigger | DateTrigger:
    if job.run_at:
        return DateTrigger(run_date=datetime.fromisoformat(job.run_at))
    assert job.cron is not None
    return CronTrigger.from_crontab(job.cron, timezone=job.timezone)


class Jobs:
    def __init__(
        self,
        config: Config,
        execution: Execution,
        channels: Channels,
        clock: Callable[[], datetime] = now,
    ) -> None:
        self.config, self.execution, self.channels, self.clock = (
            config,
            execution,
            channels,
            clock,
        )
        self.definitions = Definitions(execution.store)
        self.jobs: dict[str, Job] = {}
        self.states: dict[str, JobState] = {}
        self.triggers: dict[str, CronTrigger | DateTrigger] = {}
        self.tasks: dict[str, asyncio.Task[Run]] = {}
        self.admissions: dict[str, asyncio.Future[dict]] = {}
        self.lock = asyncio.Lock()
        self.scheduler: AsyncIOScheduler | None = None
        self.closing = False

    async def initialize(self) -> None:
        loaded = await durable(
            self.definitions.load, self.config.jobs, validate=self.validate_job
        )
        for job in loaded:
            # Persisted jobs may outlive a profile edit. Retain the definition,
            # expose drift, and intersect at execution rather than widen access.
            self.validate_job(job, check_tools=False)
            self.jobs[job.id] = job
            self.triggers[job.id] = trigger_for(job)
            schedule = schedule_key(job)
            saved = await durable(self.execution.store.job_state, job.id)
            state = (
                JobState.model_validate(saved)
                if saved
                else JobState(id=job.id, schedule=schedule)
            )
            if not saved or state.schedule != schedule:
                state.schedule = schedule
                state.consumed = False
                state.next_fire = self._next(job, self.clock())
            if not job.enabled or state.consumed:
                state.next_fire = None
            elif state.next_fire is None:
                state.next_fire = self._next(job, self.clock())
            self.states[job.id] = state
            if state.active:
                state.active = False
                if job.run_at:
                    state.consumed, state.next_fire = True, None
                await self._missed(
                    job,
                    state,
                    self.clock(),
                    "Interrupted dispatch during downtime; not replayed",
                )
            await self._save(state)

    def _next(self, job: Job, at: datetime) -> datetime | None:
        return self.triggers[job.id].get_next_fire_time(None, at)

    async def _save(self, state: JobState) -> None:
        await durable(
            self.execution.store.save_job, state.id, state.model_dump(mode="json")
        )

    async def start(self) -> None:
        await self.initialize()
        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_job(
            self.tick,
            "interval",
            seconds=1,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
        self.scheduler.start()
        await self.tick()

    async def stop_admission(self) -> None:
        async with self.lock:
            self.closing = True
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown(wait=False)

    async def drain(self) -> None:
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def _session(self, job: Job) -> Session:
        parent = await self._bound_session(job)
        if not job.sub_session:
            return parent
        return await durable(
            self.execution.store.create_session,
            Session(
                title=f"Job {job.id}",
                source="cron",
                default_profile=job.profile,
                memory_scope=parent.memory_scope,
                parent_session=parent.id,
            ),
        )

    async def _bound_session(self, job: Job) -> Session:
        channel = next(
            (c for c in self.config.channels if c.id == job.bind_session), None
        )
        if channel:
            return await durable(
                self.channels.router.channel,
                channel.type,
                channel.channel_id,
                source="cron",
            )
        if job.bind_session == "ephemeral":
            return await durable(
                self.execution.store.create_session,
                Session(
                    source="cron", default_profile=job.profile, memory_scope="ephemeral"
                ),
            )
        return await durable(
            self.execution.store.create_session,
            Session(id=job.bind_session, source="cron", default_profile=job.profile),
        )

    async def _missed(
        self, job: Job, state: JobState, at: datetime, reason: str
    ) -> None:
        session = await self._session(job)
        view = self.tool_view(job)
        run = Run(
            session=session.id,
            profile=job.profile,
            origin=Origin(channel="job", job=job.id),
            input_message=render_task(job, at),
            tool_view=view.metadata(),
            effective_tools=list(view.effective),
            status=RunStatus.missed,
            created_at=at,
            finished_at=self.clock(),
            error=reason,
        )
        await durable(self.execution.store.save_run, run)
        state.last_run, state.last_status = run.id, "missed"

    async def tick(self) -> None:
        async with self.lock:
            if self.closing:
                return
            at = self.clock()
            for job in self.jobs.values():
                state = self.states[job.id]
                if (
                    not job.enabled
                    or state.consumed
                    or state.paused
                    or state.next_fire is None
                    or state.next_fire > at
                ):
                    continue
                due = state.next_fire
                state.next_fire = (
                    None
                    if job.run_at
                    else self._next(job, at + timedelta(microseconds=1))
                )
                late = (at - due).total_seconds() > 60
                if state.active:
                    await self._missed(
                        job,
                        state,
                        due,
                        "Skipped firing while prior dispatch is queued/running",
                    )
                    await self._save(state)
                elif late and not job.catch_up and not job.run_at:
                    await self._missed(
                        job,
                        state,
                        due,
                        f"Missed firing range through {at.isoformat()}; skipped",
                    )
                    await self._save(state)
                else:
                    if late and not job.run_at:
                        await self._missed(
                            job,
                            state,
                            due,
                            f"Missed firing range through {at.isoformat()}; "
                            "catch up once",
                        )
                    await self._launch(job, state, due)

    async def _launch(
        self, job: Job, state: JobState, at: datetime
    ) -> asyncio.Task[Run]:
        if state.active:
            raise ValueError("Job already queued/running")
        if job.run_at:
            if state.consumed:
                raise RuntimeError(
                    "One-time job already attempted; change run_at to schedule again"
                )
            state.consumed, state.next_fire = True, None
        state.active, state.last_status = True, "queued"
        await self._save(state)  # Persist cursor/intent before creating accepted work.
        admitted: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        # Consume unobserved failures; trigger_run may also await this receipt.
        admitted.add_done_callback(
            lambda receipt: receipt.exception() if not receipt.cancelled() else None
        )
        self.admissions[job.id] = admitted
        task = asyncio.create_task(self._execute(job, state, at, admitted))
        self.tasks[job.id] = task
        task.add_done_callback(lambda completed: self._ended(job.id, completed))
        return task

    def _ended(self, job_id: str, task: asyncio.Task[Run]) -> None:
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            assert exc is not None
            logging.getLogger(__name__).error(
                "Job %s dispatch failed",
                job_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def _execute(
        self, job: Job, state: JobState, at: datetime, admitted: asyncio.Future[dict]
    ) -> Run:
        run: Run | None = None
        try:
            session = await self._session(job)
            if self.closing:
                run = Run(
                    session=session.id,
                    profile=job.profile,
                    tool_view=self.tool_view(job).metadata(),
                    effective_tools=self.execution.registry.effective_tools(
                        job.profile, job.tools
                    ),
                    origin=Origin(channel="job", job=job.id),
                    input_message=render_task(job, at),
                    status=RunStatus.cancelled,
                    error="Shutdown before acceptance",
                    finished_at=self.clock(),
                )
                await durable(self.execution.store.save_run, run)
                admitted.set_result(run.model_dump(mode="json"))
                return run
            pending = await self.execution.enqueue(
                session.id,
                render_task(job, at),
                profile=job.profile,
                tools=job.tools,
                tool_disclosure=job.tool_disclosure,
                tool_resident=job.tool_resident,
                origin=Origin(channel="job", job=job.id),
                playbook=job.playbook,
                output_schema=job.output_schema,
                output_render=job.render,
            )
            admitted.set_result(pending.run.model_dump(mode="json"))
            run = await asyncio.shield(pending.future)
            if run.status == RunStatus.model_unavailable:
                run.status = RunStatus.missed
                await durable(self.execution.store.save_run, run)
            delivery = job.deliver
            if (
                delivery is None
                and not job.polling
                and any(c.id == job.bind_session for c in self.config.channels)
            ):
                delivery = Delivery(to=job.bind_session)
            if delivery is not None:
                await self.channels.deliver(run, delivery)
            if run.status == RunStatus.completed and job.then:
                await self._chain(job)
            return run
        finally:
            if not admitted.done():
                admitted.set_exception(RuntimeError("Job failed before run admission"))
            async with self.lock:
                state.active = False
                if run is not None:
                    state.last_run, state.last_status = run.id, run.status.value
                else:
                    state.last_status = "failed"
                await self._save(state)

    async def _chain(self, job: Job) -> None:
        """Queue the follow-up job once; a missing/disabled/busy target is logged."""
        try:
            assert job.then is not None
            await self.trigger(job.then)
        except (KeyError, ValueError, RuntimeError) as exc:
            logging.getLogger(__name__).warning(
                "Job %s: follow-up %s not started: %s", job.id, job.then, exc
            )

    async def _accept(
        self, job_id: str, authorize: Callable[[Job], None] | None = None
    ) -> tuple[asyncio.Task[Run], dict, asyncio.Future[dict]]:
        async with self.lock:
            job = self.job(job_id)
            if authorize:
                authorize(job)
            if self.closing or not job.enabled:
                raise ValueError("Job is disabled or scheduler is shutting down")
            await self._check_binding(job)
            task = await self._launch(job, self.states[job_id], self.clock())
            accepted = self.get(job_id)
        return task, accepted, self.admissions[job_id]

    async def trigger(
        self, job_id: str, *, authorize: Callable[[Job], None] | None = None
    ) -> dict:
        _, accepted, _ = await self._complete(self._accept(job_id, authorize))
        return accepted

    async def trigger_run(self, job_id: str) -> dict:
        """Return the durable run receipt without waiting for generation/delivery."""

        async def accept():
            _, _, receipt = await self._accept(job_id)
            return await asyncio.shield(receipt)

        return await self._complete(accept())

    async def run_now(self, job_id: str) -> Run:
        task, _, _ = await self._complete(self._accept(job_id))
        return await asyncio.shield(task)

    async def pause(
        self,
        job_id: str,
        paused: bool,
        *,
        authorize: Callable[[Job], None] | None = None,
    ) -> None:
        async with self.lock:
            if self.closing:
                raise ValueError("Scheduler is shutting down")
            job = self.job(job_id)
            if authorize:
                authorize(job)
            state = self.states[job_id]
            state.paused = paused
            if not paused:
                job = self.job(job_id)
                state.next_fire = (
                    self._next(job, self.clock() + timedelta(microseconds=1))
                    if job.enabled and not state.consumed
                    else None
                )
            await self._save(state)

    def job(self, job_id: str) -> Job:
        if job_id not in self.jobs:
            raise KeyError("Job not found")
        return self.jobs[job_id]

    def tool_view(self, job: Job) -> ToolView:
        return self.execution.registry.view(
            job.profile, job.tools, job.tool_disclosure, job.tool_resident, strict=False
        )

    def get(self, job_id: str) -> dict:
        job = self.job(job_id)
        effective = self.execution.registry.effective_tools(job.profile, job.tools)
        return {
            **job.model_dump(mode="json"),
            **self.states[job_id].model_dump(mode="json"),
            "tool_view": self.tool_view(job).diagnostics(
                self.config.profiles[job.profile].context_budget
            ),
            "unavailable_resident": sorted(
                set(job.tool_resident or []) - set(effective)
            ),
            "effective_tools": effective,
            "unavailable_tools": sorted(set(job.tools or []) - set(effective)),
        }

    def status(self, *, definitions: bool = False) -> list[dict]:
        if definitions:
            return [self.get(identity) for identity in self.jobs]
        return [
            {
                **self.states[job.id].model_dump(mode="json"),
                "enabled": job.enabled,
                "cron": job.cron,
                "run_at": job.run_at,
                "timezone": job.timezone,
            }
            for job in self.jobs.values()
        ]

    def validate_job(self, job: Job, *, check_tools: bool = True):  # noqa -> None: C901
        safe_id(job.id)
        if not any(c.id == job.bind_session for c in self.config.channels):
            safe_id(job.bind_session)
        if job.profile not in self.config.profiles:
            raise ValueError("Job references unknown profile")
        if check_tools and job.tools is not None:
            try:
                expanded = self.execution.registry.expand_grants(job.tools, job.profile)
                granted = set(self.execution.registry.effective_tools(job.profile))
                unavailable = {
                    entry
                    for entry in job.tools
                    if "*" not in entry and not entry.startswith("@")
                } - granted
                if unavailable:
                    raise ValueError(", ".join(sorted(unavailable)))
                job.tools = [name for name in expanded if name in granted]
            except ValueError as exc:
                raise JobToolsError(
                    f"Job tools not granted by profile {job.profile}: {exc}"
                ) from exc
        if check_tools:
            try:
                view = self.execution.registry.view(
                    job.profile, job.tools, job.tool_disclosure, job.tool_resident
                )
                if job.tool_resident is not None:
                    job.tool_resident = list(view.resident)
            except ValueError as exc:
                raise JobToolsError(f"Job tools resident selection: {exc}") from exc
        if job.profile == "maintenance":
            raise ValueError("Maintenance cannot be scheduled")
        if job.then is not None:
            seen = {job.id}
            current: str | None = job.then
            while current is not None:
                if current in seen:
                    raise ValueError("Job then-chain forms a cycle")
                seen.add(current)
                following = self.jobs.get(current)
                current = following.then if following else None
        if job.playbook is not None:
            from solakit.core import playbooks

            try:
                playbooks.load(self.config, job.playbook)
            except playbooks.PlaybookError as exc:
                raise ValueError(f"Job playbook: {exc}") from exc
        if (
            job.deliver
            and job.deliver.mode != "silent"
            and not any(
                channel.id == job.deliver.to for channel in self.config.channels
            )
        ):
            raise ValueError("Job delivery references unknown channel")
        # Also bound placeholder expansion, not only the source template.
        render_task(job, self.clock())

    async def _check_binding(self, job: Job) -> None:
        if job.bind_session == "ephemeral":
            return
        identity = next(
            (c.session for c in self.config.channels if c.id == job.bind_session),
            job.bind_session,
        )
        try:
            session = await durable(self.execution.store.session, identity)
        except FileNotFoundError:
            return
        if session.archived_at:
            raise ValueError(
                "Restore the archived session before scheduling work there"
            )

    async def _complete(self, operation: Coroutine[Any, Any, T]) -> T:
        task = asyncio.create_task(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def create(
        self, job: Job, *, authorize: Callable[[Job], None] | None = None
    ) -> dict:
        return await self._complete(self._change(job, create=True, authorize=authorize))

    async def update(
        self,
        job_id: str,
        changes: dict,
        *,
        authorize: Callable[[Job], None] | None = None,
    ) -> dict:
        return await self._complete(self._update(job_id, changes, authorize))

    async def _update(
        self, job_id: str, changes: dict, authorize: Callable[[Job], None] | None
    ) -> dict:
        async with self.lock:
            old = self.job(job_id)
            if authorize:
                authorize(old)
            patch = JobUpdate.model_validate(changes).model_dump(exclude_unset=True)
            job = Job.model_validate({**old.model_dump(), **patch})
            return await self._change_locked(job, create=False, authorize=authorize)

    async def _change(
        self, job: Job, *, create: bool, authorize: Callable[[Job], None] | None
    ) -> dict:
        async with self.lock:
            return await self._change_locked(job, create=create, authorize=authorize)

    async def _change_locked(
        self, job: Job, *, create: bool, authorize: Callable[[Job], None] | None
    ) -> dict:
        if self.closing:
            raise ValueError("Scheduler is shutting down")
        self.validate_job(job)
        if authorize:
            authorize(job)
        if create and job.id in self.jobs:
            raise FileExistsError("Job already exists")
        if not create and self.states[job.id].active:
            raise RuntimeError("Job is queued/running; wait before editing")
        trigger = trigger_for(job)
        schedule = schedule_key(job)
        old = self.jobs.get(job.id)
        state = self.states.get(job.id)
        changed = state is None or state.schedule != schedule
        if job.enabled and (state is None or changed or not state.consumed):
            await self._check_binding(job)
        if (
            changed
            and job.run_at
            and datetime.fromisoformat(job.run_at) <= self.clock()
        ):
            raise ValueError("One-time schedule must be in the future")
        if state is None:
            state = JobState(id=job.id, schedule=schedule)
        else:
            state = state.model_copy(deep=True)
        if changed:
            state.consumed = False
        if changed or old is None or old.enabled != job.enabled:
            state.schedule = schedule
            state.next_fire = (
                trigger.get_next_fire_time(
                    None, self.clock() + timedelta(microseconds=1)
                )
                if job.enabled and not state.consumed
                else None
            )
        await durable(self.definitions.save, job)
        self.jobs[job.id], self.states[job.id] = job, state
        self.triggers[job.id] = trigger
        await self._save(state)
        return self.get(job.id)

    async def delete(
        self, job_id: str, *, authorize: Callable[[Job], None] | None = None
    ) -> dict:
        return await self._complete(self._delete(job_id, authorize))

    async def _delete(
        self, job_id: str, authorize: Callable[[Job], None] | None
    ) -> dict:
        async with self.lock:
            if self.closing:
                raise ValueError("Scheduler is shutting down")
            job = self.job(job_id)
            if authorize:
                authorize(job)
            if self.states[job_id].active:
                raise RuntimeError("Job is queued/running; wait before deleting")
            await durable(self.definitions.save, job, deleted=True)
            del self.jobs[job_id], self.states[job_id], self.triggers[job_id]
            self.tasks.pop(job_id, None)
            return {"id": job_id, "deleted": True}
