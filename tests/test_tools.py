import asyncio
import json
import sys
from types import ModuleType

import pytest

from solakit.core.config import Profile
from solakit.core.schemas import Session
from solakit.core.store import Store
from solakit.tools import Registry, ToolContext, ToolResult, tool


def context(config):
    store = Store(config.paths.data)
    session = store.create_session(Session(id="s"))
    return ToolContext(session, "interactive", config, store, "r")


def test_drop_in_gating_validation_and_logs(config, monkeypatch):
    module = ModuleType("solakit.tools.fixture")
    calls = []

    @tool(name="fixture.echo", profiles=["interactive"], effect="send")
    async def echo(ctx: ToolContext, text: str, repeat: int = 1) -> ToolResult:
        """Repeat text."""
        calls.append(text)
        return ToolResult(content=text * repeat)

    module.echo = echo
    monkeypatch.setitem(sys.modules, module.__name__, module)
    config.enabled_tools = ["fixture"]
    config.profiles["interactive"] = Profile(tools=["fixture.echo"])
    with pytest.raises(ValueError, match="requires effect"):
        Registry(config)
    profile = config.profiles["interactive"]
    profile.effects = ["send"]
    profile.tool_output_max_chars = 32
    registry = Registry(config)
    assert registry.schemas("interactive")[0]["function"]["parameters"]["required"] == [
        "text"
    ]
    ctx = context(config)

    async def exercise():
        assert (
            await registry.dispatch(
                "fixture.echo", '{"text": "x", "repeat": "3.5"}', ctx
            )
        ).error
        assert calls == []
        result = await registry.dispatch(
            "fixture.echo", json.dumps({"text": "x" * 60}), ctx
        )
        assert "[truncated " in result.content
        assert not result.content_complete
        assert len(result.content) <= 32
        profile.tools = []
        assert (
            await registry.dispatch("fixture.echo", '{"text":"forbidden"}', ctx)
        ).error
        assert calls == ["x" * 60]

    asyncio.run(exercise())
    records = ctx.store._read(ctx.store.root / "runs/r.tools.jsonl")
    assert len(records) == 3
    assert records[-1]["result"]["error"]


