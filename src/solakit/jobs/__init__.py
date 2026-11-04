"""Persistent job cursors and operations; APScheduler supplies the wake-up/cron clock.

Definitions remain TOML. JSONL state is authoritative and SQLite is its index;
no pickled runtime objects or global callback registry need to survive restart.
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta
from string import Formatter
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel

from solakit.core.config import Delivery, Job
from solakit.core.io import durable
from solakit.core.schemas import Origin, Run, RunStatus, Session, now


def render_task(job: Job, at: datetime) -> str:
    local = at.astimezone(ZoneInfo(job.timezone))
    values = {
        "date": local.date().isoformat(),
        "time": local.time().isoformat(),
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


class Jobs:
    def __init__(self, config, execution, channels, clock=now):
        self.config, self.execution, self.channels, self.clock = (
            config,
            execution,
            channels,
            clock,
        )
        self.states: dict[str, JobState] = {}
        self.triggers = {
            j.id: CronTrigger.from_crontab(j.cron, timezone=j.timezone)
            for j in config.jobs
        }
        self.tasks: dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()
        self.scheduler = None
        self.closing = False

    async def initialize(self):
        for job in self.config.jobs:
            schedule = hashlib.sha256(f"{job.cron}:{job.timezone}".encode()).hexdigest()
            saved = await durable(self.execution.store.job_state, job.id)
            state = (
                JobState.model_validate(saved)
                if saved
                else JobState(id=job.id, schedule=schedule)
            )
            if not saved or state.schedule != schedule:
                state.schedule = schedule
                state.next_fire = self._next(job, self.clock())
            if not job.enabled:
                state.next_fire = None
            elif state.next_fire is None:
                state.next_fire = self._next(job, self.clock())
            self.states[job.id] = state
            if state.active:
                state.active = False
                await self._missed(
                    job,
                    state,
                    self.clock(),
                    "Interrupted dispatch during downtime; not replayed",
                )
            await self._save(state)

    def _next(self, job, at):
        return self.triggers[job.id].get_next_fire_time(None, at)

    async def _save(self, state):
        await durable(
            self.execution.store.save_job, state.id, state.model_dump(mode="json")
        )

    async def start(self):
        await self.initialize()
        if not any(j.enabled for j in self.config.jobs):
            return
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

    async def stop_admission(self):
        async with self.lock:
            self.closing = True
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown(wait=False)

    async def drain(self):
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def _session(self, job):
        channel = next(
            (c for c in self.config.channels if c.id == job.bind_session), None
        )
        if channel:
            return await durable(
                self.channels.router.channel, channel.type, channel.channel_id
            )
        if job.bind_session == "ephemeral":
            return await durable(
                self.execution.store.create_session,
                Session(default_profile=job.profile, memory_scope="ephemeral"),
            )
        return await durable(
            self.execution.store.create_session,
            Session(id=job.bind_session, default_profile=job.profile),
        )

    async def _missed(self, job, state, at, reason):
        session = await self._session(job)
        run = Run(
            session=session.id,
            profile=job.profile,
            origin=Origin(channel="job", job=job.id),
            input_message=render_task(job, at),
            status=RunStatus.missed,
            created_at=at,
            finished_at=self.clock(),
            error=reason,
        )
        await durable(self.execution.store.save_run, run)
        state.last_run, state.last_status = run.id, "missed"

    async def tick(self):
        async with self.lock:
            if self.closing:
                return
            at = self.clock()
            for job in self.config.jobs:
                state = self.states[job.id]
                if (
                    not job.enabled
                    or state.paused
                    or state.next_fire is None
                    or state.next_fire > at
                ):
                    continue
                due = state.next_fire
                state.next_fire = self._next(job, at + timedelta(microseconds=1))
                late = (at - due).total_seconds() > 60
                if state.active:
                    await self._missed(
                        job,
                        state,
                        due,
                        "Skipped firing while prior dispatch is queued/running",
                    )
                    await self._save(state)
                elif late and not job.catch_up:
                    await self._missed(
                        job,
                        state,
                        due,
                        f"Missed firing range through {at.isoformat()}; skipped",
                    )
                    await self._save(state)
                else:
                    if late:
                        await self._missed(
                            job,
                            state,
                            due,
                            f"Missed firing range through {at.isoformat()}; "
                            "catch up once",
                        )
                    await self._launch(job, state, at)

    async def _launch(self, job, state, at):
        if state.active:
            raise ValueError("Job already queued/running")
        state.active, state.last_status = True, "queued"
        await self._save(state)  # Persist cursor/intent before creating accepted work.
        task = asyncio.create_task(self._execute(job, state, at))
        self.tasks[job.id] = task
        task.add_done_callback(lambda completed: self._ended(job.id, completed))
        return task

    def _ended(self, job_id, task):
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            logging.getLogger(__name__).error(
                "Job %s dispatch failed",
                job_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def _execute(self, job, state, at):
        run = None
        try:
            session = await self._session(job)
            if self.closing:
                run = Run(
                    session=session.id,
                    profile=job.profile,
                    origin=Origin(channel="job", job=job.id),
                    input_message=render_task(job, at),
                    status=RunStatus.cancelled,
                    error="Shutdown before acceptance",
                    finished_at=self.clock(),
                )
                await durable(self.execution.store.save_run, run)
                return run
            run = await self.execution.submit(
                session.id,
                render_task(job, at),
                profile=job.profile,
                origin=Origin(channel="job", job=job.id),
            )
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
            return run
        finally:
            async with self.lock:
                state.active = False
                if run is not None:
                    state.last_run, state.last_status = run.id, run.status.value
                else:
                    state.last_status = "failed"
                await self._save(state)

    async def run_now(self, job_id):
        async with self.lock:
            job = next(j for j in self.config.jobs if j.id == job_id)
            if self.closing or not job.enabled:
                raise ValueError("Job is disabled or scheduler is shutting down")
            task = await self._launch(job, self.states[job_id], self.clock())
        return await asyncio.shield(task)

    async def pause(self, job_id, paused):
        async with self.lock:
            state = self.states[job_id]
            state.paused = paused
            if not paused:
                job = next(j for j in self.config.jobs if j.id == job_id)
                state.next_fire = self._next(
                    job, self.clock() + timedelta(microseconds=1)
                )
            await self._save(state)

    def status(self):
        return [
            {
                **self.states[j.id].model_dump(mode="json"),
                "enabled": j.enabled,
                "timezone": j.timezone,
                "cron": j.cron,
            }
            for j in self.config.jobs
        ]
