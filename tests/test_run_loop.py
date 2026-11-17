import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
import pytest

from solakit.config import EffectiveProfile
from solakit.evidence import canonical
from solakit.records import (
    ErrorCode,
    ModelStep,
    Origin,
    RecoveryNote,
    RunState,
    SessionMode,
    ToolReceipt,
    new_id,
)
from solakit.registry import Registry
from solakit.run_loop import QueueFull, project_run, records, render_receipt, requested_tools
from solakit.runtime import ApplicationContext, compose
from solakit.sdk import Tool, ToolArguments, ToolContext, ToolResult
from solakit.tools import TOOLS


def reply(text: str = "Finished.", *, calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = calls
    return {
        "model": "fixture-qwen",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if calls else "stop",
            }
        ],
    }


def call(name: str, arguments: str = "{}") -> dict[str, Any]:
    return {
        "id": new_id(),
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }


def wire(data: dict[str, Any]) -> bytes:
    choice = data["choices"][0]
    delta = dict(choice["message"])
    if "tool_calls" in delta:
        delta["tool_calls"] = [c | {"index": i} for i, c in enumerate(delta["tool_calls"])]
    return (
        b"".join(
            b"data: " + canonical(event) + b"\n\n"
            for event in (
                {
                    "model": data["model"],
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {"choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]},
            )
        )
        + b"data: [DONE]\n\n"
    )


def harness(
    profile: EffectiveProfile,
    responses: list[dict[str, Any]] | Callable[[dict[str, Any]], Awaitable[bytes]],
    extra: tuple[Tool[Any], ...] = (),
) -> tuple[ApplicationContext, list[dict[str, Any]]]:
    context = compose(profile)
    assert context.tools and context.model
    context.tools.registry = Registry(
        (*(tool for tool in TOOLS if not tool.name.startswith("jobs.")), *extra)
    )
    names = frozenset(context.tools.registry.tools)
    context.profile = profile.model_copy(
        update={
            "loaded_tools": names,
            "granted_tools": names,
            "tools": names,
        }
    )
    requests: list[dict[str, Any]] = []
    iterator = iter(responses) if isinstance(responses, list) else None

    async def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": profile.model_pin}]})
        body = json.loads(request.content)
        requests.append(body)
        await asyncio.sleep(0)
        if callable(responses):
            result = await responses(body)
        else:
            assert iterator is not None
            result = wire(next(iterator))
        return httpx2.Response(200, content=result, headers={"content-type": "text/event-stream"})

    context.model.transport = httpx2.MockTransport(respond)
    return context, requests


def test_multistep_evidence_idempotency_and_history(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        context, requests = harness(
            profile,
            [
                reply("", calls=[call("files.write", '{"path":"hello.txt","content":"hello λ"}')]),
                reply("Saved hello.txt."),
                reply("Second turn."),
            ],
        )
        await context.start()
        assert context.store and context.runs
        try:
            session = context.store.create_session()
            key = new_id()
            run = context.runs.submit(
                session.id,
                profile=context.profile,
                content="Write hello λ to hello.txt.",
                request_id=key,
            )
            assert (
                context.runs.submit(
                    session.id,
                    profile=context.profile,
                    content="Write hello λ to hello.txt.",
                    request_id=key,
                )
                == run
            )
            with pytest.raises(ValueError, match="idempotency"):
                context.runs.submit(
                    session.id, profile=context.profile, content="different", request_id=key
                )
            second = context.runs.submit(
                session.id, profile=context.profile, content="Next turn", request_id=new_id()
            )
            assert (await context.runs.wait(run.id, timeout=3)).state == RunState.COMPLETED
            assert (await context.runs.wait(second.id, timeout=3)).state == RunState.COMPLETED
            assert (profile.workspace_roots[0] / "hello.txt").read_text() == "hello λ"
            assert "Next turn" not in canonical(requests[0]).decode()
            exchange = requests[1]["messages"][-2:]
            assert requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
            assert exchange[0]["tool_calls"][0]["id"] == exchange[1]["tool_call_id"]
            assert json.loads(exchange[1]["content"])["status"] == "succeeded"
            assert any(m.get("tool_calls") for m in requests[2]["messages"])
            assert (
                context.runs.submit(
                    session.id,
                    profile=context.profile,
                    content="Write hello λ to hello.txt.",
                    request_id=key,
                ).id
                == run.id
            )
            with pytest.raises(ValueError):
                context.store.transition_run(run.id, RunState.FAILED)
        finally:
            await context.stop()
        assert not context.runs.active and not context.runs.sessions
        assert not context.inference.locked()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["arguments", "schema", "narration", "repeated"])
def test_independent_bounded_recovery(profile: EffectiveProfile, kind: str) -> None:
    async def scenario() -> None:
        action = reply("", calls=[call("files.write", '{"path":"out.txt","content":"ok"}')])
        first = reply("I'll write out.txt now.")
        if kind == "arguments":
            first = reply("original content", calls=[call("files.write", '{"path":')])
        if kind == "schema":
            first = action
        responses = [first, first if kind == "repeated" else action, reply("Saved out.txt.")]
        context, requests = harness(profile, responses)
        assert context.runs
        if kind == "schema":
            context.runs.deferred = frozenset({"files.write"})
        await context.start()
        assert context.store
        try:
            session = context.store.create_session()
            run = context.runs.submit(
                session.id,
                profile=context.profile,
                content="Write ok to out.txt.",
                request_id=new_id(),
            )
            result = await context.runs.wait(run.id, timeout=3)
            assert result.state == (RunState.FAILED if kind == "repeated" else RunState.COMPLETED)
            notes = [r for r in records(context.store, run.id) if isinstance(r, RecoveryNote)]
            assert len(notes) == 1
            assert len(requests) == (2 if kind == "repeated" else 3)
            if kind == "arguments":
                steps = [r for r in records(context.store, run.id) if isinstance(r, ModelStep)]
                assert steps[0].content == "original content"
                assert steps[0].tool_calls[0].arguments == '{"path":'
            if kind == "schema":
                assert "files.write" not in canonical(requests[0].get("tools", [])).decode()
                assert "files.write" in canonical(requests[1]["tools"]).decode()
        finally:
            await context.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "text",
    [
        'Explain "I will write the file now." Do not write anything.',
        "If someone said they would publish a note, what would that mean? Do not publish anything.",
        "Can you explain how to write a file?",
        "Write a poem about cats.",
        "Show me how to write a file.",
        "Can you show me how to write a file?",
        "Generate a list of ideas.",
        "Write a poem. Do not write a file.",
        "check this out, what do you think?",
        "Check out my new photo",
        "can you check the weather?",
        "Inspect this photo.",
        "Can you inspect this picture?",
        "Hello!",
    ],
)
def test_discussion_never_continues(profile: EffectiveProfile, text: str) -> None:
    assert not requested_tools(
        text, ("files.write", "publish_note", "images.generate", "runtime.status", "runtime_info")
    )

    async def scenario() -> None:
        context, requests = harness(profile, [reply('The phrase "I will write" describes intent.')])
        await context.start()
        assert context.store and context.runs
        try:
            session = context.store.create_session()
            run = context.runs.submit(
                session.id, profile=context.profile, content=text, request_id=new_id()
            )
            assert (await context.runs.wait(run.id, timeout=3)).state == RunState.COMPLETED
            assert len(requests) == 1 and context.store.tool_attempts(run.id) == 0
        finally:
            await context.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Check runtime.status", "runtime.status"),
        ("Inspect the evaluation runtime with runtime_info.", "runtime_info"),
    ],
)
def test_explicit_status_tool_request(text: str, expected: str) -> None:
    assert requested_tools(text, ("runtime.status", "runtime_info")) == {expected}


