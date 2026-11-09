"""Tool-surface simplification: jobs.schedule, deprecation, auto disclosure,
tool_json and next-step hints (TOOL-SURFACE-SIMPLIFICATION-PLAN)."""

import asyncio
import json

import pytest

from solakit.core.config import Config, Profile
from solakit.tools import Registry, ToolResult, render_for_model, tool_json
from solakit.tools.jobs import parse_when


def test_parse_when():
    assert parse_when("0 8 * * 1-5") == ("0 8 * * 1-5", None)
    assert parse_when("2026-12-01T09:00") == (None, "2026-12-01T09:00")
    assert parse_when("2026-12-01 09:00") == (None, "2026-12-01 09:00")
    for bad in ("tomorrow", "2026-12-01", "0 8 * *"):
        with pytest.raises(Exception, match="when|time"):
            parse_when(bad)


def test_deprecated_tools_need_explicit_grants():
    config = Config(
        enabled_tools=["jobs"],
        profiles={
            "interactive": Profile(tools=["jobs.*"], effects=["operate"]),
            "legacy": Profile(tools=["jobs.create"], effects=["operate"]),
        },
    )
    registry = Registry(config)
    assert "jobs.create" not in registry.tool_names("interactive")
    assert "jobs.schedule" in registry.tool_names("interactive")
    assert registry.tool_names("legacy") == ["jobs.create"]
    assert registry.deprecation_warnings() == [
        "Profile legacy grants deprecated jobs.create; use jobs.schedule"
    ]
    schema = registry.tools["jobs.schedule"].schema()["function"]["parameters"]
    assert set(schema["properties"]) == {"id", "task", "when", "enabled"}
    assert len(json.dumps(registry.tools["jobs.schedule"].schema())) < 1500


def test_schedule_validation_message_names_the_problem(config):
    from types import SimpleNamespace

    from solakit.core.schemas import Session
    from solakit.core.store import Store
    from solakit.tools import ToolContext

    config.enabled_tools = ["jobs"]
    config.profiles["interactive"] = Profile(tools=["jobs.*"], effects=["operate"])
    registry = Registry(config)
    store = Store(config.paths.data)
    store.create_session(Session(id="s"))
    ctx = ToolContext(store.session("s"), "interactive", config, store, "r")
    created = []

    async def create(job, authorize=None):
        authorize(job)
        created.append(job)
        return {"created": job.id, "cron": job.cron, "run_at": job.run_at}

    registry.job_manager = SimpleNamespace(create=create)
    result = asyncio.run(
        registry.dispatch(
            "jobs.schedule",
            json.dumps({"id": "x", "task": "Use {weird}", "when": "0 8 * * *"}),
            ctx,
        )
    )
    assert result.error and "Unsupported job task placeholder" in result.content
    result = asyncio.run(
        registry.dispatch(
            "jobs.schedule",
            json.dumps({"id": "rent", "task": "Pay rent", "when": "2026-12-01T09:00"}),
            ctx,
        )
    )
    assert not result.error and created[0].run_at.startswith("2026-12-01T16:00")
    assert result.content.startswith('<external-content source="jobs"')


def test_auto_disclosure_is_a_pure_function_of_catalog_bytes():
    modules = ["files", "math", "web", "datetime", "github", "wiki", "feeds"]
    small = Config(
        enabled_tools=modules,
        profiles={"interactive": Profile(tools=["math.eval"], tool_disclosure="auto")},
    )
    assert Registry(small).view("interactive").mode == "eager"
    large = Config(
        enabled_tools=modules,
        profiles={
            "interactive": Profile(
                tools=["*"],
                write="workspace",
                tool_disclosure="auto",
                tool_auto_threshold=2000,
            )
        },
    )
    registry = Registry(large)
    first, second = registry.view("interactive"), registry.view("interactive")
    assert first.mode == "deferred"
    assert first.index_text == second.index_text
    assert first.schemas() == second.schemas()
    # 20% of a small byte budget also forces deferral.
    large.profiles["interactive"].tool_auto_threshold = 10**9
    large.profiles["interactive"].context_budget = 5000
    assert Registry(large).view("interactive").mode == "deferred"


def test_tool_json_and_next_hint_rendering(config):
    from solakit.core.schemas import Session
    from solakit.core.store import Store
    from solakit.tools import ToolContext

    store = Store(config.paths.data)
    store.create_session(Session(id="s"))
    ctx = ToolContext(store.session("s"), "interactive", config, store, "r")
    plain = tool_json(ctx, {"a": "<b>"})
    assert json.loads(plain.content) == {"a": "<b>"}
    wrapped = tool_json(ctx, {"a": "<b>"}, "x")
    assert wrapped.content.splitlines()[1] == '{"a": "\\u003cb\\u003e"}'
    result = ToolResult(content="hits")
    result._meta["next"] = "files.read(path=...)"
    assert render_for_model(result, "text").endswith("\nNext: files.read(path=...)")
    assert "Next:" not in render_for_model(result)


def test_resident_suggestions_are_advisory_and_stable(config):
    from datetime import UTC, datetime

    from solakit.core.residents import suggest
    from solakit.core.schemas import Origin, Run
    from solakit.core.store import Store

    config.enabled_tools = ["math", "datetime"]
    config.profiles["interactive"] = Profile(
        tools=["*"], tool_resident=["datetime.weekday"]
    )
    registry = Registry(config)
    store = Store(config.paths.data)
    for index in range(10):
        run = Run(session="s", profile="interactive", origin=Origin())
        store.save_run(run)
        store.log_tool(run.id, {"name": "math.eval", "result": {"error": False}})
        if index < 2:
            store.log_tool(
                run.id, {"name": "datetime.add_days", "result": {"error": False}}
            )
        if index == 0:
            store.log_tool(
                run.id, {"name": "datetime.weekday", "result": {"error": False}}
            )
    first = suggest(config, registry, "interactive", min_runs=5)
    assert first["runs"] == 10 and first["enough_evidence"]
    assert first["proposed"] == ["datetime.weekday", "math.eval"]
    assert first["added"] == ["math.eval"] and first["removed"] == []
    # Hysteresis: 10% keeps a resident, add_days at 20% is not proposed.
    assert "datetime.add_days" not in first["proposed"]
    assert suggest(config, registry, "interactive", min_runs=5) == first
    assert config.profiles["interactive"].tool_resident == ["datetime.weekday"]
    assert not suggest(config, registry, "interactive", min_runs=50)["enough_evidence"]
    assert datetime.now(UTC)
