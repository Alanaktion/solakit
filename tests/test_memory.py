import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError
from test_execution import completion, model_list, setup

from solakit.core.config import MemoryConfig
from solakit.core.memory import MemoryClient, MemoryUnavailable, memory_messages
from solakit.core.schemas import Session
from solakit.core.store import Store


def memory_config():
    return MemoryConfig(
        enabled=True, base_url="http://memory", roots=["viking://user/sola/memories"]
    )


def test_recall_then_commit_then_recall(config):
    remembered = []
    batches = []
    model_requests = []

    def memory_handler(request):
        path = request.url.path
        assert request.headers["X-API-Key"] == "memory-test-secret"
        result = {}
        if path.endswith("search/find"):
            result = {"memories": remembered.copy()}
        elif path.endswith("messages/batch"):
            batches.append(json.loads(request.content)["messages"])
        elif path.endswith("/commit"):
            remembered.append(
                {
                    "uri": "viking://user/sola/memories/fact.md",
                    "abstract": "Sola likes apricots.",
                }
            )
            result = {
                "status": "accepted",
                "task_id": "task1",
                "archive_uri": "viking://user/sola/sessions/test/history/archive_001",
            }
        return httpx.Response(200, json={"status": "ok", "result": result})

    def model_handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        model_requests.append(json.loads(request.content))
        return completion(config, "I remember.")

    config.memory = memory_config()
    (config.paths.fragments / "recall-header.md").write_text("Recalled records:")
    execution = setup(config, model_handler)
    execution.memory = MemoryClient(
        config.memory,
        "memory-test-secret",
        transport=httpx.MockTransport(memory_handler),
    )

    async def exercise():
        first = await execution.submit("s", "Sola likes apricots.")
        assert first.status == "completed"
        assert first.memory_status == "accepted"
        assert first.memory_commit["task_id"] == "task1"
        second = await execution.submit("s", "What does Sola like?")
        assert second.status == "completed"
        record = execution.store.run(second.id)
        prompt, turn = record["prompt"], record["turn_context"]
        assert (
            "<recall>\nviking://user/sola/memories/fact.md\nSola likes apricots."
            in turn
        )
        assert "<recall>" not in prompt
        assert model_requests[1]["messages"][0]["content"] == prompt
        assert model_requests[1]["messages"][-1]["content"].startswith(turn)
        # Repeating a known acknowledged commit never appends messages twice.
        scoped = execution.memory.scoped(Session(id="s"))
        count = len(batches)
        result = await scoped.commit(execution.store, first.id, [])
        assert result["task_id"] == "task1"
        assert len(batches) == count
        assert [m["role"] for m in batches[0]] == ["user", "assistant"]
        await execution.close()

    asyncio.run(exercise())


def test_recall_failure_leaves_transcript_untouched(config):
    def model_handler(request):
        assert request.url.path.endswith("/models")
        return model_list(config)

    def memory_handler(request):
        raise httpx.ConnectError("offline")

    config.memory = memory_config()
    execution = setup(config, model_handler)
    execution.memory = MemoryClient(
        config.memory, transport=httpx.MockTransport(memory_handler)
    )

    async def exercise():
        run = await execution.submit("s", "hello")
        assert run.status == "memory_unavailable"
        assert run.memory_status == "recall_failed"
        assert execution.store.transcript("s") == []
        await execution.close()

    asyncio.run(exercise())


def test_ambiguous_commit_refuses_replay_and_scope_checks(config):
    writes = []

    def handler(request):
        if request.url.path.endswith("messages/batch"):
            writes.append(request.content)
            # Server might have accepted this before the connection disappeared.
            raise httpx.ReadError("lost acknowledgment")
        if request.url.path.endswith("search/find"):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "result": {
                        "memories": [
                            {"uri": "viking://user/other/private", "abstract": "no"}
                        ]
                    },
                },
            )
        return httpx.Response(200, json={"status": "ok", "result": {}})

    client = MemoryClient(memory_config(), transport=httpx.MockTransport(handler))
    store = Store(config.paths.data)
    scoped = client.scoped(Session(id="s"))

    async def exercise():
        for _attempt in range(2):
            with pytest.raises(MemoryUnavailable):
                await scoped.commit(
                    store, "run1", [{"role": "user", "content": "fact"}]
                )
        assert len(writes) == 1
        assert store.memory_events("run1")[-1]["stage"] == "sending"
        with pytest.raises(MemoryUnavailable):
            await scoped.search("anything")
        for scope in ("isolated", "ephemeral"):
            with pytest.raises(MemoryUnavailable):
                await client.scoped(Session(memory_scope=scope)).search("query")
        for uri in (
            "viking://user/sola/memories/../private",
            "viking://user/sola/memories-other/file",
        ):
            with pytest.raises(MemoryUnavailable):
                await scoped.read(uri)
        await client.close()

    asyncio.run(exercise())