@pytest.mark.parametrize("after_effect", [False, True])
def test_cancellation_and_uncertainty(profile: EffectiveProfile, after_effect: bool) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        async def effect(context: ToolContext, args: ToolArguments) -> ToolResult:
            if after_effect:
                context.begin_effect(())
            entered.set()
            await asyncio.Event().wait()
            return ToolResult()

        context, _ = harness(
            profile,
            [reply("", calls=[call("test.effect")])],
            (Tool("test.effect", "Blocking fixture effect", ToolArguments, effect, True),),
        )
        await context.start()
        assert context.store and context.runs
        try:
            session = context.store.create_session()
            run = context.runs.submit(
                session.id, profile=context.profile, content="fixture", request_id=new_id()
            )
            await asyncio.wait_for(entered.wait(), 3)
            queued = context.runs.submit(
                session.id, profile=context.profile, content="queued", request_id=new_id()
            )
            assert context.runs.cancel(queued.id).state == RunState.CANCELLED
            context.runs.cancel(run.id)
            context.runs.cancel(run.id)
            assert (await context.runs.wait(run.id, timeout=3)).state == RunState.CANCELLED
            receipt = next(r for r in records(context.store, run.id) if isinstance(r, ToolReceipt))
            assert receipt.status == ("uncertain" if after_effect else "cancelled")
        finally:
            await context.stop()
        assert not context.inference.locked() and not context.runs.active

    asyncio.run(scenario())


