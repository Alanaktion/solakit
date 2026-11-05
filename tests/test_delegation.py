"""One-slot delegation retains private child evidence and bounded reports."""

import asyncio
import json

import httpx
import pytest

from solakit.core.config import Config, DelegationConfig, Profile
from solakit.core.loop import Execution
from solakit.core.model import ModelClient
from solakit.core.schemas import Origin, Session
from solakit.core.store import Store
from solakit.tools import Registry, ToolContext, ToolResult, tool


def answer(config, content=None, *, name=None, arguments=None):
    message = {"role": "assistant", "content": content}
    if name:
        message["tool_calls"] = [
            {
                "id": "call",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments or {}),
                },
            }
        ]
    return httpx.Response(
        200,
        json={
            "model": config.model.model_id,
            "choices": [
                {"message": message, "finish_reason": "tool_calls" if name else "stop"}
            ],
        },
    )


def setup(config, worker=None, *, memory=None):
    config.enabled_tools = ["delegate"]
    config.model.concurrency = 1
    config.profiles["interactive"].tools = ["delegate.run"]
    config.profiles["interactive"].context_budget = 100000
    config.profiles["research"] = Profile(context_budget=100000)
    config.delegation = DelegationConfig(allowed_profiles=["research"])
    (config.paths.fragments / "profiles/research.md").write_text("Research worker.")
    (config.paths.fragments / "recall-header.md").write_text("Scoped recall:")
    store = Store(config.paths.data)
    store.create_session(Session(id="parent"))
    registry = Registry(config)
    args = {"task": "Find the answer\nKeep this brief verbatim.", "profile": "research"}
    captured = []

    async def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})
        data = json.loads(request.content)
        captured.append(data)
        if "profile: research\n" in data["messages"][0]["content"]:
            if worker:
                return await worker(data)
            return answer(config, "A concise worker report")
        if data["messages"][-1]["role"] == "user":
            return answer(config, name="delegate.run", arguments=args)
        return answer(config, "Parent final reply")

    execution = Execution(
        config,
        store,
        registry,
        ModelClient(config.model, transport=httpx.MockTransport(handler)),
        memory,
    )
    return execution, args, captured


def receipt(execution, run):
    logs = execution.store.run(run.id)["tools"]
    result = logs[-1]["result"]
    return result, json.loads(result["content"].split("\n", 1)[1].rsplit("\n", 1)[0])


class MemorySpy:
    def __init__(self):
        self.calls = []

    def scoped(self, session):
        spy = self

        class Scoped:
            async def search(self, query):
                spy.calls.append((session.id, "search"))
                return "Remember this parent context"

            async def commit(self, store, run_id, messages):
                spy.calls.append((session.id, "commit"))
                return {"status": "committed"}

        return Scoped()

    async def close(self):
        pass


def test_one_slot_isolation_ephemeral_memory_and_continuation(config):
    memory = MemorySpy()

    async def worker(data):
        messages = data["messages"]
        start = max(i for i, m in enumerate(messages) if m["role"] == "user")
        if sum(m["role"] == "tool" for m in messages[start:]) < 2:
            return answer(config, name="fixture.read")
        return answer(config, "Only the final report")

    execution, args, captured = setup(config, worker, memory=memory)

    @tool(name="fixture.read", profiles=["research"])
    async def read(ctx: ToolContext) -> ToolResult:
        return ToolResult(content="CHILD BULK DATA " * 500)

    execution.registry.register(read)
    config.profiles["research"].tools = ["fixture.read"]
    execution.registry.validate()

    async def exercise():
        try:
            async with asyncio.timeout(5):
                parent = await execution.submit("parent", "Parent-only context")
                assert parent.status == "completed"
                result, data = receipt(execution, parent)
                assert not result["error"] and data["report"] == "Only the final report"
                child = execution.store.session(data["session_id"])
                assert (
                    child.parent_session == "parent"
                    and child.memory_scope == "ephemeral"
                )
                assert (
                    child.bound_channel is None and child.default_profile == "research"
                )
                assert child.title == "Find the answer"
                child_run = execution.store.run(data["run_id"])
                assert child_run["input_message"] == args["task"]
                assert child_run["memory_status"] == "ephemeral"
                assert child_run["deliveries"] == []
                assert child_run["origin"] == {
                    "channel": "delegate",
                    "channel_id": "parent",
                    "author_id": None,
                    "external_id": parent.id,
                    "job": None,
                }
                history = execution.store.transcript("parent")
                assert [m["role"] for m in history] == [
                    "user",
                    "assistant",
                    "tool",
                    "assistant",
                ]
                assert "CHILD BULK DATA" not in json.dumps(history)
                for body in captured:
                    prompt = body["messages"][0]["content"]
                    if "profile: interactive\n" in prompt:
                        assert "CHILD BULK DATA" not in json.dumps(body)
                    else:
                        assert "Parent-only context" not in json.dumps(body)
                        assert "<recall>" not in prompt
                assert memory.calls == [("parent", "search"), ("parent", "commit")]
                args["session"] = child.id
                args["task"] = "Continue with the next question"
                again = await execution.submit("parent", "Continue")
                _, second = receipt(execution, again)
                assert (
                    second["session_id"] == child.id
                    and second["run_id"] != data["run_id"]
                )
                assert len(execution.store.transcript(child.id)) == 12
                assert (
                    len(execution.store.search("CHILD BULK DATA", session_id=child.id))
                    == 4
                )
        finally:
            await execution.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "refusal", ["profile", "other_parent", "archived", "shared", "bound", "blank"]
)
def test_refusals_create_no_child_runs_and_are_recorded(config, refusal):
    execution, args, _ = setup(config)
    if refusal == "profile":
        args["profile"] = "interactive"
    elif refusal == "blank":
        args["task"] = "  "
    else:
        child = execution.store.create_session(
            Session(
                id="child",
                parent_session="elsewhere" if refusal == "other_parent" else "parent",
                memory_scope="shared" if refusal == "shared" else "ephemeral",
                bound_channel="operator-pin" if refusal == "bound" else None,
            )
        )
        if refusal == "archived":
            execution.store.set_session_archived(child.id, True)
        args["session"] = child.id
    count = len(execution.store.list_records("sessions"))

    async def exercise():
        try:
            async with asyncio.timeout(5):
                parent = await execution.submit("parent", "Try delegation")
                assert parent.status == "completed"
                assert execution.store.run(parent.id)["tools"][0]["result"]["error"]
                assert len(execution.store.list_records("runs")) == 1
                assert len(execution.store.list_records("sessions")) == count
        finally:
            await execution.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["budget", "unavailable"])
