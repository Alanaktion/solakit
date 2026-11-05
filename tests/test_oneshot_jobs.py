"""One-time admission, restart protection, timezone resolution and management."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_execution import completion, model_list, setup

from solakit.channels import Channels
from solakit.core.config import Job
from solakit.jobs import Jobs
from solakit.main import create_app


def definition(**changes):
    return {
        "id": "tomorrow",
        "run_at": "2026-11-06T09:00:00",
        "timezone": "America/Denver",
        "profile": "interactive",
        "task": "Check on the earlier request",
        "bind_session": "s",
        "enabled": True,
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"cron": "0 9 * * *"},
        {"run_at": None},
        {"run_at": "2026-11-06"},
        {"run_at": "not a timestamp"},
        {"run_at": "2027-03-14T02:30:00"},
        {"run_at": "2026-11-01T01:30:00"},
        {"catch_up": True, "polling": True},
    ],
)
def test_invalid_schedule_and_dst_inputs(changes):
    with pytest.raises(ValidationError):
        Job(**definition(**changes))


def test_local_and_explicit_offsets_are_normalized():
    assert Job(**definition()).run_at == "2026-11-06T16:00:00+00:00"
    assert (
        Job(**definition(run_at="2027-06-01T09:00:00")).run_at
        == "2027-06-01T15:00:00+00:00"
    )
    first = Job(**definition(run_at="2026-11-01T01:30:00-06:00"))
    second = Job(**definition(run_at="2026-11-01T01:30:00-07:00"))
    assert datetime.fromisoformat(second.run_at) - datetime.fromisoformat(
        first.run_at
    ) == timedelta(hours=1)


def scheduler(config, clock, calls):
    def model(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        calls.append(json.loads(request.content))
        return completion(config, "Checked once")

    execution = setup(config, model)
    return execution, Jobs(
        config, execution, Channels(config, execution, {}), lambda: clock[0]
    )


def test_once_survives_restart_rebuild_edits_and_enablement_without_replay(config):
    clock, calls = [datetime(2026, 11, 5, tzinfo=UTC)], []
    execution, jobs = scheduler(config, clock, calls)

    async def exercise():
        try:
            await jobs.initialize()
            await jobs.create(Job(**definition()))
            await jobs.tick()
            assert not calls
            clock[0] = datetime(2026, 11, 6, 16, tzinfo=UTC)
            await jobs.tick()
            assert jobs.get("tomorrow")["consumed"]
            assert jobs.get("tomorrow")["next_fire"] is None
            await jobs.drain()
            assert (
                len(calls) == 1 and jobs.get("tomorrow")["last_status"] == "completed"
            )
            execution.store.rebuild()
            restarted = Jobs(config, execution, jobs.channels, lambda: clock[0])
            await restarted.initialize()
            await restarted.update("tomorrow", {"task": "Edited task"})
            await restarted.update("tomorrow", {"enabled": False})
            await restarted.update("tomorrow", {"enabled": True, "timezone": "UTC"})
            await restarted.pause("tomorrow", True)
            await restarted.pause("tomorrow", False)
            clock[0] += timedelta(days=1)
            await restarted.tick()
            await restarted.drain()
            assert restarted.get("tomorrow")["next_fire"] is None and len(calls) == 1
            with pytest.raises(RuntimeError, match="already attempted"):
                await restarted.trigger("tomorrow")
            await restarted.update("tomorrow", {"run_at": "2026-11-08T09:00:00Z"})
            assert not restarted.get("tomorrow")["consumed"]
            clock[0] = datetime(2026, 11, 8, 9, tzinfo=UTC)
            await restarted.tick()
            await restarted.drain()
            assert len(calls) == 2
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_manual_early_run_consumes_scheduled_attempt(config):
    clock, calls = [datetime(2026, 11, 5, tzinfo=UTC)], []
    execution, jobs = scheduler(config, clock, calls)

    async def exercise():
        try:
            await jobs.initialize()
            await jobs.create(Job(**definition()))
            assert (await jobs.run_now("tomorrow")).status == "completed"
            clock[0] += timedelta(days=3)
            await jobs.tick()
            await jobs.drain()
            assert len(calls) == 1
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_overdue_unattempted_job_catches_up_once_after_restart(config):
    clock, calls = [datetime(2026, 11, 5, tzinfo=UTC)], []
    execution, jobs = scheduler(config, clock, calls)

    async def exercise():
        try:
            await jobs.initialize()
            await jobs.create(Job(**definition()))
            clock[0] += timedelta(days=3)
            restarted = Jobs(config, execution, jobs.channels, lambda: clock[0])
            await restarted.initialize()
            await restarted.tick()
            await restarted.drain()
            await restarted.tick()
            assert len(calls) == 1
            assert len(execution.store.list_records("runs")) == 1
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_interrupted_admission_is_recorded_and_never_replayed(config):
    clock, calls = [datetime(2026, 11, 5, tzinfo=UTC)], []
    execution, jobs = scheduler(config, clock, calls)

    async def exercise():
        try:
            await jobs.initialize()
            await jobs.create(Job(**definition()))
            # Crash after admission intent was saved, before creating work.
            state = jobs.states["tomorrow"]
            state.active, state.consumed, state.next_fire = True, True, None
            await jobs._save(state)
            clock[0] += timedelta(days=3)
            for _ in range(2):
                restarted = Jobs(config, execution, jobs.channels, lambda: clock[0])
                await restarted.initialize()
                await restarted.tick()
                assert restarted.get("tomorrow")["consumed"]
                assert restarted.get("tomorrow")["last_status"] == "missed"
            assert not calls and len(execution.store.list_records("runs")) == 1
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_api_one_time_switch_and_past_dates(config, mock_model, auth):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        future = definition(run_at="2099-01-02T09:00:00")
        response = client.post("/api/jobs", headers=auth, json=future)
        assert response.status_code == 201 and response.json()["cron"] is None
        assert response.json()["run_at"] == "2099-01-02T16:00:00+00:00"
        assert (
            client.patch(
                "/api/jobs/tomorrow",
                headers=auth,
                json={"run_at": "2000-01-01T09:00:00Z"},
            ).status_code
            == 422
        )
        assert client.post("/api/jobs/tomorrow/run", headers=auth).status_code == 200
        assert (
            client.post("/api/jobs/tomorrow/run?wait=false", headers=auth).status_code
            == 409
        )
        switched = client.patch(
            "/api/jobs/tomorrow",
            headers=auth,
            json={"run_at": None, "cron": "0 9 * * *"},
        )
        assert switched.status_code == 200 and not switched.json()["consumed"]
        assert (
            client.patch(
                "/api/jobs/tomorrow",
                headers=auth,
                json={"run_at": "2099-01-03T09:00:00Z"},
            ).status_code
            == 422
        )
        switched = client.patch(
            "/api/jobs/tomorrow",
            headers=auth,
            json={"cron": None, "run_at": "2099-01-03T09:00:00Z"},
        )
        assert switched.status_code == 200 and switched.json()["next_fire"]


def test_chat_request_can_schedule_tomorrow_morning_in_current_conversation(config):
    config.enabled_tools = ["jobs"]
    config.profiles["interactive"].tools = ["jobs.create"]
    config.profiles["interactive"].capabilities = ["jobs.manage"]
    tomorrow = datetime.now(ZoneInfo(config.timezone)).date() + timedelta(days=1)
    local = f"{tomorrow.isoformat()}T09:00:00"
    requests = []

    def model(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return completion(
                config,
                calls=[
                    {
                        "id": "remind",
                        "type": "function",
                        "function": {
                            "name": "jobs.create",
                            "arguments": json.dumps(
                                {
                                    "id": "check-tomorrow",
                                    "run_at": local,
                                    "task": "Check the earlier request.",
                                    "enabled": True,
                                }
                            ),
                        },
                    }
                ],
            )
        return completion(config, "I'll check tomorrow morning at 9 AM.")

    execution = setup(config, model)
    jobs = Jobs(config, execution, Channels(config, execution, {}))
    execution.registry.job_manager = jobs

    async def exercise():
        try:
            await jobs.initialize()
            run = await execution.submit("s", "Check on this tomorrow morning.")
            assert run.status == "completed"
            scheduled = jobs.get("check-tomorrow")
            assert scheduled["bind_session"] == "s" and scheduled["enabled"]
            instant = datetime.fromisoformat(scheduled["run_at"]).astimezone(
                ZoneInfo(config.timezone)
            )
            assert instant.date() == tomorrow and instant.hour == 9
            assert scheduled["cron"] is None
            assert (
                "run_at"
                in requests[0]["tools"][0]["function"]["parameters"]["properties"]
            )
        finally:
            await execution.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("fraction", ["", ".123456"])
def test_ui_one_time_editor_preserves_exact_ambiguous_instant(
    config, mock_model, fraction
):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        client.post("/login", data={"token": "operator-test-token"})
        response = client.post(
            "/api/jobs", json=definition(run_at=f"2027-11-07T01:30:00{fraction}-06:00")
        )
        assert response.status_code == 201
        original = response.json()["run_at"]
        editor = client.get("/operations/jobs/tomorrow/edit")
        assert f'value="2027-11-07T01:30:00{fraction}"' in editor.text
        response = client.post(
            "/operations/jobs/tomorrow/edit",
            data={
                "id": "tomorrow",
                "schedule_type": "once",
                "run_at": f"2027-11-07T01:30:00{fraction}"
                if fraction
                else "2027-11-07T01:30",
                "timezone": "America/Denver",
                "task": "Edited only the task",
                "profile": "interactive",
                "bind_session": "s",
                "enabled": "on",
                "delivery_mode": "automatic",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert client.get("/api/jobs/tomorrow").json()["run_at"] == original
