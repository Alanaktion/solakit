"""Audit regressions using synthetic local evidence, never operator data."""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_ui import sign_in

from solakit.core.schemas import Message, Origin, Run, RunStatus, Session
from solakit.main import create_app
from solakit.ui.markdown import preview_text
from solakit.ui.routes import TURN_WINDOW


def seed(store, identity, count):
    store.create_session(Session(id=identity, memory_scope="ephemeral"))
    for index in range(count):
        at = datetime(2020, 1, 1, tzinfo=UTC) + timedelta(seconds=index)
        run = Run(
            id=f"turn-{index:04}",
            session=identity,
            profile="interactive",
            origin=Origin(),
            status=RunStatus.completed,
            created_at=at,
        )
        store.save_run(run)
        store.append_message(
            identity,
            Message(
                role="user",
                content=f"Question {index:04}",
                run_id=run.id,
                origin=Origin(),
                ts=at,
            ),
        )
        store.append_message(
            identity,
            Message(
                role="assistant",
                content=f"Answer {index:04}",
                run_id=run.id,
                origin=Origin(),
                ts=at,
            ),
        )


def run_ids(html):
    return re.findall(r'data-run-id="([^"]+)"', html)


def test_500_turn_window_pagination_archives_and_anchor(
    config, mock_model, monkeypatch
):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        seed(app.state.store, "long", 500)
        # Context boundaries are explanatory markers, not conversation turns.
        app.state.store.append_message(
            "long",
            Message(
                role="assistant",
                kind="compaction",
                content="Earlier context summary",
                run_id="turn-0499",
                origin=Origin(),
                ts=datetime(2020, 1, 1, tzinfo=UTC) + timedelta(seconds=498.5),
            ),
        )
        page = client.get("/sessions/long")
        assert len(run_ids(page.text)) == TURN_WINDOW
        assert run_ids(page.text)[0] == "turn-0480"
        assert "Load earlier messages" in page.text
        assert len(page.content) < 50000
        older = client.get("/sessions/long/earlier?before=turn-0480").json()
        assert run_ids(older["html"]) == [f"turn-{n:04}" for n in range(460, 480)]
        assert older["before"] == "turn-0460"
        earliest = client.get("/sessions/long/earlier?before=turn-0020").json()
        assert earliest["before"] is None
        anchored = client.get("/sessions/long?turn=turn-0003")
        assert "Answer 0003" in anchored.text
        assert 'id="message-8"' in anchored.text
        assert "Return to latest messages" in anchored.text
        fragment = client.get("/sessions/long/earlier?anchor=message-8").json()
        assert "Answer 0003" in fragment["html"]

        # The observer must never load full history, prompts or historical runs.
        def forbidden(*args, **kwargs):
            raise AssertionError("Observer fetched full session history")

        monkeypatch.setattr(app.state.store, "transcript", forbidden)
        monkeypatch.setattr(app.state.store, "all_messages", forbidden)
        event = client.get("/sessions/long/events")
        payload = json.loads(event.text.split("data: ", 1)[1].strip())
        assert run_ids(payload["html"]) == ["turn-0499"]
        assert len(event.content) < 3000
        monkeypatch.undo()
        assert client.post("/api/sessions/long/archive").status_code == 200
        archived = client.get("/sessions/long/archives/1")
        assert len(run_ids(archived.text)) == TURN_WINDOW
        assert 'id="composer"' not in archived.text
        earlier = client.get("/sessions/long/earlier?archive=1&before=turn-0480").json()
        assert len(run_ids(earlier["html"])) == TURN_WINDOW
        assert "Answer 0460" in earlier["html"]
        deep = client.get("/sessions/long/archives/1?turn=turn-0003")
        assert "Answer 0003" in deep.text


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Intro\n\n-", "Intro\n\n"),
        ("Intro\n\n- first\n- sec", "Intro\n\n"),
        ("Intro\n\n- first\n\n", "Intro\n\n- first\n\n"),
        ("Intro\n\n```py\nprint(", "Intro\n\n"),
        ("Intro\n\n```py\nprint(1)\n```", "Intro\n\n```py\nprint(1)\n```"),
        ("Intro\n\n**par", "Intro\n\n"),
        ("Intro\n\n**partial**", "Intro\n\n**partial**"),
        ("Intro\n\n[link](https://exam", "Intro\n\n"),
        ("Intro\n\n| Name | Value |\n| --- | --- |\n| par", "Intro\n\n"),
    ],
)
def test_trailing_markdown_is_held_until_complete(source, expected):
    assert preview_text(source) == expected


