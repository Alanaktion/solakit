"""Session retirement is metadata, never transcript rotation or deletion."""

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from solakit.core.config import Channel, Job
from solakit.core.schemas import Message, Origin, Run, Session
from solakit.core.store import Store
from solakit.main import create_app

OLD = datetime(2020, 1, 1, tzinfo=UTC)


def seed(store, identity="old", at=OLD, *, profile="interactive", run=False):
    # A recent import timestamp must not override the actual historical activity.
    store.create_session(Session(id=identity, default_profile=profile))
    store.append_message(
        identity,
        Message(
            role="user",
            content=f"needle {identity}",
            ts=at,
            origin=Origin(channel="job"),
            run_id=f"run-{identity}",
        ),
    )
    if run:
        store.save_run(
            Run(
                id=f"run-{identity}",
                session=identity,
                profile=profile,
                origin=Origin(channel="job"),
                status="completed",
                created_at=at,
                finished_at=at,
            )
        )


def test_api_lifecycle_preserves_files_search_and_explicit_history(
    config, mock_model, auth
):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        seed(app.state.store, run=True)
        path = config.paths.data / "sessions/old.meta.json"
        metadata = json.loads(path.read_bytes())
        metadata["import_source"] = {"agent": "legacy", "opaque": 42}
        path.write_text(json.dumps(metadata))
        evidence = {
            p: p.read_bytes()
            for p in config.paths.data.rglob("*")
            if p.is_file() and p.suffix != ".db" and not p.name.endswith(".meta.json")
        }
        assert (
            client.post(
                "/api/sessions/old/lifecycle", json={"archived": True}
            ).status_code
            == 401
        )
        response = client.post(
            "/api/sessions/old/lifecycle", headers=auth, json={"archived": True}
        )
        assert response.status_code == 200 and response.json()["archived_at"]
        assert (
            json.loads(path.read_bytes())["import_source"] == metadata["import_source"]
        )
        assert client.get("/api/sessions", headers=auth).json() == []
        assert client.get("/api/runs", headers=auth).json() == []
        assert (
            client.get("/api/runs?include_archived=true", headers=auth).json()[0]["id"]
            == "run-old"
        )
        assert (
            client.get("/api/sessions?status=archived", headers=auth).json()[0]["id"]
            == "old"
        )
        assert (
            client.get("/api/search?query=needle", headers=auth).json()[0]["session_id"]
            == "old"
        )
        assert (
            client.get("/api/sessions/old", headers=auth).json()["messages"][0][
                "content"
            ]
            == "needle old"
        )
        assert client.get("/api/runs/run-old", headers=auth).status_code == 200
        assert (
            client.post(
                "/api/sessions/old/messages", headers=auth, json={"message": "blocked"}
            ).status_code
            == 409
        )
        assert client.post("/api/sessions/old/archive", headers=auth).status_code == 409
        assert client.post("/api/sessions/old/compact", headers=auth).status_code == 409
        assert all(p.read_bytes() == value for p, value in evidence.items())
        app.state.store.rebuild()
        assert client.get("/api/sessions", headers=auth).json() == []
        assert app.state.store.search("needle")
        restored = client.post(
            "/api/sessions/old/lifecycle", headers=auth, json={"archived": False}
        )
        assert restored.status_code == 200 and restored.json()["archived_at"] is None
        assert client.get("/api/sessions", headers=auth).json()[0]["id"] == "old"
        assert (
            client.post(
                "/api/sessions/old/messages", headers=auth, json={"message": "continue"}
            ).status_code
            == 200
        )


def test_activity_paging_filters_and_rebuild_use_history_not_import_time(config):
    store = Store(config.paths.data)
    for index in range(125):
        seed(store, f"cron-{index:03}", OLD + timedelta(days=index), profile="imported")
    first = store.list_sessions(limit=50)
    assert first["total"] == 125 and len(first["sessions"]) == 50
    assert first["sessions"][0]["id"] == "cron-124"
    assert first["sessions"][0]["last_activity_at"].startswith("2020-")
    second = store.list_sessions(limit=50, offset=50)
    assert not {s["id"] for s in first["sessions"]} & {
        s["id"] for s in second["sessions"]
    }
    assert store.list_sessions(q="cron-12")["total"] == 5
    assert store.list_sessions(profile="interactive")["total"] == 0
    assert store.list_sessions(before="2020-01-02T00:00:00Z")["total"] == 1
    assert store.list_sessions()["profiles"] == ["imported"]
    store.set_session_archived("cron-124", True)
    store.rebuild()
    rebuilt = Store(config.paths.data)
    assert rebuilt.list_sessions()["total"] == 124
    assert rebuilt.list_sessions(status="archived")["sessions"][0]["id"] == "cron-124"
    assert rebuilt.list_sessions()["sessions"][0]["id"] == "cron-123"


