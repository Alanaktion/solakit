"""SQLite-owned schedules, bounded autonomous admission and local delivery."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from pydantic import JsonValue

from solakit.config import Budgets, EffectiveProfile, restrict_to_admission
from solakit.evidence import canonical, digest
from solakit.lineage import deny_autonomous_successor
from solakit.records import (
    Delivery,
    DeliveryState,
    ErrorCode,
    Failure,
    Job,
    Message,
    Occurrence,
    Origin,
    Run,
    RunState,
    SchedulerControl,
    SessionMode,
    new_id,
    utc_now,
)
from solakit.store import Store, valid_id

if TYPE_CHECKING:
    from solakit.run_loop import RunLoop

CONTROL_ID = "0" * 32
AUTONOMOUS = (Origin.SCHEDULED, Origin.PROACTIVE)
WAKE_INSTRUCTIONS = (
    "\n\nThis is an operator-enabled background occurrence. Inspect only the bound topic, "
    "scoped recall and granted resources. Perform useful permitted work, leave one concise "
    "message, or return exactly NO_ACTION when nothing warrants action or notification. "
    "Do not create descendants or create, rearm, enable or reschedule wake-ups."
)


def one_shot(job: Job) -> datetime | None:
    if not job.schedule.startswith("once:"):
        return None
    instant = datetime.fromisoformat(job.schedule[5:])
    if instant.tzinfo is None:
        raise ValueError("one-shot schedules require an explicit UTC offset")
    return instant.astimezone(UTC)


def next_due(job: Job, after: datetime) -> datetime | None:
    """Cron wall labels use APScheduler; resolve DST explicitly with the first offset."""
    try:
        zone = ZoneInfo(job.timezone)
    except ValueError, ZoneInfoNotFoundError:
        raise ValueError("job timezone must be an IANA timezone") from None
    if (instant := one_shot(job)) is not None:
        return None if job.consumed else instant
    if not job.schedule.startswith("cron:"):
        raise ValueError("schedule must be once:<ISO timestamp> or cron:<five fields>")
    trigger = CronTrigger.from_crontab(job.schedule[5:], timezone=UTC)
    wall = after.astimezone(zone).replace(tzinfo=UTC, second=0, microsecond=0)
    # Even a full skipped civil day contains at most 1,440 minute slots.
    for _ in range(3000):
        candidate = trigger.get_next_fire_time(None, wall)
        if candidate is None:
            return None
        local = candidate.replace(tzinfo=None).replace(tzinfo=zone, fold=0)
        instant = local.astimezone(UTC)
        if instant > after and instant.astimezone(zone).replace(tzinfo=None) == (
            candidate.replace(tzinfo=None)
        ):
            return cast(datetime, instant)
        wall = candidate + timedelta(minutes=1)
    raise ValueError("cron cannot resolve a valid local slot")


def quiet_until(profile: EffectiveProfile, at: datetime) -> datetime:
    policy = profile.background
    local = at.astimezone(ZoneInfo(policy.timezone))
    start, end = policy.quiet_start, policy.quiet_end
    quiet = (
        (start <= local.hour < end) if start < end else (local.hour >= start or local.hour < end)
    )
    if start == end or not quiet:
        return at
    end_date = local.date() + timedelta(days=int(local.hour >= start and start > end))
    # Resolve a quiet-end in a DST gap forwards, and in a fold using the later offset.
    finish = datetime.combine(end_date, datetime.min.time(), local.tzinfo).replace(hour=end, fold=1)
    finish = finish.astimezone(UTC)
    while finish <= at or finish.astimezone(local.tzinfo).hour < end:
        finish += timedelta(minutes=1)
    return finish


class JobService:
    def __init__(
        self,
        store: Store,
        runs: RunLoop,
        profile: Callable[[], EffectiveProfile],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store, self.runs, self.profile, self.clock = store, runs, profile, clock
        self.scheduler: AsyncIOScheduler | None = None
        self.lock = asyncio.Lock()
        self.failure: str | None = None

    def policy(self) -> EffectiveProfile:
        return self.runs.dispatcher.current(self.profile())

    def control(self) -> SchedulerControl:
        try:
            return self.store._load("scheduler", CONTROL_ID, SchedulerControl)
        except KeyError:
            return SchedulerControl(id=CONTROL_ID, origin=Origin.SYSTEM)

    def list(self) -> list[Job]:
        return self.store.jobs()

    def invoke(
        self, action: str, arguments: dict[str, JsonValue], run: Run, authority: EffectiveProfile
    ) -> JsonValue:
        if action == "list":
            return cast(
                JsonValue,
                [
                    job.model_dump(mode="json")
                    for job in self.list()
                    if job.session_id == run.session_id
                ],
            )
        deny_autonomous_successor()
        if run.origin != Origin.INTERACTIVE:
            raise ValueError("job management requires interactive origin")
        if action == "pause_all":
            return self.global_pause(cast(bool, arguments["paused"])).model_dump(mode="json")
        if action == "create":
            job = Job.model_validate_json(
                canonical(
                    arguments
                    | {
                        "session_id": run.session_id,
                        "mode": authority.mode,
                    }
                )
            )
        else:
            job = self.store.job(cast(str, arguments["job_id"]))
            if job.session_id != run.session_id:
                raise ValueError("job belongs to a different topic")
            if action == "update":
                job = Job.model_validate_json(
                    canonical(
                        job.model_dump(mode="json")
                        | {key: value for key, value in arguments.items() if key != "job_id"}
                    )
                )
        if action in ("create", "update", "resume", "run_now"):
            selected = self._profile(job)
            if (
                not selected.tools <= authority.tools
                or any(
                    not any(root.is_relative_to(parent) for parent in authority.workspace_roots)
                    for root in selected.workspace_roots
                )
                or any(
                    getattr(selected.budgets, key) > getattr(authority.budgets, key)
                    for key in Budgets.model_fields
                )
            ):
                raise ValueError("job exceeds the invoking run's authority")
        if action == "create":
            result: Job | Occurrence = self.create(job)
        elif action == "update":
            result = self.update(
                job.id,
                **{
                    key: value
                    for key, value in job.model_dump().items()
                    if key in arguments and key != "job_id"
                },
            )
        elif action == "pause":
            result = self.pause(job.id)
        elif action == "resume":
            result = self.resume(job.id)
        elif action == "run_now":
            result = self.manual(job.id, request_id=cast(str, arguments["request_id"]))
        elif action == "delete":
            result = self.delete(job.id)
        else:
            raise ValueError("unsupported job operation")
        return result.model_dump(mode="json")

    def _profile(self, job: Job) -> EffectiveProfile:
        template = self.profile().model_copy(
            update={"name": job.profile, "origin": job.origin, "mode": job.mode}
        )
        selected = self.runs.dispatcher.current(template)
        if (selected.name, selected.mode, selected.origin) != (job.profile, job.mode, job.origin):
            raise ValueError("job profile resolver returned a different selection")
        if selected.data_dir != self.store.root or (
            selected.model_pin,
            selected.model_base_url,
        ) != (self.runs.model.profile.model_pin, self.runs.model.profile.model_base_url):
            raise ValueError("job profile belongs to a different runtime/model")
        tools = selected.tools if job.tools is None else frozenset(job.tools)
        if not tools <= selected.tools:
            raise ValueError("job tools exceed current profile grants")
        roots = selected.workspace_roots
        if job.workspace_roots is not None:
            roots = tuple(Path(root).expanduser().resolve() for root in job.workspace_roots)
            if any(
                not root.is_dir()
                or not any(root.is_relative_to(r) for r in selected.workspace_roots)
                for root in roots
            ):
                raise ValueError("job roots exceed current profile roots")
        else:
            tools -= {"files.write", "images.generate"}
        changes = {"model_calls": 8, "tool_calls": 12, "active_seconds": 600}
        changes.update(selected.proactive_budgets.model_dump(exclude_none=True))
        changes = {
            key: min(value, getattr(selected.budgets, key)) for key, value in changes.items()
        }
        budgets = Budgets.model_validate(selected.budgets.model_dump() | changes)
        narrowed = Budgets.model_validate(budgets.model_dump() | job.budgets)
        if any(getattr(narrowed, key) > getattr(budgets, key) for key in job.budgets):
            raise ValueError("job budgets exceed current profile budgets")
        budgets = narrowed
        return selected.model_copy(
            update={
                "tools": tools,
                "granted_tools": tools,
                "workspace_roots": roots,
                "budgets": budgets,
                "memory_mode": selected.memory_mode if job.mode == SessionMode.SHARED else "off",
            }
        )

    def _validate(self, job: Job) -> None:
        if job.origin not in AUTONOMOUS:
            raise ValueError("jobs require scheduled or proactive origin")
        if not job.task.strip() or len(job.task.encode()) > 32768:
            raise ValueError("job task must contain 1–32768 UTF-8 bytes")
        next_due(job, self.clock())
        self._profile(job)
        if job.session_id is not None:
            topic = self.store.session(job.session_id)
            if topic.archived or topic.mode != job.mode:
                raise ValueError("job target is archived or has a different mode")

    def create(self, job: Job) -> Job:
        deny_autonomous_successor()
        if self.store.db.execute(
            "SELECT 1 FROM entities WHERE kind='job' AND id=?", (job.id,)
        ).fetchone():
            raise ValueError("job ID already exists, including tombstones")
        if job.version != 1 or job.generation != 1 or job.deleted or job.consumed or job.next_due:
            raise ValueError("new job cannot supply operational state")
        self._validate(job)
        job = job.model_copy(update={"next_due": next_due(job, self.clock())})
        self.store._commit((job,))
        return job

    def update(self, identity: str, **changes: object) -> Job:
        deny_autonomous_successor()
        old = self.store.job(identity)
        if old.deleted:
            raise ValueError("deleted jobs cannot be changed")
        if changes.keys() - {
            "task",
            "schedule",
            "timezone",
            "profile",
            "tools",
            "workspace_roots",
            "budgets",
            "session_id",
            "mode",
            "enabled",
            "delivery",
        }:
            raise ValueError("unsupported job edit")
        job = Job.model_validate(old.model_dump() | changes)
        if job == old:
            return old
        temporal = (job.schedule, job.timezone) != (old.schedule, old.timezone)
        if temporal:
            rearm = job.schedule != old.schedule
            job = job.model_copy(
                update={
                    "generation": old.generation + 1,
                    "consumed": False if rearm else old.consumed,
                }
            )
            if rearm and (instant := one_shot(job)) is not None and instant <= self.clock():
                raise ValueError("rearming requires a new future one-shot schedule")
        self._validate(job)
        if temporal or (job.enabled and not old.enabled):
            job = job.model_copy(update={"next_due": next_due(job, self.clock())})
        job = job.model_copy(update={"version": old.version + 1})
        self.store._commit((job,))
        self.runs._notify()
        return job

    def pause(self, identity: str) -> Job:
        return self.update(identity, enabled=False)

    def resume(self, identity: str) -> Job:
        return self.update(identity, enabled=True)

    def delete(self, identity: str) -> Job:
        deny_autonomous_successor()
        old = self.store.job(identity)
        if not old.deleted:
            old = old.model_copy(
                update={
                    "deleted": True,
                    "enabled": False,
                    "version": old.version + 1,
                }
            )
            self.store._commit((old,))
        self.runs._notify()
        return old

    def global_pause(self, paused: bool) -> SchedulerControl:
        deny_autonomous_successor()
        if type(paused) is not bool:
            raise ValueError("pause requires a boolean")
        control = self.control()
        if control.paused == paused:
            return control
        control = control.model_copy(update={"paused": paused})
        updates: list[Job] = []
        if not paused:
            updates = [
                job.model_copy(update={"next_due": next_due(job, self.clock())})
                for job in self.list()
                if job.schedule.startswith("cron:")
            ]
        self.store._commit((control, *updates))
        self.runs._notify()
        return control

    def manual(self, identity: str, *, request_id: str) -> Occurrence:
        deny_autonomous_successor()
        valid_id(request_id)
        key = digest(canonical(["manual", identity, request_id]))[:32]
        try:
            return self.store.occurrence(key)
        except KeyError:
            pass
        job = self.store.job(identity)
        if job.deleted:
            raise ValueError("deleted jobs cannot run")
        return self._claim(job, self.clock(), manual_id=key)

    def _room(self, job: Job, *, manual: bool = False) -> bool:
        policy = self.policy().background
        pending = self.store.queued_runs()
        if len(pending) >= self.runs.max_pending:
            return False
        if sum(r.origin in AUTONOMOUS for r in pending) >= policy.max_pending:
            return False
        if self.store.db.execute(
            "SELECT 1 FROM entities WHERE kind='run' AND json_extract(body,'$.job_id')=? "
            "AND json_extract(body,'$.state') IN ('queued','running')",
            (job.id,),
        ).fetchone():
            return False
        if manual:
            return True
        now = self.clock()
        recent = [
            Occurrence.model_validate_json(row[0])
            for row in self.store.db.execute(
                "SELECT body FROM entities WHERE kind='occurrence' AND created_at>? "
                "AND json_extract(body,'$.run_id') IS NOT NULL "
                "AND json_extract(body,'$.trigger')='scheduled'",
                ((now - timedelta(days=1)).isoformat(),),
            ).fetchall()
        ]
        hourly = [o for o in recent if o.created_at > now - timedelta(hours=1)]
        return (
            len(recent) < policy.daily_runs
            and len(hourly) < policy.hourly_runs
            and not any(
                o.definition.get("session_id") == job.session_id
                and o.created_at > now - timedelta(seconds=policy.topic_cooldown_seconds)
                for o in recent
                if job.session_id is not None
            )
        )

    def _claim(
        self,
        job: Job,
        due: datetime,
        *,
        manual_id: str | None = None,
        next_instant: datetime | None = None,
        coalesced_from: datetime | None = None,
    ) -> Occurrence:
        if not self.runs.accepting or self.control().paused:
            raise ValueError("background service is paused or unavailable")
        if not self._room(job, manual=manual_id is not None):
            raise ValueError("job overlap, queue or wake rate limit")
        profile = self._profile(job)
        occurrence = Occurrence(
            id=manual_id or digest(canonical([job.id, job.generation, due.isoformat()]))[:32],
            created_at=self.clock(),
            origin=job.origin,
            job_id=job.id,
            due_at=due,
            generation=job.generation,
            trigger="manual" if manual_id else "scheduled",
            definition=job.model_dump(mode="json"),
            local_slot=due.astimezone(ZoneInfo(job.timezone)).isoformat(),
            coalesced_from=coalesced_from,
            explanation=job.task,
        )
        advanced = job.model_copy(
            update={
                "consumed": job.consumed or one_shot(job) is not None,
                "next_due": None
                if one_shot(job) is not None
                else (job.next_due if manual_id else next_instant),
            }
        )
        session_id = job.session_id
        if session_id is None:
            session_id = self.store.create_session(mode=job.mode, origin=job.origin).id
        run = self.store.admit(
            session_id,
            request_id=occurrence.id,
            content=job.task + WAKE_INSTRUCTIONS,
            profile=profile.name,
            admission=profile.model_dump(mode="json"),
            origin=job.origin,
            job_id=job.id,
            occurrence_id=occurrence.id,
            claim=(job, occurrence, advanced),
        )
        self.runs._notify()
        return self.store.occurrence(cast(str, run.occurrence_id))

    def can_start(self, run: Run) -> bool:
        if run.occurrence_id is None:
            return True
        job = self.store.job(cast(str, run.job_id))
        occurrence = self.store.occurrence(run.occurrence_id)
        if job.deleted:
            return True
        if self.control().paused:
            return False
        if occurrence.trigger != "manual" and not job.enabled:
            return False
        # Outbound-capable profiles wait until daylight; local-only preparation can run.
        admitted = EffectiveProfile.model_validate_json(
            canonical(self.store.admission_profile(run.id))
        )
        try:
            current = restrict_to_admission(self.runs.dispatcher.current(admitted), admitted)
        except ValueError:
            return True  # Execution records the invalid admission as a terminal failure.
        if (
            occurrence.trigger != "manual"
            and quiet_until(self.policy(), self.clock()) > self.clock()
        ):
            return not any(
                name in current.tools
                and tool.consequential
                and name not in ("files.write", "memory.remember")
                and not name.startswith("jobs.")
                for name, tool in self.runs.dispatcher.registry.tools.items()
            )
        return True

    def validate_execution(self, run: Run) -> None:
        if run.job_id is not None and self.store.job(run.job_id).deleted:
            raise ValueError("job was deleted before execution")

    def effect_allowed(self, run: Run, tool: str) -> bool:
        if run.occurrence_id is None or tool in ("files.write", "memory.remember"):
            return True
        if self.control().paused:
            return False
        return (
            self.store.occurrence(run.occurrence_id).trigger == "manual"
            or quiet_until(self.policy(), self.clock()) <= self.clock()
        )

    async def wait_for_effect(self, run: Run, tool: str) -> None:
        while not self.effect_allowed(run, tool):
            changed = self.runs.changed
            delay = (quiet_until(self.policy(), self.clock()) - self.clock()).total_seconds()
            try:
                await asyncio.wait_for(changed.wait(), timeout=max(0.1, min(60, delay)))
            except TimeoutError:
                pass

    def finish(self, run: Run, content: str, failure: Failure | None) -> None:
        assert run.occurrence_id is not None
        occurrence = self.store.occurrence(run.occurrence_id)
        definition = Job.model_validate_json(canonical(occurrence.definition))
        worked = bool(
            self.store.db.execute(
                "SELECT 1 FROM entities WHERE kind='receipt' AND run_id=? "
                "AND json_extract(body,'$.effect')='confirmed'",
                (run.id,),
            ).fetchone()
        )
        no_action = content.strip() == "NO_ACTION"
        decision = (
            "failed" if failure else "work" if worked else "no_action" if no_action else "message"
        )
        occurrence = Occurrence.model_validate(
            occurrence.model_dump()
            | {
                "state": "failed" if failure else "completed",
                "decision": decision,
                "explanation": content,
                "reason": failure,
            }
        )
        terminal = run.transition(
            RunState.FAILED if failure else RunState.COMPLETED,
            at=utc_now(),
            reason=failure,
        )
        archive = self.store.job_topic_archive(terminal)
        delivery: tuple[Delivery, ...] = ()
        if not failure and not no_action and content.strip() and definition.delivery == "session":
            now = self.clock()
            delivery = (
                Delivery(
                    origin=run.origin,
                    session_id=run.session_id,
                    run_id=run.id,
                    message_id=new_id(),
                    content=content,
                    created_at=now,
                    not_before=now,
                    expires_at=now
                    + timedelta(seconds=self.policy().background.notification_ttl_seconds),
                    publish_to_archive=bool(archive),
                ),
            )
        self.store._commit((terminal, occurrence, *delivery, *archive))
        self.deliver()

    def deliver(self) -> None:
        now, profile = self.clock(), self.policy()
        pending = [
            self.store._load("delivery", row[0], Delivery)
            for row in self.store.db.execute(
                "SELECT id FROM entities WHERE kind='delivery' "
                "AND json_extract(body,'$.state')='pending' "
                "AND json_extract(body,'$.content') IS NOT NULL ORDER BY created_at DESC,id",
            ).fetchall()
        ]
        topics: set[str] = set()
        for delivery in pending:
            run = self.store.run(delivery.run_id)
            manual = run.occurrence_id is not None and (
                self.store.occurrence(run.occurrence_id).trigger == "manual"
            )
            cancel = (
                (delivery.expires_at is not None and delivery.expires_at <= now)
                or (
                    not delivery.publish_to_archive
                    and self.store.session(delivery.session_id).archived
                )
                or (run.job_id is not None and self.store.job(run.job_id).deleted)
                or (not manual and delivery.session_id in topics)
            )
            topics.add(delivery.session_id)
            if cancel:
                self.store.transition_delivery(delivery.id, DeliveryState.CANCELLED)
                continue
            if self.control().paused or (delivery.not_before and delivery.not_before > now):
                continue
            if not manual and quiet_until(profile, now) > now:
                continue
            latest = self.store.db.execute(
                "SELECT max(json_extract(body,'$.not_before')) FROM entities WHERE kind='delivery' "
                "AND session_id=? AND json_extract(body,'$.state')='delivered' "
                "AND json_extract(body,'$.content') IS NOT NULL",
                (delivery.session_id,),
            ).fetchone()[0]
            if (
                not manual
                and latest is not None
                and datetime.fromisoformat(latest)
                > (now - timedelta(seconds=profile.background.topic_cooldown_seconds))
            ):
                continue
            duplicate = self.store.db.execute(
                "SELECT 1 FROM entities WHERE kind='delivery' AND session_id=? "
                "AND json_extract(body,'$.content')=? AND json_extract(body,'$.state')='delivered' "
                "AND json_extract(body,'$.not_before')>?",
                (
                    delivery.session_id,
                    delivery.content,
                    (now - timedelta(days=1)).isoformat(),
                ),
            ).fetchone()
            if duplicate and not manual:
                self.store.transition_delivery(delivery.id, DeliveryState.CANCELLED)
                continue
            sending = delivery.transition(DeliveryState.SENDING)
            completed = sending.transition(DeliveryState.DELIVERED).model_copy(
                update={"not_before": now}
            )
            message = Message(
                id=delivery.message_id,
                origin=delivery.origin,
                session_id=delivery.session_id,
                run_id=delivery.run_id,
                role="assistant",
                content=cast(str, delivery.content),
            )
            # Local acceptance/publication/completion share one transaction and stable ID.
            self.store._commit((sending, message, completed))

    def _skip(
        self, job: Job, due: datetime, next_instant: datetime | None, *, failure: bool = False
    ) -> None:
        occurrence = Occurrence(
            id=digest(canonical([job.id, job.generation, due.isoformat()]))[:32],
            created_at=self.clock(),
            origin=job.origin,
            job_id=job.id,
            due_at=due,
            generation=job.generation,
            state="failed" if failure else "skipped",
            decision="failed" if failure else "skipped",
            definition=job.model_dump(mode="json"),
            coalesced_from=due,
            local_slot=due.astimezone(ZoneInfo(job.timezone)).isoformat(),
            reason=Failure(
                code=ErrorCode.CONFIGURATION, message="Job profile or target is no longer valid"
            )
            if failure
            else None,
            explanation="Invalid authority/target"
            if failure
            else "Cron grace, overlap or rate limit",
        )
        self.store._commit(
            (
                occurrence,
                job.model_copy(
                    update={
                        "next_due": next_instant,
                        "consumed": job.consumed or one_shot(job) is not None,
                    }
                ),
            )
        )

    async def tick(self) -> None:
        async with self.lock:
            self.deliver()
            if self.control().paused:
                return
            now = self.clock()
            for job in self.list():
                if not job.enabled or job.consumed or job.next_due is None:
                    continue
                if job.next_due > now:
                    continue
                if one_shot(job) is not None:
                    if self._room(job):
                        try:
                            self._claim(job, job.next_due)
                        except ValueError, KeyError:
                            self._skip(job, job.next_due, None, failure=True)
                    continue
                due = job.next_due
                candidate = next_due(
                    job, max(due - timedelta(microseconds=1), now - timedelta(hours=1))
                )
                chosen = None
                while candidate is not None and candidate <= now:
                    chosen = candidate
                    candidate = next_due(job, candidate)
                if chosen is not None and self._room(job):
                    try:
                        self._claim(job, chosen, next_instant=candidate, coalesced_from=due)
                    except ValueError, KeyError:
                        self._skip(job, due, candidate, failure=True)
                else:
                    self._skip(job, due, candidate)
            self.runs._notify()

    async def _wake(self) -> None:
        try:
            await self.tick()
        except Exception as error:
            self.failure = type(error).__name__
            if self.store.failed:
                self.runs._fault(error)

    async def start(self) -> None:
        if self.scheduler is not None:
            return
        await self.tick()
        self.scheduler = AsyncIOScheduler(timezone=UTC)
        self.scheduler.add_job(self._wake, "interval", seconds=1, coalesce=True, max_instances=1)
        self.scheduler.start()

    async def stop(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
            await asyncio.sleep(0)
            async with self.lock:
                pass
