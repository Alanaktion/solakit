import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError
from test_channels import FakeAdapter
from test_execution import completion, model_list, setup

from solakit.channels import Channels
from solakit.channels.base import Inbound
from solakit.core.config import Channel, Job
from solakit.jobs import Jobs, render_task


def configured(config, *, catch_up=False):
    config.channels = [
        Channel(
            id="room",
            type="discord",
            channel_id="100",
            session="room",
            enabled=True,
            trigger_authors=["alan"],
        )
    ]
    config.jobs = [
        Job(
            id="scheduled",
            cron="*/5 * * * *",
            timezone="America/Denver",
            profile="interactive",
            task="Job {date}",
            bind_session="room",
            enabled=True,
            catch_up=catch_up,
            polling=catch_up,
        )
    ]


@pytest.mark.parametrize("catch_up", [False, True])
def test_restart_misfire_skip_or_exactly_one_catch_up(config, catch_up):
    configured(config, catch_up=catch_up)
    captured = []
    clock = [datetime.fromisoformat("2026-11-04T19:01:00+00:00")]

    def model(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        captured.append(json.loads(request.content))
        return completion(config, "scheduled result")

    async def exercise():
        first = setup(config, model)
        jobs = Jobs(
            config,
            first,
            Channels(config, first, {"discord": FakeAdapter()}),
            lambda: clock[0],
        )
        await jobs.initialize()
        expected = jobs.states["scheduled"].next_fire
        await jobs.stop_admission()
        await first.close()
        (config.paths.data / "app.db").unlink()
        clock[0] += timedelta(minutes=20)
        restarted = setup(config, model)
        jobs = Jobs(
            config,
            restarted,
            Channels(config, restarted, {"discord": FakeAdapter()}),
            lambda: clock[0],
        )
        await jobs.initialize()
        assert jobs.states["scheduled"].next_fire == expected
        await jobs.tick()
        await jobs.drain()
        await jobs.tick()
        await jobs.drain()
        assert len(captured) == (1 if catch_up else 0)
        missed = [
            r for r in restarted.store.list_records("runs") if r["status"] == "missed"
        ]
        assert len(missed) == 1
        assert "firing range" in missed[0]["error"]
        assert jobs.states["scheduled"].next_fire > clock[0]
        assert restarted.store.list_records("jobs")[0]["id"] == "scheduled"
        await jobs.stop_admission()
        await restarted.close()

    asyncio.run(exercise())


def test_nonoverlap_includes_queued_jobs_and_channel_continuity(config):
    configured(config)
    captured = []

    def model(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        captured.append(json.loads(request.content)["messages"])
        return completion(config, "stored job result")

    execution, adapter = setup(config, model), FakeAdapter()
    channels = Channels(config, execution, {"discord": adapter})
    jobs = Jobs(config, execution, channels)

    async def exercise():
        await jobs.initialize()
        await execution.semaphore.acquire()
        first = asyncio.create_task(jobs.run_now("scheduled"))
        while not execution.pending:
            await asyncio.sleep(0)
        assert jobs.states["scheduled"].active
        with pytest.raises(ValueError, match="already queued/running"):
            await jobs.run_now("scheduled")
        execution.semaphore.release()
        run = await first
        assert run.status == "completed"
        assert len(adapter.sent) == 1
        assert adapter.sent[0][1].text == "stored job result"
        assert all(
            m["origin"]["job"] == "scheduled"
            for m in execution.store.transcript("room")
        )
        await channels.receive(
            Inbound("discord", "100", "alan", "follow up", "external")
        )
        assert captured[-1][1]["content"].startswith("Job ")
        assert captured[-1][2]["content"] == "stored job result"
        assert captured[-1][3]["content"] == "follow up"
        await jobs.stop_admission()
        await execution.close()
        await jobs.drain()

    asyncio.run(exercise())


def test_pause_survives_index_rebuild_and_resume_skips_paused_interval(config):
    configured(config)
    clock = [datetime.fromisoformat("2026-11-04T19:01:00+00:00")]

    def model(request):
        return model_list(config)

    async def exercise():
        execution = setup(config, model)
        jobs = Jobs(
            config, execution, Channels(config, execution, {}), lambda: clock[0]
        )
        await jobs.initialize()
        await jobs.pause("scheduled", True)
        execution.store.rebuild()
        saved = execution.store.job_state("scheduled")
        assert saved["paused"]
        clock[0] += timedelta(days=2)
        await jobs.tick()
        assert not execution.pending
        await jobs.pause("scheduled", False)
        assert jobs.states["scheduled"].next_fire > clock[0]
        await jobs.stop_admission()
        await execution.close()

    asyncio.run(exercise())


def test_model_down_is_a_gap_without_channel_messages(config):
    configured(config)
    execution = setup(config, lambda request: httpx.Response(503))
    adapter = FakeAdapter()
    jobs = Jobs(config, execution, Channels(config, execution, {"discord": adapter}))

    async def exercise():
        await jobs.initialize()
        run = await jobs.run_now("scheduled")
        assert run.status == "missed"
        assert execution.store.transcript("room") == []
        assert not adapter.sent
        assert jobs.states["scheduled"].last_status == "missed"
        await jobs.stop_admission()
        await execution.close()

    asyncio.run(exercise())


def test_job_placeholders_and_timezone_are_bounded():
    base = dict(
        id="j", cron="0 19 * * tue,thu", profile="interactive", bind_session="s"
    )
    job = Job(**base, task="{date} {timezone}")
    assert (
        render_task(job, datetime.fromisoformat("2026-11-04T01:00:00+00:00"))
        == "2026-11-03 America/Denver"
    )
    for task in ("{date.__class__}", "{date!r}", "{date:100000000}", "{unknown}"):
        with pytest.raises(ValidationError):
            Job(**base, task=task)
    with pytest.raises(ValidationError):
        Job(**base, task="poll", catch_up=True)


def test_job_api_controls_disabled_jobs_without_activating_integrations(
    config, auth, mock_model
):
    from fastapi.testclient import TestClient

    from solakit.main import create_app

    config.jobs = [
        Job(
            id="example",
            cron="0 19 * * *",
            profile="interactive",
            task="task",
            bind_session="s",
        )
    ]
    with TestClient(create_app(config=config, model=mock_model)) as client:
        assert client.get("/api/jobs").status_code == 401
        assert client.get("/api/jobs", headers=auth).json()[0]["enabled"] is False
        assert client.post("/api/jobs/example/pause", headers=auth).json()["paused"]
        assert not client.post("/api/jobs/example/resume", headers=auth).json()[
            "paused"
        ]
        assert client.post("/api/jobs/example/run", headers=auth).status_code == 409
        assert client.post("/api/jobs/unknown/run", headers=auth).status_code == 404
        assert client.post("/api/jobs/example/delete", headers=auth).status_code == 404


def test_apscheduler_wakeup_lifecycle_and_interrupted_dispatch_recovery(config):
    configured(config)

    def clock():
        return datetime.fromisoformat("2026-11-04T19:01:00+00:00")

    async def exercise():
        execution = setup(config, lambda request: model_list(config))
        jobs = Jobs(config, execution, Channels(config, execution, {}), clock)
        await jobs.start()
        assert jobs.scheduler.running
        assert jobs.scheduler.get_jobs()[0].max_instances == 1
        await jobs.stop_admission()
        await asyncio.sleep(0)
        assert not jobs.scheduler.running
        state = jobs.states["scheduled"]
        state.active = True  # Simulated process interruption before dispatch finalized.
        await jobs._save(state)
        restarted = Jobs(config, execution, Channels(config, execution, {}), clock)
        await restarted.initialize()
        assert not restarted.states["scheduled"].active
        assert restarted.states["scheduled"].last_status == "missed"
        assert (
            "Interrupted dispatch"
            in execution.store.run(restarted.states["scheduled"].last_run)["error"]
        )
        await execution.close()

    asyncio.run(exercise())
