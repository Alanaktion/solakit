"""Forgiving argument parsing, name resolution, rendering and repair accounting
(TOOL-CALL-ROBUSTNESS-PLAN)."""

import ast
import asyncio
import json
from pathlib import Path
from typing import Annotated

import pytest
from pydantic import Field
from test_execution import completion, model_list, setup

from solakit.core.loop import RepairBudget
from solakit.tools import Registry, ToolContext, ToolResult, render_for_model, tool


def registry_with(config):
    registry = Registry(config)

    @tool(name="read.items", profiles=["*"], summary="Read items")
    async def read_items(
        ctx: ToolContext,
        tags: Annotated[list[str], Field(description="Tags")] = [],
        limit: Annotated[int, Field(description="Limit")] = 10,
        options: Annotated[dict[str, int], Field(description="Options")] = {},
    ) -> ToolResult:
        """Read items."""
        return ToolResult(content=json.dumps({"tags": tags, "limit": limit}))

    @tool(name="read.none", profiles=["*"], summary="No arguments")
    async def read_none(ctx: ToolContext) -> ToolResult:
        """No arguments."""
        return ToolResult(content="ok")

    @tool(name="read.need", profiles=["*"], summary="Required argument")
    async def read_need(
        ctx: ToolContext, query: Annotated[str, Field(description="Query")]
    ) -> ToolResult:
        """Requires a query."""
        return ToolResult(content=query)

    @tool(name="act.send", profiles=["*"], effect="send", summary="Send")
    async def act_send(
        ctx: ToolContext, to: Annotated[list[str], Field(description="To")]
    ) -> ToolResult:
        """Send."""
        return ToolResult(content="sent " + ",".join(to))

    for function in (read_items, read_none, read_need, act_send):
        registry.register(function)
    config.profiles["interactive"].tools = ["read.*", "act.send"]
    config.profiles["interactive"].effects = ["send"]
    registry.validate()
    return registry


STORES = {}


def call(registry, config, name, arguments):
    from solakit.core.schemas import Session
    from solakit.core.store import Store

    key = str(config.paths.data)
    if key not in STORES:
        store = Store(config.paths.data)
        store.create_session(Session(id="s"))
        STORES[key] = store
    store = STORES[key]
    ctx = ToolContext(store.session("s"), "interactive", config, store, "r")
    return asyncio.run(registry.dispatch(name, arguments, ctx)), ctx


def last_log(ctx):
    return ctx.store._read(ctx.store._path("runs", ctx.run_id, ".tools.jsonl"))[-1]


@pytest.mark.parametrize("raw", ["", "  ", "{}"])
def test_blank_arguments_for_zero_arg_tool(config, raw):
    registry = registry_with(config)
    result, ctx = call(registry, config, "read.none", raw)
    assert not result.error and result.content == "ok"


def test_blank_arguments_with_required_fields_names_them(config):
    registry = registry_with(config)
    result, _ = call(registry, config, "read.need", "")
    assert result.error and result.repairable
    assert "required: query" in result.content


def test_string_arrays_objects_and_nulls_normalize_with_evidence(config):
    registry = registry_with(config)
    result, ctx = call(
        registry,
        config,
        "read.items",
        json.dumps({"tags": '["a", "b"]', "limit": None, "options": '{"x": 1}'}),
    )
    assert not result.error
    assert json.loads(result.content) == {"tags": ["a", "b"], "limit": 10}
    kinds = {(n["path"], n["kind"]) for n in last_log(ctx)["normalizations"]}
    assert kinds == {
        ("tags", "string_to_array"),
        ("limit", "null_to_default"),
        ("options", "string_to_object"),
    }


def test_comma_strings_are_never_split(config):
    registry = registry_with(config)
    result, _ = call(registry, config, "read.items", '{"tags": "a, b"}')
    assert result.error and result.repairable


def test_unknown_keys_ignored_for_read_tools_only(config):
    registry = registry_with(config)
    result, ctx = call(registry, config, "read.need", '{"query": "q", "bogus": 1}')
    assert not result.error
    assert result.content == "Ignored unknown argument(s): bogus.\nq"
    assert last_log(ctx)["ignored_args"] == ["bogus"]
    # Required arguments must remain; otherwise the call is still repaired.
    result, _ = call(registry, config, "read.need", '{"bogus": 1}')
    assert result.error
    # Effectful tools: no key dropping and no string→array coercion.
    result, _ = call(registry, config, "act.send", '{"to": ["a"], "bogus": 1}')
    assert result.error and result.repairable
    result, _ = call(registry, config, "act.send", '{"to": "[\\"a\\"]"}')
    assert result.error and result.repairable
    result, _ = call(registry, config, "act.send", '{"to": ["a"]}')
    assert result.content == "sent a"


@pytest.mark.parametrize(
    "name",
    ["read_items", "functions.read.items", "Read.Items", "read-items", "read:items"],
)
def test_name_variants_resolve_uniquely(config, name):
    registry = registry_with(config)
    result, ctx = call(registry, config, name, "{}")
    assert not result.error
    log = last_log(ctx)
    assert log["name"] == "read.items" and log["resolved_from"] == name