def test_tool_parts_and_invalid_memory_configuration():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "c1", "function": {"name": "example", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    converted = memory_messages(messages)
    assert converted[0]["parts"][0]["tool_output"] == "result"
    assert converted[0]["parts"][0]["tool_status"] == "completed"
    with pytest.raises(ValidationError):
        MemoryConfig(enabled=True)


def test_commit_failure_is_visible_without_losing_conversation(config):
    def memory_handler(request):
        if request.url.path.endswith("search/find"):
            return httpx.Response(
                200, json={"status": "ok", "result": {"memories": []}}
            )
        if request.url.path.endswith("/commit"):
            return httpx.Response(503)
        return httpx.Response(200, json={"status": "ok", "result": {}})

    def model_handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        return completion(config, "finished")

    execution = setup(config, model_handler)
    execution.memory = MemoryClient(
        memory_config(), transport=httpx.MockTransport(memory_handler)
    )

    async def exercise():
        run = await execution.submit("s", "fact")
        assert run.status == "completed"
        assert run.memory_status == "commit_failed_or_uncertain"
        assert len(execution.store.transcript("s")) == 2
        assert execution.store.run(run.id)["memory_events"][-1]["stage"] == "committing"
        # Ephemeral sessions do not touch external memory.
        execution.store.create_session(Session(id="temp", memory_scope="ephemeral"))
        run = await execution.submit("temp", "experiment")
        assert run.status == "completed"
        assert run.memory_status == "ephemeral"
        assert execution.store.run(run.id)["memory_events"] == []
        await execution.close()

    asyncio.run(exercise())


def test_background_refresh_records_terminal_failure_without_replaying(config):
    from solakit.core.memory_ops import refresh_pending
    from solakit.core.schemas import Origin, Run

    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "result": {"status": "failed", "error": "extraction failed"},
            },
        )

    execution = setup(config, lambda request: None)
    execution.memory = MemoryClient(
        memory_config(), transport=httpx.MockTransport(handler)
    )
    run = Run(
        session="s",
        profile="interactive",
        origin=Origin(),
        status="completed",
        memory_status="accepted",
        memory_commit={"status": "accepted", "task_id": "t1"},
    )
    execution.store.save_run(run)
    execution.store.log_memory(
        run.id, {"stage": "accepted", "result": run.memory_commit}
    )

    async def exercise():
        await refresh_pending(execution)
        record = execution.store.run(run.id)
        assert record["status"] == "completed"
        assert record["memory_status"] == "failed"
        assert record["memory_commit"]["task"]["error"] == "extraction failed"
        await refresh_pending(execution)
        assert len(calls) == 1
        # Delivery updates only their own fields, so extraction status is retained.
        execution.store.update_run(run.id, {"delivery_status": "delivered"})
        assert execution.store.run(run.id)["memory_status"] == "failed"
        await execution.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("actual_user", ["sola", "wrong"])
def test_explicit_openviking_user_checked_before_message_writes(config, actual_user):
    cfg = memory_config()
    cfg.user = "sola"
    writes = []

    def handler(request):
        assert request.headers["X-OpenViking-User"] == "sola"
        if request.url.path.endswith("/solakit-r"):
            result = {"uri": f"viking://user/{actual_user}/sessions/solakit-r"}
        elif request.url.path.endswith("/commit"):
            result = {"status": "accepted", "task_id": "t1"}
        else:
            if request.url.path.endswith("messages/batch"):
                writes.append(True)
            result = {}
        return httpx.Response(200, json={"status": "ok", "result": result})

    memory = MemoryClient(cfg, transport=httpx.MockTransport(handler))
    store = Store(config.paths.data)

    async def exercise():
        if actual_user == "wrong":
            with pytest.raises(MemoryUnavailable, match="user does not match"):
                await memory.scoped(Session(id="s")).commit(
                    store, "r", [{"role": "user", "content": "fact"}]
                )
            assert not writes
        else:
            await memory.scoped(Session(id="s")).commit(
                store, "r", [{"role": "user", "content": "fact"}]
            )
            assert writes == [True]
        await memory.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            '{"path":"scratch/file","options":{"enabled":false,"limit":0},'
            '"labels":["月",null]}',
            {
                "path": "scratch/file",
                "options": {"enabled": False, "limit": 0},
                "labels": ["月", None],
            },
        ),
        ('{"incomplete":', {"invalid_arguments": '{"incomplete":'}),
    ],
)
def test_memory_tool_arguments_preserve_nested_values_and_invalid_evidence(
    raw, expected
):
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "nested", "function": {"name": "example", "arguments": raw}}
            ],
        },
        {"role": "tool", "tool_call_id": "nested", "content": "complete output"},
    ]
    before = json.dumps(messages, ensure_ascii=False)
    part = memory_messages(messages)[0]["parts"][0]
    assert part["tool_input"] == expected
    assert part["tool_output"] == "complete output"
    assert json.dumps(messages, ensure_ascii=False) == before


def test_observer_backoff_and_manual_recovery(config, monkeypatch):
    from solakit.core import memory_ops
    from solakit.core.schemas import Origin, Run

    execution = setup(config, lambda request: None)
    run = Run(
        session="s",
        profile="interactive",
        origin=Origin(),
        status="completed",
        memory_status="accepted",
    )
    execution.store.save_run(run)
    attempts = []

    async def unavailable(*args, **kwargs):
        attempts.append(1)
        raise MemoryUnavailable("unknown task")

    original_refresh = memory_ops.refresh_run
    monkeypatch.setattr(memory_ops, "refresh_run", unavailable)

    async def exercise():
        await memory_ops.refresh_pending(execution)
        await memory_ops.refresh_pending(execution)
        assert len(attempts) == 1
        for _ in range(7):
            execution.store.update_run(run.id, {"memory_next_observation": None})
            await memory_ops.refresh_pending(execution)
        record = execution.store.run(run.id)
        assert record["memory_status"] == "status_lost"
        assert record["memory_observation_failures"] == 8
        await memory_ops.refresh_pending(execution)
        assert len(attempts) == 8
        execution.store.log_memory(
            run.id,
            {"stage": "accepted", "result": {"status": "accepted", "task_id": "t1"}},
        )
        execution.memory = MemoryClient(
            memory_config(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"status": "ok", "result": {"status": "completed"}}
                )
            ),
        )
        monkeypatch.setattr(memory_ops, "refresh_run", original_refresh)
        recovered = await memory_ops.refresh_run(execution, run.id)
        assert recovered.memory_status == "completed"
        assert recovered.memory_observation_failures == 0
        assert recovered.memory_next_observation is None
        await execution.close()

    asyncio.run(exercise())
