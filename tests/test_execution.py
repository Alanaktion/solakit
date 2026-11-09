import asyncio
import json

import httpx
import pytest
from turns import plain

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
            {"role": "user", "content": record["turn_context"] + "\n\nhello"},
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
            seen.append(plain(body["messages"][-1]["content"]))
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
        assert [plain(m[-1]["content"]) for m in captured] == [
            "first",
            "second",
            "third",
        ]
        assert [plain(m.get("content")) for m in captured[-1][1:]] == [
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


@pytest.mark.parametrize("queued", [True, False])
def test_concurrent_cancellations_share_one_operation(config, queued):
    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()
        model_cancellations = []

        async def handler(request):
            if request.url.path.endswith("/models"):
                return model_list(config)
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                model_cancellations.append(True)
                raise
            return completion(config, "done")

        execution = setup(config, handler)
        first = asyncio.create_task(execution.submit("s", "first"))
        await entered.wait()
        second = None
        if queued:
            second = asyncio.create_task(execution.submit("s", "second"))
            while len(execution.pending) < 2:
                await asyncio.sleep(0)
        selected = "second" if queued else "first"
        run_id = next(
            p.run.id for p in execution.pending.values() if p.text == selected
        )
        results = await asyncio.gather(
            execution.cancel(run_id), execution.cancel(run_id)
        )
        assert results == [True, True]
        run = await (second if queued else first)
        assert run.status == "cancelled"
        assert len(model_cancellations) == (0 if queued else 1)
        assert not await execution.cancel(run_id)
        release.set()
        if queued:
            assert (await first).status == "completed"
        await execution.close()

    asyncio.run(exercise())


def test_cancel_request_disconnect_does_not_interrupt_finalization(config, monkeypatch):
    import threading

    async def exercise():
        model_entered = asyncio.Event()
        finalizing, finish = threading.Event(), threading.Event()

        async def handler(request):
            if request.url.path.endswith("/models"):
                return model_list(config)
            model_entered.set()
            await asyncio.Event().wait()

        execution = setup(config, handler)
        original_save = execution.store.save_run

        def delayed_save(run):
            if run.status == "cancelled":
                finalizing.set()
                assert finish.wait(5), "test did not release finalization"
            original_save(run)

        monkeypatch.setattr(execution.store, "save_run", delayed_save)
        submission = asyncio.create_task(execution.submit("s", "cancel"))
        await model_entered.wait()
        run_id = next(iter(execution.pending))
        request = asyncio.create_task(execution.cancel(run_id))
        while not finalizing.is_set():
            await asyncio.sleep(0)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        second_request = asyncio.create_task(execution.cancel(run_id))
        finish.set()
        assert await second_request
        assert (await submission).status == "cancelled"
        assert execution.store.run(run_id)["status"] == "cancelled"
        await execution.close()

    asyncio.run(exercise())


def test_shutdown_waits_for_inflight_acceptance_and_rejects_new_work(
    config, monkeypatch
):
    import threading

    async def exercise():
        accepting, finish = threading.Event(), threading.Event()

        async def handler(request):
            if request.url.path.endswith("/models"):
                return model_list(config)
            await asyncio.Event().wait()

        execution = setup(config, handler)
        original_save = execution.store.save_run

        def delayed_save(run):
            if not accepting.is_set():
                accepting.set()
                assert finish.wait(5), "test did not release acceptance"
            original_save(run)

        monkeypatch.setattr(execution.store, "save_run", delayed_save)
        submission = asyncio.create_task(execution.submit("s", "during shutdown"))
        while not accepting.is_set():
            await asyncio.sleep(0)
        closing = asyncio.create_task(execution.close())
        while execution._close_task is None:
            await asyncio.sleep(0)
        assert not closing.done()
        assert not execution.model.client.is_closed
        finish.set()
        async with asyncio.timeout(5):
            run, _ = await asyncio.gather(submission, closing)
        assert run.status == "cancelled"
        assert not execution.pending
        assert all(worker.done() for worker in execution.workers.values())
        assert execution.model.client.is_closed
        with pytest.raises(ValueError, match="shutting down"):
            await execution.submit("s", "too late")
        # Concurrent/repeated close requests use the same completed operation.
        await asyncio.gather(execution.close(), execution.close())

    asyncio.run(exercise())


@pytest.mark.parametrize("fail_diagnostic_write", [False, True])
def test_unexpected_failure_retains_redacted_cause(
    config, monkeypatch, caplog, fail_diagnostic_write
):
    from solakit.core.secrets import Redactor

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        try:
            raise OSError("diagnostic-secret")
        except OSError as cause:
            raise RuntimeError("unexpected provider failure") from cause

    execution = setup(config, handler)
    execution.store.redactor = Redactor(["diagnostic-secret"])
    if fail_diagnostic_write:

        def failed_write(*args):
            raise OSError("diagnostic disk unavailable")

        monkeypatch.setattr(execution.store, "log_diagnostic", failed_write)

    async def exercise():
        run = await execution.submit("s", "hello")
        assert run.status == "failed"
        assert "unexpected provider failure" not in run.error
        record = execution.store.run(run.id)
        if fail_diagnostic_write:
            trace = caplog.text
        else:
            assert record["diagnostics"][0]["exception_type"] == "RuntimeError"
            trace = record["diagnostics"][0]["traceback"]
        assert "OSError" in trace
        assert "RuntimeError" in trace
        assert "unexpected provider failure" in trace
        assert "[redacted]" in trace
        assert "diagnostic-secret" not in trace
        await execution.close()

    asyncio.run(exercise())


def test_shutdown_finishes_other_runs_when_one_finalization_fails(config, monkeypatch):
    import threading

    async def exercise():
        entered = 0
        both_running = asyncio.Event()
        first_failed, second_finishing, finish = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        async def handler(request):
            nonlocal entered
            if request.url.path.endswith("/models"):
                return model_list(config)
            entered += 1
            if entered == 2:
                both_running.set()
            await asyncio.Event().wait()

        config.model.concurrency = 2
        execution = setup(config, handler)
        execution.store.create_session(Session(id="other"))
        original_save = execution.store.save_run

        def failing_save(run):
            if run.status == "cancelled":
                if run.session == "s":
                    first_failed.set()
                    raise OSError("failed to persist cancellation")
                second_finishing.set()
                assert finish.wait(5), "test did not release finalization"
            original_save(run)

        monkeypatch.setattr(execution.store, "save_run", failing_save)
        first = asyncio.create_task(execution.submit("s", "first"))
        second = asyncio.create_task(execution.submit("other", "second"))
        await both_running.wait()
        closing = asyncio.create_task(execution.close())
        while not (first_failed.is_set() and second_finishing.is_set()):
            await asyncio.sleep(0)
        assert not closing.done()
        assert not execution.model.client.is_closed
        finish.set()
        with pytest.raises(ExceptionGroup, match="Run finalization failed"):
            await closing
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(outcomes[0], OSError)
        assert outcomes[1].status == "cancelled"
        assert execution.model.client.is_closed
        assert not execution.pending
        assert all(worker.done() for worker in execution.workers.values())

    asyncio.run(exercise())


def test_verbatim_transcript_and_context_are_separate_from_redacted_logs(config):
    from solakit.core.secrets import Redactor

    secret = config.operator_token.resolve()
    captured = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            return completion(
                config, calls=[call(arguments=json.dumps({"text": secret}))]
            )
        return completion(config, "done")

    execution = setup(config, handler)
    execution.store.redactor = Redactor([secret])

    @tool(name="echo", profiles=["interactive"])
    async def echo(ctx: ToolContext, text: str) -> ToolResult:
        return ToolResult(content=text)

    execution.registry.register(echo)
    config.profiles["interactive"].tools = ["echo"]
    execution.registry.validate()

    async def exercise():
        first = await execution.submit("s", f"Verbatim {secret}")
        assert first.status == "completed"
        transcript = execution.store.transcript("s")
        assert transcript[0]["content"] == f"Verbatim {secret}"
        assert transcript[1]["tool_calls"][0]["function"]["arguments"] == json.dumps(
            {"text": secret}
        )
        assert json.loads(transcript[2]["content"])["content"] == secret
        await execution.submit("s", "continue")
        history = captured[-1]["messages"][1:-1]
        assert plain(history[0]["content"]) == f"Verbatim {secret}"
        assert history[1]["tool_calls"][0]["function"]["arguments"] == json.dumps(
            {"text": secret}
        )
        assert json.loads(history[2]["content"])["content"] == secret
        root = execution.store.root
        for pattern in ("*.events.jsonl", "*.tools.jsonl"):
            for path in (root / "runs").glob(pattern):
                assert secret.encode() not in path.read_bytes()
        assert b"[redacted]" in (root / "runs" / f"{first.id}.tools.jsonl").read_bytes()
        execution.store.write_prompt("verbatim-audit", secret)
        assert (root / "runs/verbatim-audit.prompt.txt").read_bytes() == secret.encode()
        await execution.close()

    asyncio.run(exercise())


def test_tool_free_generation_is_one_bounded_completion(config):
    captured = []
    config.model.max_tokens = 128
    assert config.enabled_tools == []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        captured.append(json.loads(request.content))
        return completion(config, "No tools are available.")

    execution = setup(config, handler)

    async def exercise():
        run = await execution.submit("s", "Do you have any tools?")
        assert run.status == "completed"
        assert run.steps == len(captured) == 1
        assert captured[0]["tool_choice"] == "none"
        assert captured[0]["max_tokens"] == 128
        assert "tools" not in captured[0]
        assert execution.store.run(run.id)["tools"] == []
        await execution.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("with_calls", [False, True])
def test_output_token_limit_stops_without_retry_or_tool_dispatch(config, with_calls):
    captured = []
    config.model.max_tokens = 128

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        captured.append(json.loads(request.content))
        response = completion(
            config, "Partial response", [call()] if with_calls else None
        )
        data = json.loads(response.content)
        data["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=data)

    execution = setup(config, handler)

    @tool(name="echo", profiles=["interactive"])
    async def echo(ctx: ToolContext, text: str) -> ToolResult:
        raise AssertionError("Token-limited tool call must not execute")

    if with_calls:
        execution.registry.register(echo)
        config.profiles["interactive"].tools = ["echo"]
        execution.registry.validate()

    async def exercise():
        run = await execution.submit("s", "hello")
        assert run.status == "budget_exceeded"
        assert run.steps == len(captured) == 1
        assert "model.max_tokens (128)" in run.error
        assert execution.store.run(run.id)["tools"] == []
        transcript = execution.store.transcript("s")
        assert [m["role"] for m in transcript] == ["user", "assistant"]
        assert transcript[-1]["content"] == "Partial response"
        await execution.close()

    asyncio.run(exercise())