def test_uncertain_response_cannot_complete(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        async def effect(context: ToolContext, args: ToolArguments) -> ToolResult:
            context.begin_effect(())
            return ToolResult(data="Maybe sent")

        context, requests = harness(
            profile,
            [reply("", calls=[call("test.effect")]), reply("Done!")],
            (Tool("test.effect", "Uncertain effect", ToolArguments, effect, True),),
        )
        await context.start()
        assert context.store and context.runs
        try:
            session = context.store.create_session()
            run = context.runs.submit(
                session.id, profile=context.profile, content="fixture", request_id=new_id()
            )
            result = await context.runs.wait(run.id, timeout=3)
            assert result.reason and result.reason.code == ErrorCode.EFFECT_UNCERTAIN
            assert result.state == RunState.FAILED and len(requests) == 1
        finally:
            await context.stop()

    asyncio.run(scenario())


def test_fairness_queue_bound_wait_disconnect_and_tool_permit(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        observed = []

        async def slow(context: ToolContext, args: ToolArguments) -> ToolResult:
            assert not app.inference.locked()
            entered.set()
            await release.wait()
            return ToolResult()

        async def respond(body: dict[str, Any]) -> bytes:
            users = [m["content"] for m in body["messages"] if m["role"] == "user"]
            text = users[-1]
            observed.append(text)
            if text == "slow" and not any(m["role"] == "tool" for m in body["messages"]):
                return wire(reply("", calls=[call("test.slow")]))
            return wire(reply("Finished."))

        app, _ = harness(profile, respond, (Tool("test.slow", "Wait", ToolArguments, slow),))
        assert app.runs
        app.runs.max_active = 2
        await app.start()
        assert app.store
        try:
            session = app.store.create_session()
            first = app.runs.submit(
                session.id, profile=app.profile, content="slow", request_id=new_id()
            )
            await asyncio.wait_for(entered.wait(), 3)
            with pytest.raises(TimeoutError):
                await app.runs.wait(first.id, timeout=0.01)
            waiter = asyncio.create_task(app.runs.wait(first.id))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            app.runs.max_pending = 1
            second = app.runs.submit(
                session.id, profile=app.profile, content="later", request_id=new_id()
            )
            with pytest.raises(QueueFull):
                app.runs.submit(
                    session.id, profile=app.profile, content="overflow", request_id=new_id()
                )
            app.runs.max_pending = 64
            others = []
            for i in range(12):
                topic = app.store.create_session()
                others.append(
                    app.runs.submit(
                        topic.id, profile=app.profile, content=f"other {i}", request_id=new_id()
                    )
                )
            for other in others:
                assert (await app.runs.wait(other.id, timeout=3)).state == RunState.COMPLETED
            assert observed == ["slow", *[f"other {i}" for i in range(12)]]
            assert app.store.run(second.id).state == RunState.QUEUED
            release.set()
            assert (await app.runs.wait(second.id, timeout=3)).state == RunState.COMPLETED
        finally:
            await app.stop()

    asyncio.run(scenario())


def test_restart_resumes_queued_only_and_revalidates(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        async def blocked(body: dict[str, Any]) -> bytes:
            entered.set()
            await asyncio.Event().wait()
            return b""

        app, _ = harness(profile, blocked)
        await app.start()
        assert app.store and app.runs
        session = app.store.create_session()
        active = app.runs.submit(
            session.id, profile=app.profile, content="active", request_id=new_id()
        )
        await asyncio.wait_for(entered.wait(), 3)
        queued = app.runs.submit(
            session.id, profile=app.profile, content="queued", request_id=new_id()
        )
        await app.stop()
        assert not app.runs.active and not app.runs.sessions
        replacement, requests = harness(profile, [reply("Resumed.")])
        await replacement.start()
        assert replacement.store and replacement.runs
        try:
            result = await replacement.runs.wait(queued.id, timeout=3)
            assert result.state == RunState.COMPLETED
            assert replacement.store.run(active.id).state == RunState.INTERRUPTED
            assert len(requests) == 1
        finally:
            await replacement.stop()

    asyncio.run(scenario())


def test_ephemeral_cleanup_and_same_session_wait(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        async def wait_on_self(context: ToolContext, args: ToolArguments) -> ToolResult:
            assert app.runs
            receipt = await app.runs.wait(context.run_id, timeout=0.1)
            assert receipt.state == RunState.RUNNING
            return ToolResult()

        app, requests = harness(
            profile,
            [
                item
                for _ in range(12)
                for item in (
                    reply("", calls=[call("test.wait")]),
                    reply("Finished."),
                )
            ],
            (Tool("test.wait", "Wait on same session", ToolArguments, wait_on_self),),
        )
        await app.start()
        assert app.store and app.runs
        ephemeral = app.profile.model_copy(
            update={"mode": SessionMode.EPHEMERAL, "memory_mode": "off"}
        )
        try:
            for i in range(12):
                session = app.store.create_session(mode=SessionMode.EPHEMERAL)
                run = app.runs.submit(
                    session.id, profile=ephemeral, content=f"private {i}", request_id=new_id()
                )
                assert (await app.runs.wait(run.id, timeout=3)).state == RunState.COMPLETED
                await asyncio.sleep(0)
                assert not app.runs.active and not app.runs.sessions
                assert f"private {i - 1}" not in canonical(requests[-1]).decode()
                assert project_run(app.store, run.id, 20)
        finally:
            await app.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("cap", [1, 11, 80])
def test_combined_receipt_render_is_bounded(cap: int) -> None:
    receipt = ToolReceipt(
        origin=Origin.INTERACTIVE,
        session_id=new_id(),
        run_id=new_id(),
        tool_call_id=new_id(),
        status="succeeded",
        result="🐈" * 80,
    )
    assert len(render_receipt(receipt, cap).encode()) <= cap
    exact = receipt.model_copy(update={"result": "blue\n"})
    assert json.loads(render_receipt(exact, 1000))["result"] == "blue\n"


@pytest.mark.parametrize("fault_kind", ["admission", "intent", "receipt", "stream"])
def test_faults_never_acknowledge_success_or_replay_effects(
    profile: EffectiveProfile,
    monkeypatch: pytest.MonkeyPatch,
    fault_kind: str,
) -> None:
    from solakit.evidence import StorageError
    from solakit.records import ToolCall

    async def scenario() -> None:
        async def respond(body: dict[str, Any]) -> bytes:
            data = wire(
                reply("", calls=[call("files.write", '{"path":"effect.txt","content":"once"}')])
            )
            return data.removesuffix(b"data: [DONE]\n\n") if fault_kind == "stream" else data

        app, requests = harness(profile, respond)
        await app.start()
        assert app.store and app.runs
        session = app.store.create_session()
        key = new_id()
        original = app.store.append

        def fail(record: Any) -> None:
            if (fault_kind == "intent" and isinstance(record, ToolCall)) or (
                fault_kind == "receipt" and isinstance(record, ToolReceipt)
            ):
                raise StorageError("Injected persistence failure")
            original(record)

        if fault_kind == "admission":

            def acknowledge(stage: str) -> None:
                if stage == "acknowledge":
                    raise OSError("Injected lost acknowledgement")

            app.store.fault = acknowledge
            with pytest.raises(OSError):
                app.runs.submit(session.id, profile=app.profile, content="fixture", request_id=key)
        else:
            monkeypatch.setattr(app.store, "append", fail)
            run = app.runs.submit(
                session.id, profile=app.profile, content="fixture", request_id=key
            )
            if fault_kind == "stream":
                assert (await app.runs.wait(run.id, timeout=3)).state == RunState.FAILED
            else:
                with pytest.raises(StorageError):
                    await app.runs.wait(run.id, timeout=3)
        await app.stop()
        assert not app.inference.locked()
        assert (profile.workspace_roots[0] / "effect.txt").exists() == (fault_kind == "receipt")
        replacement, resumed = harness(profile, [reply("Recovered queue.")])
        await replacement.start()
        assert replacement.store and replacement.runs
        try:
            saved = replacement.store.request(key)
            assert saved
            if fault_kind == "admission":
                assert (
                    await replacement.runs.wait(saved.id, timeout=3)
                ).state == RunState.COMPLETED
                assert (
                    replacement.runs.submit(
                        session.id, profile=replacement.profile, content="fixture", request_id=key
                    ).id
                    == saved.id
                )
                assert len(resumed) == 1 and not requests
            else:
                assert not resumed
                assert saved.state == (
                    RunState.FAILED if fault_kind == "stream" else RunState.INTERRUPTED
                )
                if fault_kind == "receipt":
                    receipt = next(
                        r
                        for r in records(replacement.store, saved.id)
                        if isinstance(r, ToolReceipt)
                    )
                    assert receipt.status == "uncertain"
                    assert (profile.workspace_roots[0] / "effect.txt").read_text() == "once"
        finally:
            await replacement.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("budget", ["model_calls", "tool_calls", "active_seconds"])
def test_run_budgets_and_model_wait_cancellation(profile: EffectiveProfile, budget: str) -> None:
    async def scenario() -> None:
        budgets = profile.budgets.model_copy(update={budget: 1})
        short = profile.model_copy(update={"budgets": budgets})

        async def respond(body: dict[str, Any]) -> bytes:
            if budget == "active_seconds":
                await asyncio.sleep(2)
            return wire(reply("", calls=[call("runtime.status"), call("runtime.status")]))

        app, requests = harness(short, respond)
        await app.start()
        assert app.store and app.runs
        try:
            session = app.store.create_session()
            run = app.runs.submit(
                session.id, profile=app.profile, content="fixture", request_id=new_id()
            )
            result = await app.runs.wait(run.id, timeout=3)
            assert result.state == RunState.FAILED
            assert result.reason and result.reason.code == ErrorCode.BUDGET_EXCEEDED
            assert len(requests) == 1
            assert app.store.tool_attempts(run.id) == (
                0 if budget == "active_seconds" else 1 if budget == "tool_calls" else 2
            )
            await app.inference.acquire()
            session = app.store.create_session()
            waiting = app.runs.submit(
                session.id, profile=app.profile, content="waiting", request_id=new_id()
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            app.runs.cancel(waiting.id)
            assert (await app.runs.wait(waiting.id, timeout=3)).state == RunState.CANCELLED
            assert not any(isinstance(r, ModelStep) for r in records(app.store, waiting.id))
            app.inference.release()
            assert not app.inference.locked()
        finally:
            await app.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["pin", "archived"])
def test_restart_revalidates_queued_admission(profile: EffectiveProfile, invalid: str) -> None:
    from solakit.store import Store

    async def scenario() -> None:
        store = Store(profile.data_dir)
        await store.start()
        session = store.create_session()
        queued = store.admit(
            session.id,
            request_id=new_id(),
            content="Write nothing",
            profile=profile.name,
            admission=profile.model_dump(mode="json"),
        )
        running = store.admit(
            session.id,
            request_id=new_id(),
            content="old interrupted",
            profile=profile.name,
            admission=profile.model_dump(mode="json"),
        )
        store.transition_run(running.id, RunState.RUNNING)
        await store.stop()
        app, requests = harness(profile, [reply("Should not run")])
        assert app.tools
        if invalid == "pin":
            app.tools.current = lambda admitted: profile.model_copy(update={"model_pin": "changed"})
        else:
            await store.start()
            store.update_session(session.id, archived=True)
            await store.stop()
        await app.start()
        assert app.store and app.runs
        try:
            result = await app.runs.wait(queued.id, timeout=3)
            assert result.reason and result.reason.code == (
                ErrorCode.CONFIGURATION if invalid == "pin" else ErrorCode.INVALID_INPUT
            )
            assert result.state == RunState.FAILED and not requests
            assert app.store.run(running.id).state == RunState.INTERRUPTED
        finally:
            await app.stop()

    asyncio.run(scenario())


def test_shared_inference_fifo_under_mixed_load(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        count = 0
        peak = 0
        order: list[str] = []

        async def respond(body: dict[str, Any]) -> bytes:
            nonlocal count, peak
            count += 1
            peak = max(count, peak)
            order.append(
                next(
                    m["content"]
                    for m in body["messages"]
                    if m["role"] == "user" and m.get("name") != "recorded_recall"
                )
            )
            await asyncio.sleep(0.001)
            count -= 1
            if body["messages"][-1]["role"] == "user":
                return wire(reply("", calls=[call("runtime.status")]))
            return wire(reply("Finished."))

        app, _ = harness(profile, respond)
        await app.start()
        assert app.store and app.runs
        try:
            runs = []
            for i in range(24):
                origin = Origin.SYSTEM if i % 2 else Origin.INTERACTIVE
                selected = app.profile.model_copy(update={"origin": origin})
                topic = app.store.create_session(origin=origin)
                runs.append(
                    app.runs.submit(
                        topic.id, profile=selected, content=f"run {i}", request_id=new_id()
                    )
                )
            results = await asyncio.wait_for(
                asyncio.gather(*(app.runs.wait(r.id) for r in runs)), 5
            )
            assert all(r.state == RunState.COMPLETED for r in results)
            assert peak == 1 and len(order) == 48
            assert order[:8] == [f"run {i}" for i in range(8)]
            for i in range(24):
                first, last = [index for index, name in enumerate(order) if name == f"run {i}"]
                assert last - first <= 8
            assert not app.inference.locked()
        finally:
            await app.stop()

    asyncio.run(scenario())


def test_waiting_for_queued_work_in_current_session_returns_admission(
    profile: EffectiveProfile,
) -> None:
    async def scenario() -> None:
        child_ids: list[str] = []

        async def enqueue(context: ToolContext, args: ToolArguments) -> ToolResult:
            assert app.runs
            child = app.runs.submit(
                context.session_id, profile=app.profile, content="child", request_id=new_id()
            )
            receipt = await app.runs.wait(child.id, timeout=0.1)
            assert receipt.state == RunState.QUEUED
            child_ids.append(child.id)
            return ToolResult(data={"run_id": receipt.id, "state": receipt.state.value})

        app, _ = harness(
            profile,
            [reply("", calls=[call("test.enqueue")]), reply("Parent"), reply("Child")],
            (Tool("test.enqueue", "Fixture admission", ToolArguments, enqueue),),
        )
        await app.start()
        assert app.store and app.runs
        try:
            session = app.store.create_session()
            parent = app.runs.submit(
                session.id, profile=app.profile, content="parent", request_id=new_id()
            )
            assert (await app.runs.wait(parent.id, timeout=3)).state == RunState.COMPLETED
            assert (await app.runs.wait(child_ids[0], timeout=3)).state == RunState.COMPLETED
        finally:
            await app.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("malformed", [False, True])
def test_refusal_and_unrepaired_calls_are_failed_outcomes(
    profile: EffectiveProfile, malformed: bool
) -> None:
    async def scenario() -> None:
        responses = [reply("I cannot write the file.")]
        if malformed:
            responses.insert(0, reply("", calls=[call("files.write", '{"path":')]))
        app, _ = harness(profile, responses)
        await app.start()
        assert app.store and app.runs
        try:
            session = app.store.create_session()
            run = app.runs.submit(
                session.id,
                profile=app.profile,
                content="Write ok to output.txt.",
                request_id=new_id(),
            )
            assert (await app.runs.wait(run.id, timeout=3)).state == RunState.FAILED
            assert app.store.tool_attempts(run.id) == 0
        finally:
            await app.stop()

    asyncio.run(scenario())


def test_confirmed_effect_argument_error_is_not_repaired(profile: EffectiveProfile) -> None:
    from solakit.sdk import ToolError

    async def scenario() -> None:
        async def effect(context: ToolContext, args: ToolArguments) -> ToolResult:
            context.files.write_text("partial.txt", "confirmed")
            raise ToolError("Error after confirmed work", ErrorCode.INVALID_INPUT)

        app, _ = harness(
            profile,
            [reply("", calls=[call("test.effect")]), reply("Finished.")],
            (Tool("test.effect", "Partial effect", ToolArguments, effect, True),),
        )
        await app.start()
        assert app.store and app.runs
        try:
            session = app.store.create_session()
            run = app.runs.submit(
                session.id, profile=app.profile, content="fixture", request_id=new_id()
            )
            assert (await app.runs.wait(run.id, timeout=3)).state == RunState.FAILED
            source = records(app.store, run.id)
            assert not any(isinstance(r, RecoveryNote) for r in source)
            assert next(r for r in source if isinstance(r, ToolReceipt)).effect == "confirmed"
            assert (profile.workspace_roots[0] / "partial.txt").read_text() == "confirmed"
            assert "Previous run outcome: failed" in project_run(app.store, run.id, 100)[-1].content
        finally:
            await app.stop()

    asyncio.run(scenario())


def test_required_memory_cannot_silently_run_without_recall(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        app, requests = harness(profile.model_copy(update={"memory_mode": "required"}), [reply()])
        await app.start()
        assert app.store and app.runs
        try:
            session = app.store.create_session()
            run = app.runs.submit(
                session.id, profile=app.profile, content="hello", request_id=new_id()
            )
            outcome = await app.runs.wait(run.id, timeout=3)
            assert outcome.state == RunState.FAILED and outcome.reason
            assert outcome.reason.code == ErrorCode.MEMORY_UNAVAILABLE
            assert not requests
        finally:
            await app.stop()

    asyncio.run(scenario())


def test_memory_reply_does_not_wait_for_extraction(profile: EffectiveProfile) -> None:
    from solakit.config import MemorySettings
    from solakit.records import MemoryRecall

    async def scenario() -> None:
        selected = profile.model_copy(
            update={
                "memory": MemorySettings(
                    enabled=True,
                    account="test",
                    roots=("viking://user/default/memories",),
                )
            }
        )
        app, requests = harness(
            selected,
            [
                reply("", calls=[call("memory.remember", '{"content":"precise λ"}')]),
                reply("The note is staged."),
            ],
        )
        assert app.memory
        memory_requests: list[httpx2.Request] = []
        submitting = asyncio.Event()
        release = asyncio.Event()

        async def respond(request: httpx2.Request) -> httpx2.Response:
            memory_requests.append(request)
            if request.url.path == "/health":
                return httpx2.Response(200, json={"status": "ok", "version": "0.5.5"})
            if request.url.path.endswith("/find"):
                result: dict[str, Any] = {"memories": []}
            elif request.url.path.endswith("/commit"):
                submitting.set()
                await release.wait()
                result = {"task_id": "test-task"}
            elif request.url.path == "/api/v1/sessions":
                session = json.loads(request.content)["session_id"]
                result = {
                    "session_id": session,
                    "uri": f"viking://user/default/sessions/{session}",
                    "user": {"account_id": "test", "user_id": "default"},
                }
            else:
                result = {"status": "completed"}
            return httpx2.Response(200, json={"status": "ok", "result": result})

        app.memory.adapter.transport = httpx2.MockTransport(respond)
        await app.start()
        assert app.store and app.runs
        try:
            topic = app.store.create_session()
            run = app.runs.submit(
                topic.id, profile=app.profile, content="Remember this note.", request_id=new_id()
            )
            assert (await app.runs.wait(run.id, timeout=2)).state == RunState.COMPLETED
            await asyncio.wait_for(submitting.wait(), 2)
            source = records(app.store, run.id)
            assert sum(isinstance(r, MemoryRecall) for r in source) == 1
            receipt = next(r for r in source if isinstance(r, ToolReceipt))
            assert receipt.status == "succeeded" and "staged" in str(receipt.result)
            assert sum(r.url.path.endswith("/find") for r in memory_requests) == 1
            assert len(requests) == 2
            recall = [m for m in requests[0]["messages"] if m.get("name") == "recorded_recall"]
            assert json.loads(recall[0]["content"])["status"] == "ok"
        finally:
            release.set()
            await app.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("required", [False, True])
def test_memory_outage_is_recorded_separately_from_reply(
    profile: EffectiveProfile, required: bool
) -> None:
    from solakit.config import MemorySettings
    from solakit.records import MemoryRecall

    async def scenario() -> None:
        selected = profile.model_copy(
            update={
                "memory": MemorySettings(enabled=True, roots=("viking://user/default/memories",)),
                "memory_mode": "required" if required else "optional",
            }
        )
        app, requests = harness(selected, [reply("Hello.")])
        assert app.memory
        app.memory.adapter.transport = httpx2.MockTransport(lambda r: httpx2.Response(503))
        await app.start()
        assert app.store and app.runs
        try:
            session = app.store.create_session()
            run = app.runs.submit(
                session.id, profile=app.profile, content="Hello", request_id=new_id()
            )
            outcome = await app.runs.wait(run.id, timeout=2)
            assert outcome.state == (RunState.FAILED if required else RunState.COMPLETED)
            observation = next(r for r in records(app.store, run.id) if isinstance(r, MemoryRecall))
            assert observation.status == "degraded"
            assert len(requests) == (0 if required else 1)
        finally:
            await app.stop()

    asyncio.run(scenario())