def test_bulk_preview_protects_channels_jobs_and_records_changed_sessions(
    config, mock_model, auth
):
    config.channels = [
        Channel(
            id="live", type="api", channel_id="live", session="pinned", enabled=True
        )
    ]
    config.jobs = [
        Job(
            id="future",
            cron="0 0 1 1 *",
            task="task",
            profile="interactive",
            bind_session="scheduled",
            enabled=True,
        ),
        Job(
            id="done",
            run_at="2099-01-01T00:00:00Z",
            task="task",
            profile="interactive",
            bind_session="finished",
            enabled=True,
        ),
    ]
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        for identity in ("ancient", "pinned", "scheduled", "finished", "changed"):
            seed(app.state.store, identity)
        app.state.jobs.states["done"].consumed = True
        app.state.jobs.states["done"].next_fire = None
        client.portal.call(lambda: app.state.jobs._save(app.state.jobs.states["done"]))
        preview = client.post(
            "/api/sessions/archive-batch", headers=auth, json={"inactive_days": 90}
        ).json()
        assert {s["id"] for s in preview["sessions"]} == {
            "ancient",
            "finished",
            "changed",
        }
        assert {s["id"] for s in preview["skipped"]} == {"pinned", "scheduled"}
        assert app.state.store.list_sessions()["total"] == 5
        for identity in ("pinned", "scheduled"):
            assert (
                client.post(
                    f"/api/sessions/{identity}/lifecycle",
                    headers=auth,
                    json={"archived": True},
                ).status_code
                == 409
            )
        app.state.store.append_message(
            "changed",
            Message(
                role="user", content="new activity", origin=Origin(), run_id="recent"
            ),
        )
        result = client.post(
            "/api/sessions/archive-batch",
            headers=auth,
            json={
                "ids": [s["id"] for s in preview["sessions"]],
                "dry_run": False,
                "before": preview["before"],
                "expected_activity": {
                    s["id"]: s["last_activity_at"] for s in preview["sessions"]
                },
            },
        ).json()
        assert result["archived"] == 2 and result["skipped"][0]["id"] == "changed"
        restored = client.post(
            "/api/sessions/restore-batch",
            headers=auth,
            json={"ids": ["ancient", "finished"], "dry_run": False},
        ).json()
        assert restored["restored"] == 2
        assert client.get("/api/sessions?status=all&page=true", headers=auth).json()[
            "counts"
        ] == {"active": 5, "archived": 0}


def test_busy_sessions_cannot_be_archived_even_during_bulk_cleanup(
    config, mock_model, auth
):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        seed(app.state.store)

        async def exercise():
            await app.state.execution.semaphore.acquire()
            try:
                pending = await app.state.execution.enqueue("old", "waiting")
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    headers=auth,
                ) as api:
                    assert (
                        await api.post(
                            "/api/sessions/old/lifecycle", json={"archived": True}
                        )
                    ).status_code == 409
                    result = (
                        await api.post(
                            "/api/sessions/archive-batch",
                            json={"ids": ["old"], "dry_run": False},
                        )
                    ).json()
                    assert result["archived"] == 0 and result["skipped"]
                await app.state.execution.cancel(pending.run.id)
            finally:
                app.state.execution.semaphore.release()

        client.portal.call(exercise)


def test_jobs_cannot_enable_or_rearm_work_in_archived_session(config, mock_model, auth):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        seed(app.state.store)
        client.post(
            "/api/sessions/old/lifecycle", headers=auth, json={"archived": True}
        )
        job = {
            "id": "j",
            "cron": "0 9 * * *",
            "task": "task",
            "profile": "interactive",
            "bind_session": "old",
        }
        assert (
            client.post(
                "/api/jobs", headers=auth, json={**job, "enabled": True}
            ).status_code
            == 422
        )
        assert client.post("/api/jobs", headers=auth, json=job).status_code == 201
        assert (
            client.patch(
                "/api/jobs/j", headers=auth, json={"enabled": True}
            ).status_code
            == 422
        )
        client.post(
            "/api/sessions/old/lifecycle", headers=auth, json={"archived": False}
        )
        assert (
            client.patch(
                "/api/jobs/j", headers=auth, json={"enabled": True}
            ).status_code
            == 200
        )


@pytest.mark.parametrize(
    "params",
    [{"status": "bad"}, {"limit": 501}, {"offset": -1}, {"before": "2020-01-01"}],
)
def test_invalid_list_filters(config, mock_model, auth, params):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        assert (
            client.get("/api/sessions", headers=auth, params=params).status_code == 422
        )


