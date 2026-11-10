"""Job tool narrowing: schema exposure, dispatch, saved evidence and profile drift."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from test_execution import call, completion, model_list, setup

from solakit.channels import Channels
from solakit.core.config import Job, Profile
from solakit.core.model import ModelClient
from solakit.core.schemas import Origin, Run, RunStatus, Session
from solakit.core.store import StorageCorruption
from solakit.jobs import Jobs
from solakit.main import create_app
from solakit.tools import ToolContext, ToolResult, tool


def job(**changes):
    return {
        "id": "narrow",
        "cron": "0 19 * * *",
        "profile": "interactive",
        "task": "A scheduled task",
        "bind_session": "ephemeral",
        "enabled": True,
        **changes,
    }


def grants(config):
    config.enabled_tools = ["runtime", "files"]
    config.profiles["interactive"].tools = ["runtime.status", "files.read"]


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        (None, ["runtime.status", "files.read"]),
        (["runtime.status"], ["runtime.status"]),
        ([], []),
    ],
)
def test_job_schema_exposure_and_immutable_run_inspection(config, selected, expected):
    grants(config)
    bodies = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        bodies.append(json.loads(request.content))
        return completion(config, "Announcement")

    app = create_app(
        config=config,
        model=ModelClient(config.model, transport=httpx.MockTransport(handler)),
    )
    with TestClient(app) as client:
        client.post("/login", data={"token": "operator-test-token"})
        assert client.post("/api/jobs", json=job(tools=selected)).status_code == 201
        response = client.post("/api/jobs/narrow/run")
        assert response.status_code == 200
        record = response.json()
        assert record["status"] == "completed"
        assert record["effective_tools"] == expected
        sent = bodies[0].get("tools", [])
        assert [s["function"]["name"] for s in sent] == expected
        if not expected:
            assert bodies[0]["tool_choice"] == "none"
        assert record["tool_schemas"] == sent
        path = config.paths.data / "runs" / f"{record['id']}.tool-schemas.json"
        assert (
            hashlib.sha256(path.read_bytes()).hexdigest()
            == record["tool_schema_sha256"]
        )
        with pytest.raises(FileExistsError):
            app.state.store.write_tool_schemas(record["id"], [])
        before = client.get("/api/prompt", params={"run_id": record["id"]}).json()
        assert before["tools"] == sent
        assert before["sha256"] == record["prompt_sha256"]
        # Replaying evidence must not consult current profile/schema definitions.
        config.profiles["interactive"].tools = []
        app.state.registry.tools.clear()
        config.paths.soul.write_text("Changed persona after this run")
        assert (
            client.get("/api/prompt", params={"run_id": record["id"]}).json() == before
        )
        assert (
            client.get(f"/settings/evidence/prompt?run_id={record['id']}").status_code
            == 200
        )
        assert (
            "Recorded prompt and tool schemas"
            in client.get(f"/settings/evidence/prompt?run_id={record['id']}").text
        )
        assert client.get("/api/prompt?run_id=missing").status_code == 404
        path.write_bytes(path.read_bytes() + b" ")
        with pytest.raises(StorageCorruption):
            app.state.store.run(record["id"])
        assert app.state.store.storage_health()["status"] == "degraded"


def test_save_rejects_outside_grants_and_revalidates_changed_profile(
    config, mock_model
):
    grants(config)
    config.profiles["limited"] = Profile(tools=["runtime.status"])
    (config.paths.fragments / "profiles/limited.md").write_text("Limited mode")
    with TestClient(create_app(config=config, model=mock_model)) as client:
        client.post("/login", data={"token": "operator-test-token"})
        for selected in (
            ["shell.exec"],
            ["missing"],
            ["runtime.status", "files.write"],
        ):
            response = client.post("/api/jobs", json=job(tools=selected))
            assert response.status_code == 422
            assert (
                "Job tools not granted by profile interactive"
                in response.json()["detail"]
            )
        assert client.get("/api/jobs").json() == []
        assert (
            client.post("/api/jobs", json=job(tools=["files.read"])).status_code == 201
        )
        assert (
            client.patch("/api/jobs/narrow", json={"profile": "limited"}).status_code
            == 422
        )
        assert client.get("/api/jobs/narrow").json()["profile"] == "interactive"
        changed = client.patch(
            "/api/jobs/narrow", json={"profile": "limited", "tools": None}
        )
        assert changed.status_code == 200
        assert changed.json()["tools"] is None
        assert changed.json()["effective_tools"] == ["runtime.status"]
        assert (
            client.patch("/api/jobs/narrow", json={"tools": []}).json()[
                "effective_tools"
            ]
            == []
        )
        for invalid in ("runtime.status", [None], [7], ["runtime.status"] * 257):
            assert (
                client.patch("/api/jobs/narrow", json={"tools": invalid}).status_code
                == 422
            )


def test_profile_trim_survives_restart_warns_and_shrinks_future_run(config, mock_model):
    grants(config)
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        client.post("/login", data={"token": "operator-test-token"})
        client.post("/api/jobs", json=job(tools=["runtime.status", "files.read"]))
    config.profiles["interactive"].tools = ["runtime.status"]
    bodies = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        bodies.append(json.loads(request.content))
        return completion(config, "Trimmed run")

    fresh = create_app(
        config=config,
        model=ModelClient(config.model, transport=httpx.MockTransport(handler)),
    )
    with TestClient(fresh) as client:
        client.post("/login", data={"token": "operator-test-token"})
        saved = client.get("/api/jobs/narrow").json()
        assert saved["tools"] == ["runtime.status", "files.read"]
        assert saved["effective_tools"] == ["runtime.status"]
        assert saved["unavailable_tools"] == ["files.read"]
        assert (
            "No longer granted by profile (excluded): files.read"
            in client.get("/settings/jobs").text
        )
        result = client.post("/api/jobs/narrow/run").json()
        assert result["effective_tools"] == ["runtime.status"]
        assert [t["function"]["name"] for t in bodies[0]["tools"]] == ["runtime.status"]
        # A subsequent save must explicitly repair stale selections.
        assert (
            client.patch("/api/jobs/narrow", json={"task": "Updated"}).status_code
            == 422
        )
        assert (
            client.patch(
                "/api/jobs/narrow", json={"tools": ["runtime.status"]}
            ).status_code
            == 200
        )


@pytest.mark.parametrize("selected", [[], ["allowed"]])
def test_unexposed_tool_calls_are_rejected_even_if_profile_grants_them(
    config, selected
):
    captured, invoked = [], []
    config.profiles["interactive"].repair_attempts = 0

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        captured.append(json.loads(request.content))
        return completion(config, calls=[call("forbidden", "{}")])

    execution = setup(config, handler)

    @tool(name="allowed", profiles=["interactive"])
    async def allowed(ctx: ToolContext):
        return "Allowed"

    @tool(name="forbidden", profiles=["interactive"])
    async def forbidden(ctx: ToolContext):
        invoked.append(True)
        return "Should never run"

    execution.registry.register(allowed)
    execution.registry.register(forbidden)
    config.profiles["interactive"].tools = ["*"]
    execution.registry.validate()
    manager = Jobs(config, execution, Channels(config, execution, {}))

    async def exercise():
        await manager.initialize()
        await manager.create(Job(**job(tools=selected)))
        run = await manager.run_now("narrow")
        assert run.status == "tool_error"
        assert not invoked
        assert [s["function"]["name"] for s in captured[0].get("tools", [])] == selected
        assert run.effective_tools == selected
        evidence = execution.store.run(run.id)
        assert evidence["tools"][0]["result"]["error"]
        assert "not permitted for this run" in evidence["tools"][0]["result"]["content"]
        await manager.stop_admission()
        await execution.close()

    asyncio.run(exercise())


def test_profile_capabilities_and_scopes_still_apply_to_selected_tool(config):
    invoked = []
    counter = 0

    def handler(request):
        nonlocal counter
        if request.url.path.endswith("/models"):
            return model_list(config)
        counter += 1
        if counter == 1:
            config.profiles["interactive"].effects.clear()
            return completion(config, calls=[call("write", "{}")])
        return completion(config, "Stopped")

    execution = setup(config, handler)

    @tool(name="write", profiles=["interactive"], effect="shell")
    async def write(ctx: ToolContext):
        invoked.append(ctx.profile)
        return "Unexpected"

    execution.registry.register(write)
    config.profiles["interactive"].tools = ["write"]
    config.profiles["interactive"].effects = ["shell"]
    execution.registry.validate()

    async def exercise():
        # Admission snapshots names, never a copy of profile authority.
        pending = await execution.enqueue("s", "Task", tools=["write", "unknown"])
        await pending.future
        assert not invoked
        assert pending.run.effective_tools == ["write"]
        assert execution.store.run(pending.run.id)["tools"][0]["result"]["error"]
        await execution.close()

    asyncio.run(exercise())


def test_cron_and_one_time_dispatch_share_job_policy(config):
    grants(config)
    seen = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        seen.append(json.loads(request.content).get("tools", []))
        return completion(config, "Scheduled")

    execution = setup(config, handler)
    clock = [datetime(2099, 1, 1, tzinfo=UTC)]
    manager = Jobs(
        config, execution, Channels(config, execution, {}), clock=lambda: clock[0]
    )

    async def exercise():
        await manager.initialize()
        await manager.create(Job(**job(cron="* * * * *", tools=[])))
        await manager.create(
            Job(
                **job(
                    id="once",
                    cron=None,
                    run_at=(clock[0] + timedelta(minutes=1)).isoformat(),
                    tools=["runtime.status"],
                )
            )
        )
        clock[0] += timedelta(minutes=1)
        await manager.tick()
        await manager.drain()
        assert sorted(len(tools) for tools in seen) == [0, 1]
        records = [
            r
            for r in execution.store.list_records("runs")
            if r["status"] == "completed"
        ]
        assert {tuple(r["effective_tools"]) for r in records} == {
            (),
            ("runtime.status",),
        }
        assert manager.states["once"].consumed
        await manager.stop_admission()
        await execution.close()

    asyncio.run(exercise())


def test_schema_snapshot_keeps_sensitive_named_property_definitions(config):
    execution = setup(
        config,
        lambda request: (
            model_list(config)
            if request.url.path.endswith("/models")
            else completion(config, "Done")
        ),
    )

    @tool(name="schema", profiles=["interactive"])
    async def schema(ctx: ToolContext, api_key: str):
        return ToolResult(content="unused")

    execution.registry.register(schema)
    config.profiles["interactive"].tools = ["schema"]

    async def exercise():
        run = await execution.submit("s", "Do not invoke tools", tools=["schema"])
        saved = execution.store.run(run.id)["tool_schemas"]
        assert (
            saved[0]["function"]["parameters"]["properties"]["api_key"]["type"]
            == "string"
        )
        await execution.close()

    asyncio.run(exercise())


def test_old_runs_do_not_invent_a_tool_snapshot(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        client.post("/login", data={"token": "operator-test-token"})
        app.state.store.create_session(Session(id="legacy"))
        app.state.store.save_run(
            Run(
                id="legacy",
                session="legacy",
                profile="interactive",
                origin=Origin(),
                status=RunStatus.completed,
            )
        )
        assert client.get("/api/prompt?run_id=legacy").status_code == 409
        assert (
            "Not recorded for this run"
            in client.get("/settings/evidence/runs/legacy").text
        )


def test_chat_job_tools_preserve_empty_and_null_and_report_invalid_names(
    config, mock_model
):
    from solakit.tools import ToolError
    from solakit.tools import jobs as chat_jobs

    grants(config)
    config.profiles["interactive"].effects = ["operate"]
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        session = app.state.store.create_session(Session(id="chat"))
        ctx = ToolContext(
            session,
            "interactive",
            config,
            app.state.store,
            "author",
            job_manager=app.state.jobs,
        )

        async def exercise():
            await chat_jobs.create(
                ctx, id="chat-job", cron="0 19 * * *", task="Announce", tools=[]
            )
            assert app.state.jobs.job("chat-job").tools == []
            await chat_jobs.update(ctx, "chat-job", changes={"tools": None})
            assert app.state.jobs.job("chat-job").tools is None
            with pytest.raises(ToolError, match="Job tools not granted"):
                await chat_jobs.update(
                    ctx, "chat-job", changes={"tools": ["shell.exec"]}
                )

        client.portal.call(exercise)


def test_new_config_seed_rejects_invalid_job_tools(config, mock_model):
    from solakit.jobs.schemas import JobToolsError

    grants(config)
    config.jobs = [Job(**job(tools=["shell.exec"]))]
    with pytest.raises(JobToolsError, match="Job tools not granted"):
        with TestClient(create_app(config=config, model=mock_model)):
            pass


def test_selected_tool_keeps_profile_filesystem_scopes(config, tmp_path):
    config.enabled_tools = ["files"]
    config.profiles["interactive"].tools = ["files.read"]
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("MUST NOT BE RETURNED")
    config.profiles["interactive"].read = "workspace"
    config.profiles["interactive"].workspace = [f"path:{allowed}"]
    calls = 0

    def handler(request):
        nonlocal calls
        if request.url.path.endswith("/models"):
            return model_list(config)
        calls += 1
        if calls == 1:
            return completion(
                config, calls=[call("files.read", json.dumps({"path": str(outside)}))]
            )
        return completion(config, "Access denied")

    app = create_app(
        config=config,
        model=ModelClient(config.model, transport=httpx.MockTransport(handler)),
    )
    with TestClient(app) as client:
        client.post("/login", data={"token": "operator-test-token"})
        assert (
            client.post("/api/jobs", json=job(tools=["files.read"])).status_code == 201
        )
        result = client.post("/api/jobs/narrow/run").json()
        assert result["tools"][0]["result"]["error"]
        assert "MUST NOT BE RETURNED" not in result["tools"][0]["result"]["content"]
        assert config.profiles["interactive"].workspace == [f"path:{allowed}"]


def test_queued_run_can_only_shrink_its_admission_snapshot(config):
    execution = setup(
        config,
        lambda request: (
            model_list(config)
            if request.url.path.endswith("/models")
            else completion(config, "Done")
        ),
    )
    for name in ("first", "second"):

        @tool(name=name, profiles=["interactive"])
        async def example(ctx: ToolContext):
            return "unused"

        execution.registry.register(example)
    config.profiles["interactive"].tools = ["first"]

    async def exercise():
        await execution.semaphore.acquire()
        pending = await execution.enqueue("s", "queued", tools=["first", "second"])
        assert pending.run.effective_tools == ["first"]
        config.profiles["interactive"].tools = ["second"]
        execution.semaphore.release()
        result = await pending.future
        assert result.effective_tools == []
        assert execution.store.run(result.id)["tool_schemas"] == []
        await execution.close()

    asyncio.run(exercise())


def test_job_tool_form_preserves_tri_state_and_rejects_invalid_selection(
    config, mock_model
):
    grants(config)
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        client.post("/login", data={"token": "operator-test-token"})
        fields = {
            "id": "ui-tools",
            "schedule_type": "cron",
            "cron": "0 19 * * *",
            "timezone": "America/Denver",
            "profile": "interactive",
            "task": "Announce",
            "bind_session": "ephemeral",
        }
        assert (
            client.post(
                "/settings/jobs",
                data={**fields, "tools_mode": "none"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert client.get("/api/jobs/ui-tools").json()["tools"] == []
        selected = client.post(
            "/settings/jobs/ui-tools/edit",
            data={**fields, "tools_mode": "selected", "tools": "runtime.status"},
            follow_redirects=False,
        )
        assert selected.status_code == 303
        assert client.get("/api/jobs/ui-tools").json()["tools"] == ["runtime.status"]
        # An old tab submitting the former form must not restore inheritance.
        old_form = client.post(
            "/settings/jobs/ui-tools/edit", data=fields, follow_redirects=False
        )
        assert old_form.status_code == 303
        assert (
            client.post("/settings/jobs/missing/edit", data=fields).status_code == 404
        )
        assert client.get("/api/jobs/ui-tools").json()["tools"] == ["runtime.status"]
        invalid = client.post(
            "/settings/jobs/ui-tools/edit",
            data={**fields, "tools_mode": "selected", "tools": "shell.exec"},
        )
        assert invalid.status_code == 422
        assert (
            "Job tools not granted by profile interactive: tool shell.exec"
            in invalid.text
        )
        assert 'value="shell.exec" checked' in invalid.text
        restored = client.post(
            "/settings/jobs/ui-tools/edit",
            data={**fields, "tools_mode": "inherit"},
            follow_redirects=False,
        )
        assert restored.status_code == 303
        assert client.get("/api/jobs/ui-tools").json()["tools"] is None


def test_recorded_schema_ui_masks_values_without_losing_property_definitions(
    config, mock_model
):
    import html

    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        client.post("/login", data={"token": "operator-test-token"})

        @tool(name="secret-parameter", profiles=["interactive"])
        async def parameter(ctx: ToolContext, api_key: str = "operator-test-token"):
            return "unused"

        app.state.registry.register(parameter)
        config.profiles["interactive"].tools = ["secret-parameter"]
        client.post("/api/jobs", json=job(tools=["secret-parameter"]))
        record = client.post("/api/jobs/narrow/run").json()
        page = client.get("/settings/evidence/prompt", params={"run_id": record["id"]})
        assert page.status_code == 200
        assert "operator-test-token" not in page.text
        assert '"api_key": {' in html.unescape(page.text)
        assert '"default": "[redacted]"' in html.unescape(page.text)
