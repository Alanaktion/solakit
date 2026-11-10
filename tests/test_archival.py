import asyncio
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from test_execution import completion, model_list, setup
from turns import plain

from solakit.core.archival import archive_session
from solakit.core.schemas import Message, Origin, Session
from solakit.core.store import StorageCorruption, Store
from solakit.main import create_app


def seed(store):
    store.create_session(Session(id="s", bound_channel="discord:123"))
    store.append_message(
        "s",
        Message(
            role="user",
            content="original <text> operator-test-token",
            origin=Origin(),
            run_id="old",
        ),
    )


def test_archive_api_preserves_binding_evidence_and_starts_fresh(config, auth):
    captured = []

    def backend(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        captured.append(json.loads(request.content))
        return completion(config, "Reply")

    from solakit.core.model import ModelClient

    model = ModelClient(config.model, transport=httpx.MockTransport(backend))
    app = create_app(config=config, model=model)
    with TestClient(app) as client:
        app.state.store.create_session(Session(id="s", bound_channel="discord:123"))
        assert client.post("/api/sessions/s/archive").status_code == 401
        assert (
            client.post("/api/sessions/missing/archive", headers=auth).status_code
            == 404
        )
        first = client.post(
            "/api/sessions/s/messages", json={"message": "Earlier turn"}, headers=auth
        ).json()
        source = (config.paths.data / "sessions/s.jsonl").read_bytes()
        metadata = (config.paths.data / "sessions/s.meta.json").read_bytes()
        archived = client.post("/api/sessions/s/archive", headers=auth)
        assert archived.status_code == 200
        assert archived.json()["archive"] == "s.jsonl.1"
        assert archived.json()["message_count"] == 2
        assert (config.paths.data / "sessions/s.jsonl.1").read_bytes() == source
        assert (config.paths.data / "sessions/s.jsonl").read_bytes() == b""
        assert (config.paths.data / "sessions/s.meta.json").read_bytes() == metadata
        session = client.get("/api/sessions/s", headers=auth).json()
        assert session["bound_channel"] == "discord:123"
        assert session["messages"] == []
        assert session["context_started_at"]
        assert (
            client.get("/api/sessions/s/archives/1", headers=auth).json()[0]["content"]
            == "Earlier turn"
        )
        assert (
            len(client.get(f"/api/runs/{first['id']}", headers=auth).json()["messages"])
            == 2
        )
        again = client.post("/api/sessions/s/archive", headers=auth).json()
        assert again["archived"] is False
        assert app.state.store.archives("s") == ["s.jsonl.1"]
        client.post(
            "/api/sessions/s/messages", json={"message": "Fresh turn"}, headers=auth
        )
        assert [message["role"] for message in captured[-1]["messages"]] == [
            "system",
            "user",
        ]
        assert "Earlier turn" not in json.dumps(captured[-1])
        assert plain(captured[-1]["messages"][-1]["content"]) == "Fresh turn"
        client.post("/api/sessions/s/archive", headers=auth)
        assert app.state.store.archives("s") == ["s.jsonl.1", "s.jsonl.2"]
        assert (config.paths.data / "sessions/s.jsonl.1").read_bytes() == source


@pytest.mark.parametrize("point", ["rename", "active", "commit"])
def test_archive_crash_recovery_blocks_writes_until_restart(config, monkeypatch, point):
    import solakit.core.compaction as rotation

    store = Store(config.paths.data)
    seed(store)
    source = (store.root / "sessions/s.jsonl").read_bytes()
    original_write, original_replace = rotation.atomic_write, rotation.os.replace

    def fail_write(path, payload):
        if point == "active" and path.name == "s.jsonl":
            raise OSError("crash before new active transcript")
        if (
            point == "commit"
            and path.name == "s.archive.json"
            and json.loads(payload)["stage"] == "committed"
        ):
            raise OSError("crash before committed manifest")
        return original_write(path, payload)

    def fail_replace(source, target):
        if point == "rename" and source.name == "s.jsonl":
            raise OSError("crash before archive rename")
        return original_replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(rotation, "atomic_write", fail_write)
        patch.setattr(rotation.os, "replace", fail_replace)
        with pytest.raises(OSError):
            archive_session(store, "s")
    with pytest.raises(StorageCorruption):
        store.transcript("s")
    with pytest.raises(StorageCorruption):
        store.append_message(
            "s",
            Message(
                role="user", content="must not append", origin=Origin(), run_id="new"
            ),
        )
    recovered = Store(config.paths.data)
    assert recovered.transcript("s") == []
    assert recovered.session("s").bound_channel == "discord:123"
    assert (recovered.root / "sessions/s.jsonl.1").read_bytes() == source
    assert archive_session(recovered, "s")["archived"] is False


def test_damaged_transcript_is_not_archived(config):
    store = Store(config.paths.data)
    seed(store)
    path = store.root / "sessions/s.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"torn":')
    source = path.read_bytes()
    with pytest.raises(StorageCorruption):
        archive_session(store, "s")
    assert store.archives("s") == []
    assert path.read_bytes() == source


def test_archive_rejects_running_and_queued_turns(config):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def backend(request):
            if request.url.path.endswith("/models"):
                return model_list(config)
            entered.set()
            await release.wait()
            return completion(config, "Done")

        execution = setup(config, backend)
        first = asyncio.create_task(execution.submit("s", "one"))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(execution.submit("s", "two"))
        async with asyncio.timeout(2):
            while len(execution.pending) < 2:  # noqa: ASYNC110 - observe asynchronous test state
                await asyncio.sleep(0)
        with pytest.raises(ValueError, match="queued or running"):
            await execution.archive_session("s")
        release.set()
        await asyncio.gather(first, second)
        result = await execution.archive_session("s")
        assert result["message_count"] == 4
        assert execution.store.transcript("s") == []
        await execution.close()

    asyncio.run(exercise())


def test_cancelled_archive_request_cannot_race_new_turn(config, monkeypatch):
    import solakit.core.archival as archival

    entered, release = threading.Event(), threading.Event()
    finish = archival.finish_rotation

    def held_rotation(*args):
        entered.set()
        assert release.wait(3)
        return finish(*args)

    def backend(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        return completion(config, "New reply")

    async def exercise():
        execution = setup(config, backend)
        execution.store.append_message(
            "s", Message(role="user", content="Old turn", origin=Origin(), run_id="old")
        )
        monkeypatch.setattr(archival, "finish_rotation", held_rotation)
        archiving = asyncio.create_task(execution.archive_session("s"))
        assert await asyncio.to_thread(entered.wait, 2)
        archiving.cancel()
        fresh = asyncio.create_task(execution.submit("s", "Fresh turn"))
        await asyncio.sleep(0.01)
        assert not fresh.done()
        assert not execution.pending
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await archiving
        assert (await fresh).status == "completed"
        assert [m["content"] for m in execution.store.transcript("s")] == [
            "Fresh turn",
            "New reply",
        ]
        assert execution.store.archives("s") == ["s.jsonl.1"]
        await execution.close()

    asyncio.run(exercise())


def test_operator_archives_and_compaction_share_numbering_without_memory_writes(config):
    from test_compaction import memory_handler
    from test_memory import memory_config

    from solakit.core.compaction import Compactor
    from solakit.core.memory import MemoryClient

    seen = []
    memory = MemoryClient(
        memory_config(), transport=httpx.MockTransport(memory_handler(seen))
    )
    store = Store(config.paths.data)
    seed(store)
    original = (store.root / "sessions/s.jsonl").read_bytes()
    archive_session(store, "s")
    assert seen == []
    store.append_message(
        "s", Message(role="user", content="Next chapter", origin=Origin(), run_id="new")
    )

    async def exercise():
        await Compactor(store, memory).compact(store.session("s"))
        count = len(seen)
        result = archive_session(store, "s")
        assert result["number"] == 3
        assert len(seen) == count
        assert store.archives("s") == ["s.jsonl.1", "s.jsonl.2", "s.jsonl.3"]
        assert (store.root / "sessions/s.jsonl.1").read_bytes() == original
        restarted = Store(config.paths.data)
        assert restarted.transcript("s") == []
        assert len(restarted.all_messages("s")) == 3
        await memory.close()

    asyncio.run(exercise())
