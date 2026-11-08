import hashlib

import pytest
from fastapi.testclient import TestClient

from solakit.core.config import Channel
from solakit.core.routing import SessionRouter
from solakit.core.schemas import Message, Origin, Run, RunStatus, Session
from solakit.core.store import Store
from solakit.main import create_app


def test_restart_rebuild_and_channel_isolation(config):
    config.channels = [
        Channel(id=n, type="api", channel_id=n, session=n) for n in ("first", "second")
    ]
    store = Store(config.paths.data)
    router = SessionRouter(config, store)
    first = router.channel("api", "first")
    second = router.channel("api", "second")
    assert first.id != second.id
    origin = Origin(channel="api", channel_id="first")
    store.append_message(
        first.id,
        Message(role="user", content="Remember me", origin=origin, run_id="r1"),
    )
    run = Run(id="r1", session=first.id, profile="interactive", origin=origin)
    store.save_run(run)
    prompt = "Exact\r\nSOUL\n"
    run.prompt_sha256 = store.write_prompt(run.id, prompt)
    store.save_run(run)
    store.log_tool(run.id, {"API_TOKEN": "hidden", "result": "done"})
    evidence = (store.root / "sessions/first.jsonl").read_bytes()
    (store.root / "app.db").unlink()
    recovered = Store(config.paths.data)
    assert recovered.transcript(first.id)[0]["origin"]["channel_id"] == "first"
    assert recovered.transcript(second.id) == []
    assert (store.root / "sessions/first.jsonl").read_bytes() == evidence
    record = recovered.run(run.id)
    assert record["status"] == "interrupted"
    assert record["prompt"] == prompt
    assert record["prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    assert record["tools"][0]["API_TOKEN"] == "[redacted]"
    assert len(recovered.list_records("sessions")) == 2
    assert router.channel("api", "first").created_at == first.created_at


def test_partial_evidence_is_preserved(config):
    store = Store(config.paths.data)
    store.create_session(Session(id="s"))
    run = Run(id="r", session="s", profile="interactive", origin=Origin())
    store.save_run(run)
    path = store.root / "runs/r.events.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"partial":')
    before = path.read_bytes()
    recovered = Store(config.paths.data)
    assert path.read_bytes().startswith(before)
    assert recovered.run("r")["status"] == RunStatus.interrupted
    with pytest.raises(ValueError):
        store.session("../escape")
    with pytest.raises(FileExistsError):
        store.write_prompt("p", "one")
        store.write_prompt("p", "two")


def test_inspection_api(config, auth):
    app = create_app(config=config)
    with TestClient(app) as client:
        app.state.store.create_session(Session(id="s"))
        assert client.get("/api/sessions", headers=auth).json()[0]["id"] == "s"
        assert client.get("/api/sessions/s", headers=auth).json()["messages"] == []
        assert client.get("/api/sessions/missing", headers=auth).status_code == 404
        assert client.get("/api/runs/missing", headers=auth).status_code == 404


def test_only_one_runtime_owns_data(config):
    first = Store(config.paths.data, exclusive=True)
    with pytest.raises(RuntimeError, match="Another runtime"):
        Store(config.paths.data, exclusive=True)
    first.close()
    second = Store(config.paths.data, exclusive=True)
    second.close()


def test_session_metadata_is_published_atomically(config, monkeypatch):
    import json
    import os

    store = Store(config.paths.data)
    original_replace = os.replace
    target = store.root / "sessions/new.meta.json"

    def fail_before_publication(source, destination):
        if destination == target:
            assert not target.exists()
            assert json.loads(source.read_bytes())["id"] == "new"
            raise OSError("simulated failure before atomic publication")
        return original_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr("solakit.core.store.os.replace", fail_before_publication)
        with pytest.raises(OSError, match="simulated failure"):
            store.create_session(Session(id="new"))
    assert not target.exists()
    # A process killed before publication may leave a partial temporary file.
    (target.parent / ".new.meta.json.crash.tmp").write_bytes(b'{"id":')
    restarted = Store(config.paths.data)
    assert restarted.storage_health() == {"status": "ok", "issues": []}
    assert restarted.create_session(Session(id="new")).id == "new"
    assert target.stat().st_mode & 0o777 == 0o600


def test_torn_metadata_isolated_and_reported(config, auth, mock_model):
    from solakit.core.store import StorageCorruption

    store = Store(config.paths.data)
    store.create_session(Session(id="healthy"))
    damaged = store.root / "sessions/damaged.meta.json"
    damaged.write_bytes(b'{"id":"damaged",')
    original = damaged.read_bytes()
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "degraded"
        assert client.get("/api/storage/health").status_code == 401
        health = client.get("/api/storage/health", headers=auth).json()
        assert health["issues"] == [
            {
                "file": "sessions/damaged.meta.json",
                "kind": "invalid_metadata",
                "offset": None,
            }
        ]
        assert client.get("/api/sessions/healthy", headers=auth).status_code == 200
        assert client.get("/api/sessions/damaged", headers=auth).status_code == 503
        assert (
            client.post(
                "/api/sessions", json={"id": "damaged"}, headers=auth
            ).status_code
            == 503
        )
        with pytest.raises(StorageCorruption):
            app.state.store.session("damaged")
    assert damaged.read_bytes() == original
    # Explicit restoration from a trusted backup makes the session usable again.
    from solakit.core.store import atomic_write

    atomic_write(
        damaged,
        Session(id="damaged", memory_scope="ephemeral").model_dump_json().encode(),
    )
    restored = Store(config.paths.data)
    assert restored.session("damaged").memory_scope == "ephemeral"
    assert restored.storage_health()["status"] == "ok"


def test_interior_corruption_fails_closed_without_rewriting_history(config):
    from solakit.core.store import StorageCorruption

    store = Store(config.paths.data)
    store.create_session(Session(id="s"))
    message = Message(role="user", content="before", origin=Origin(), run_id="r")
    store.append_message("s", message)
    path = store.root / "sessions/s.jsonl"
    with path.open("ab") as stream:
        stream.write(b"{invalid complete record}\n")
        stream.write(message.model_dump_json().encode() + b"\n")
    original = path.read_bytes()
    with pytest.raises(StorageCorruption):
        store.transcript("s")
    assert store.storage_health()["issues"][0]["kind"] == "corrupt_record"
    with pytest.raises(StorageCorruption):
        store.append_message("s", message)
    assert path.read_bytes() == original
    restarted = Store(config.paths.data)
    with pytest.raises(StorageCorruption):
        restarted.transcript("s")
    assert restarted.storage_health()["status"] == "degraded"


def test_torn_tail_recovery_is_explicit_and_bound_to_exact_bytes(config):
    import json

    from solakit.core.store import StorageCorruption

    store = Store(config.paths.data)
    store.create_session(Session(id="s"))
    message = Message(role="user", content="before", origin=Origin(), run_id="r")
    store.append_message("s", message)
    path = store.root / "sessions/s.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"partial":')
    original = path.read_bytes()
    assert len(store.transcript("s")) == 1
    assert store.storage_health()["issues"][0]["kind"] == "incomplete_tail"
    store.append_message("s", message)
    assert path.read_bytes().startswith(original)
    assert len(store.transcript("s")) == 2
    assert store.storage_health()["issues"][0]["kind"] == "recovered_tail"
    restarted = Store(config.paths.data)
    assert len(restarted.transcript("s")) == 2
    assert restarted.storage_health()["issues"][0]["kind"] == "recovered_tail"
    marker = next((store.root / "recovery").glob("*.json"))
    record = json.loads(marker.read_bytes())
    assert record["sha256"] == hashlib.sha256(b'{"partial":').hexdigest()
    record["sha256"] = "incorrect"
    marker.write_text(json.dumps(record))
    with pytest.raises(StorageCorruption):
        restarted.transcript("s")


def test_store_closes_connections_on_success_and_error(config, monkeypatch):
    import sqlite3

    from solakit.core.schemas import Session
    from solakit.core.store import Store

    opened = []

    class Connection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    connect = sqlite3.connect

    def tracked(*args, **kwargs):
        db = connect(*args, **kwargs, factory=Connection)
        opened.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", tracked)
    store = Store(config.paths.data)
    store.create_session(Session(id="closed"))
    assert opened and all(db.closed for db in opened)
    try:
        store._index("invalid_table", "x", {})
    except sqlite3.Error:
        pass
    assert all(db.closed for db in opened)
