"""Invalid calls teach a safe retry; normalization never broadens authority."""

import asyncio
import json
from typing import Annotated, Literal

import pytest
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic_core import PydanticCustomError
from test_execution import call, completion, model_list, setup
from test_tool_disclosure import rig
from test_tools import context

from solakit.tools import Registry, ToolContext, ToolResult, tool
from solakit.tools.arguments import argument_error, validate_arguments


class Payload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    count: Annotated[int, Field(ge=1, le=5)]
    enabled: bool
    label: str


class Arguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    payload: Payload
    counts: list[int]
    ratio: float | None = None
    choice: Literal["append", "replace"] = "append"
    ambiguous: int | str = "unchanged"


def test_normalization_preserves_text_and_ambiguous_unions():
    submitted = {
        "payload": {"count": " 3 ", "enabled": "FALSE", "label": "  exact text\n"},
        "counts": ["1", "-2"],
        "ratio": "1.5e-2",
        "ambiguous": "123",
    }
    original = json.dumps(submitted)
    validated = validate_arguments(Arguments, submitted)
    assert validated.payload.count == 3
    assert validated.payload.enabled is False
    assert validated.payload.label == "  exact text\n"
    assert validated.counts == [1, -2]
    assert validated.ratio == 0.015
    assert validated.ambiguous == "123"
    assert json.dumps(submitted) == original


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("payload", {"count": "1.5", "enabled": True, "label": "ok"}, "JSON integer"),
        ("payload", {"count": True, "enabled": True, "label": "ok"}, "JSON integer"),
        ("payload", {"count": "6", "enabled": True, "label": "ok"}, '"maximum": 5'),
        ("payload", {"count": 1, "enabled": "yes", "label": "ok"}, "true or false"),
        ("payload", {"count": 1, "enabled": 1, "label": "ok"}, "true or false"),
        ("counts", "1", "JSON array"),
        ("choice", "APPEND", '"append", "replace"'),
        ("ratio", "NaN", "JSON number"),
        ("ratio", "1e999", "JSON number"),
        ("ratio", "1e-999", "JSON number"),
        ("payload", {"count": 1, "enabled": True, "label": 42}, "JSON string"),
    ],
)
def test_unsafe_coercions_and_bounds_remain_errors(field, value, expected):
    args = {"payload": {"count": 1, "enabled": True, "label": "ok"}, "counts": []}
    args[field] = value
    with pytest.raises(ValueError) as error:
        validate_arguments(Arguments, args)
    guidance = argument_error(Arguments, error.value)
    assert expected in guidance
    assert "The tool was not executed" in guidance
    assert "Correct the arguments and retry" in guidance


def test_custom_validator_messages_and_codes_do_not_echo_values():
    def reject(value):
        raise PydanticCustomError(
            "private-code", "private-message {input}", {"input": "private-value"}
        )

    class Custom(BaseModel):
        count: Annotated[int, AfterValidator(reject)]

    with pytest.raises(ValueError) as error:
        validate_arguments(Custom, {"count": 1})
    guidance = argument_error(Custom, error.value)
    assert "private" not in guidance
    assert "count: invalid_value" in guidance


def test_dispatch_repairs_invalid_call_and_logs_actual_arguments(config):
    registry, ctx = Registry(config), context(config)
    invoked = []

    @tool(name="sample", profiles=["*"])
    async def sample(
        ctx: ToolContext, count: Annotated[int, Field(ge=1, le=5)], enabled: bool
    ) -> ToolResult:
        invoked.append((count, enabled))
        return ToolResult(content="done")

    registry.register(sample)
    config.profiles["interactive"].tools = ["sample"]

    async def exercise():
        failed = await registry.dispatch(
            "sample", '{"count": "6", "private-key": "private-value"}', ctx
        )
        assert failed.error and failed.repairable
        assert '"maximum": 5' in failed.content
        assert "provide this required argument" in failed.content
        assert "remove unsupported arguments" in failed.content
        assert "count (required), enabled (required)" in failed.content
        assert "private" not in failed.content
        assert invoked == []
        corrected = await registry.dispatch(
            "sample", '{"count": "3", "enabled": "false"}', ctx
        )
        assert not corrected.error
        assert invoked == [(3, False)]
        config.profiles["interactive"].tools = []
        denied = await registry.dispatch(
            "sample", '{"count": "3", "enabled": "false"}', ctx
        )
        assert (
            denied.error and "changing arguments cannot grant access" in denied.content
        )
        assert "not executed" in denied.content
        assert invoked == [(3, False)]

    asyncio.run(exercise())
    records = ctx.store._read(ctx.store.root / "runs/r.tools.jsonl")
    assert records[0]["validated_args"] is None
    assert records[1]["args"] == {"count": "3", "enabled": "false"}
    assert records[1]["validated_args"] == {"count": 3, "enabled": False}


