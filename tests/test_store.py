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
