import asyncio
import json

import httpx
import pytest
from test_execution import completion, model_list, setup
from test_memory import memory_config

from solakit.core.compaction import Compactor, ContextLimit
from solakit.core.memory import MemoryClient
from solakit.core.schemas import Message, Origin, Session
from solakit.core.store import Store


def memory_handler(
    recorded, *, task_status="completed", overview="Earlier conversation summary"
):
    def handle(request):
        recorded.append(
            (
                request.method,
                request.url.path,
                json.loads(request.content) if request.content else None,
            )
        )
        path = request.url.path
        if path.endswith("search/find"):
            result = {"memories": []}
        elif path.endswith("/commit"):
            remote = path.split("/")[-2]
            result = {
                "status": "accepted",
                "task_id": "t1",
                "archive_uri": f"viking://user/sola/sessions/{remote}/history/archive_001",
            }
        elif "/tasks/" in path:
            result = {"status": task_status}
        elif "/archives/" in path:
            result = {
                "status": "completed",
                "archive_id": "archive_001",
                "overview": overview,
            }
        else:
            result = {}
        return httpx.Response(200, json={"status": "ok", "result": result})

    return handle


def seed(store, text="x" * 2000):
    store.create_session(Session(id="s"))
    store.append_message(
        "s", Message(role="user", content=text, origin=Origin(), run_id="old")
    )
    store.append_message(
        "s", Message(role="assistant", content="reply", origin=Origin(), run_id="old")
    )


def test_compaction_preserves_archive_and_index_rebuild(config):
    seen = []
    memory = MemoryClient(
        memory_config(), transport=httpx.MockTransport(memory_handler(seen))
    )
    store = Store(config.paths.data)
    seed(store)
    source = (store.root / "sessions/s.jsonl").read_bytes()

    async def exercise():
        first = await Compactor(store, memory).compact(store.session("s"))
        assert first["stage"] == "committed"
        assert (store.root / "sessions/s.jsonl.1").read_bytes() == source
        boundary = store.transcript("s")[0]
        assert boundary["kind"] == "compaction"
        assert boundary["content"] == "Earlier conversation summary"
        assert boundary["summary_uri"].endswith("/.overview.md")
        create = next(body for method, path, body in seen if path == "/api/v1/sessions")
        assert create["memory_policy"]["self"]["enabled"] is False
        assert create["memory_policy"]["peer"]["enabled"] is False
        assert create["memory_policy"]["working_memory"]["enabled"] is True
        store.append_message(
            "s", Message(role="user", content="more", origin=Origin(), run_id="new")
        )
        await Compactor(store, memory).compact(store.session("s"))
        assert store.archives("s") == ["s.jsonl.1", "s.jsonl.2"]
        assert len(store.all_messages("s")) == 5
        (store.root / "app.db").unlink()
        restarted = Store(config.paths.data)
        assert len(restarted.all_messages("s")) == 5
        assert restarted.transcript("s")[0]["kind"] == "compaction"
        await memory.close()

    asyncio.run(exercise())


def test_rotation_recovers_crash_after_archive_rename(config, monkeypatch):
    import solakit.core.compaction as module

    memory = MemoryClient(
        memory_config(), transport=httpx.MockTransport(memory_handler([]))
    )
    store = Store(config.paths.data)
    seed(store)
    source = (store.root / "sessions/s.jsonl").read_bytes()
    original = module.atomic_write

    def fail_new_active(path, payload):
        if path.name == "s.jsonl":
            raise OSError("simulated crash after archival")
        original(path, payload)

    async def exercise():
        with monkeypatch.context() as patch:
            patch.setattr(module, "atomic_write", fail_new_active)
            with pytest.raises(OSError):
                await Compactor(store, memory).compact(store.session("s"))
        assert not (store.root / "sessions/s.jsonl").exists()
        restarted = Store(config.paths.data)
        assert (store.root / "sessions/s.jsonl.1").read_bytes() == source
        assert restarted.transcript("s")[0]["content"] == "Earlier conversation summary"
        assert (
            json.loads((store.root / "sessions/s.compaction.json").read_bytes())[
                "stage"
            ]
            == "committed"
        )
        await memory.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("status", ["pending", "failed", "cancelled"])
def test_incomplete_summary_never_rotates_history(config, status):
    cfg = memory_config()
    cfg.timeout_s = 0.001
    memory = MemoryClient(
        cfg, transport=httpx.MockTransport(memory_handler([], task_status=status))
    )
    store = Store(config.paths.data)
    seed(store)
    original = (store.root / "sessions/s.jsonl").read_bytes()

    async def exercise():
        with pytest.raises(ContextLimit):
            await Compactor(store, memory).compact(store.session("s"))
        assert (store.root / "sessions/s.jsonl").read_bytes() == original
        assert store.archives("s") == []
        await memory.close()

    asyncio.run(exercise())