def test_status_poll_unchanged_and_recovers_on_success(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        app.state.model.available = False
        down = client.get("/ui/status")
        known = re.search(r'"known":"([^"]+)"', down.text)[1]
        assert "unavailable" in down.text
        assert client.get("/ui/status?known=" + known).status_code == 204
        # A successful preflight/completion clears stale failure state immediately.
        client.post(
            "/api/sessions", json={"id": "recovery", "memory_scope": "ephemeral"}
        )
        result = client.post(
            "/api/sessions/recovery/messages", json={"message": "hello"}
        )
        assert result.json()["status"] == "completed"
        recovered = client.get("/ui/status?known=" + known)
        assert recovered.status_code == 200
        assert "unavailable" not in recovered.text
        assert "ready" in recovered.text
        assert client.get("/favicon.ico").status_code == 200


def test_archive_selection_requires_review(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        seed(app.state.store, "review", 1)
        listing = client.get("/")
        assert 'href="/sessions/review/lifecycle/archive"' in listing.text
        confirmation = client.get("/sessions/review/lifecycle/archive")
        assert "restore it afterwards" in confirmation.text
        review = client.post("/sessions/cleanup/review", data={"session_ids": "review"})
        assert "Archive selected sessions" in review.text
        assert client.get("/api/sessions/review").json()["archived_at"] is None
        assert 'action="/sessions/cleanup/apply"' in review.text


def test_job_run_now_redirects_before_generation_finishes(config):
    import asyncio
    import threading

    import httpx

    from solakit.core.config import Job
    from solakit.core.model import ModelClient

    entered, release = threading.Event(), threading.Event()
    config.jobs = [
        Job(
            id="background",
            cron="0 0 1 1 *",
            task="Wait for QA",
            profile="interactive",
            bind_session="ephemeral",
            enabled=True,
        )
    ]

    async def backend(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        return httpx.Response(
            200,
            json={
                "model": config.model.model_id,
                "choices": [
                    {"message": {"role": "assistant", "content": "Background finished"}}
                ],
            },
        )

    app = create_app(
        config=config,
        model=ModelClient(config.model, transport=httpx.MockTransport(backend)),
    )
    with TestClient(app) as client:
        sign_in(client)
        try:
            response = client.post(
                "/operations/jobs/background/run", follow_redirects=False
            )
            assert response.status_code == 303
            assert entered.wait(2)
            assert not release.is_set()
            location = response.headers["location"]
            run = client.get("/api" + location).json()
            assert run["status"] in {"queued", "running"}
            page = client.get(location)
            assert "Refresh run status" in page.text
            assert (
                client.post(
                    "/operations/jobs/background/run", follow_redirects=False
                ).status_code
                == 409
            )
        finally:
            release.set()
        client.get(f"/sessions/{run['session']}/events")
        assert client.get("/api" + location).json()["status"] == "completed"


def test_run_view_reads_only_contiguous_tail_and_handles_split_utf8(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        seed(app.state.store, "tail", 2)
        run = Run(
            id="big",
            session="tail",
            profile="interactive",
            origin=Origin(),
            status=RunStatus.completed,
        )
        app.state.store.save_run(run)
        content = "月" * 12000
        app.state.store.append_message(
            "tail",
            Message(role="user", content=content, run_id=run.id, origin=Origin()),
        )
        app.state.store.append_message(
            "tail",
            Message(
                role="assistant", content="Finished", run_id=run.id, origin=Origin()
            ),
        )
        view = client.get("/api/runs/big?view=true").json()
        assert [m["content"] for m in view["messages"]] == [content, "Finished"]
        assert "prompt" not in view
        path = app.state.store.root / "sessions/tail.jsonl"
        with path.open("ab") as stream:
            stream.write(b'{"unfinished')
        assert app.state.store.run("big", view=True)["messages"] == view["messages"]
        assert app.state.store.storage_health()["status"] == "degraded"


def test_queued_successor_settles_with_its_own_delta(config, monkeypatch):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import httpx

    from solakit.core.model import ModelClient

    entered, release, observed = threading.Event(), threading.Event(), threading.Event()

    async def backend(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        return httpx.Response(
            200,
            json={
                "model": config.model.model_id,
                "choices": [{"message": {"role": "assistant", "content": "Finished"}}],
            },
        )

    app = create_app(
        config=config,
        model=ModelClient(config.model, transport=httpx.MockTransport(backend)),
    )
    with TestClient(app) as client:
        sign_in(client)
        client.post("/api/sessions", json={"id": "queued", "memory_scope": "ephemeral"})
        first = client.post(
            "/api/sessions/queued/messages?wait=false", json={"message": "First"}
        ).json()
        assert entered.wait(2)
        second = client.post(
            "/api/sessions/queued/messages?wait=false", json={"message": "Second"}
        ).json()
        original = app.state.store.run

        def watching(run_id, **kwargs):
            if kwargs.get("view"):
                observed.set()
            return original(run_id, **kwargs)

        monkeypatch.setattr(app.state.store, "run", watching)
        with ThreadPoolExecutor(max_workers=1) as pool:
            observer = pool.submit(client.get, "/sessions/queued/events")
            try:
                assert observed.wait(2)
            finally:
                release.set()
            event = observer.result(5)
        snapshots = [
            json.loads(line.removeprefix("data: "))
            for line in event.text.splitlines()
            if line.startswith("data: ")
        ]
        assert {s["run_id"] for s in snapshots} == {first["id"], second["id"]}
        final = {s["run_id"]: s for s in snapshots}
        for run_id, snapshot in final.items():
            assert run_ids(snapshot["html"]) == [run_id]
            assert "Finished" in snapshot["html"]
        assert not snapshots[-1]["pending"]


def test_dom_identities_are_not_treated_as_credentials():
    from solakit.core.secrets import Redactor
    from solakit.ui.view import turns

    data = {
        "id": "s",
        "default_profile": "interactive",
        "messages": [
            {
                "run_id": "r",
                "ts": "2026-11-05T00:00:00+00:00",
                "role": "assistant",
                "content": "One",
                "tool_calls": [{"id": "one", "function": {"name": "runtime.status"}}],
            },
            {
                "run_id": "r",
                "ts": "2026-11-05T00:00:01+00:00",
                "role": "assistant",
                "content": "Two",
                "tool_calls": [{"id": "two", "function": {"name": "runtime.status"}}],
            },
        ],
    }
    rendered = Redactor().redact(turns(data, [], {}))
    identities = [item["node_id"] for item in rendered[0]["items"]]
    assert identities == ["message-0", "tool-1-one", "message-2", "tool-3-two"]


def test_compaction_boundary_is_part_of_active_turn_delta(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        app.state.store.create_session(Session(id="boundary"))
        run = Run(
            id="compacted",
            session="boundary",
            profile="interactive",
            origin=Origin(),
            status=RunStatus.completed,
        )
        app.state.store.save_run(run)
        app.state.store.append_message(
            "boundary",
            Message(
                role="assistant",
                kind="compaction",
                content="Earlier summary",
                run_id=run.id,
                origin=Origin(),
            ),
        )
        app.state.store.append_message(
            "boundary",
            Message(
                role="assistant", content="New answer", run_id=run.id, origin=Origin()
            ),
        )
        response = client.get("/sessions/boundary/events")
        snapshot = json.loads(response.text.split("data: ", 1)[1].strip())
        assert run_ids(snapshot["html"]) == [run.id]
        assert snapshot["html"].index('<section class="turn"') < snapshot["html"].index(
            'class="context-boundary"'
        )
        assert "Earlier summary" in snapshot["html"]
        assert "New answer" in snapshot["html"]
