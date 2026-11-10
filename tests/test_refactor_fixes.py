"""History scaling and request isolation contracts for review D1–D8."""

import asyncio
import sqlite3

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from solakit.core.archival import archive_session
from solakit.core.schemas import Message, Origin, Run, RunStatus, Session
from solakit.core.store import StorageCorruption, Store
from solakit.main import create_app
from solakit.ui.context import internal_client


def add_run(store, identity, *, session="history", status=RunStatus.completed):
    run = Run(
        id=identity,
        session=session,
        profile="interactive",
        origin=Origin(channel="api"),
        status=status,
    )
    store.save_run(run)
    store.append_message(
        session,
        Message(
            origin=Origin(channel="api"), role="user", content=identity, run_id=identity
        ),
    )
    return run


def test_run_offsets_include_archives_and_avoid_unrelated_history(config, monkeypatch):
    store = Store(config.paths.data)
    store.create_session(Session(id="history"))
    add_run(store, "old")
    target = add_run(store, "target")
    archive_session(store, "history")
    store.append_message(
        "history",
        Message(
            origin=Origin(channel="api"),
            role="assistant",
            content="continued",
            run_id=target.id,
        ),
    )
    add_run(store, "later")
    expected = [m for m in store.all_messages("history") if m["run_id"] == target.id]
    # First lookup reconciles the active file if it was not indexed yet.
    assert store.run(target.id)["messages"] == expected
    store.close()
    store = Store(config.paths.data)
    original = store._read_lines

    def no_history_scan(path):
        assert path.parent.name != "sessions", "run lookup scanned session history"
        return original(path)

    monkeypatch.setattr(store, "_read_lines", no_history_scan)
    assert store.run(target.id)["messages"] == expected
    assert store.run(target.id, include_messages=False)["messages"] == []
    store.close()


def test_run_offsets_reconcile_missed_writes_and_external_damage(config, monkeypatch):
    store = Store(config.paths.data)
    store.create_session(Session(id="history"))
    run = add_run(store, "target")
    store.run(run.id)

    def unavailable(*args):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_append_offset", unavailable)
        store.append_message(
            "history",
            Message(
                origin=Origin(channel="api"),
                role="assistant",
                content="durable",
                run_id=run.id,
            ),
        )
    assert [m["content"] for m in store.run(run.id)["messages"]] == [
        "target",
        "durable",
    ]
    archive_session(store, "history")
    archive = store.root / "sessions/history.jsonl.1"
    archive.write_bytes(b"{damaged}\n" + archive.read_bytes())
    with pytest.raises(StorageCorruption):
        store.run(run.id)
    store.close()


def test_indexed_run_read_rejects_missing_active_rotation_evidence(config):
    store = Store(config.paths.data)
    store.create_session(Session(id="history"))
    run = add_run(store, "target")
    archive_session(store, "history")
    (store.root / "sessions/history.jsonl").unlink()
    with pytest.raises(StorageCorruption, match="history.jsonl"):
        store.run(run.id)
    assert any(
        i["kind"] == "missing_active_transcript"
        for i in store.storage_health()["issues"]
    )
    store.close()


@pytest.mark.parametrize("separator", [b"\n", b"\r\n", b"\r"])
def test_offset_addresses_follow_evidence_line_boundaries(config, separator):
    store = Store(config.paths.data)
    store.create_session(Session(id="history"))
    run = add_run(store, "target")
    path = store.root / "sessions/history.jsonl"
    records = [
        Message(role="user", origin=Origin(), content=identity, run_id=identity)
        for identity in ("earlier", "target", "later")
    ]
    payload = separator.join(m.model_dump_json().encode() for m in records) + separator
    path.write_bytes(payload)
    expected = [m for m in store.all_messages("history") if m["run_id"] == run.id]
    assert store.run(run.id)["messages"] == expected
    assert path.read_bytes() == payload
    store.close()


