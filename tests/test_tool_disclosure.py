"""Conditional schema exposure preserves grants, evidence and execution budgets."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from test_execution import call, completion, model_list, setup
from test_job_tools import grants, job

from solakit.core.config import Job, Profile
from solakit.core.model import ModelClient
from solakit.core.prompt import read_prompt
from solakit.core.store import StorageCorruption
from solakit.main import create_app
from solakit.tools import Manifest, Registry, ToolContext, ToolResult, tool
from solakit.tools.disclosure import META_NAMES


def names(schemas):
    return [s["function"]["name"] for s in schemas]


def rig(config, responses, *, mode="deferred", max_steps=4, resident=None):
    bodies, invoked = [], []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        bodies.append(json.loads(request.content))
        index = min(len(bodies) - 1, len(responses) - 1)
        response = responses[index]
        if isinstance(response, str):
            return completion(config, response)
        return completion(config, calls=response)

    execution = setup(config, handler)

    @tool(
        name="alpha.read",
        profiles=["*"],
        summary="Read alpha facts",
        keywords=("unread", "inbox"),
    )
    async def alpha(ctx: ToolContext, text: str = "hello") -> ToolResult:
        """Read an alpha resource using its stable identity."""
        invoked.append(text)
        return ToolResult(content=text)

    @tool(name="beta.read", profiles=["*"], summary="Read beta archive")
    async def beta(ctx: ToolContext) -> ToolResult:
        """Read beta archive."""
        return ToolResult(content="beta")

    execution.registry.register(alpha)
    execution.registry.register(beta)
    profile = config.profiles["interactive"]
    profile.tools = ["alpha.read", "beta.read"]
    profile.tool_resident = resident or []
    profile.tool_disclosure = mode
    profile.max_steps = max_steps
    execution.registry.validate()
    return execution, bodies, invoked


def test_eager_golden_schema_and_prompt(config):
    execution, bodies, _ = rig(config, ["Done"], mode="eager")
    at = datetime(2026, 11, 5, tzinfo=UTC)
    plain = read_prompt(config, profile="interactive", session="s", origin="api", at=at)
    view = execution.registry.view("interactive")
    assert view.index_text == ""
    assert json.dumps(view.schemas()) == json.dumps(
        execution.registry.schemas("interactive")
    )
    assert (
        read_prompt(
            config,
            profile="interactive",
            session="s",
            origin="api",
            at=at,
            tool_index=view.index_text,
        )
        == plain
    )

    async def exercise():
        run = await execution.submit("s", "Hello")
        assert run.status == "completed"
        assert names(bodies[0]["tools"]) == ["alpha.read", "beta.read"]
        assert not set(META_NAMES) & set(names(bodies[0]["tools"]))
        assert "<tool-index>" not in bodies[0]["messages"][0]["content"]
        await execution.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "discovery",
    [
        call("tools.search", '{"query":"unread inbox"}'),
        call("tools.search", '{"query":"alpha_read"}'),
        call("tools.load", '{"names":["alpha.read"]}'),
    ],
)
def test_search_load_activation_and_frozen_step_evidence(config, discovery):
    execution, bodies, invoked = rig(
        config,
        [[discovery], [call("alpha.read", '{"text":"executed"}')], "Done"],
        max_steps=2,
    )

    async def exercise():
        run = await execution.submit("s", "Read alpha", tools=["alpha.read"])
        assert run.status == "completed"
        assert invoked == ["executed"]
        assert names(bodies[0]["tools"]) == list(META_NAMES)
        assert names(bodies[1]["tools"]) == [*META_NAMES, "alpha.read"]
        prompt = bodies[0]["messages"][0]["content"]
        assert "alpha.read — Read alpha facts" in prompt
        assert "beta.read" not in prompt
        assert run.steps == 3
        assert run.tool_view["disclosure_steps"] == 1
        assert run.tool_view["work_steps"] == 2
        assert run.tool_view["activations"] == [
            {"step": 1, "activated": ["alpha.read"]}
        ]
        record = execution.store.run(run.id)
        assert names(
            [
                record["tool_catalog"][n]
                for n in run.tool_view["schema_steps"][1]["visible"]
            ]
        ) == names(bodies[1]["tools"])
        execution.registry.tools.clear()
        assert execution.store.run(run.id)["tool_catalog"] == record["tool_catalog"]
        path = config.paths.data / "runs" / f"{run.id}.tool-catalog.json"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == run.tool_catalog_sha256
        path.write_bytes(path.read_bytes() + b" ")
        with pytest.raises(StorageCorruption):
            execution.store.run(run.id)
        await execution.close()

    asyncio.run(exercise())


def test_direct_inactive_interception_does_not_execute_or_consume_repair(config):
    execution, bodies, invoked = rig(
        config,
        [
            [call("alpha.read", "not even JSON")],
            [call("alpha.read", '{"text":"once"}')],
            "Done",
        ],
        max_steps=2,
    )
    config.profiles["interactive"].repair_attempts = 0

    async def exercise():
        run = await execution.submit("s", "Read")
        assert run.status == "completed"
        assert invoked == ["once"]
        notice = json.loads(bodies[1]["messages"][-1]["content"])
        assert notice["error"] and not notice["repairable"]
        assert "It was not executed" in notice["content"]
        assert run.tool_view["disclosure_steps"] == 1
        await execution.close()

    asyncio.run(exercise())


def test_load_then_real_call_in_same_group_is_work(config):
    calls = [
        call("tools.load", '{"names":["alpha.read"]}'),
        {**call("alpha.read", "{}"), "id": "call2"},
    ]
    execution, _, invoked = rig(config, [calls, "Done"], max_steps=2)

    async def exercise():
        run = await execution.submit("s", "Read")
        assert run.status == "completed"
        assert invoked == ["hello"]
        assert run.tool_view["disclosure_steps"] == 0
        assert run.tool_view["work_steps"] == 2
        await execution.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "arguments",
    [
        '{"names":["alpha.read","beta.read","missing"]}',
        '{"names":["alpha.read","alpha.read"]}',
    ],
)
def test_load_partial_success_rejection_and_already_loaded(config, arguments):
    execution, bodies, _ = rig(
        config,
        [
            [call("tools.load", arguments)],
            [call("tools.load", '{"names":["alpha.read"]}')],
            "Done",
        ],
    )

    async def exercise():
        run = await execution.submit("s", "Load", tools=["alpha.read"])
        assert run.status == "completed"
        first = json.loads(json.loads(bodies[1]["messages"][-1]["content"])["content"])
        assert first["loaded"] == ["alpha.read"]
        if first["rejected"]:
            assert first["rejected"] == [
                {"name": "beta.read", "reason": "not granted to this run"},
                {"name": "missing", "reason": "unknown tool"},
            ]
        second = json.loads(json.loads(bodies[2]["messages"][-1]["content"])["content"])
        assert second["already_loaded"] == ["alpha.read"]
        assert run.tool_view["activations"] == [
            {"step": 1, "activated": ["alpha.read"]}
        ]
        await execution.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "discovery",
    [
        call("tools.search", '{"query":"absent"}'),
        call("tools.search", '{"query":"alpha","limit":6}'),
        call("tools.load", '{"names":[]}'),
        call("tools.load", '{"names":["missing"]}'),
    ],
)
def test_disclosure_cap_is_separate_from_work_and_repairs(config, discovery):
    execution, bodies, _ = rig(config, [[discovery]], max_steps=1)
    config.profiles["interactive"].repair_attempts = 0

    async def exercise():
        run = await execution.submit("s", "Try discovery")
        assert run.status == "tool_error"
        assert run.error == "tool loading did not converge"
        assert len(bodies) == 8
        assert run.tool_view["disclosure_steps"] == 8
        assert run.tool_view["work_steps"] == 0
        await execution.close()

    asyncio.run(exercise())


def test_ungranted_direct_call_keeps_repair_budget(config):
    execution, _, invoked = rig(config, [[call("beta.read", "{}")]], max_steps=4)
    config.profiles["interactive"].repair_attempts = 0

    async def exercise():
        run = await execution.submit("s", "No", tools=["alpha.read"])
        assert run.status == "tool_error"
        assert not invoked
        assert not run.tool_view["activations"]
        assert run.tool_view["work_steps"] == 1
        await execution.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("profile_mode", "job_mode", "residents", "expected_mode", "expected_residents"),
    [
        ("eager", "inherit", None, "eager", ["runtime.status"]),
        ("eager", "deferred", [], "deferred", []),
        ("deferred", "eager", None, "eager", ["runtime.status"]),
        ("deferred", "inherit", None, "deferred", ["runtime.status"]),
        ("deferred", "inherit", ["files.read"], "deferred", ["files.read"]),
    ],
)
def test_job_modes_residents_and_initial_prompt_inspection(
    config, auth, profile_mode, job_mode, residents, expected_mode, expected_residents
):
    grants(config)
    cfg = config.profiles["interactive"]
    cfg.tool_disclosure = profile_mode
    cfg.tool_resident = ["runtime.status"]
    bodies = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        bodies.append(json.loads(request.content))
        return completion(config, "Done")

    app = create_app(
        config=config,
        model=ModelClient(config.model, transport=httpx.MockTransport(handler)),
    )
    with TestClient(app) as client:
        client.post("/login", data={"token": "operator-test-token"})
        response = client.post(
            "/api/jobs",
            headers=auth,
            json=job(tool_disclosure=job_mode, tool_resident=residents),
        )
        assert response.status_code == 201
        preview = client.get("/api/prompt?job=narrow", headers=auth).json()
        assert preview["tool_view"]["mode"] == expected_mode
        assert preview["tool_view"]["resident"] == expected_residents
        record = client.post("/api/jobs/narrow/run", headers=auth).json()
        assert record["status"] == "completed"
        assert names(bodies[0]["tools"]) == names(preview["tools"])
        assert client.get("/prompt?job=narrow").status_code == 200
        assert client.get("/tools").status_code == 200
        assert client.get(f"/runs/{record['id']}").status_code == 200
        historical = client.get(
            "/api/prompt", params={"run_id": record["id"]}, headers=auth
        ).json()
        assert historical["tool_view"]["mode"] == expected_mode
        if expected_mode == "deferred":
            assert (
                client.get(
                    "/api/prompt",
                    params={"run_id": record["id"], "step": 1},
                    headers=auth,
                ).json()["tools"]
                == preview["tools"]
            )
        assert client.get("/api/prompt?job=absent", headers=auth).status_code == 404
        assert client.get("/api/prompt?profile=absent", headers=auth).status_code == 422


@pytest.mark.parametrize(
    "residents",
    [
        ["missing"],
        ["runtime.status", "runtime.status"],
        ["tools.search"],
        ["files.read"],
    ],
)
def test_job_resident_validation(config, auth, residents):
    grants(config)
    app = create_app(config=config)
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs",
            headers=auth,
            json=job(tools=["runtime.status"], tool_resident=residents),
        )
        assert response.status_code == 422
        assert "resident" in response.json()["detail"]


def test_inherited_residents_intersect_job_ceiling_and_profile_drift(config, auth):
    grants(config)
    cfg = config.profiles["interactive"]
    cfg.tool_disclosure = "deferred"
    cfg.tool_resident = ["runtime.status"]
    with TestClient(create_app(config=config)) as client:
        assert (
            client.post(
                "/api/jobs", headers=auth, json=job(tools=["files.read"])
            ).status_code
            == 201
        )
        assert (
            client.get("/api/prompt?job=narrow", headers=auth).json()["tool_view"][
                "resident"
            ]
            == []
        )
        assert (
            client.patch(
                "/api/jobs/narrow", headers=auth, json={"tool_resident": ["files.read"]}
            ).status_code
            == 200
        )
    cfg.tools = ["runtime.status"]
    with TestClient(create_app(config=config)) as client:
        record = client.get("/api/jobs/narrow", headers=auth).json()
        assert record["effective_tools"] == []
        assert record["unavailable_resident"] == ["files.read"]
        assert names(
            client.get("/api/prompt?job=narrow", headers=auth).json()["tools"]
        ) == list(META_NAMES)


@pytest.mark.parametrize(
    "residents",
    [["missing"], ["tools.load"], ["runtime.status", "runtime.status"], ["files.read"]],
)
def test_profile_resident_validation(config, residents):
    config.enabled_tools = ["runtime", "files"]
    config.profiles["interactive"].tools = ["runtime.status"]
    config.profiles["interactive"].tool_resident = residents
    with pytest.raises(ValueError, match="Profile interactive.*resident"):
        Registry(config)


def test_reserved_namespace_and_metadata_validation(config):
    registry = Registry(config)
    for name in META_NAMES:

        @tool(name=name, profiles=["*"])
        async def forbidden(ctx: ToolContext):
            return "no"

        with pytest.raises(ValueError, match="reserved"):
            registry.register(forbidden)

    async def function(ctx: ToolContext):
        """Fallback first line.

        Full multiline docs stay in schemas.
        """

    registry.register(function, Manifest("fallback", ("*",), (), None))
    assert registry.summary("fallback") == "Fallback first line."
    with pytest.raises(ValueError, match="summary"):
        registry.register(
            function, Manifest("invalid", ("*",), (), None, summary="too\nlong")
        )
    for model, data in [
        (Profile, {"tool_disclosure": "bad"}),
        (Job, job(tool_disclosure="bad")),
    ]:
        with pytest.raises(ValueError):
            model.model_validate(data)


def test_index_order_hash_and_diagnostics(config):
    execution, _, _ = rig(config, ["Done"], resident=["beta.read", "alpha.read"])
    view = execution.registry.view("interactive")
    assert view == execution.registry.view("interactive")
    assert view.index_sha256 == hashlib.sha256(view.index_text.encode()).hexdigest()
    assert names(view.schemas()) == [*META_NAMES, "beta.read", "alpha.read"]
    assert view.index_text.index("alpha:") < view.index_text.index("beta:")
    diagnostics = view.diagnostics(10)
    assert diagnostics["warnings"]
    assert diagnostics["resident_count"] == 2
    assert diagnostics["deferred_count"] == 0
    assert diagnostics["effective_schema_bytes"] > 0
    assert "tools.search" not in execution.registry.catalog()["profiles"]["interactive"]
    assert not set(META_NAMES) & set(execution.registry.tools)


@pytest.mark.parametrize("enabled", [False, True])
def test_activation_rechecks_current_visible_schema_budget(
    config, monkeypatch, enabled
):
    execution, bodies, _ = rig(
        config, [[call("tools.load", '{"names":["beta.read"]}')], "Done"]
    )
    execution.registry.tools["beta.read"].function.__doc__ = (
        "A large schema. " + "detail " * 3000
    )
    cfg = config.profiles["interactive"]
    cfg.context_budget = 9000
    cfg.mid_run_compaction = enabled
    compacted = []

    async def compact(run, session, trigger, step):
        compacted.append((trigger, step))
        # Even an empty compacted transcript cannot make the loaded schema fit.

    monkeypatch.setattr(execution, "_compact_run", compact)

    async def exercise():
        run = await execution.submit("s", "Load")
        assert run.status == "context_limit"
        assert len(bodies) == 1  # Initial deferred request fits.
        assert names(bodies[0]["tools"]) == list(META_NAMES)
        assert compacted == ([("mid_run", 1)] if enabled else [])
        if enabled:
            assert "Compacted request still exceeds" in run.error
        assert run.tool_view["activations"] == [{"step": 1, "activated": ["beta.read"]}]
        await execution.close()

    asyncio.run(exercise())


def test_eager_large_schemas_use_existing_context_limit(config, monkeypatch):
    execution, bodies, _ = rig(config, ["Done"], mode="eager")
    execution.registry.tools["beta.read"].function.__doc__ = (
        "A large schema. " + "detail " * 3000
    )
    config.profiles["interactive"].context_budget = 9000
    compacted = []

    async def compact(run, session, trigger, step):
        compacted.append((trigger, step))

    monkeypatch.setattr(execution, "_compact_run", compact)

    async def exercise():
        run = await execution.submit("s", "Read")
        assert run.status == "context_limit"
        assert not bodies
        assert compacted == [("before_turn", 0)]
        await execution.close()

    asyncio.run(exercise())


def test_streaming_does_not_replace_loaded_names_with_transcript(config, monkeypatch):
    execution, bodies, invoked = rig(
        config,
        [
            [call("tools.load", '{"names":["alpha.read"]}')],
            [call("alpha.read", "{}")],
            "Done",
        ],
    )
    original = execution.model.complete

    async def complete(messages, tools, on_text=None, **kwargs):
        # Exercise preview baselines without constructing a network SSE fixture.
        if on_text:
            await on_text("Preview")
        return await original(messages, tools)

    monkeypatch.setattr(execution.model, "complete", complete)

    async def exercise():
        run = await execution.submit("s", "Read", stream=True)
        assert run.status == "completed"
        assert invoked == ["hello"]
        assert names(bodies[2]["tools"]) == [*META_NAMES, "alpha.read"]
        await execution.close()

    asyncio.run(exercise())


def test_activation_does_not_bypass_capability_check(config):
    execution, bodies, _ = rig(
        config,
        [
            [call("tools.load", '{"names":["alpha.write"]}')],
            [call("alpha.write", "{}")],
            "Done",
        ],
    )
    invoked = []

    @tool(name="alpha.write", profiles=["*"], effect="send")
    async def write(ctx: ToolContext):
        invoked.append(True)
        return "written"

    execution.registry.register(write)
    cfg = config.profiles["interactive"]
    cfg.tools = ["alpha.write"]
    cfg.effects = ["send"]
    original = execution.model.complete

    async def revoke(messages, schemas, **kwargs):
        answer = await original(messages, schemas, **kwargs)
        if len(bodies) == 2:
            cfg.effects = []
        return answer

    execution.model.complete = revoke

    async def exercise():
        run = await execution.submit("s", "Write")
        assert run.status == "completed"  # Model can report the tool failure honestly.
        assert not invoked
        failure = json.loads(bodies[2]["messages"][-1]["content"])
        assert failure["error"] and not failure["repairable"]
        assert names(bodies[1]["tools"]) == [*META_NAMES, "alpha.write"]
        await execution.close()

    asyncio.run(exercise())


def test_child_delegation_uses_child_profile_and_next_session_run_is_fresh(config):
    from test_delegation import setup as delegation_setup

    execution, _, captured = delegation_setup(config)
    config.profiles["interactive"].tool_disclosure = "deferred"
    config.profiles["interactive"].tool_resident = ["delegate.run"]

    async def exercise():
        run = await execution.submit(
            "parent",
            "Delegate",
            tool_disclosure="deferred",
            tool_resident=["delegate.run"],
        )
        assert run.status == "completed"
        child = next(
            record
            for record in execution.store.list_runs()
            if record["origin"]["channel"] == "delegate"
        )
        assert child["tool_view"]["mode"] == "eager"
        child_body = next(
            body
            for body in captured
            if "profile: research\n" in body["messages"][0]["content"]
        )
        assert not set(META_NAMES) & set(names(child_body.get("tools", [])))
        await execution.close()

    asyncio.run(exercise())


def test_job_override_does_not_persist_to_next_interactive_turn(config):
    execution, bodies, _ = rig(config, ["Done"], mode="eager")

    async def exercise():
        first = await execution.submit(
            "s", "Deferred task", tool_disclosure="deferred", tool_resident=[]
        )
        second = await execution.submit("s", "Interactive task")
        assert first.tool_view["mode"] == "deferred"
        assert second.tool_view["mode"] == "eager"
        assert names(bodies[0]["tools"]) == list(META_NAMES)
        assert names(bodies[1]["tools"]) == ["alpha.read", "beta.read"]
        await execution.close()

    asyncio.run(exercise())


def test_job_disclosure_form_roundtrip_validation_and_profile_update(config, auth):
    grants(config)
    config.profiles["status"] = Profile(tools=["runtime.status"])
    (config.paths.fragments / "profiles/status.md").write_text("Status")
    with TestClient(create_app(config=config)) as client:
        client.post("/login", data={"token": "operator-test-token"})
        payload = {
            "id": "form",
            "cron": "0 0 * * *",
            "timezone": "America/Denver",
            "profile": "interactive",
            "task": "Announce",
            "bind_session": "ephemeral",
            "tool_disclosure": "deferred",
            "resident_mode": "selected",
            "tool_resident": "files.read",
            "tools_mode": "inherit",
        }
        response = client.post("/operations/jobs", data=payload, follow_redirects=False)
        assert response.status_code == 303
        record = client.get("/api/jobs/form", headers=auth).json()
        assert record["tool_disclosure"] == "deferred"
        assert record["tool_resident"] == ["files.read"]
        page = client.get("/operations/jobs/form/edit")
        assert 'value="files.read"' in page.text
        assert 'value="deferred" selected' in page.text
        invalid = client.patch(
            "/api/jobs/form", headers=auth, json={"profile": "status"}
        )
        assert invalid.status_code == 422
        assert (
            client.get("/api/jobs/form", headers=auth).json()["profile"]
            == "interactive"
        )
        payload.update(resident_mode="selected", tool_resident="", profile="status")
        assert (
            client.post(
                "/operations/jobs/form/edit", data=payload, follow_redirects=False
            ).status_code
            == 303
        )
        assert client.get("/api/jobs/form", headers=auth).json()["tool_resident"] == []
        payload["resident_mode"] = "inherit"
        assert (
            client.post(
                "/operations/jobs/form/edit", data=payload, follow_redirects=False
            ).status_code
            == 303
        )
        assert (
            client.get("/api/jobs/form", headers=auth).json()["tool_resident"] is None
        )


def test_job_subsession_and_missed_run_use_same_disclosure_view(config):
    from solakit.channels import Channels
    from solakit.jobs import Jobs

    grants(config)
    bodies = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        bodies.append(json.loads(request.content))
        return completion(config, "Done")

    execution = setup(config, handler)
    clock = datetime(2099, 1, 1, tzinfo=UTC)
    manager = Jobs(
        config, execution, Channels(config, execution, {}), clock=lambda: clock
    )

    async def exercise():
        await manager.initialize()
        definition = Job(
            **job(
                bind_session="s",
                sub_session=True,
                tools=["runtime.status"],
                tool_disclosure="deferred",
                tool_resident=["runtime.status"],
            )
        )
        await manager.create(definition)
        run = await manager.run_now("narrow")
        assert run.status == "completed"
        assert run.session != "s"
        assert run.tool_view["mode"] == "deferred"
        assert names(bodies[0]["tools"]) == [*META_NAMES, "runtime.status"]
        await manager._missed(definition, manager.states["narrow"], clock, "Skipped")
        missed = execution.store.run(manager.states["narrow"].last_run)
        assert missed["status"] == "missed"
        assert missed["effective_tools"] == ["runtime.status"]
        assert missed["tool_view"]["mode"] == "deferred"
        assert missed["tool_view"]["resident"] == ["runtime.status"]
        await manager.stop_admission()
        await execution.close()

    asyncio.run(exercise())


def test_check_reads_stored_jobs_without_mutating_database(config, monkeypatch, capsys):
    from solakit.channels import Channels
    from solakit.cli import main
    from solakit.jobs import Jobs

    grants(config)
    execution = setup(config, lambda request: model_list(config))
    manager = Jobs(config, execution, Channels(config, execution, {}))

    async def create():
        await manager.initialize()
        await manager.create(Job(**job(tool_disclosure="deferred", tool_resident=[])))
        await manager.stop_admission()
        await execution.close()

    asyncio.run(create())
    path = config.paths.data / "app.db"
    before = path.read_bytes()
    monkeypatch.setattr("solakit.core.startup.load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["sola", "check"])
    main()
    output = capsys.readouterr().out
    assert "Tool disclosure job narrow:" in output
    assert '"mode": "deferred"' in output
    assert path.read_bytes() == before
    path.write_bytes(b"broken database")
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert "cannot inspect stored job definitions" in capsys.readouterr().err
