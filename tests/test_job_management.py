"""Persistent CRUD, nonblocking chat dispatch and API-backed management forms."""

import asyncio
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from test_execution import completion, model_list, setup

from solakit.channels import Channels
from solakit.core.config import Job
from solakit.core.model import ModelClient
from solakit.jobs import Jobs
from solakit.main import create_app
from solakit.tools import ToolContext


def definition(**changes):
    return dict(
        id="daily",
        cron="0 19 * * *",
        timezone="America/Denver",
        task="Reflect on {date}",
        profile="interactive",
        bind_session="evening",
        **changes,
    )


def test_api_crud_import_once_restart_and_rebuild(config, mock_model, auth):
    config.jobs = [Job(**definition())]
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        assert client.post("/api/jobs", json=definition()).status_code == 401
        assert client.patch("/api/jobs/daily", json={"task": "edit"}).status_code == 401
        assert client.delete("/api/jobs/daily").status_code == 401
        assert (
            client.post("/api/jobs", headers=auth, json=definition()).status_code == 409
        )
        response = client.patch(
            "/api/jobs/daily",
            headers=auth,
            json={"task": "Updated task", "cron": "0 8 * * *", "enabled": True},
        )
        assert response.status_code == 200
        assert response.json()["next_fire"] and response.json()["cron"] == "0 8 * * *"
        # Rebuilding derived tables preserves authoritative definitions.
        app.state.store.rebuild()
        created = client.post(
            "/api/jobs", headers=auth, json={**definition(), "id": "api-job"}
        )
        assert created.status_code == 201 and created.json()["enabled"] is False
        assert (
            client.get("/api/jobs/api-job", headers=auth).json()["task"]
            == "Reflect on {date}"
        )
        assert client.delete("/api/jobs/api-job", headers=auth).json()["deleted"]
        assert client.get("/api/jobs/api-job", headers=auth).status_code == 404
        assert (
            client.patch("/api/jobs/missing", headers=auth, json={}).status_code == 404
        )
    # Editing the legacy seed must not overwrite its imported, API-managed value.
    config.jobs[0].task = "Stale TOML"
    config.jobs.append(Job(**{**definition(), "id": "api-job"}))
    fresh_model = ModelClient(
        config.model, transport=httpx.MockTransport(lambda request: model_list(config))
    )
    with TestClient(create_app(config=config, model=fresh_model)) as client:
        assert (
            client.get("/api/jobs/daily", headers=auth).json()["task"] == "Updated task"
        )
        assert client.get("/api/jobs/api-job", headers=auth).status_code == 404
        assert len(client.get("/api/jobs", headers=auth).json()) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"cron": "invalid"},
        {"timezone": "No/Such_Zone"},
        {"profile": "missing"},
        {"profile": "maintenance"},
        {"task": "{unsupported}"},
        {"catch_up": True},
        {"deliver": {"to": "missing"}},
        {"bind_session": "../outside"},
        {"task": ""},
        {"id": "../bad"},
        {"enabled": "true"},
    ],
)
def test_invalid_api_definitions_do_not_create_jobs(config, mock_model, auth, changes):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        response = client.post(
            "/api/jobs", headers=auth, json={**definition(), **changes}
        )
        assert response.status_code == 422, response.text
        assert client.get("/api/jobs", headers=auth).json() == []


def test_api_patch_nulls_extra_fields_and_delivery_clear(config, mock_model, auth):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        client.post(
            "/api/jobs",
            headers=auth,
            json={**definition(), "deliver": {"mode": "silent"}},
        )
        assert (
            client.patch(
                "/api/jobs/daily", headers=auth, json={"deliver": None}
            ).json()["deliver"]
            is None
        )
        for patch in (
            {"id": "renamed"},
            {"typo": True},
            {"cron": None},
            {"cron": "bad"},
        ):
            assert (
                client.patch("/api/jobs/daily", headers=auth, json=patch).status_code
                == 422
            )
        assert (
            client.get("/api/jobs/daily", headers=auth).json()["cron"] == "0 19 * * *"
        )


def test_api_nonblocking_trigger_acknowledges_while_inference_is_busy(
    config, mock_model, auth
):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        created = client.post(
            "/api/jobs", headers=auth, json={**definition(), "enabled": True}
        )
        assert created.status_code == 201

        async def exercise():
            await app.state.execution.semaphore.acquire()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    headers=auth,
                ) as api:
                    response = await asyncio.wait_for(
                        api.post("/api/jobs/daily/run?wait=false"), 2
                    )
                    assert response.status_code == 202 and response.json()["active"]
                    assert (
                        await api.post("/api/jobs/daily/run?wait=false")
                    ).status_code == 409
                    assert (
                        await api.patch("/api/jobs/daily", json={"task": "busy"})
                    ).status_code == 409
                    assert (await api.delete("/api/jobs/daily")).status_code == 409
            finally:
                app.state.execution.semaphore.release()
            await app.state.jobs.drain()
            assert app.state.jobs.get("daily")["last_status"] == "completed"

        client.portal.call(exercise)