def test_child_failure_returns_ids_and_no_report(config, failure):
    async def worker(data):
        if failure == "unavailable":
            raise httpx.ConnectError("offline")
        return answer(config, name="fixture.read")

    execution, _, _ = setup(config, worker)
    config.profiles["research"].max_steps = 1

    async def exercise():
        try:
            async with asyncio.timeout(5):
                parent = await execution.submit("parent", "Delegate")
                result, data = receipt(execution, parent)
                assert result["error"] and "report" not in data
                assert data["session_id"] and data["run_id"]
                assert data["status"] == (
                    "budget_exceeded" if failure == "budget" else "model_unavailable"
                )
                assert execution.store.run(data["run_id"])["status"] == data["status"]
        finally:
            await execution.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "mode", ["parent_cancel", "child_cancel", "shutdown", "timeout"]
)
def test_nested_cancellation_finalizes_and_timeout_overrides_generic(config, mode):
    async def exercise():
        entered = asyncio.Event()

        async def worker(data):
            entered.set()
            await asyncio.Event().wait()

        execution, _, _ = setup(config, worker)
        config.profiles["interactive"].tool_timeout_s = 0.001
        config.delegation.timeout_s = 0.15 if mode == "timeout" else 5
        try:
            async with asyncio.timeout(5):
                pending = await execution.enqueue("parent", "Delegate")
                await entered.wait()
                child = next(p for p in execution.pending.values() if p.parent_run)
                if mode == "parent_cancel":
                    await execution.cancel(pending.run.id)
                elif mode == "child_cancel":
                    await execution.cancel(child.run.id)
                elif mode == "shutdown":
                    await execution.close()
                parent = await pending.future
                assert parent.status == (
                    "completed" if mode in {"timeout", "child_cancel"} else "cancelled"
                )
                record = execution.store.run(child.run.id)
                assert record["status"] == "cancelled" and record["finished_at"]
                assert child.future.done()
                if parent.status == "completed":
                    result, data = receipt(execution, parent)
                    assert result["error"] and data["run_id"] == child.run.id
                    assert data["status"] == "cancelled" and "report" not in data
                    assert bool(data.get("timed_out")) == (mode == "timeout")
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_config_validation_grants_and_nested_guard(config):
    with pytest.raises(ValueError, match="unknown delegation profile"):
        Config(delegation={"allowed_profiles": ["missing"]})
    for timeout in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            DelegationConfig(timeout_s=timeout)
    execution, _, _ = setup(config)
    assert "delegate.run" not in [
        s["function"]["name"] for s in execution.registry.schemas("research")
    ]

    async def exercise():
        with pytest.raises(ValueError, match="running tool call"):
            await execution.submit_nested(
                "missing", "task", profile="research", origin=Origin()
            )
        await execution.close()

    asyncio.run(exercise())