def test_oversized_context_compacts_before_model_or_fails_closed(config):
    captured = []

    def model(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        captured.append(json.loads(request.content))
        return completion(config, "done")

    execution = setup(config, model)
    seed(execution.store)
    config.profiles["interactive"].context_budget = 1000

    async def exercise():
        failed = await execution.submit("s", "hello")
        assert failed.status == "context_limit"
        assert not captured
        execution.memory = MemoryClient(
            memory_config(), transport=httpx.MockTransport(memory_handler([]))
        )
        (config.paths.fragments / "recall-header.md").write_text("Recall:")
        run = await execution.submit("s", "hello")
        assert run.status == "completed"
        assert run.compaction["stage"] == "committed"
        assert captured[0]["messages"][1]["content"] == "Earlier conversation summary"
        assert execution.store.run(run.id)["messages"][-1]["content"] == "done"
        await execution.close()

    asyncio.run(exercise())


def test_summary_keeps_tool_groups_and_does_not_skip_after_interrupted_call(config):
    from solakit.core.memory import memory_messages

    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "c", "function": {"name": "example", "arguments": "{}"}}
            ],
        },
        {"role": "user", "content": "after interruption"},
    ]
    converted = memory_messages(messages)
    assert converted[0]["parts"][0]["tool_status"] == "error"
    assert converted[1]["parts"][0]["text"] == "after interruption"
    messages.insert(
        1, {"role": "tool", "tool_call_id": "c", "content": '{"error":true}'}
    )
    converted = memory_messages(messages)
    assert converted[0]["parts"][0]["tool_status"] == "error"
    assert converted[1]["parts"][0]["text"] == "after interruption"


def test_explicit_memory_probe_verifies_recall_not_model_claim(config):
    from solakit.core.memory_ops import probe

    committed = []

    def memory(request):
        result = {}
        if request.url.path.endswith("search/find"):
            result = {
                "memories": [
                    {
                        "uri": "viking://user/sola/memories/probe.md",
                        "abstract": "scratch-code-123",
                    }
                ]
                if committed
                else []
            }
        elif request.url.path.endswith("/commit"):
            committed.append(True)
            result = {"status": "accepted", "task_id": "t1"}
        elif "/tasks/" in request.url.path:
            result = {"status": "completed"}
        return httpx.Response(200, json={"status": "ok", "result": result})

    def model(request):
        return (
            model_list(config)
            if request.url.path.endswith("/models")
            else completion(config, "claimed success")
        )

    execution = setup(config, model)
    execution.memory = MemoryClient(
        memory_config(), transport=httpx.MockTransport(memory)
    )
    (config.paths.fragments / "recall-header.md").write_text("Recall:")

    async def exercise():
        result = await probe(
            execution, "Remember scratch-code-123", "Recall code", "scratch-code-123"
        )
        assert result["passed"]
        assert execution.store.run(result["first_run"])["memory_status"] == "completed"
        failed = await probe(execution, "Remember something", "Recall", "not-in-recall")
        assert not failed["passed"]
        persona = await probe(execution, "Scratch note", "Recall", "I am Sola.")
        assert not persona["passed"]  # A persona match is not evidence of recall.
        await execution.close()

    asyncio.run(exercise())


def test_openviking_04171_archive_contract_without_status(config):
    recorded = []
    upstream_shape = memory_handler(recorded)

    def version_04171(request):
        if request.url.path.endswith("/commit"):
            assert json.loads(request.content) == {"keep_recent_count": 0}
        response = upstream_shape(request)
        if "/archives/" in request.url.path:
            payload = response.json()
            del payload["result"]["status"]
            return httpx.Response(200, json=payload)
        return response

    store = Store(config.paths.data)
    seed(store)
    memory = MemoryClient(memory_config(), transport=httpx.MockTransport(version_04171))

    async def exercise():
        result = await Compactor(store, memory).compact(store.session("s"))
        assert result["stage"] == "committed"
        await memory.close()

    asyncio.run(exercise())


def test_lost_compacted_active_file_is_not_treated_as_empty_session(config):
    from solakit.core.store import StorageCorruption

    memory = MemoryClient(
        memory_config(), transport=httpx.MockTransport(memory_handler([]))
    )
    store = Store(config.paths.data)
    seed(store)

    async def exercise():
        await Compactor(store, memory).compact(store.session("s"))
        (store.root / "sessions/s.jsonl").unlink()
        restarted = Store(config.paths.data)
        with pytest.raises(StorageCorruption, match="recovery required"):
            restarted.transcript("s")
        assert restarted.storage_health()["status"] == "degraded"
        assert restarted.archives("s") == ["s.jsonl.1"]
        await memory.close()

    asyncio.run(exercise())