def test_ui_archive_view_cleanup_undo_and_restore(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        seed(app.state.store)
        client.post("/login", data={"token": "operator-test-token"})
        preview = client.get("/sessions/cleanup/preview?days=90")
        assert (
            preview.status_code == 200 and "Archive selected sessions" in preview.text
        )
        # Exercise multi-selection, which is also used for large imported pages.
        result = client.post("/sessions/cleanup/apply", data={"session_ids": "old"})
        assert result.status_code == 200 and "Archived 1 sessions" in result.text
        assert "needle old" in client.get("/sessions/old").text
        assert 'id="composer"' not in client.get("/sessions/old").text
        assert "Restore session" in client.get("/sessions/old").text
        assert "/sessions/old" not in client.get("/").text
        assert "/sessions/old" in client.get("/?status=archived").text
        assert (
            client.post(
                "/sessions/cleanup/apply",
                data={"session_ids": "old", "operation": "restore"},
            ).status_code
            == 200
        )
        assert 'id="composer"' in client.get("/sessions/old").text
        assert (
            client.post("/sessions/cleanup/apply", data={"selection": "[]"}).status_code
            == 422
        )


def test_legacy_messages_without_timestamps_do_not_invent_activity(config):
    store = Store(config.paths.data)
    seed(store)
    path = store.root / "sessions/old.jsonl"
    message = json.loads(path.read_text())
    del message["ts"]
    path.write_text(json.dumps(message) + "\n")
    metadata = store.root / "sessions/old.meta.json"
    data = json.loads(metadata.read_text())
    data["created_at"] = OLD.isoformat()
    del data["archived_at"]
    metadata.write_text(json.dumps(data))
    original = path.read_bytes()
    store.rebuild()
    assert store.list_sessions()["sessions"][0]["last_activity_at"].startswith("2020-")
    assert path.read_bytes() == original


def test_recovering_ancient_imported_runs_does_not_make_them_recent(config):
    store = Store(config.paths.data)
    seed(store)
    store.save_run(
        Run(
            id="waiting",
            session="old",
            profile="interactive",
            origin=Origin(channel="job"),
            created_at=OLD,
            status="queued",
        )
    )
    restarted = Store(config.paths.data)
    assert restarted.run("waiting")["status"] == "interrupted"
    assert restarted.list_sessions()["sessions"][0]["last_activity_at"].startswith(
        "2020-"
    )
    restarted.rebuild()
    assert restarted.list_sessions()["sessions"][0]["last_activity_at"].startswith(
        "2020-"
    )


def test_preview_scans_past_protected_pages(config):
    from test_execution import setup

    from solakit.channels import Channels
    from solakit.core.session_lifecycle import ArchiveBatch, SessionLifecycle
    from solakit.jobs import Jobs

    store = Store(config.paths.data)
    config.channels = [
        Channel(
            id=f"live-{n}",
            type="api",
            channel_id=str(n),
            session=f"live-{n}",
            enabled=True,
        )
        for n in range(500)
    ]
    for channel in config.channels:
        store.create_session(
            Session(id=channel.session, created_at=OLD + timedelta(days=1))
        )
    store.create_session(Session(id="eligible", created_at=OLD))
    execution = setup(config, lambda request: None)
    jobs = Jobs(config, execution, Channels(config, execution, {}))

    async def exercise():
        try:
            await jobs.initialize()
            result = await SessionLifecycle(config, execution, jobs).batch(
                ArchiveBatch(inactive_days=90)
            )
            assert [s["id"] for s in result["sessions"]] == ["eligible"]
            assert len(result["skipped"]) == 500 and not result["more"]
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_cancelled_lifecycle_change_finishes_publication(
    config, mock_model, monkeypatch
):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        seed(app.state.store)
        entered, release = threading.Event(), threading.Event()
        original = app.state.store.set_session_archived

        def slow(*args):
            result = original(*args)
            entered.set()
            assert release.wait(5)
            return result

        monkeypatch.setattr(app.state.store, "set_session_archived", slow)

        async def exercise():
            task = asyncio.create_task(app.state.session_lifecycle.change("old", True))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert app.state.store.session("old").archived_at
            assert app.state.store.list_sessions()["total"] == 0

        client.portal.call(exercise)


def test_preview_does_not_read_transcripts_and_ui_can_exclude_candidates(
    config, mock_model, monkeypatch
):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        seed(app.state.store, "keep")
        seed(app.state.store, "retire")
        client.post("/login", data={"token": "operator-test-token"})

        def forbidden(*args):
            raise AssertionError("Listing must use the derived metadata index")

        monkeypatch.setattr(app.state.store, "_read_lines", forbidden)
        preview = client.post(
            "/api/sessions/archive-batch", json={"inactive_days": 90}
        ).json()
        selection = {
            "ids": [s["id"] for s in preview["sessions"]],
            "before": preview["before"],
            "expected_activity": {
                s["id"]: s["last_activity_at"] for s in preview["sessions"]
            },
        }
        response = client.post(
            "/sessions/cleanup/apply",
            data={
                "from_preview": "1",
                "selection": json.dumps(selection),
                "session_ids": "retire",
            },
        )
        assert response.status_code == 200
        assert app.state.store.session("retire").archived_at
        assert app.state.store.session("keep").archived_at is None