def test_normal_child_turns_cannot_overlap_nested_work(config):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        active = 0

        async def worker(data):
            nonlocal active
            active += 1
            assert active == 1, "Two turns entered the same child session"
            task = next(
                m["content"] for m in reversed(data["messages"]) if m["role"] == "user"
            )
            if task != "External queued turn":
                entered.set()
                await release.wait()
            active -= 1
            return answer(
                config,
                "external report"
                if task == "External queued turn"
                else "nested report",
            )

        execution, _, _ = setup(config, worker)
        config.model.concurrency = 2
        execution.semaphore = asyncio.Semaphore(2)
        try:
            async with asyncio.timeout(5):
                parent = await execution.enqueue("parent", "Delegate")
                await entered.wait()
                child = next(p for p in execution.pending.values() if p.parent_run)
                external = await execution.enqueue(
                    child.run.session, "External queued turn", profile="research"
                )
                await asyncio.sleep(0.02)
                assert external.run.status == "queued"
                release.set()
                await parent.future
                await external.future
                assert external.run.status == "completed"
                _, data = receipt(execution, parent.run)
                assert data["report"] == "nested report"
                assert (
                    execution.store.run(external.run.id)["messages"][-1]["content"]
                    == "external report"
                )
        finally:
            release.set()
            await execution.close()

    asyncio.run(exercise())


def test_busy_continuation_refuses_instead_of_waiting_for_queue(config):
    async def exercise():
        entered = asyncio.Event()

        async def worker(data):
            entered.set()
            await asyncio.Event().wait()

        execution, args, _ = setup(config, worker)
        config.model.concurrency = 2
        execution.semaphore = asyncio.Semaphore(2)
        execution.store.create_session(
            Session(
                id="child",
                parent_session="parent",
                memory_scope="ephemeral",
                default_profile="research",
            )
        )
        args["session"] = "child"
        try:
            async with asyncio.timeout(5):
                other = await execution.enqueue("child", "Already running")
                await entered.wait()
                parent = await execution.submit("parent", "Continue busy child")
                log = execution.store.run(parent.id)["tools"][0]
                assert log["result"]["error"]
                assert "queued or running" in log["result"]["content"]
                assert len(execution.store.list_records("runs")) == 2
                await execution.cancel(other.run.id)
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_cancellation_during_nested_admission_finishes_accepted_run(
    config, monkeypatch
):
    import threading

    async def exercise():
        saving, release = threading.Event(), threading.Event()
        execution, _, _ = setup(config)
        original = execution.store.save_run

        def save(run):
            if run.origin.channel == "delegate" and run.status == "queued":
                saving.set()
                assert release.wait(5)
            original(run)

        monkeypatch.setattr(execution.store, "save_run", save)
        try:
            async with asyncio.timeout(5):
                parent = await execution.enqueue("parent", "Delegate")
                assert await asyncio.to_thread(saving.wait, 3)
                cancellation = asyncio.create_task(execution.cancel(parent.run.id))
                await asyncio.sleep(0.01)
                assert not cancellation.done()
                release.set()
                await cancellation
                assert (await parent.future).status == "cancelled"
                records = execution.store.list_records("runs")
                assert len(records) == 2
                assert all(
                    r["status"] == "cancelled" and r["finished_at"] for r in records
                )
                assert not any(p.parent_run for p in execution.pending.values())
        finally:
            release.set()
            await execution.close()

    asyncio.run(exercise())


def test_worker_reports_obey_parent_output_limit(config):
    async def worker(data):
        return answer(config, "REPORT_CONTENT " * 1000)

    execution, _, captured = setup(config, worker)
    config.profiles["interactive"].tool_output_max_chars = 512

    async def exercise():
        try:
            async with asyncio.timeout(5):
                parent = await execution.submit("parent", "Delegate")
                record = execution.store.run(parent.id)["tools"][0]
                result = record["result"]
                assert parent.status == "completed" and not result["error"]
                assert (
                    not result["content_complete"]
                    and "[truncated " in result["content"]
                )
                assert len(result["content"]) <= 512 and record["omitted_chars"] > 0
                parent_bodies = [
                    body
                    for body in captured
                    if "profile: interactive\n" in body["messages"][0]["content"]
                ]
                assert all(
                    "REPORT_CONTENT " * 1000 not in json.dumps(body)
                    for body in parent_bodies
                )
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_grants_allow_nested_depth_and_parent_cancellation_propagates(config):
    async def exercise():
        entered = asyncio.Event()

        async def worker(data):
            task = next(
                m["content"] for m in reversed(data["messages"]) if m["role"] == "user"
            )
            if task == "Grandchild task":
                entered.set()
                await asyncio.Event().wait()
            return answer(
                config,
                name="delegate.run",
                arguments={"task": "Grandchild task", "profile": "research"},
            )

        execution, _, _ = setup(config, worker)
        config.profiles["research"].tools = ["delegate.run"]
        execution.registry.validate()
        try:
            async with asyncio.timeout(5):
                parent = await execution.enqueue("parent", "Delegate")
                await entered.wait()
                assert len(execution.pending) == 3
                await execution.cancel(parent.run.id)
                assert (await parent.future).status == "cancelled"
                records = execution.store.list_records("runs")
                assert len(records) == 3
                assert all(record["status"] == "cancelled" for record in records)
        finally:
            await execution.close()

    asyncio.run(exercise())
