import asyncio
import json

import httpx
import pytest

from solakit.core.loop import Execution
from solakit.core.model import ModelClient
from solakit.core.schemas import Session
from solakit.core.store import Store
from solakit.tools import Registry, ToolContext, ToolResult, tool


def setup(config, handler):
    store = Store(config.paths.data)
    store.create_session(Session(id="s"))
    registry = Registry(config)
    model = ModelClient(config.model, transport=httpx.MockTransport(handler))
    return Execution(config, store, registry, model)


def completion(config, content=None, calls=None, usage=None):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return httpx.Response(
        200,
        json={
            "model": config.model.model_id,
            "choices": [{"message": message}],
            "usage": usage,
        },
    )


def model_list(config):
    return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})


def call(name="echo", arguments='{"text":"tool input"}'):
    return {
        "id": "call1",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def test_exact_context_tool_order_and_usage(config):
    captured = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        body = json.loads(request.content)
        captured.append(body)
        usage = {"prompt_tokens": 10, "completion_tokens": 3}
        if len(captured) == 1:
            return completion(config, calls=[call()], usage=usage)
        return completion(config, "finished", usage=usage)

    execution = setup(config, handler)

    @tool(name="echo", profiles=["interactive"])
    async def echo(ctx: ToolContext, text: str) -> ToolResult:
        return ToolResult(content=text)

    execution.registry.register(echo)
    config.profiles["interactive"].tools = ["echo"]
    execution.registry.validate()

    async def exercise():
        run = await execution.submit("s", "hello")
        assert run.status == "completed"
        assert run.tokens == {"prompt_tokens": 20, "completion_tokens": 6}
        record = execution.store.run(run.id)
        assert captured[0]["messages"] == [
            {"role": "system", "content": record["prompt"]},
            {"role": "user", "content": "hello"},
        ]
        assert captured[0]["tools"][0]["function"]["name"] == "echo"
        assert [m["role"] for m in captured[1]["messages"]] == [
            "system",
            "user",
            "assistant",
            "tool",
        ]
        history = execution.store.transcript("s")
        assert history[1]["tool_calls"][0]["id"] == history[2]["tool_call_id"]
        assert all(m["origin"]["channel"] == "api" for m in history)
        await execution.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["down", "wrong", "chat_down", "chat_wrong"])
def test_unavailable_does_not_mutate_session(config, mode):
    def handler(request):
        if request.url.path.endswith("/models") and mode.startswith("chat_"):
            return model_list(config)
        if mode == "chat_wrong":
            return httpx.Response(200, json={"model": "other-model"})
        if mode in {"down", "chat_down"}:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"data": [{"id": "other-model"}]})

    execution = setup(config, handler)

    async def exercise():
        run = await execution.submit("s", "never appended")
        assert run.status == "model_unavailable"
        assert execution.store.transcript("s") == []
        assert execution.store.run(run.id)["error"]
        await execution.close()

    asyncio.run(exercise())


def test_forbidden_tools_repairs_and_budget(config):
    captured = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        captured.append(json.loads(request.content))
        return completion(config, calls=[call("shell", "{}")])

    execution = setup(config, handler)

    async def exercise():
        run = await execution.submit("s", "try forbidden")
        assert run.status == "tool_error"
        assert run.steps == 3
        assert all("tools" not in body for body in captured)
        assert len(execution.store.run(run.id)["tools"]) == 3
        config.profiles["interactive"].max_steps = 1
        run = await execution.submit("s", "try again")
        assert run.status == "budget_exceeded"
        await execution.close()

    asyncio.run(exercise())


def test_fifo_global_limit_cancellation_and_shutdown(config):
    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()
        active = 0
        maximum = 0
        seen = []

        async def handler(request):
            nonlocal active, maximum
            if request.url.path.endswith("/models"):
                return model_list(config)
            active += 1
            maximum = max(maximum, active)
            body = json.loads(request.content)
            seen.append(body["messages"][-1]["content"])
            entered.set()
            try:
                await release.wait()
                return completion(config, "done")
            finally:
                active -= 1

        execution = setup(config, handler)
        execution.store.create_session(Session(id="other"))
        first = asyncio.create_task(execution.submit("s", "first"))
        await entered.wait()
        second = asyncio.create_task(execution.submit("s", "second"))
        third = asyncio.create_task(execution.submit("other", "third"))
        # Wait for acceptance, using task progress rather than a wall-clock sleep.
        while len(execution.pending) < 3:
            await asyncio.sleep(0)
        second_id = next(
            p.run.id for p in execution.pending.values() if p.text == "second"
        )
        assert await execution.cancel(second_id)
        assert (await second).status == "cancelled"
        release.set()
        assert (await first).status == "completed"
        assert (await third).status == "completed"
        assert seen == ["first", "third"]
        assert maximum == 1
        # A queued follow-up sees the completed earlier run.
        assert (await execution.submit("s", "fourth")).status == "completed"
        entered.clear()
        release.clear()
        running = asyncio.create_task(execution.submit("s", "cancel me"))
        await entered.wait()
        running_id = next(p.run.id for p in execution.pending.values())
        assert await execution.cancel(running_id)
        assert (await running).status == "cancelled"
        # Shutdown finalizes active work and closes the model client.
        entered.clear()
        shutdown_run = asyncio.create_task(execution.submit("s", "shutdown"))
        await entered.wait()
        await execution.close()
        assert (await shutdown_run).status == "cancelled"
        assert execution.model.client.is_closed

    asyncio.run(exercise())


def test_successful_fifo_and_history(config):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        captured = []

        async def handler(request):
            if request.url.path.endswith("/models"):
                return model_list(config)
            captured.append(json.loads(request.content)["messages"])
            if len(captured) == 1:
                entered.set()
                await release.wait()
            return completion(config, f"reply{len(captured)}")

        execution = setup(config, handler)
        first = asyncio.create_task(execution.submit("s", "first"))
        await entered.wait()
        second = asyncio.create_task(execution.submit("s", "second"))
        while len(execution.pending) != 2:
            await asyncio.sleep(0)
        third = asyncio.create_task(execution.submit("s", "third"))
        while len(execution.pending) != 3:
            await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second, third)
        assert [m[-1]["content"] for m in captured] == ["first", "second", "third"]
        assert [m.get("content") for m in captured[-1][1:]] == [
            "first",
            "reply1",
            "second",
            "reply2",
            "third",
        ]
        await execution.close()

    asyncio.run(exercise())


def test_incomplete_tool_group_preserved_but_excluded(config):
    from solakit.core.schemas import Message, Origin

    captured = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        captured.append(json.loads(request.content)["messages"])
        return completion(config, "recovered")

    execution = setup(config, handler)
    execution.store.append_message(
        "s",
        Message(role="assistant", tool_calls=[call()], origin=Origin(), run_id="old"),
    )

    async def exercise():
        run = await execution.submit("s", "resume")
        assert run.status == "completed"
        assert run.context_skipped_messages == 1
        assert [m["role"] for m in captured[0]] == ["system", "user"]
        assert len(execution.store.transcript("s")) == 3
        await execution.close()

    asyncio.run(exercise())
