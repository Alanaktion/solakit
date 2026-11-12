"""Chat-first navigation retains operator capabilities and API evidence contracts."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from test_maintainability import route_table
from test_ui import sign_in
from test_ui_ux import seed

from solakit.core.config import Channel, Job
from solakit.core.schemas import Session
from solakit.main import create_app
from solakit.ui.navigation import LEGACY, health_indicator


def test_route_inventory_preserves_old_routes_and_api(config, mock_model):
    before = set(
        json.loads(
            (
                Path(__file__).parent / "fixtures/route-table-before-chat-first.json"
            ).read_text()
        )
    )
    after = set(route_table(config, mock_model))
    assert before <= after
    # The API only grows: existing routes stay, and pinning is the sole addition.
    api_before = {r for r in before if " /api/" in r}
    api_after = {r for r in after if " /api/" in r}
    assert api_after - api_before == {"POST /api/sessions/{session_id}/pin"}
    assert api_before <= api_after


def test_legacy_gets_redirect_with_queries_and_canonical_pages_work(config, mock_model):
    config.jobs = [
        Job(
            id="sample",
            cron="0 0 * * *",
            task="Sample",
            profile="interactive",
            enabled=False,
            bind_session="ephemeral",
        )
    ]
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        seed(app.state.store, "topic", 1)
        pairs = {
            **LEGACY,
            "/runs/turn-0000": "/settings/evidence/runs/turn-0000",
            "/operations/jobs/sample/edit": "/settings/jobs/sample/edit",
            "/operations/jobs/sample/delete": "/settings/jobs/sample/delete",
            "/operations/jobs/new": "/settings/jobs/new",
        }
        for old, new in pairs.items():
            response = client.get(old + "?window=50", follow_redirects=False)
            assert response.status_code == 308
            assert response.headers["location"] == new + "?window=50"
            assert client.get(new).status_code == 200
        settings = client.get("/settings/system").text
        for destination in [*LEGACY.values(), "/settings/sessions"]:
            assert f'href="{destination}"' in settings
        assert "Back to chat" in settings


def test_landing_uses_activity_without_pinning_or_get_side_effects(config, mock_model):
    config.channels = [
        Channel(id="channel", type="api", session="pinned", channel_id="pinned")
    ]
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        assert "Start a new chat" in client.get("/?new=1").text
        # Configured channel sessions exist (and are pinned) before any message.
        listed = client.get("/api/sessions").json()
        assert [(s["id"], s["pinned"]) for s in listed] == [("pinned", True)]
        store = app.state.store
        store.create_session(Session(id="recent", source="web"))
        store.create_session(Session(id="scheduled", source="cron"))
        store.create_session(Session(id="child", parent_session="recent"))
        store.create_session(Session(id="grandchild", parent_session="child"))
        store.create_session(Session(id="archived", archived_at=datetime.now(UTC)))
        assert (
            client.get("/", follow_redirects=False).headers["location"]
            == "/sessions/recent"
        )
        page = client.get("/sessions/recent").text
        assert "Pinned" in page and 'title="channel"' in page
        assert 'action="/sessions/pinned/pin"' in page
        assert 'href="/sessions/scheduled"' not in page
        assert "Delegated topics" in page
        assert 'id="topic-details"' in page
        assert "System status" not in page
        assert "session-badges" not in page
        assert (
            'href="/settings/evidence/prompt?session=recent&amp;profile=interactive"'
            in page
        )
        assert 'href="/sessions/archived"' in page
        assert 'id="topic-filter"' in page
        assert 'href="/sessions/grandchild"' in client.get("/sessions/child").text


def test_title_fallback_details_and_archived_readonly(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        seed(app.state.store, "topic", 2)
        page = client.get("/sessions/topic").text
        assert "Question 0000" in page
        assert 'href="/settings/evidence/runs/turn-0001"' in page
        assert "Memory scope" in page
        response = client.post(
            "/api/sessions/topic/messages", json={"message": "Fresh details"}
        )
        run_id = response.json()["id"]
        refreshed = client.get("/sessions/topic/details")
        assert refreshed.status_code == 200
        assert f"/settings/evidence/runs/{run_id}" in refreshed.text
        app.state.store.rename_session("topic", "<A title>")
        page = client.get("/sessions/topic").text
        assert "&lt;A title&gt;" in page and "<A title>" not in page
        client.post("/sessions/topic/lifecycle/archive")
        archived = client.get("/sessions/topic").text
        assert 'id="composer"' not in archived
        assert "Restore chat" in archived
        client.post("/sessions/topic/lifecycle/restore")
        assert 'id="composer"' in client.get("/sessions/topic").text


def test_health_dot_uses_real_health_and_bounded_degradation_data(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        assert client.get("/ui/health").status_code == 401
        sign_in(client)
        assert client.get("/ui/health").json()["color"] == "green"
        app.state.model.available = False
        assert client.get("/ui/health").json()["color"] == "red"
        app.state.model.available = True
        seed(app.state.store, "lagging", 1)
        app.state.store.update_run(
            "turn-0000",
            {
                "memory_status": "accepted",
                "created_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
            },
        )
        state = client.get("/ui/health").json()
        assert state["color"] == "amber"
        assert "1 extractions delayed" in state["label"]
        assert health_indicator({})["color"] == "amber"
