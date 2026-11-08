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


@pytest.mark.parametrize("stop", [False, True])
def test_job_run_now_redirects_before_generation_finishes(config, stop):
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
            session_url = f"/sessions/{run['session']}"
            stop_url = location + "/cancel?return_to_session=true"
            session_page = client.get(session_url).text
            assert f'action="{stop_url}"' in session_page
            assert session_page.index('id="pending"') < session_page.index(
                'id="stop-run"'
            )
            assert 'data-key="stop"' not in session_page
            if stop:
                response = client.post(stop_url, follow_redirects=False)
                assert response.status_code == 303
                assert response.headers["location"] == session_url
                assert client.get("/api" + location).json()["status"] == "cancelled"
                assert f'action="{stop_url}"' not in client.get(session_url).text
        finally:
            release.set()
        client.get(f"/sessions/{run['session']}/events")
        assert client.get("/api" + location).json()["status"] == (
            "cancelled" if stop else "completed"
        )


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


def test_continuous_timeline_across_compactions(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        store = app.state.store
        seed(store, "timeline", 25)
        for number in (1, 2):
            assert client.post("/api/sessions/timeline/archive").status_code == 200
            at = datetime(2020, 1, 1, tzinfo=UTC) + timedelta(seconds=25 + number)
            store.append_message(
                "timeline",
                Message(
                    role="assistant",
                    kind="compaction",
                    content=f"Summary {number}",
                    archive=f"timeline.jsonl.{number}",
                    run_id=f"compact-{number}",
                    origin=Origin(),
                    ts=at,
                ),
            )
            run = Run(
                id=f"after-{number}",
                session="timeline",
                profile="interactive",
                origin=Origin(),
                status=RunStatus.completed,
                created_at=at,
            )
            store.save_run(run)
            store.append_message(
                "timeline",
                Message(
                    role="user",
                    content=f"After {number}",
                    run_id=run.id,
                    origin=Origin(),
                    ts=at,
                ),
            )
        page = client.get("/sessions/timeline")
        assert run_ids(page.text) == ["after-2"]
        assert "Load earlier messages" in page.text
        older_page = client.get(
            "/sessions/timeline/earlier",
            params={"before": "boundary-" + at.isoformat().replace("+00:00", "Z")},
        ).json()
        page_text = older_page["html"] + page.text
        assert run_ids(page_text) == [f"turn-{n:04}" for n in range(6, 25)] + [
            "after-1",
            "after-2",
        ]
        assert (
            page_text.index("Answer 0024")
            < page_text.index("Summary 1")
            < page_text.index("After 1")
            < page_text.index("Summary 2")
            < page_text.index("After 2")
        )
        assert page_text.count("Context compacted ·") == 2
        assert '<details class="context-boundary"><summary>' in page_text
        assert 'href="/sessions/timeline/archives/1">View full archive' in page_text
        assert 'href="/sessions/timeline/archives/2">View full archive' in page_text
        assert 'id="message-archive-1-50"' in page_text
        assert 'id="message-2"' in page_text
        older = client.get(
            "/sessions/timeline/earlier?before=turn-0007-archive-1"
        ).json()
        assert run_ids(older["html"]) == [f"turn-{n:04}" for n in range(7)]
        assert older["before"] is None
        anchored = client.get("/sessions/timeline?anchor=message-archive-1-2")
        assert "Answer 0000" in anchored.text
        archive = client.get("/sessions/timeline/archives/2")
        assert "After 1" in archive.text and "Answer 0024" not in archive.text
        assert 'id="composer"' not in archive.text


def test_mid_run_segments_keep_tool_results_and_live_target_separate():
    from solakit.ui.view import turns

    messages = [
        {
            "run_id": "r",
            "ts": f"2026-11-05T00:00:0{index}+00:00",
            "role": "assistant",
            "content": f"Step {index}",
            "timeline_segment": segment,
            "tool_calls": [{"id": str(index), "function": {"name": "runtime.status"}}],
        }
        for index, segment in enumerate(("1", None))
    ]
    history = turns(
        {"id": "s", "default_profile": "interactive", "messages": messages},
        [
            {
                "id": "r",
                "session": "s",
                "status": "running",
                "created_at": messages[0]["ts"],
            }
        ],
        {
            "r": {
                "tools": [
                    {"name": "runtime.status", "args": {}, "result": {"content": value}}
                    for value in ("Archived result", "Current result")
                ]
            }
        },
    )
    assert [turn["timeline_id"] for turn in history] == ["r-archive-1", "r"]
    assert [turn["pending"] for turn in history] == [False, True]
    assert history[0]["items"][1]["content"] == "Archived result"
    assert history[1]["items"][1]["content"] == "Current result"


def test_segment_loading_and_manual_seam(config, mock_model):
    app = create_app(config=config, model=mock_model)
    reads = []

    @app.middleware("http")
    async def track(request, call_next):
        if request.url.path.startswith("/api/sessions/mixed/archives/"):
            reads.append(request.url.path)
        return await call_next(request)

    with TestClient(app) as client:
        sign_in(client)
        store = app.state.store
        seed(store, "mixed", 25)
        for number in (1, 2, 3):
            assert client.post("/api/sessions/mixed/archive").status_code == 200
            at = datetime(2020, 1, 1, tzinfo=UTC) + timedelta(seconds=30 + number)
            if number < 3:
                store.append_message(
                    "mixed",
                    Message(
                        role="assistant",
                        kind="compaction",
                        content=f"Summary {number}",
                        archive=f"mixed.jsonl.{number}",
                        run_id=f"compact-{number}",
                        origin=Origin(),
                        ts=at,
                    ),
                )
            run = Run(
                id=f"mixed-{number}",
                session="mixed",
                profile="interactive",
                origin=Origin(),
                status=RunStatus.completed,
                created_at=at,
            )
            store.save_run(run)
            store.append_message(
                "mixed",
                Message(
                    role="user",
                    content=f"Later {number}",
                    run_id=run.id,
                    origin=Origin(),
                    ts=at,
                ),
            )
        page = client.get("/sessions/mixed")
        assert reads == []
        assert run_ids(page.text) == ["mixed-3"]
        batch = client.get("/sessions/mixed/earlier?before=mixed-3").json()
        assert [path.rsplit("/", 1)[1] for path in reads] == ["3", "2", "1"]
        html = batch["html"]
        assert run_ids(html) == [f"turn-{n:04}" for n in range(7, 25)] + [
            "mixed-1",
            "mixed-2",
        ]
        assert html.count("Context compacted ·") == 2
        assert html.count("Sola no longer holds the original turns verbatim") == 2
        assert html.count("Archived ·") == 1
        assert 'archives/3">View full archive' in html
        assert "2 messages" in html
        assert (
            html.index("Summary 1")
            < html.index("Later 1")
            < html.index("Summary 2")
            < html.index("Later 2")
            < html.index("Archived ·")
        )
        older = client.get(
            "/sessions/mixed/earlier", params={"before": batch["before"]}
        ).json()
        assert run_ids(older["html"]) == [f"turn-{n:04}" for n in range(7)]
        assert older["before"] is None
        reads.clear()
        # A window wholly within archive 1 does not read any older segment.
        page = client.get("/sessions/mixed?turn=turn-0003-archive-1")
        assert "Answer 0003" in page.text


def test_lazy_window_stops_at_first_sufficient_archive(config, mock_model):
    app = create_app(config=config, model=mock_model)
    reads = []

    @app.middleware("http")
    async def track(request, call_next):
        if request.url.path.startswith("/api/sessions/bounded/archives/"):
            reads.append(request.url.path.rsplit("/", 1)[1])
        return await call_next(request)

    with TestClient(app) as client:
        sign_in(client)
        store = app.state.store
        seed(store, "bounded", 25)
        assert client.post("/api/sessions/bounded/archive").status_code == 200
        # A second large segment, with distinct stable run identities.
        for index in range(25):
            at = datetime(2020, 1, 2, tzinfo=UTC) + timedelta(seconds=index)
            run = Run(
                id=f"second-{index}",
                session="bounded",
                profile="interactive",
                origin=Origin(),
                status=RunStatus.completed,
                created_at=at,
            )
            store.save_run(run)
            store.append_message(
                "bounded",
                Message(
                    role="user",
                    content=f"Second {index}",
                    run_id=run.id,
                    origin=Origin(),
                    ts=at,
                ),
            )
        assert client.post("/api/sessions/bounded/archive").status_code == 200
        page = client.get("/sessions/bounded")
        assert reads == ["2"]
        assert run_ids(page.text) == []
        assert "Archived ·" in page.text
        assert "Current conversation is empty" in page.text
        assert "A new conversation." not in page.text
        reads.clear()
        batch = client.get("/sessions/bounded/earlier?before=seam-archive-2").json()
        assert reads == ["2"]
        assert run_ids(batch["html"]) == [f"second-{index}" for index in range(5, 25)]
        assert batch["before"] == "second-5-archive-2"
        older = client.get(
            "/sessions/bounded/earlier", params={"before": batch["before"]}
        ).json()
        assert "Archived ·" in older["html"]
        assert run_ids(older["html"]) == [
            f"turn-{index:04}" for index in range(10, 25)
        ] + [f"second-{index}" for index in range(5)]
        pending = Run(
            id="pending-empty",
            session="bounded",
            profile="interactive",
            origin=Origin(),
            status=RunStatus.queued,
        )
        store.save_run(pending)
        waiting = client.get("/sessions/bounded")
        assert run_ids(waiting.text) == [pending.id]
        assert "Archived ·" in waiting.text


def test_active_tool_hydration_skips_unloaded_mid_run_calls(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        store = app.state.store
        store.create_session(Session(id="split", memory_scope="ephemeral"))
        at = datetime(2020, 1, 1, tzinfo=UTC)
        run = Run(
            id="split-run",
            session="split",
            profile="interactive",
            origin=Origin(),
            status=RunStatus.completed,
            created_at=at,
        )
        store.save_run(run)
        for index in (0, 1):
            if index:
                assert client.post("/api/sessions/split/archive").status_code == 200
            store.append_message(
                "split",
                Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        {
                            "id": f"call-{index}",
                            "type": "function",
                            "function": {"name": "runtime.status", "arguments": "{}"},
                        }
                    ],
                    run_id=run.id,
                    origin=Origin(),
                    ts=at + timedelta(seconds=index),
                ),
            )
            store.log_tool(
                run.id,
                {
                    "name": "runtime.status",
                    "args": {},
                    "result": {"content": f"Result {index}"},
                },
            )
            store.append_message(
                "split",
                Message(
                    role="tool",
                    content=f"Result {index}",
                    tool_call_id=f"call-{index}",
                    run_id=run.id,
                    origin=Origin(),
                    ts=at + timedelta(seconds=index),
                ),
            )
        page = client.get("/sessions/split")
        assert "Result 1" in page.text
        assert "Result 0" not in page.text
        earlier = client.get("/sessions/split/earlier?before=split-run").json()
        assert "Result 0" in earlier["html"]
        assert "Result 1" not in earlier["html"]