def test_run_evidence_remains_readable_without_offset_index(config, monkeypatch):
    store = Store(config.paths.data)
    store.create_session(Session(id="history"))
    run = add_run(store, "target")

    def unavailable(*args):
        raise sqlite3.OperationalError("unavailable index")

    monkeypatch.setattr(store, "_refresh_offsets", unavailable)
    assert store.run(run.id)["messages"][0]["content"] == "target"
    assert store.storage_health()["status"] == "degraded"
    store.close()


def test_run_pagination_filters_before_limit_and_uses_indexes(config, mock_model, auth):
    with TestClient(create_app(config, mock_model)) as client:
        store = client.app.state.store
        store.create_session(Session(id="history"))
        store.create_session(Session(id="archived"))
        for index in range(105):
            add_run(
                store,
                f"run-{index:03d}",
                status=RunStatus.failed if index == 104 else RunStatus.completed,
            )
        add_run(store, "hidden", session="archived")
        store.set_session_archived("archived", True)
        first = client.get("/api/runs", headers=auth).json()
        assert len(first) == 100
        assert first[0]["id"] == "run-000"
        assert len(client.get("/api/runs?offset=100", headers=auth).json()) == 5
        filtered = client.get(
            "/api/runs?status=failed&limit=1&descending=true", headers=auth
        ).json()
        assert [r["id"] for r in filtered] == ["run-104"]
        assert client.get("/api/runs?limit=1001", headers=auth).status_code == 422
        assert client.get("/api/runs?offset=-1", headers=auth).status_code == 422
        assert (
            client.get(
                "/api/runs?session_id=archived&include_archived=true", headers=auth
            ).json()[0]["id"]
            == "hidden"
        )
        with store._database() as db:
            plan = db.execute(
                "EXPLAIN QUERY PLAN SELECT payload FROM runs "
                "WHERE json_extract(payload, '$.session')=?",
                ("history",),
            ).fetchall()
            assert any("runs_session" in row[-1] for row in plan)
            pending = db.execute(
                "EXPLAIN QUERY PLAN SELECT payload FROM runs WHERE "
                "json_extract(payload, '$.memory_status') IN "
                "('accepted','pending','running','cancelling','status_unavailable') "
                "AND json_extract(payload, '$.status') NOT IN ('queued','running') "
                "ORDER BY json_extract(payload, '$.memory_status')="
                "'status_unavailable', rowid LIMIT 100"
            ).fetchall()
            assert any("INDEX" in row[-1] for row in pending)
        page = client.get("/runs", headers=auth)
        assert "offset=100" in page.text and "run-104" in page.text
        assert "run-000" in client.get("/runs?offset=100", headers=auth).text


def test_internal_client_never_retains_request_authentication():
    app = FastAPI()

    @app.get("/")
    async def reflect(request: Request):
        from starlette.responses import JSONResponse

        response = JSONResponse(
            {
                "cookie": request.headers.get("cookie"),
                "authorization": request.headers.get("authorization"),
            }
        )
        response.set_cookie("solakit_session", "must-not-leak")
        return response

    async def check():
        client = internal_client(app)
        async with client:
            first, second = await asyncio.gather(
                client.get("/", headers={"Cookie": "solakit_session=alice"}),
                client.get("/", headers={"Authorization": "Bearer bob"}),
            )
            assert first.json() == {
                "cookie": "solakit_session=alice",
                "authorization": None,
            }
            assert second.json() == {"cookie": None, "authorization": "Bearer bob"}
            assert (await client.get("/")).json() == {
                "cookie": None,
                "authorization": None,
            }
            assert not list(client.cookies.jar)
        assert client.is_closed

    asyncio.run(check())


def test_app_reuses_and_closes_internal_client(config, mock_model):
    with TestClient(create_app(config, mock_model)) as client:
        shared = client.app.state.ui_client
        client.post("/login", data={"token": "operator-test-token"})
        assert client.get("/tools").status_code == 200
        assert client.get("/runs").status_code == 200
        assert client.app.state.ui_client is shared
        assert not list(shared.cookies.jar)
        response = client.portal.call(shared.get, "/api/profiles")
        assert response.status_code == 401
    assert shared.is_closed
