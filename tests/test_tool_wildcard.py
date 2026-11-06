"""Wildcard grants share schema, catalog, dispatch and capability enforcement."""

import asyncio
import json
import sys
from types import ModuleType

import pytest

from solakit.core.config import Profile
from solakit.core.schemas import Session
from solakit.core.store import Store
from solakit.tools import Registry, ToolContext, ToolResult, tool


@pytest.fixture
def wildcard(config, monkeypatch):
    module = ModuleType("wildcard_fixture.tools")

    @tool(name="fixture.any", profiles=["*"])
    async def any_profile(ctx: ToolContext) -> ToolResult:
        return ToolResult(content="available")

    @tool(name="fixture.private", profiles=["maintenance"])
    async def private(ctx: ToolContext) -> ToolResult:
        return ToolResult(content="maintenance only")

    @tool(name="fixture.write", profiles=["*"], writes=["filesystem"], effect="push")
    async def write(ctx: ToolContext) -> ToolResult:
        return ToolResult(content="written")

    module.any_profile = any_profile
    module.private = private
    module.write = write
    monkeypatch.setitem(sys.modules, module.__name__, module)
    config.enabled_tools = [module.__name__]
    config.profiles = {
        "interactive": Profile(
            tools=["*"],
            write_scopes=[f"path:{config.paths.data}"],
            capabilities=["git.push"],
        ),
        "maintenance": Profile(
            tools=["*"],
            write_scopes=[f"path:{config.paths.data}"],
            capabilities=["git.push"],
        ),
        "plain": Profile(),
    }
    return config


def test_wildcard_expansion_schema_catalog_and_dispatch(wildcard):
    registry = Registry(wildcard)
    assert registry.catalog()["profiles"] == {
        "interactive": ["fixture.any", "fixture.write"],
        "maintenance": ["fixture.any", "fixture.private", "fixture.write"],
        "plain": [],
    }
    assert wildcard.profiles["interactive"].tools == ["*"]
    assert registry.catalog()["tools"][1]["profiles"] == ["maintenance"]
    store = Store(wildcard.paths.data)
    ctx = ToolContext(
        store.create_session(Session(id="s")), "interactive", wildcard, store, "run"
    )

    async def exercise():
        assert (
            await registry.dispatch("fixture.any", "{}", ctx)
        ).content == "available"
        assert (
            await registry.dispatch("fixture.write", "{}", ctx)
        ).content == "written"
        assert (await registry.dispatch("fixture.private", "{}", ctx)).error
        assert (await registry.dispatch("unloaded.tool", "{}", ctx)).error
        ctx.profile = "plain"
        assert (await registry.dispatch("fixture.any", "{}", ctx)).error
        ctx.profile = "interactive"
        wildcard.profiles[ctx.profile].effects.clear()
        assert (await registry.dispatch("fixture.write", "{}", ctx)).error

    asyncio.run(exercise())
    logs = store._read(store.root / "runs/run.tools.jsonl")
    assert [log["result"]["error"] for log in logs] == [
        False,
        False,
        True,
        True,
        True,
        True,
    ]


@pytest.mark.parametrize("grant", ["write", "effects"])
def test_wildcard_requires_existing_write_and_capability_grants(wildcard, grant):
    setattr(wildcard.profiles["interactive"], grant, "none" if grant == "write" else [])
    with pytest.raises(ValueError, match="filesystem write tier|requires effect"):
        Registry(wildcard)


def test_mixed_wildcard_is_deduplicated_and_explicit_errors_are_not_hidden(wildcard):
    profile = wildcard.profiles["interactive"]
    profile.tools = ["fixture.write", "*", "fixture.any", "*"]
    registry = Registry(wildcard)
    assert [s["function"]["name"] for s in registry.schemas("interactive")] == [
        "fixture.write",
        "fixture.any",
    ]
    for name in ("missing", "fixture.private"):
        profile.tools = ["*", name]
        with pytest.raises(ValueError, match="not available"):
            Registry(wildcard)


def test_empty_wildcard_and_real_config_toml(config, tmp_path, monkeypatch):
    from solakit.core.config import load_config

    path = tmp_path / "config.toml"
    path.write_text('[profiles.interactive]\ntools = ["*"]\n')
    monkeypatch.setenv("SOLA_CONFIG", str(path))
    loaded = load_config()
    registry = Registry(loaded)
    assert registry.schemas("interactive") == []
    assert loaded.enabled_tools == []
    loaded.enabled_tools = ["runtime", "sessions"]
    registry = Registry(loaded)
    assert registry.catalog()["profiles"]["interactive"] == [
        "runtime.status",
        "sessions.search",
    ]
    assert json.dumps(registry.catalog())