def test_filesystem_scopes_and_symlinks(config, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "private"
    outside.write_text("private")
    (root / "escape").symlink_to(outside)
    config.enabled_tools = ["files"]
    config.profiles["interactive"] = Profile(tools=["files.read", "files.write"])
    with pytest.raises(ValueError, match="filesystem write tier"):
        Registry(config)
    profile = config.profiles["interactive"]
    profile.write = "workspace"
    profile.write_workspace = [f"path:{root}"]
    registry = Registry(config)
    ctx = context(config)

    async def exercise():
        for path in (outside, root / "../private", root / "escape"):
            assert (
                await registry.dispatch(
                    "files.read", json.dumps({"path": str(path)}), ctx
                )
            ).error
        target = root / "new.txt"
        args = json.dumps({"path": str(target), "content": "new"})
        assert not (await registry.dispatch("files.write", args, ctx)).error
        assert (await registry.dispatch("files.write", args, ctx)).error
        assert target.read_text() == "new"
        profile.write = "none"
        profile.write_workspace = []
        assert (await registry.dispatch("files.write", args, ctx)).error

    asyncio.run(exercise())


def test_timeout_and_unexpected_errors(config):
    registry = Registry(config)

    @tool(name="slow", profiles=["interactive"], timeout=0.01)
    async def slow(ctx: ToolContext):
        await asyncio.sleep(1)

    @tool(name="broken", profiles=["interactive"])
    async def broken(ctx: ToolContext):
        raise RuntimeError("private implementation detail")

    registry.register(slow)
    registry.register(broken)
    config.profiles["interactive"].tools = ["slow", "broken"]
    registry.validate()
    ctx = context(config)

    async def exercise():
        result = await registry.dispatch("slow", "{}", ctx)
        assert result.error and not result.repairable
        assert "Completion is unknown" in result.content
        assert "before retrying" in result.content
        result = await registry.dispatch("broken", "{}", ctx)
        assert result.error
        assert "private" not in result.content

    asyncio.run(exercise())


def test_file_reads_reject_special_files_without_waiting_for_a_writer(config, tmp_path):
    import os
    import threading

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    config.enabled_tools = ["files"]
    config.profiles["interactive"] = Profile(
        tools=["files.read"], read_scopes=[f"path:{tmp_path}"], tool_timeout_s=0.02
    )
    registry, ctx = Registry(config), context(config)
    fallback_used = threading.Event()

    def unblock_regression():
        # Bound the test even if a future regression blocks opening the FIFO.
        fallback_used.set()
        try:
            descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return
        os.close(descriptor)

    fallback = threading.Timer(1, unblock_regression)
    fallback.daemon = True
    fallback.start()
    try:
        result = asyncio.run(
            registry.dispatch("files.read", json.dumps({"path": str(fifo)}), ctx)
        )
        assert result.error
        assert result.content == "Only regular files can be read"
        assert not fallback_used.is_set()
    finally:
        fallback.cancel()
        fallback.join()

    # Directories are also excluded; ordinary files still support bounded reads.
    result = asyncio.run(
        registry.dispatch("files.read", json.dumps({"path": str(tmp_path)}), ctx)
    )
    assert result.error
    regular = tmp_path / "regular"
    regular.write_text("readable")
    assert (
        asyncio.run(
            registry.dispatch("files.read", json.dumps({"path": str(regular)}), ctx)
        ).content
        == "readable"
    )


@pytest.mark.parametrize("length", [31, 32, 33, 999, 1000, 10001, 30000])
def test_truncation_reports_exact_unicode_count_within_budget(length):
    import re

    from solakit.tools import truncate_output

    content = "雪" * length
    rendered, omitted = truncate_output(content, 32, complete=True)
    assert len(rendered) <= 32
    if length <= 32:
        assert rendered == content
        assert omitted == 0
    else:
        prefix, marker = rendered.split("\n", 1)
        assert content.startswith(prefix)
        assert omitted == length - len(prefix)
        assert int(re.fullmatch(r"\[truncated (\d+) chars\]", marker)[1]) == omitted


def test_bounded_file_truncation_does_not_invent_a_total(config, tmp_path):
    import re

    path = tmp_path / "large.txt"
    path.write_text("雪" * 30000)
    config.enabled_tools = ["files"]
    config.profiles["interactive"] = Profile(
        tools=["files.read"],
        read_scopes=[f"path:{tmp_path}"],
        tool_output_max_chars=20000,
    )
    registry, ctx = Registry(config), context(config)
    result = asyncio.run(
        registry.dispatch("files.read", json.dumps({"path": str(path)}), ctx)
    )
    prefix, marker = result.content.rsplit("\n", 1)
    minimum = int(re.fullmatch(r"\[truncated >=(\d+) chars\]", marker)[1])
    assert len(result.content) <= 20000
    assert minimum == 20001 - len(prefix)
    assert minimum <= 30000 - len(prefix)
    assert not result.content_complete
    logged = ctx.store._read(ctx.store.root / "runs/r.tools.jsonl")[0]
    assert logged["output_chars"] is None
    assert logged["output_observed_chars"] == 20001
    assert logged["omitted_chars"] is None
    assert logged["omitted_chars_min"] == minimum


def test_argument_errors_teach_without_echoing_submitted_values(config):
    config.enabled_tools = ["files"]
    config.profiles["interactive"] = Profile(tools=["files.list"])
    registry, ctx = Registry(config), context(config)

    async def exercise():
        result = await registry.dispatch(
            "files.list",
            json.dumps(
                {"limit": "private-value", "private-extra-key": "private-value"}
            ),
            ctx,
        )
        assert result.error and result.repairable
        assert "path: missing" in result.content
        assert "limit: int_type" in result.content
        assert "extra_forbidden" in result.content
        assert "private" not in result.content
        result = await registry.dispatch("files.list", "{private-invalid-json", ctx)
        assert result.repairable and "expected a JSON object" in result.content
        assert "private" not in result.content

    asyncio.run(exercise())


def test_generic_schema_routing_and_parameter_guidance(config):
    config.enabled_tools = ["files", "memory", "shell", "runtime"]
    config.profiles["interactive"] = Profile(
        tools=[
            "files.read",
            "files.list",
            "memory.search",
            "memory.read",
            "shell.exec",
            "shell.run",
            "runtime.status",
        ],
        write_scopes=["path:/tmp"],
        capabilities=["shell.exec"],
    )
    schemas = {
        item["function"]["name"]: item["function"]
        for item in Registry(config).schemas("interactive")
    }
    for schema in schemas.values():
        assert all(
            field.get("description")
            for field in schema["parameters"]["properties"].values()
        )
    for name in ("files.read", "files.list", "shell.exec", "shell.run"):
        assert "domain" in schemas[name]["description"]
    assert "sessions.search" in schemas["memory.search"]["description"]


def test_invalid_artifact_is_tool_error(config, monkeypatch):
    module = ModuleType("solakit.tools.artifact_fixture")

    @tool(name="fixture.artifact", profiles=["*"])
    async def artifact(ctx):
        return ToolResult(content="done", artifacts=["/outside/private.txt"])

    module.artifact = artifact
    monkeypatch.setitem(sys.modules, module.__name__, module)
    config.enabled_tools = [module.__name__]
    config.profiles["interactive"] = Profile(tools=["fixture.artifact"], read="none")
    result = asyncio.run(
        Registry(config).dispatch("fixture.artifact", "{}", context(config))
    )
    assert result.error
    assert result.artifacts == []
