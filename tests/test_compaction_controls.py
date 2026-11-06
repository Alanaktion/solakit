import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_compaction import memory_handler, seed
from test_execution import call, completion, model_list, setup
from test_memory import memory_config
from test_ui import send_and_observe, sign_in

from solakit.core.memory import MemoryClient
from solakit.core.schemas import Message, Origin, Session
from solakit.core.store import Store
from solakit.main import create_app
from solakit.tools import ToolContext, ToolResult, tool


@pytest.mark.parametrize(
    "enabled,large_summary", [(False, False), (True, False), (True, True)]
)
def test_mid_run_compaction_preserves_groups_goal_budgets_and_full_commit(
    config, enabled, large_summary
):
    captured, seen, dispatched = [], [], []
    config.memory = memory_config()
    (config.paths.fragments / "recall-header.md").write_text("Recall:")
    profile = config.profiles["interactive"]
    assert profile.mid_run_compaction is False
    profile.mid_run_compaction = enabled
    profile.context_budget = 2000

    def backend(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        captured.append(json.loads(request.content))
        usage = {"prompt_tokens": 10, "completion_tokens": 2}
        return completion(
            config,
            calls=[call()] if len(captured) == 1 else None,
            content=None if len(captured) == 1 else "finished",
            usage=usage,
        )

    execution = setup(config, backend)
    summary = "s" * 10000 if large_summary else "Task underway; tool completed."
    config.memory.timeout_s = 0.01
    config.memory.compaction_timeout_s = 1
    handler = memory_handler(seen, overview=summary)
    polls = 0

    async def delayed_memory(request):
        nonlocal polls
        if "/tasks/" in request.url.path:
            polls += 1
            if polls == 1:
                return httpx.Response(
                    200, json={"status": "ok", "result": {"status": "running"}}
                )
        return handler(request)

    execution.memory = MemoryClient(
        config.memory,
        transport=httpx.MockTransport(delayed_memory),
    )

    @tool(name="echo", profiles=["interactive"])
    async def echo(ctx: ToolContext, text: str) -> ToolResult:
        dispatched.append(text)
        return ToolResult(content="x" * 6000)

    execution.registry.register(echo)
    profile.tools = ["echo"]
    execution.registry.validate()

    async def exercise():
        run = await execution.submit("s", "original task")
        assert len(dispatched) == 1
        expected = "completed" if enabled and not large_summary else "context_limit"
        assert run.status == expected
        if enabled:
            assert execution.store.archives("s") == ["s.jsonl.1"]
            original = execution.store._read(
                execution.store.root / "sessions/s.jsonl.1"
            )
            assert [m["role"] for m in original] == ["user", "assistant", "tool"]
            assert original[1]["tool_calls"][0]["id"] == original[2]["tool_call_id"]
            assert run.compaction["trigger"] == "mid_run"
        else:
            assert execution.store.archives("s") == []
        assert len(captured) == (2 if expected == "completed" else 1)
        if expected == "completed":
            assert [m["role"] for m in captured[1]["messages"]] == [
                "system",
                "assistant",
                "user",
            ]
            assert captured[1]["messages"][-1]["content"] == "original task"
            evidence = execution.store.run(run.id)
            assert [m["role"] for m in evidence["messages"]] == [
                "user",
                "assistant",
                "tool",
                "assistant",
            ]
            batches = [
                body
                for method, path, body in seen
                if path == f"/api/v1/sessions/solakit-{run.id}/messages/batch"
            ]
            assert len(batches) == 1
            assert "original task" in json.dumps(
                batches[0]
            ) and "finished" in json.dumps(batches[0])
            assert "x" * 6000 in json.dumps(batches[0])
            assert run.steps == 2 and run.tokens == {
                "prompt_tokens": 20,
                "completion_tokens": 4,
            }
        await execution.close()

    asyncio.run(exercise())


@pytest.fixture
def memory_app(config, mock_model, monkeypatch):
    seen = []
    config.memory = memory_config()
    (config.paths.fragments / "recall-header.md").write_text("Recall:")
    monkeypatch.setattr(
        "solakit.main.MemoryClient",
        lambda cfg, key: MemoryClient(
            cfg,
            key,
            transport=httpx.MockTransport(
                memory_handler(seen, overview="Old context summary")
            ),
        ),
    )
    app = create_app(config=config, model=mock_model)
    return app, seen


def test_explicit_compaction_api_and_ui_preserve_session_and_archive(memory_app):
    app, _ = memory_app
    with TestClient(app) as client:
        assert client.post("/api/sessions/s/compact").status_code == 401
        assert (
            client.post("/sessions/s/compact", follow_redirects=False).headers[
                "location"
            ]
            == "/login?next=%2F"
        )
        sign_in(client)
        seed(app.state.store)
        source = (app.state.store.root / "sessions/s.jsonl").read_bytes()
        metadata = (app.state.store.root / "sessions/s.meta.json").read_bytes()
        confirmation = client.get("/sessions/s/compact")
        assert (
            confirmation.status_code == 200
            and "Summarize and continue" in confirmation.text
        )
        response = client.post("/api/sessions/s/compact")
        assert response.status_code == 200
        assert response.json()["stage"] == "committed"
        assert (app.state.store.root / "sessions/s.jsonl.1").read_bytes() == source
        assert (app.state.store.root / "sessions/s.meta.json").read_bytes() == metadata
        assert (
            client.get("/api/sessions/s").json()["messages"][0]["kind"] == "compaction"
        )
        app.state.store.append_message(
            "s",
            Message(
                role="user", content="next chapter", origin=Origin(), run_id="old2"
            ),
        )
        response = client.post("/sessions/s/compact", follow_redirects=False)
        assert (
            response.status_code == 303
            and response.headers["location"] == "/sessions/s"
        )
        assert "Old context summary" in client.get("/sessions/s").text
        assert client.post("/api/sessions/missing/compact").status_code == 404
        client.post("/api/sessions", json={"id": "empty"})
        assert client.post("/api/sessions/empty/compact").status_code == 409
        app.state.store.create_session(Session(id="private", memory_scope="ephemeral"))
        assert client.post("/api/sessions/private/compact").status_code == 409


def test_pre_turn_summary_is_rendered_before_new_message_and_excludes_it(
    memory_app, config
):
    app, seen = memory_app
    config.profiles["interactive"].context_budget = 1500
    with TestClient(app) as client:
        sign_in(client)
        seed(app.state.store)
        response = send_and_observe(
            client,
            "/sessions/s/messages",
            data={"message": "NEW MESSAGE AFTER SUMMARY"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert response.text.index(
            "Earlier context was summarized"
        ) < response.text.index("NEW MESSAGE AFTER SUMMARY")
        assert "Hello back" in response.text
        summary_batches = [
            body
            for method, path, body in seen
            if "/solakit-compact-" in path and path.endswith("messages/batch")
        ]
        assert summary_batches
        assert all(
            "NEW MESSAGE AFTER SUMMARY" not in json.dumps(batch)
            for batch in summary_batches
        )


def test_explicit_compaction_rejects_busy_and_waits_on_disconnect(config):
    started, release = asyncio.Event(), asyncio.Event()

    def model(request):
        return (
            model_list(config)
            if request.url.path.endswith("/models")
            else completion(config, "reply")
        )

    execution = setup(config, model)
    seed(execution.store)

    async def held_summary(session):
        started.set()
        await release.wait()
        return {"stage": "committed"}

    execution.compactor.compact = held_summary

    async def exercise():
        task = asyncio.create_task(execution.compact_session("s"))
        await started.wait()
        task.cancel()
        submission = asyncio.create_task(execution.submit("s", "new"))
        await asyncio.sleep(0.01)
        assert not submission.done() and not execution.pending
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await submission).status == "completed"
        # An accepted queued/running turn cannot be summarized by a manual request.
        from solakit.core.loop import Pending
        from solakit.core.schemas import Run

        run = Run(session="s", profile="interactive", origin=Origin())
        execution.pending[run.id] = Pending(
            run, "queued", asyncio.get_running_loop().create_future()
        )
        with pytest.raises(ValueError, match="queued or running"):
            await execution.compact_session("s")
        execution.pending.pop(run.id)
        await execution.close()

    asyncio.run(exercise())


def test_cancel_between_prepared_manifest_and_rotation_finishes_atomically(
    config, monkeypatch
):
    import threading

    import solakit.core.compaction as rotation

    store = Store(config.paths.data)
    seed(store)
    original = (store.root / "sessions/s.jsonl").read_bytes()
    memory = MemoryClient(
        memory_config(), transport=httpx.MockTransport(memory_handler([]))
    )
    entered, release = threading.Event(), threading.Event()
    write = rotation.atomic_write

    def held_write(path, payload):
        write(path, payload)
        if (
            path.name == "s.compaction.json"
            and json.loads(payload)["stage"] == "prepared"
        ):
            entered.set()
            assert release.wait(3)

    monkeypatch.setattr(rotation, "atomic_write", held_write)

    async def exercise():
        task = asyncio.create_task(
            rotation.Compactor(store, memory).compact(store.session("s"))
        )
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.transcript("s")[0]["kind"] == "compaction"
        assert (store.root / "sessions/s.jsonl.1").read_bytes() == original
        assert (
            json.loads((store.root / "sessions/s.compaction.json").read_bytes())[
                "stage"
            ]
            == "committed"
        )
        await memory.close()

    asyncio.run(exercise())


def test_failed_compaction_commit_marker_blocks_writes_until_recovery(
    config, monkeypatch
):
    import solakit.core.compaction as rotation
    from solakit.core.store import StorageCorruption, Store

    store = Store(config.paths.data)
    seed(store)
    memory = MemoryClient(
        memory_config(), transport=httpx.MockTransport(memory_handler([]))
    )
    write = rotation.atomic_write

    def fail_commit(path, payload):
        if (
            path.name == "s.compaction.json"
            and json.loads(payload)["stage"] == "committed"
        ):
            raise OSError("simulated failed commit marker")
        write(path, payload)

    async def exercise():
        with monkeypatch.context() as patch:
            patch.setattr(rotation, "atomic_write", fail_commit)
            with pytest.raises(OSError):
                await rotation.Compactor(store, memory).compact(store.session("s"))
        with pytest.raises(StorageCorruption):
            store.transcript("s")
        with pytest.raises(StorageCorruption):
            store.append_message(
                "s",
                Message(
                    role="user", content="no late write", run_id="new", origin=Origin()
                ),
            )
        restored = Store(config.paths.data)
        assert restored.transcript("s")[0]["kind"] == "compaction"
        assert restored.archives("s") == ["s.jsonl.1"]
        await memory.close()

    asyncio.run(exercise())


def test_pre_turn_compaction_failure_banner_remains_after_summary(memory_app, config):
    from solakit.core.model import ModelUnavailable

    app, _ = memory_app
    config.profiles["interactive"].context_budget = 1500

    async def unavailable(*args):
        raise ModelUnavailable("backend became unavailable")

    with TestClient(app) as client:
        sign_in(client)
        seed(app.state.store)
        app.state.model.complete = unavailable
        result = send_and_observe(
            client,
            "/sessions/s/messages",
            data={"message": "not yet persisted"},
            headers={"HX-Request": "true"},
        )
        assert result.status_code == 200
        assert "Model unavailable" in result.text
        assert result.text.index("Earlier context was summarized") < result.text.index(
            "Model unavailable"
        )
        assert "hx-trigger" not in result.headers