def test_empty_scheduler_accepts_dynamic_job_and_busy_edits_preserve_evidence(config):
    execution = setup(
        config,
        lambda request: (
            model_list(config)
            if request.url.path.endswith("/models")
            else completion(config, "Scheduled reply")
        ),
    )
    jobs = Jobs(config, execution, Channels(config, execution, {}))

    async def exercise():
        try:
            await jobs.start()
            assert jobs.scheduler.running
            await jobs.create(Job(**{**definition(), "enabled": True}))
            original = jobs.get("daily")["next_fire"]
            await jobs.pause("daily", True)
            # Manual runs are allowed while paused and do not advance cron.
            await execution.semaphore.acquire()
            queued = await jobs.trigger("daily")
            assert queued["active"]
            with pytest.raises(ValueError, match="already queued/running"):
                await jobs.trigger("daily")
            with pytest.raises(RuntimeError, match="queued/running"):
                await jobs.update("daily", {"task": "Changed"})
            with pytest.raises(RuntimeError, match="queued/running"):
                await jobs.delete("daily")
            execution.semaphore.release()
            await jobs.drain()
            last_run = jobs.get("daily")["last_run"]
            assert jobs.get("daily")["next_fire"] == original
            await jobs.update("daily", {"enabled": False})
            assert jobs.get("daily")["next_fire"] is None
            await jobs.delete("daily")
            assert execution.store.run(last_run)["status"] == "completed"
            assert (
                execution.store.transcript("evening")[-1]["content"]
                == "Scheduled reply"
            )
        finally:
            await jobs.stop_admission()
            await execution.close()
            await jobs.drain()

    asyncio.run(exercise())


def test_chat_tools_create_edit_trigger_same_session_and_scope_grants(config):
    config.enabled_tools = ["jobs"]
    config.profiles["interactive"].tools = [
        "jobs.list",
        "jobs.create",
        "jobs.update",
        "jobs.run",
        "jobs.pause",
        "jobs.delete",
    ]
    config.profiles["interactive"].effects = ["operate", "operate"]
    execution = setup(
        config,
        lambda request: (
            model_list(config)
            if request.url.path.endswith("/models")
            else completion(config, "Job done")
        ),
    )
    jobs = Jobs(config, execution, Channels(config, execution, {}))
    execution.registry.job_manager = jobs
    ctx = ToolContext(
        execution.store.session("s"), "interactive", config, execution.store, "chat"
    )

    async def dispatch(name, **args):
        return await execution.registry.dispatch("jobs." + name, json.dumps(args), ctx)

    async def exercise():
        try:
            await jobs.initialize()
            created = await dispatch(
                "create", id="chat-job", cron="0 19 * * *", task="Chat task"
            )
            assert not created.error, created.content
            assert jobs.get("chat-job")["bind_session"] == "s"
            assert not jobs.get("chat-job")["enabled"]
            assert (await dispatch("run", id="chat-job")).error
            assert not (
                await dispatch("update", id="chat-job", changes={"enabled": True})
            ).error
            assert (
                await dispatch(
                    "update", id="chat-job", changes={"profile": "maintenance"}
                )
            ).error
            assert not (
                await dispatch(
                    "update", id="chat-job", changes={"bind_session": "another"}
                )
            ).error
            await dispatch("update", id="chat-job", changes={"bind_session": "s"})
            await execution.semaphore.acquire()
            reply = await asyncio.wait_for(dispatch("run", id="chat-job"), timeout=2)
            assert not reply.error and jobs.states["chat-job"].active
            execution.semaphore.release()
            await jobs.drain()
            assert execution.store.transcript("s")[-1]["content"] == "Job done"
            assert not (await dispatch("pause", id="chat-job")).error
            await jobs.create(Job(**{**definition(), "id": "outside"}))
            assert "outside" in (await dispatch("list")).content
            config.profiles["interactive"].effects.clear()
            assert (await dispatch("run", id="outside")).error
            config.profiles["interactive"].effects.append("operate")
            assert "outside" in (await dispatch("list")).content
            assert not (await dispatch("delete", id="chat-job")).error
        finally:
            await jobs.stop_admission()
            await execution.close()
            await jobs.drain()

    asyncio.run(exercise())