@pytest.mark.parametrize(
    "arguments", ["[]", "null", '"private-value"', '{"private-value":']
)
def test_bad_argument_envelopes_get_correction_without_execution(config, arguments):
    registry, ctx = Registry(config), context(config)

    @tool(name="sample", profiles=["*"])
    async def sample(ctx: ToolContext, count: int):
        pytest.fail("invalid envelopes must not execute")

    registry.register(sample)
    config.profiles["interactive"].tools = ["sample"]
    result = asyncio.run(registry.dispatch("sample", arguments, ctx))
    assert result.error and result.repairable
    assert "JSON object" in result.content
    assert "not executed" in result.content
    assert "private" not in result.content


def test_execution_loop_receives_guidance_and_recovers(config):
    captured, invoked = [], []

    def backend(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            return completion(config, calls=[call("sample", '{"count":"fraction"}')])
        if len(captured) == 2:
            result = json.loads(captured[-1]["messages"][-1]["content"])
            assert result["repairable"] and "JSON integer" in result["content"]
            assert "not executed" in result["content"]
            assert invoked == []
            return completion(config, calls=[call("sample", '{"count":"3"}')])
        return completion(config, content="finished")

    execution = setup(config, backend)

    @tool(name="sample", profiles=["*"])
    async def sample(ctx: ToolContext, count: int):
        invoked.append(count)
        return "done"

    execution.registry.register(sample)
    config.profiles["interactive"].tools = ["sample"]

    async def exercise():
        try:
            run = await execution.submit("s", "do the task")
            assert run.status == "completed"
            assert invoked == [3]
            evidence = execution.store.run(run.id)["messages"]
            assert (
                evidence[1]["tool_calls"][0]["function"]["arguments"]
                == '{"count":"fraction"}'
            )
            assert (
                evidence[3]["tool_calls"][0]["function"]["arguments"] == '{"count":"3"}'
            )
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_discovery_corrects_arguments_without_using_repair_budget(config):
    execution, bodies, invoked = rig(
        config,
        [
            [call("tools.search", '{"query":"alpha","limit":"6"}')],
            [call("tools.search", '{"query":"alpha","limit":"1"}')],
            [call("alpha.read", "{}")],
            "finished",
        ],
        max_steps=2,
    )
    config.profiles["interactive"].repair_attempts = 0

    async def exercise():
        try:
            run = await execution.submit("s", "read alpha")
            assert run.status == "completed"
            failed = json.loads(bodies[1]["messages"][-1]["content"])
            assert failed["error"] and not failed["repairable"]
            assert '"maximum": 5' in failed["content"]
            assert "not executed" in failed["content"]
            assert invoked == ["hello"]
            assert run.tool_view["disclosure_steps"] == 2
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_normalized_argument_logs_are_redacted(config):
    registry, ctx = Registry(config), context(config)

    @tool(name="sample", profiles=["*"])
    async def sample(ctx: ToolContext, password: str, count: int):
        assert password == "private-value" and count == 3
        return "done"

    registry.register(sample)
    config.profiles["interactive"].tools = ["sample"]
    result = asyncio.run(
        registry.dispatch("sample", '{"password":"private-value","count":"3"}', ctx)
    )
    assert not result.error
    records = ctx.store._read(ctx.store.root / "runs/r.tools.jsonl")
    assert records[0]["validated_args"] == {"password": "[redacted]", "count": 3}
    assert "private-value" not in json.dumps(records)


def test_runtime_value_error_is_not_misreported_as_argument_rejection(config):
    registry, ctx = Registry(config), context(config)

    @tool(name="sample", profiles=["*"])
    async def sample(ctx: ToolContext, count: int):
        raise ValueError("private operational details")

    registry.register(sample)
    config.profiles["interactive"].tools = ["sample"]
    result = asyncio.run(registry.dispatch("sample", '{"count":"3"}', ctx))
    assert result.error and not result.repairable
    assert "not executed" not in result.content
    assert "Invalid tool arguments" not in result.content
    assert "before retrying" in result.content
    assert "private" not in result.content
