"""Request parameters, thinking control, reasoning evidence, tool-call fallback
and calibrated token budgets (MODEL-REQUEST-TUNING-PLAN)."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError
from test_execution import model_list, setup

from solakit.core.config import ProfileModel, RequestParams
from solakit.core.loop import step_thinking
from solakit.core.model_stream import split_think
from solakit.core.toolcall_fallback import parse
from solakit.tools import ToolContext, ToolResult, tool


def sse(config, deltas, finish="stop", usage=None):
    chunks = [
        {
            "model": config.model.model_id,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        for delta in deltas
    ]
    chunks[-1]["choices"][0]["finish_reason"] = finish
    if usage:
        chunks.append({"model": config.model.model_id, "choices": [], "usage": usage})
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(
        200, content=body, headers={"content-type": "text/event-stream"}
    )


def echo_tool(execution, config):
    @tool(name="echo", profiles=["interactive"])
    async def echo(ctx: ToolContext, text: str) -> ToolResult:
        return ToolResult(content=text)

    execution.registry.register(echo)
    config.profiles["interactive"].tools = ["echo"]
    execution.registry.validate()


def test_request_params_validation_and_wire():
    with pytest.raises(ValidationError):
        RequestParams(extra_body={"model": "other"})
    with pytest.raises(ValidationError):
        RequestParams(extra_body={"tools": []})
    with pytest.raises(ValidationError):
        RequestParams(temperature=3)
    with pytest.raises(ValidationError):
        RequestParams(extra_body={"x": "y" * 5000})
    assert RequestParams().wire() == {}
    params = RequestParams(
        temperature=0.7,
        top_k=20,
        parallel_tool_calls=False,
        extra_body={"chat_template_kwargs": {"keep": 1}, "seed": 3},
    )
    assert params.wire(thinking="on", override=ProfileModel(temperature=0.6)) == {
        "temperature": 0.6,
        "top_k": 20,
        "parallel_tool_calls": False,
        "chat_template_kwargs": {"keep": 1, "enable_thinking": True},
        "seed": 3,
    }
    none = RequestParams(thinking_control="none")
    assert none.wire(thinking="off") == {}


def test_step_thinking_rules():
    assert step_thinking(None, 0, False) is None
    assert step_thinking("off", 0, True) == "off"
    assert step_thinking("auto", 0, False) == "on"
    assert step_thinking("auto", 1, False) == "off"
    assert step_thinking("auto", 3, True) == "on"


def test_split_think():
    assert split_think("plain") == ("", "plain")
    assert split_think("<think>a") == ("a", "")
    assert split_think("  <think>a</think>\n\nanswer") == ("a", "answer")
    assert split_think("<thi") == ("", "")
    assert split_think("I <think> later") == ("", "I <think> later")


def test_default_wire_request_is_unchanged(config):
    bodies = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        bodies.append(json.loads(request.content))
        return sse(config, [{"role": "assistant", "content": "hi"}])

    execution = setup(config, handler)

    async def exercise():
        run = await execution.submit("s", "hello")
        await execution.close()
        return run

    run = asyncio.run(exercise())
    assert run.status == "completed"
    assert set(bodies[0]) == {
        "model",
        "messages",
        "max_tokens",
        "tool_choice",
        "stream",
        "stream_options",
    }
    assert run.request_params == {
        "params": {},
        "max_tokens": config.model.max_tokens,
        "thinking": None,
        "max_reasoning_tokens": None,
    }


def test_profile_params_thinking_and_reasoning_evidence(config):
    config.model.request = RequestParams(temperature=0.7, top_k=20)
    config.profiles["interactive"].model = ProfileModel(
        thinking="auto", max_tokens=777, temperature=0.6
    )
    bodies, previews = [], []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return sse(
                config,
                [
                    {"role": "assistant", "reasoning_content": "consider echo"},
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "echo",
                                    "arguments": '{"text": "x"}',
                                },
                            }
                        ]
                    },
                ],
                finish="tool_calls",
            )
        return sse(
            config,
            [
                {"role": "assistant", "content": "<think>inline"},
                {"content": " thought</think>\n\nDone."},
            ],
        )

    execution = setup(config, handler)
    echo_tool(execution, config)

    async def exercise():
        pending = await execution.enqueue("s", "go", stream=True)

        async def watch():
            while not pending.future.done():
                if pending.preview:
                    previews.append(pending.preview["content"])
                await asyncio.sleep(0)

        watcher = asyncio.create_task(watch())
        run = await pending.future
        await watcher
        await execution.close()
        return run

    run = asyncio.run(exercise())
    assert run.status == "completed"
    assert bodies[0]["temperature"] == 0.6 and bodies[0]["top_k"] == 20
    assert bodies[0]["max_tokens"] == 777
    assert bodies[0]["chat_template_kwargs"] == {"enable_thinking": True}
    assert bodies[1]["chat_template_kwargs"] == {"enable_thinking": False}
    record = execution.store.run(run.id)
    assert [r["text"] for r in record["reasoning"]] == [
        "consider echo",
        "inline thought",
    ]
    assert run.reasoning_steps == 2
    transcript = execution.store.transcript("s")
    assert transcript[-1]["content"] == "Done."
    assert "think" not in json.dumps(bodies[1]["messages"])
    assert "consider echo" not in json.dumps(bodies[1])
    assert all("<think>" not in p for p in previews)


def test_reasoning_cap_retries_once_with_thinking_off(config):
    config.profiles["interactive"].model = ProfileModel(
        thinking="on", max_reasoning_tokens=64
    )
    bodies = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        bodies.append(json.loads(request.content))
        if bodies[-1]["chat_template_kwargs"]["enable_thinking"]:
            return sse(
                config, [{"role": "assistant", "reasoning_content": "hmm " * 200}]
            )
        return sse(config, [{"role": "assistant", "content": "Answer."}])

    execution = setup(config, handler)

    async def exercise():
        run = await execution.submit("s", "question")
        await execution.close()
        return run

    run = asyncio.run(exercise())
    assert run.status == "completed"
    assert run.retries == [{"step": 1, "reason": "reasoning_exhausted"}]
    assert len(bodies) == 2


def test_length_stop_with_only_reasoning_has_distinct_error(config):
    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        return sse(
            config,
            [{"role": "assistant", "reasoning_content": "thinking..."}],
            finish="length",
        )

    execution = setup(config, handler)

    async def exercise():
        run = await execution.submit("s", "hard question")
        await execution.close()
        return run

    run = asyncio.run(exercise())
    assert run.status == "budget_exceeded"
    assert run.error.startswith("Thinking exhausted model.max_tokens")


@pytest.mark.parametrize(
    "content,expected",
    [
        (
            '<tool_call>\n{"name": "echo", "arguments": {"text": "a"}}\n</tool_call>',
            [("echo", {"text": "a"})],
        ),
        (
            'Let me check.\n<tool_call>{"name": "echo", "arguments": "{}"}</tool_call>',
            [("echo", {})],
        ),
        (
            "<tool_call>\n<function=echo>\n<parameter=text>\nhi there\n</parameter>\n"
            "</function>\n</tool_call>",
            [("echo", {"text": "hi there"})],
        ),
        (
            '<tool_call>{"name": "echo", "arguments": {}}</tool_call>\n'
            '<tool_call>{"name": "echo", "arguments": {"text": "b"}}</tool_call>',
            [("echo", {}), ("echo", {"text": "b"})],
        ),
    ],
)
def test_fallback_accepts(content, expected):
    calls = parse(content, {"echo"})
    assert [
        (c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls
    ] == expected
    assert calls[0]["id"] == "call_fb_0"


@pytest.mark.parametrize(
    "content",
    [
        "no calls here",
        '```\n<tool_call>{"name": "echo", "arguments": {}}</tool_call>\n```',
        '<tool_call>{"name": "rm", "arguments": {}}</tool_call>',
        '<tool_call>{"name": "echo", "arguments": [1]}</tool_call>',
        '<tool_call>{"name": "echo", "arguments": {</tool_call>',
        '<tool_call>{"name": "echo", "arguments": {}}',
        '<tool_call>{"name": "echo", "arguments": {}}</tool_call> and then more',
    ],
)
def test_fallback_rejects(content):
    assert parse(content, {"echo"}) is None


def test_fallback_executes_and_keeps_verbatim_content(config):
    config.model.tool_call_fallback = True
    text = (
        'Calling.\n<tool_call>{"name": "echo", "arguments": {"text": "x"}}</tool_call>'
    )
    bodies = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return sse(config, [{"role": "assistant", "content": text}])
        return sse(config, [{"role": "assistant", "content": "Echoed."}])

    execution = setup(config, handler)
    echo_tool(execution, config)

    async def exercise():
        run = await execution.submit("s", "echo x")
        await execution.close()
        return run

    run = asyncio.run(exercise())
    assert run.status == "completed"
    transcript = execution.store.transcript("s")
    assistant = transcript[1]
    assert assistant["content"] == text
    assert assistant["tool_call_source"] == "content_fallback"
    assert assistant["tool_calls"][0]["function"]["name"] == "echo"
    assert transcript[2]["role"] == "tool"


def test_fallback_disabled_by_default_completes_with_text(config):
    text = '<tool_call>{"name": "echo", "arguments": {"text": "x"}}</tool_call>'

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        return sse(config, [{"role": "assistant", "content": text}])

    execution = setup(config, handler)
    echo_tool(execution, config)

    async def exercise():
        run = await execution.submit("s", "echo x")
        await execution.close()
        return run

    assert asyncio.run(exercise()).status == "completed"


def test_calibration_bounds_and_token_budget(config):
    execution = setup(config, lambda request: model_list(config))
    store = execution.store
    assert store.calibration("m") is None
    assert store.record_calibration("m", 100, 0) is None
    first = store.record_calibration("m", 1000, 10)  # 100 bytes/token → ceiling
    assert first["bytes_per_token"] == 8.0
    for _ in range(50):
        store.record_calibration("m", 400, 100)
    assert 4.0 <= store.calibration("m")["bytes_per_token"] < 4.5

    cfg = config.profiles["interactive"]
    cfg.context_budget = 10**9
    messages = [{"role": "user", "content": "x" * 4000}]
    assert not execution._over_budget(cfg, messages, [], 100, {})
    cfg.context_tokens = 1000
    # No calibration sample for the configured model: byte rule only.
    assert not execution._over_budget(cfg, messages, [], 100, {})
    store.record_calibration(config.model.model_id, 4000, 1000)
    assert execution._over_budget(cfg, messages, [], 100, {})
    cfg.context_tokens = 100000
    assert not execution._over_budget(cfg, messages, [], 100, {})
    # The measured prompt_tokens of the previous step is a floor.
    assert execution._over_budget(cfg, messages, [], 100, {"tokens": 99000, "bytes": 1})