def test_ui_create_edit_validation_and_delete_are_api_backed(config, mock_model):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        assert client.get("/settings/jobs/new").status_code == 200  # redirects to login
        assert (
            client.post("/settings/jobs", data={})
            .headers.get("content-type", "")
            .startswith("text/html")
        )
        client.post("/login", data={"token": "operator-test-token"})
        page = client.get("/settings/jobs")
        assert 'href="/settings/jobs/new"' in page.text
        assert client.get("/settings/jobs/new").status_code == 200
        values = {**definition(), "delivery_mode": "automatic"}
        invalid = client.post("/settings/jobs", data={**values, "cron": "bad"})
        assert invalid.status_code == 422 and "Reflect on {date}" in invalid.text
        assert client.get("/api/jobs").json() == []
        response = client.post("/settings/jobs", data=values, follow_redirects=False)
        assert response.status_code == 303, response.text
        edit = client.get("/settings/jobs/daily/edit")
        assert 'value="daily"' in edit.text and "Reflect on {date}" in edit.text
        response = client.post(
            "/settings/jobs/daily/edit",
            data={**values, "task": "Updated <script> text", "enabled": "on"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert client.get("/api/jobs/daily").json()["task"] == "Updated <script> text"
        assert (
            "Updated &lt;script&gt; text"
            in client.get("/settings/jobs/daily/edit").text
        )
        assert client.get("/settings/jobs/daily/delete").status_code == 200
        assert client.get("/api/jobs/daily").status_code == 200
        assert (
            client.post(
                "/settings/jobs/daily/delete", follow_redirects=False
            ).status_code
            == 303
        )
        assert client.get("/api/jobs/daily").status_code == 404


def test_runner_can_create_edit_and_trigger_current_session_job_without_deadlock(
    config,
):
    calls = []
    config.enabled_tools = ["jobs"]
    config.profiles["interactive"].tools = ["jobs.create", "jobs.update", "jobs.run"]
    config.profiles["interactive"].effects = ["operate", "operate"]

    def model(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        calls.append(json.loads(request.content))
        steps = [
            (
                "jobs.create",
                {"id": "from-chat", "cron": "0 19 * * *", "task": "Scheduled task"},
            ),
            ("jobs.update", {"id": "from-chat", "changes": {"enabled": True}}),
            ("jobs.run", {"id": "from-chat"}),
        ]
        if len(calls) <= len(steps):
            name, args = steps[len(calls) - 1]
            return completion(
                config,
                calls=[
                    {
                        "id": f"step-{len(calls)}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            )
        return completion(
            config, "Job queued" if len(calls) == 4 else "Scheduled reply"
        )

    execution = setup(config, model)
    jobs = Jobs(config, execution, Channels(config, execution, {}))
    execution.registry.job_manager = jobs

    async def exercise():
        try:
            await jobs.initialize()
            run = await asyncio.wait_for(
                execution.submit("s", "Schedule and trigger a task"), 5
            )
            assert run.status == "completed"
            await asyncio.wait_for(jobs.drain(), 5)
            assert jobs.get("from-chat")["last_status"] == "completed"
            assert execution.store.transcript("s")[-1]["content"] == "Scheduled reply"
            logs = execution.store._read(
                execution.store.root / f"runs/{run.id}.tools.jsonl"
            )
            assert [log["name"] for log in logs] == [
                "jobs.create",
                "jobs.update",
                "jobs.run",
            ]
            assert all(not log["result"]["error"] for log in logs)
        finally:
            await jobs.stop_admission()
            await execution.close()
            await jobs.drain()

    asyncio.run(exercise())


def test_scope_authorization_rechecked_under_mutation_lock(config):
    config.enabled_tools = ["jobs"]
    config.profiles["interactive"].tools = ["jobs.update"]
    config.profiles["interactive"].effects = ["operate"]
    execution = setup(config, lambda request: model_list(config))
    jobs = Jobs(config, execution, Channels(config, execution, {}))
    execution.registry.job_manager = jobs
    ctx = ToolContext(
        execution.store.session("s"), "interactive", config, execution.store, "chat"
    )

    async def exercise():
        try:
            await jobs.initialize()
            await jobs.create(Job(**{**definition(), "bind_session": "s"}))
            await jobs.lock.acquire()
            operator = asyncio.create_task(
                jobs.update("daily", {"bind_session": "other"})
            )
            model = asyncio.create_task(
                execution.registry.dispatch(
                    "jobs.update",
                    json.dumps({"id": "daily", "changes": {"task": "must not apply"}}),
                    ctx,
                )
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            config.profiles["interactive"].effects.clear()
            jobs.lock.release()
            await operator
            assert (await model).error
            assert jobs.job("daily").task == "Reflect on {date}"
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_cancelled_mutation_finishes_database_and_live_publication(config, monkeypatch):
    execution = setup(config, lambda request: model_list(config))
    jobs = Jobs(config, execution, Channels(config, execution, {}))
    entered, release = threading.Event(), threading.Event()
    save = jobs.definitions.save

    def slow_save(*args, **kwargs):
        save(*args, **kwargs)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(jobs.definitions, "save", slow_save)

    async def exercise():
        try:
            await jobs.initialize()
            mutation = asyncio.create_task(jobs.create(Job(**definition())))
            assert await asyncio.to_thread(entered.wait, 5)
            mutation.cancel()
            await asyncio.sleep(0)
            assert not mutation.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await mutation
            assert jobs.get("daily")["task"] == "Reflect on {date}"
            assert execution.store.job_state("daily")["id"] == "daily"
            assert jobs.definitions.load([])[0].id == "daily"
        finally:
            release.set()
            await execution.close()

    asyncio.run(exercise())