def test_unknown_name_suggests_only_granted_tools(config):
    registry = registry_with(config)

    @tool(name="read.secret", profiles=["maintenance"], summary="Hidden")
    async def hidden(ctx: ToolContext) -> ToolResult:
        """Hidden."""
        return ToolResult(content="x")

    registry.register(hidden)
    result, _ = call(registry, config, "read.itmes", "{}")
    assert result.error and result.repairable
    assert "Unknown tool 'read.itmes'" in result.content
    assert "Did you mean: read.items" in result.content
    assert "read.secret" not in result.content
    result, _ = call(registry, config, "read.secret", "{}")
    assert "not permitted" in result.content


def test_render_for_model():
    ok = ToolResult(content="line1\nline2", artifacts=["/a", "/b"])
    assert render_for_model(ok) == ok.model_dump_json()
    assert render_for_model(ok, "text") == "line1\nline2\nFiles: /a, /b"
    bad = ToolResult(content="Invalid", error=True, repairable=True)
    assert render_for_model(bad, "text") == (
        "Error: Invalid\nFix the arguments and call again."
    )
    assert render_for_model(ToolResult(content="x", error=True), "text") == "Error: x"
    partial = ToolResult(content="abc", content_complete=False)
    assert render_for_model(partial, "text") == "abc\n[incomplete]"
    marked = ToolResult(content="abc\n[truncated 5 chars]", content_complete=False)
    assert render_for_model(marked, "text") == marked.content


def test_repair_budget_modes():
    legacy = RepairBudget(2)
    assert [legacy.fail("a", "arguments") for _ in range(3)] == [False, False, True]
    budget = RepairBudget(2, ceiling=6)
    assert not budget.fail("a", "arguments")
    assert not budget.fail("b", "arguments")
    assert not budget.fail("c", "unknown_tool")
    budget.success("c")  # refund: the failed tool succeeded next
    assert budget.total == 2
    assert not budget.fail("a", "arguments")
    assert budget.fail("a", "arguments")  # third for this tool+class


def test_text_results_and_identical_failure_note(config):
    config.profiles["interactive"].tool_result_format = "text"
    config.profiles["interactive"].repair_attempts = 5
    bodies = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        bodies.append(json.loads(request.content))
        if len(bodies) <= 2:
            return completion(
                config,
                calls=[
                    {
                        "id": f"c{len(bodies)}",
                        "type": "function",
                        "function": {"name": "read.need", "arguments": "{}"},
                    }
                ],
            )
        return completion(config, "Gave up.")

    execution = setup(config, handler)
    for name, item in registry_with(config).tools.items():
        if name.startswith(("read.", "act.")):
            execution.registry.tools[name] = item
    execution.registry.validate()

    async def exercise():
        run = await execution.submit("s", "go")
        await execution.close()
        return run

    run = asyncio.run(exercise())
    assert run.status == "completed"
    tools = [m for m in bodies[-1]["messages"] if m["role"] == "tool"]
    assert tools[0]["content"].startswith("Error: Invalid tool arguments")
    assert "Fix the arguments and call again." in tools[0]["content"]
    assert "You already tried exactly this" not in tools[0]["content"]
    assert "You already tried exactly this" in tools[1]["content"]


TOOL_SOURCES = [
    *Path(__file__).parents[1].joinpath("src/solakit/tools").glob("*.py"),
    *Path(__file__).parents[1].glob("packs/*/src/sola_packs/*.py"),
]


@pytest.mark.parametrize("path", TOOL_SOURCES, ids=lambda p: p.name)
def test_tool_error_literals_are_short_and_actionable(path):
    """Result contract lint: ToolError messages are non-empty and < 300 chars."""
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and getattr(node.exc.func, "id", None) == "ToolError"
            and node.exc.args
            and isinstance(node.exc.args[0], ast.Constant)
            and isinstance(node.exc.args[0].value, str)
        ):
            text = node.exc.args[0].value
            assert text.strip(), (path.name, node.lineno)
            assert len(text) < 300, (path.name, node.lineno)


def test_mutating_file_tools_confirm_what_changed(config, tmp_path):
    from solakit.core.config import Profile

    config.enabled_tools = ["files"]
    config.profiles["interactive"] = Profile(
        tools=["files.*"],
        write="workspace",
        workspace=[f"path:{tmp_path}"],
        write_workspace=[f"path:{tmp_path}"],
    )
    registry = Registry(config)
    target = tmp_path / "a.txt"
    result, _ = call(
        registry,
        config,
        "files.write",
        json.dumps({"path": str(target), "content": "hello"}),
    )
    assert result.content == f"Created {target} (5 bytes)"
    result, _ = call(
        registry,
        config,
        "files.append",
        json.dumps({"path": str(target), "content": "!!"}),
    )
    assert result.content == f"Appended 2 bytes to {target}"
    result, _ = call(
        registry,
        config,
        "files.edit",
        json.dumps({"path": str(target), "old": "hello", "new": "bye"}),
    )
    assert result.content == f"Edited {target}: replaced lines 1-1"
