import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_run_loop import call, harness, reply, wire

from solakit.config import BackgroundSettings, EffectiveProfile
from solakit.engine.jobs import next_due, quiet_until
from solakit.engine.lineage import current_run
from solakit.engine.run_loop import records
from solakit.records import (
    Delivery,
    DeliveryState,
    Job,
    Message,
    Occurrence,
    Origin,
    RunState,
    SessionMode,
    ToolReceipt,
    new_id,
)
from solakit.runtime import ApplicationContext
from solakit.sdk import Tool, ToolArguments, ToolContext, ToolError, ToolResult
from solakit.storage.evidence import StorageError, canonical
from solakit.storage.search import rebuild
from solakit.tools.builtins import TOOLS


@dataclass
class Clock:
    now: datetime = datetime(2026, 11, 16, 16, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def job(app: ApplicationContext, clock: Clock, **changes: Any) -> Job:
    assert app.jobs and app.store
    topic = app.store.create_session()
    return app.jobs.create(
        Job.model_validate(
            {
                "origin": Origin.PROACTIVE,
                "profile": "default",
                "task": "Evaluate the commitment.",
                "schedule": "once:" + clock.now.isoformat(),
                "session_id": topic.id,
                "tools": (),
            }
            | changes
        )
    )


def occurrences(app: ApplicationContext) -> list[Occurrence]:
    assert app.store
    return [
        app.store.occurrence(r[0])
        for r in app.store.db.execute(
            "SELECT id FROM entities WHERE kind='occurrence' ORDER BY created_at,id"
        ).fetchall()
    ]


@pytest.mark.parametrize(
    "schedule,after,expected",
    [
        ("cron:30 2 * * *", "2026-03-08T07:00:00+00:00", "2026-03-09T08:30:00+00:00"),
        ("cron:30 1 * * *", "2026-11-01T06:00:00+00:00", "2026-11-01T07:30:00+00:00"),
        ("cron:30 1 * * *", "2026-11-01T07:30:00+00:00", "2026-11-02T08:30:00+00:00"),
        ("cron:* * * * *", "2026-11-01T07:59:00+00:00", "2026-11-01T09:00:00+00:00"),
        ("cron:* * * * *", "2026-11-01T08:15:00+00:00", "2026-11-01T09:00:00+00:00"),
    ],
)
def test_apscheduler_wall_slots_skip_gaps_and_second_fold(
    schedule: str,
    after: str,
    expected: str,
) -> None:
    definition = Job(
        origin=Origin.SCHEDULED,
        profile="default",
        task="fixture",
        schedule=schedule,
        session_id=new_id(),
    )
    assert next_due(definition, datetime.fromisoformat(after)) == datetime.fromisoformat(expected)


@pytest.mark.parametrize(
    "at,expected",
    [
        ("2026-11-17T04:59:59+00:00", "2026-11-17T04:59:59+00:00"),
        ("2026-11-17T05:00:00+00:00", "2026-11-17T15:00:00+00:00"),
        ("2026-11-17T14:59:59+00:00", "2026-11-17T15:00:00+00:00"),
        ("2026-11-17T15:00:00+00:00", "2026-11-17T15:00:00+00:00"),
        ("2026-03-08T06:00:00+00:00", "2026-03-08T14:00:00+00:00"),
    ],
)
def test_quiet_hour_boundaries(profile: EffectiveProfile, at: str, expected: str) -> None:
    assert quiet_until(profile, datetime.fromisoformat(at)) == datetime.fromisoformat(expected)


def test_one_shot_claim_edit_manual_tombstone_restart_and_search(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        clock = Clock()
        app, requests = harness(profile, [reply("One attributed message."), reply("NO_ACTION")])
        assert app.jobs
        app.jobs.clock = clock
        await app.start()
        assert app.store and app.runs
        saved: Job
        try:
            saved = job(app, clock)
            await app.jobs.tick()
            assert not occurrences(app) and not saved.enabled
            saved = app.jobs.resume(saved.id)
            await asyncio.gather(*(app.jobs.tick() for _ in range(4)))
            occurrence = occurrences(app)[0]
            assert occurrence.run_id
            assert (await app.runs.wait(occurrence.run_id, timeout=3)).state == RunState.COMPLETED
            assert app.store.job(saved.id).consumed and len(occurrences(app)) == 1
            assert occurrence.definition["version"] == saved.version
            assert occurrence.local_slot.endswith("-07:00")
            deliveries = [
                r for r in records(app.store, occurrence.run_id) if isinstance(r, Delivery)
            ]
            assert len(deliveries) == 1 and deliveries[0].state == DeliveryState.DELIVERED
            for _ in range(3):
                app.jobs.deliver()
            messages = [
                r
                for r in records(app.store, occurrence.run_id)
                if isinstance(r, Message) and r.role == "assistant"
            ]
            assert len(messages) == 1 and messages[0].id == deliveries[0].message_id
            app.store.append(
                Message(
                    origin=Origin.PROACTIVE,
                    session_id=messages[0].session_id,
                    run_id=occurrence.run_id,
                    role="assistant",
                    content="Private compaction evidence",
                )
            )
            assert [m.id for _, m in app.store.messages(messages[0].session_id)] == [messages[0].id]
            app.jobs.update(saved.id, task="Edited consumed task")
            await app.jobs.tick()
            assert len(requests) == 1 and app.store.job(saved.id).consumed
            assert not app.jobs.pause(saved.id).enabled
            manual = app.jobs.manual(saved.id, request_id=(key := new_id()))
            assert manual.id != occurrence.id and manual.trigger == "manual" and manual.run_id
            assert app.jobs.manual(saved.id, request_id=key).id == manual.id
            assert (await app.runs.wait(manual.run_id, timeout=3)).state == RunState.COMPLETED
            assert app.store.occurrence(manual.id).decision == "no_action"
            assert not app.store.job(saved.id).enabled
            assert not any(
                isinstance(r, Message) and r.role == "assistant"
                for r in records(app.store, manual.run_id)
            )
            with pytest.raises(ValueError, match="future"):
                app.jobs.update(saved.id, schedule="once:2026-11-15T16:00:00+00:00")
        finally:
            await app.stop()
        replacement, resumed = harness(profile, [])
        assert replacement.jobs
        replacement.jobs.clock = clock
        await replacement.start()
        assert replacement.store
        try:
            rebuild(profile.data_dir)
            await replacement.jobs.tick()
            assert not resumed and len(occurrences(replacement)) == 2
            old = replacement.store.job(saved.id)
            changed = replacement.jobs.update(
                saved.id,
                schedule="once:" + (clock.now + timedelta(hours=2)).isoformat(),
            )
            assert changed.generation == old.generation + 1 and not changed.consumed
            tombstone = replacement.jobs.delete(saved.id)
            with pytest.raises(ValueError, match="tombstone"):
                replacement.jobs.create(saved)
            with pytest.raises(ValueError, match="deleted"):
                replacement.jobs.resume(saved.id)
            assert tombstone.deleted and not replacement.jobs.list()
        finally:
            await replacement.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "stage", ["state_commit", "append", "fsync", "outbox_complete", "acknowledge"]
)
def test_claim_crash_recovers_queue_and_consumed_one_shot_once(
    profile: EffectiveProfile,
    stage: str,
) -> None:
    async def scenario() -> None:
        clock = Clock()
        app, requests = harness(profile, [])
        assert app.jobs
        app.jobs.clock = clock
        await app.start()
        assert app.store
        saved = job(app, clock, enabled=True)

        def crash(point: str) -> None:
            if point == stage:
                raise OSError("Injected admission crash")

        app.store.fault = crash
        with pytest.raises(OSError):
            await app.jobs.tick()
        await app.stop()
        assert not requests
        replacement, replayed = harness(profile, [reply("NO_ACTION")])
        assert replacement.jobs
        replacement.jobs.clock = clock
        await replacement.start()
        assert replacement.store and replacement.runs
        try:
            occurrence = occurrences(replacement)[0]
            assert occurrence.run_id and replacement.store.job(saved.id).consumed
            assert (
                await replacement.runs.wait(occurrence.run_id, timeout=3)
            ).state == RunState.COMPLETED
            await replacement.jobs.tick()
            assert len(replayed) == 1 and len(occurrences(replacement)) == 1
        finally:
            await replacement.stop()

    asyncio.run(scenario())


def test_cron_backlog_overlap_rates_and_pause(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        clock = Clock()
        app, requests = harness(profile, [reply("NO_ACTION")] * 10)
        assert app.jobs
        app.jobs.clock = clock
        await app.start()
        assert app.store and app.runs
        try:
            saved = job(app, clock, enabled=True, schedule="cron:* * * * *")
            clock.now += timedelta(days=3, minutes=5)
            await app.jobs.tick()
            first = occurrences(app)[0]
            assert first.due_at == clock.now and first.coalesced_from == saved.next_due
            assert first.run_id
            with pytest.raises(ValueError, match="overlap"):
                app.jobs.manual(saved.id, request_id=new_id())
            assert (await app.runs.wait(first.run_id, timeout=3)).state == RunState.COMPLETED
            clock.now += timedelta(minutes=1)
            await app.jobs.tick()
            assert occurrences(app)[-1].state == "skipped" and len(requests) == 1
            app.jobs.update(saved.id, enabled=False)
            clock.now += timedelta(hours=2)
            await app.jobs.tick()
            assert len(occurrences(app)) == 2
            app.jobs.update(saved.id, enabled=True)
            assert app.store.job(saved.id).next_due == clock.now + timedelta(minutes=1)
            app.jobs.global_pause(True)
            clock.now += timedelta(hours=3)
            await app.jobs.tick()
            assert len(occurrences(app)) == 2
            app.jobs.global_pause(False)
            assert app.store.job(saved.id).next_due == clock.now + timedelta(minutes=1)
            with pytest.raises(ValueError):
                app.jobs.update(saved.id, timezone="Not/AZone")
            updated = app.jobs.update(saved.id, timezone="UTC")
            assert updated.generation == saved.generation + 1
        finally:
            await app.stop()

    asyncio.run(scenario())


def test_quiet_publication_coalesces_expires_deduplicates_and_survives_restart(
    profile: EffectiveProfile,
) -> None:
    async def scenario() -> None:
        clock = Clock(datetime(2026, 11, 17, 5, tzinfo=UTC))
        app, _ = harness(profile, [reply("First"), reply("Latest"), reply("Latest")])
        assert app.jobs
        app.jobs.clock = clock
        await app.start()
        assert app.store and app.runs
        saved = job(app, clock, enabled=True)
        topic = saved.session_id
        assert topic
        await app.jobs.tick()
        occurrence = occurrences(app)[0]
        assert occurrence.run_id
        assert (await app.runs.wait(occurrence.run_id, timeout=3)).state == RunState.COMPLETED
        assert not [m for _, m in app.store.messages(topic) if m.role == "assistant"]
        clock.now += timedelta(hours=1)
        job(app, clock, enabled=True, session_id=topic)
        await app.jobs.tick()
        occurrence2 = occurrences(app)[-1]
        assert occurrence2.run_id
        assert (await app.runs.wait(occurrence2.run_id, timeout=3)).state == RunState.COMPLETED
        await app.stop()
        replacement, requests = harness(profile, [reply("Latest")])
        assert replacement.jobs
        replacement.jobs.clock = clock
        await replacement.start()
        assert replacement.store and replacement.runs
        try:
            clock.now = datetime(2026, 11, 17, 15, tzinfo=UTC)
            await replacement.jobs.tick()
            messages = [m for _, m in replacement.store.messages(topic) if m.role == "assistant"]
            assert [m.content for m in messages] == ["Latest"]
            clock.now += timedelta(hours=1)
            job(replacement, clock, enabled=True, session_id=topic)
            await replacement.jobs.tick()
            latest = occurrences(replacement)[-1]
            assert latest.run_id
            await replacement.runs.wait(latest.run_id, timeout=3)
            replacement.jobs.deliver()
            assert (
                len([m for _, m in replacement.store.messages(topic) if m.role == "assistant"]) == 1
            )
            assert len(requests) == 1
        finally:
            await replacement.stop()

    asyncio.run(scenario())


def test_ephemeral_job_archives_before_quiet_delivery_and_preserves_results(
    profile: EffectiveProfile,
) -> None:
    async def scenario() -> None:
        clock = Clock(datetime(2026, 11, 17, 5, tzinfo=UTC))
        app, _ = harness(profile, [reply("Publish after quiet hours")])
        assert app.jobs and app.store and app.runs
        app.jobs.clock = clock
        await app.start()
        try:
            job(app, clock, enabled=True, session_id=None, mode=SessionMode.EPHEMERAL)
            await app.jobs.tick()
            occurrence = occurrences(app)[0]
            assert occurrence.run_id
            topic_id = app.store.run(occurrence.run_id).session_id
            assert not app.store.session(topic_id).archived
            run = await app.runs.wait(occurrence.run_id, timeout=3)
            assert run.state == RunState.COMPLETED
            assert app.store.session(topic_id).archived
            assert not app.store.messages(topic_id)
        finally:
            await app.stop()

        replacement, requests = harness(profile, [])
        assert replacement.jobs and replacement.store
        replacement.jobs.clock = clock
        await replacement.start()
        try:
            clock.now = datetime(2026, 11, 17, 15, tzinfo=UTC)
            await replacement.jobs.tick()
            await replacement.jobs.tick()
            assert replacement.store.session(topic_id).archived
            assert [m.content for _, m in replacement.store.messages(topic_id)] == [
                "Publish after quiet hours"
            ]
            assert not requests
        finally:
            await replacement.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["deleted", "cancelled", "interrupted"])
def test_ephemeral_job_archives_after_unsuccessful_run(
    profile: EffectiveProfile, ending: str
) -> None:
    async def scenario() -> None:
        clock = Clock()
        app, requests = harness(profile, [])
        assert app.jobs and app.store and app.runs
        app.jobs.clock = clock
        await app.start()
        try:
            saved = job(app, clock, session_id=None, mode=SessionMode.EPHEMERAL)
            occurrence = app.jobs.manual(saved.id, request_id=new_id())
            assert occurrence.run_id
            if ending == "deleted":
                app.jobs.delete(saved.id)
                run = await app.runs.wait(occurrence.run_id, timeout=3)
                assert run.state == RunState.FAILED
            elif ending == "cancelled":
                run = app.runs.cancel(occurrence.run_id)
                assert run.state == RunState.CANCELLED
            else:
                run = app.store.transition_run(occurrence.run_id, RunState.RUNNING)
            assert app.store.session(run.session_id).archived == (ending != "interrupted")
            assert not requests
        finally:
            await app.stop()

        if ending == "interrupted":
            replacement, _ = harness(profile, [])
            assert replacement.store
            await replacement.start()
            try:
                assert replacement.store.run(run.id).state == RunState.INTERRUPTED
                assert replacement.store.session(run.session_id).archived
            finally:
                await replacement.stop()

    asyncio.run(scenario())


def test_no_successors_even_with_interactive_relabel_and_delayed_context(
    profile: EffectiveProfile,
) -> None:
    async def scenario() -> None:
        clock = Clock()
        attempts: list[str] = []

        async def circumvent(context: ToolContext, args: ToolArguments) -> ToolResult:
            assert app.jobs and app.runs and app.store
            for action in (
                lambda: app.jobs.create(
                    Job(
                        origin=Origin.PROACTIVE,
                        profile="default",
                        task="child",
                        schedule="cron:0 * * * *",
                        session_id=context.session_id,
                    )
                ),
                lambda: app.jobs.resume(saved.id),
                lambda: app.jobs.manual(saved.id, request_id=new_id()),
                lambda: app.runs.submit(
                    context.session_id,
                    profile=app.profile,
                    content="relabeled",
                    request_id=new_id(),
                ),
                lambda: app.store.admit(
                    context.session_id, profile="default", content="relabeled", request_id=new_id()
                ),
            ):
                with pytest.raises(ToolError):
                    action()
                attempts.append("denied")
            token = current_run.get()
            assert token and token.origin == Origin.PROACTIVE
            return ToolResult(data="All successors denied")

        extra = Tool("test.circumvent", "Fixture lineage attack", ToolArguments, circumvent)
        app, _ = harness(
            profile, [reply("", calls=[call(extra.name)]), reply("NO_ACTION")], (extra,)
        )
        assert app.jobs
        app.jobs.clock = clock
        await app.start()
        assert app.runs
        try:
            saved = job(app, clock, enabled=True, tools=(extra.name,))
            await app.jobs.tick()
            occurrence = occurrences(app)[0]
            assert occurrence.run_id
            assert (await app.runs.wait(occurrence.run_id, timeout=3)).state == RunState.COMPLETED
            assert len(attempts) == 5 and len(app.jobs.list()) == 1
        finally:
            await app.stop()

    asyncio.run(scenario())


def test_narrow_job_tools_and_permissions(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        clock = Clock()
        extra = tuple(t for t in TOOLS if t.name.startswith("jobs."))
        app, requests = harness(
            profile,
            [
                reply(
                    "",
                    calls=[
                        call(
                            "jobs.create",
                            canonical(
                                {
                                    "task": "Future check",
                                    "schedule": "cron:0 * * * *",
                                    "tools": [],
                                }
                            ).decode(),
                        )
                    ],
                ),
                reply("Created disabled."),
            ],
            extra,
        )
        assert app.jobs
        app.jobs.clock = clock
        await app.start()
        assert app.store and app.runs
        try:
            topic = app.store.create_session()
            run = app.runs.submit(
                topic.id, profile=app.profile, content="fixture", request_id=new_id()
            )
            assert (await app.runs.wait(run.id, timeout=3)).state == RunState.COMPLETED
            saved = app.jobs.list()[0]
            assert not saved.enabled and saved.session_id == topic.id
            with pytest.raises(ValueError, match="grants"):
                job(app, clock, tools=("missing.tool",))
            with pytest.raises(ValueError, match="budgets"):
                job(app, clock, budgets={"model_calls": 200})
            with pytest.raises(ValueError, match="roots"):
                job(app, clock, workspace_roots=("/",))
            assert all("jobs.create" in str(request["tools"]) for request in requests)
        finally:
            await app.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("explicit_roots", [False, True])
def test_background_file_rename_requires_explicit_roots(
    profile: EffectiveProfile, explicit_roots: bool
) -> None:
    async def scenario() -> None:
        clock = Clock(datetime(2026, 11, 17, 5, tzinfo=UTC))
        root = profile.workspace_roots[0]
        (root / "source.txt").write_text("local file")
        app, requests = harness(
            profile,
            [
                reply(
                    "",
                    calls=[
                        call("files.rename", '{"source":"source.txt","destination":"renamed.txt"}')
                    ],
                ),
                reply("Finished."),
            ],
        )
        assert app.jobs
        app.jobs.clock = clock
        await app.start()
        assert app.store and app.runs
        try:
            job(
                app,
                clock,
                enabled=True,
                tools=("files.rename",),
                workspace_roots=(str(root),) if explicit_roots else None,
            )
            await app.jobs.tick()
            occurrence = occurrences(app)[0]
            assert occurrence.run_id
            await app.runs.wait(occurrence.run_id, timeout=3)
            receipt = next(
                record
                for record in records(app.store, occurrence.run_id)
                if isinstance(record, ToolReceipt)
            )
            assert (receipt.status, receipt.effect) == (
                ("succeeded", "confirmed") if explicit_roots else ("denied", "none")
            )
            assert ("files.rename" in str(requests[0].get("tools", []))) == explicit_roots
            assert (root / "renamed.txt").exists() == explicit_roots
        finally:
            await app.stop()

    asyncio.run(scenario())


def test_pending_bound_active_and_interactive_fairness(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        clock = Clock()
        entered, release = asyncio.Event(), asyncio.Event()
        autonomous, peak = 0, 0

        async def respond(body: dict[str, Any]) -> bytes:
            nonlocal autonomous, peak
            text = str(body["messages"])
            if "background occurrence" in text:
                autonomous += 1
                peak = max(peak, autonomous)
                entered.set()
                await release.wait()
                autonomous -= 1
            return wire(reply("NO_ACTION"))

        app, _ = harness(profile, respond)
        assert app.jobs
        app.jobs.clock = clock
        await app.start()
        assert app.store and app.runs
        try:
            for _ in range(6):
                job(app, clock, enabled=True)
            await app.jobs.tick()
            assert len(occurrences(app)) == 4
            await asyncio.wait_for(entered.wait(), 2)
            assert len(app.runs.active) == 1
            topic = app.store.create_session()
            interactive = app.runs.submit(
                topic.id, profile=app.profile, content="Interactive", request_id=new_id()
            )
            for _ in range(4):
                await asyncio.sleep(0)
            assert app.store.run(interactive.id).state == RunState.RUNNING
            release.set()
            assert (await app.runs.wait(interactive.id, timeout=3)).state == RunState.COMPLETED
            for occurrence in occurrences(app):
                assert occurrence.run_id
                await app.runs.wait(occurrence.run_id, timeout=3)
            assert peak == 1
        finally:
            release.set()
            await app.stop()

    asyncio.run(scenario())


def test_interrupted_effect_is_uncertain_and_never_replayed(
    profile: EffectiveProfile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        clock = Clock()
        app, _ = harness(
            profile,
            [
                reply(
                    "",
                    calls=[
                        call(
                            "files.write",
                            '{"path":"once.txt","content":"once"}',
                        )
                    ],
                )
            ],
        )
        assert app.jobs
        app.jobs.clock = clock
        await app.start()
        assert app.store and app.runs
        original = app.store.append

        def crash(record: Any) -> None:
            if isinstance(record, ToolReceipt):
                raise StorageError("Lost effect receipt")
            original(record)

        monkeypatch.setattr(app.store, "append", crash)
        saved = job(
            app,
            clock,
            enabled=True,
            tools=("files.write",),
            workspace_roots=tuple(str(p) for p in profile.workspace_roots),
        )
        await app.jobs.tick()
        occurrence = occurrences(app)[0]
        assert occurrence.run_id
        with pytest.raises(StorageError):
            await app.runs.wait(occurrence.run_id, timeout=3)
        await app.stop()
        assert (profile.workspace_roots[0] / "once.txt").read_text() == "once"
        replacement, requests = harness(profile, [])
        assert replacement.jobs
        replacement.jobs.clock = clock
        await replacement.start()
        assert replacement.store
        try:
            assert replacement.store.job(saved.id).consumed
            assert replacement.store.occurrence(occurrence.id).state == "uncertain"
            receipt = next(
                r
                for r in records(replacement.store, occurrence.run_id)
                if isinstance(r, ToolReceipt)
            )
            assert receipt.status == "uncertain" and not requests
            await replacement.jobs.tick()
            assert not requests
        finally:
            await replacement.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["state_commit", "fsync", "acknowledge"])
def test_local_publication_crash_keeps_one_stable_message(
    profile: EffectiveProfile,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    async def scenario() -> None:
        clock = Clock()
        app, _ = harness(profile, [reply("Publish once")])
        assert app.jobs
        app.jobs.clock = clock
        await app.start()
        assert app.store and app.runs
        commit = app.store._commit

        def crashing_commit(source: Any, **options: Any) -> None:
            if any(isinstance(r, Message) and r.role == "assistant" for r in source):
                assert app.store

                def fault(point: str) -> None:
                    if point == stage:
                        raise OSError("Lost publication acknowledgement")

                app.store.fault = fault
            commit(source, **options)

        monkeypatch.setattr(app.store, "_commit", crashing_commit)
        saved = job(app, clock, enabled=True)
        await app.jobs.tick()
        occurrence = occurrences(app)[0]
        assert occurrence.run_id
        with pytest.raises(StorageError):
            await app.runs.wait(occurrence.run_id, timeout=3)
        await app.stop()
        replacement, requests = harness(profile, [])
        assert replacement.jobs
        replacement.jobs.clock = clock
        await replacement.start()
        assert replacement.store and saved.session_id
        try:
            await replacement.jobs.tick()
            assert [m.content for _, m in replacement.store.messages(saved.session_id)] == [
                "Publish once"
            ]
            assert replacement.store.run(occurrence.run_id).state == RunState.COMPLETED
            assert not requests
        finally:
            await replacement.stop()

    asyncio.run(scenario())


def test_daily_and_pending_bounds_survive_restart(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        clock = Clock()
        selected = profile.model_copy(
            update={
                "background": BackgroundSettings(daily_runs=2, max_pending=1),
            }
        )
        app, _ = harness(selected, [reply("NO_ACTION")] * 2)
        assert app.jobs
        app.jobs.clock = clock
        await app.start()
        assert app.store and app.runs
        try:
            jobs = [job(app, clock, enabled=True) for _ in range(3)]
            for count in range(2):
                await app.jobs.tick()
                assert len(occurrences(app)) == count + 1
                assert len(app.store.queued_runs()) == 1
                occurrence = occurrences(app)[-1]
                assert occurrence.run_id
                await app.runs.wait(occurrence.run_id, timeout=3)
                clock.now += timedelta(seconds=1)
        finally:
            await app.stop()
        replacement, requests = harness(selected, [])
        assert replacement.jobs
        replacement.jobs.clock = clock
        await replacement.start()
        assert replacement.store
        try:
            await replacement.jobs.tick()
            assert len(occurrences(replacement)) == 2 and not requests
            assert not replacement.store.job(jobs[-1].id).consumed
        finally:
            await replacement.stop()

    asyncio.run(scenario())


def test_effect_crossing_quiet_boundary_waits_before_intent(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        clock = Clock()
        effects: list[str] = []

        async def outbound(context: ToolContext, args: ToolArguments) -> ToolResult:
            context.begin_effect(())
            effects.append("sent")
            context.confirm_effect()
            return ToolResult(data="accepted")

        attempts = 0

        async def respond(body: dict[str, Any]) -> bytes:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                clock.now = datetime(2026, 11, 17, 5, tzinfo=UTC)
                return wire(reply("", calls=[call("test.outbound")]))
            return wire(reply("Finished"))

        app, _ = harness(
            profile,
            respond,
            (
                Tool(
                    "test.outbound",
                    "Fixture outbound effect",
                    ToolArguments,
                    outbound,
                    True,
                ),
            ),
        )
        assert app.jobs
        app.jobs.clock = clock
        await app.start()
        assert app.store and app.runs
        try:
            job(app, clock, enabled=True, tools=("test.outbound",))
            await app.jobs.tick()
            occurrence = occurrences(app)[0]
            assert occurrence.run_id
            await asyncio.sleep(0.03)
            assert not effects and app.store.tool_attempts(occurrence.run_id) == 0
            clock.now = datetime(2026, 11, 17, 15, tzinfo=UTC)
            await app.jobs.tick()
            assert (await app.runs.wait(occurrence.run_id, timeout=3)).state == RunState.COMPLETED
            assert effects == ["sent"]
        finally:
            await app.stop()

    asyncio.run(scenario())


def test_expired_notifications_discarded_and_no_action_stays_private(
    profile: EffectiveProfile,
) -> None:
    async def scenario() -> None:
        clock = Clock(datetime(2026, 11, 17, 5, tzinfo=UTC))
        selected = profile.model_copy(
            update={
                "background": BackgroundSettings(notification_ttl_seconds=60),
            }
        )
        app, _ = harness(selected, [reply("Stale"), reply("NO_ACTION")])
        assert app.jobs
        app.jobs.clock = clock
        await app.start()
        assert app.store and app.runs
        try:
            first = job(app, clock, enabled=True)
            second = job(app, clock, enabled=True)
            await app.jobs.tick()
            for occurrence in occurrences(app):
                assert occurrence.run_id
                await app.runs.wait(occurrence.run_id, timeout=3)
            clock.now = datetime(2026, 11, 17, 15, tzinfo=UTC)
            await app.jobs.tick()
            assert first.session_id and second.session_id
            assert not app.store.messages(first.session_id) and not app.store.messages(
                second.session_id
            )
            assert all(
                app.store._load("delivery", r[0], Delivery).state == DeliveryState.CANCELLED
                for r in app.store.db.execute("SELECT id FROM entities WHERE kind='delivery'")
            )
        finally:
            await app.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["archive", "revoke", "delete"])
def test_queued_jobs_revalidate_before_inference(profile: EffectiveProfile, change: str) -> None:
    async def scenario() -> None:
        clock = Clock()
        app, requests = harness(profile, [reply("NO_ACTION")])
        assert app.jobs
        app.jobs.clock = clock
        await app.start()
        assert app.store and app.runs
        try:
            saved = job(app, clock, enabled=True, tools=("runtime.status",))
            await app.jobs.tick()
            occurrence = occurrences(app)[0]
            assert occurrence.run_id and saved.session_id
            if change == "archive":
                app.store.update_session(saved.session_id, archived=True)
            elif change == "revoke":
                app.profile = app.profile.model_copy(update={"tools": frozenset()})
            else:
                app.jobs.delete(saved.id)
            result = await app.runs.wait(occurrence.run_id, timeout=3)
            if change == "revoke":
                # Revoking an unused tool narrows execution without invalidating the task.
                assert result.state == RunState.COMPLETED
                assert requests and not requests[0].get("tools")
            else:
                assert result.state == RunState.FAILED and not requests
        finally:
            await app.stop()

    asyncio.run(scenario())
