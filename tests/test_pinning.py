"""Pinned chats: channel-bound by default, operator-overridable, API-tracked."""

from fastapi.testclient import TestClient
from test_ui import sign_in

from solakit.core.config import Channel
from solakit.core.schemas import Session
from solakit.main import create_app


def with_channel(config):
    config.channels = [
        Channel(id="moon-room", type="api", channel_id="123", session="room")
    ]
    return config


def test_channel_sessions_exist_and_are_pinned_before_any_message(config, mock_model):
    app = create_app(config=with_channel(config), model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        listed = client.get("/api/sessions?pinned=true").json()
        assert [(s["id"], s["pinned"]) for s in listed] == [("room", True)]
        assert listed[0]["channel_names"] == ["moon-room"]
        assert client.get("/api/sessions/room").json()["pinned"] is True
        assert "moon-room" in client.get("/sessions/room").text


def test_pin_unpin_and_reset_persist(config, mock_model):
    app = create_app(config=with_channel(config), model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        app.state.store.create_session(Session(id="plain", source="web"))
        assert client.get("/api/sessions/plain").json()["pinned"] is False
        pinned = client.post("/api/sessions/plain/pin", json={"pinned": True})
        assert pinned.status_code == 200 and pinned.json()["pinned"] is True
        assert {s["id"] for s in client.get("/api/sessions?pinned=true").json()} == {
            "plain",
            "room",
        }
        # Channel sessions can be unpinned; the choice survives and resets.
        assert (
            client.post("/api/sessions/room/pin", json={"pinned": False}).json()[
                "pinned"
            ]
            is False
        )
        assert [s["id"] for s in client.get("/api/sessions?pinned=true").json()] == [
            "plain"
        ]
        unpinned = client.get("/api/sessions?pinned=false").json()
        assert "room" in {s["id"] for s in unpinned}
        assert (
            client.post("/api/sessions/room/pin", json={"pinned": None}).json()[
                "pinned"
            ]
            is True
        )
        assert app.state.store.session("plain").pinned is True


def test_pin_validation_and_missing_session(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        assert (
            client.post("/api/sessions/nope/pin", json={"pinned": True}).status_code
            == 404
        )
        assert (
            client.post("/api/sessions/nope/pin", json={"pinned": "yes"}).status_code
            == 422
        )
        assert client.post("/api/sessions/nope/pin", json={}).status_code == 422


def test_pin_first_ordering_and_sidebar_never_pages_pinned_away(config, mock_model):
    app = create_app(config=with_channel(config), model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        store = app.state.store
        for index in range(25):
            store.create_session(Session(id=f"recent-{index:02}", source="web"))
        store.create_session(Session(id="old-pin", source="web"))
        client.post("/api/sessions/old-pin/pin", json={"pinned": True})
        first = client.get("/api/sessions?pin_channels=true&limit=3").json()
        assert {s["id"] for s in first[:2]} == {"room", "old-pin"}
        page = client.get("/sessions/recent-00").text
        assert page.index('aria-label="Pinned chats"') < page.index(
            'aria-label="Recent chats"'
        )
        pinned_html = page.split('aria-label="Recent chats"')[0]
        assert 'href="/sessions/old-pin"' in pinned_html
        assert 'href="/sessions/room"' in pinned_html
        # Pinned chats are not repeated in the paged Recent list.
        assert (
            'href="/sessions/old-pin"' not in page.split('aria-label="Recent chats"')[1]
        )


def test_ui_pin_form_redirects_only_to_local_paths(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        app.state.store.create_session(Session(id="plain", source="web"))
        response = client.post(
            "/sessions/plain/pin",
            data={"pinned": "1", "return_to": "/sessions/plain"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/sessions/plain"
        assert app.state.store.session("plain").pinned is True
        for target in ("//evil.example", "https://evil.example", "/a\\b"):
            response = client.post(
                "/sessions/plain/pin",
                data={"pinned": "0", "return_to": target},
                follow_redirects=False,
            )
            assert response.headers["location"] == "/sessions/plain"
        assert app.state.store.session("plain").pinned is False
        bad = client.post("/sessions/plain/pin", data={"pinned": "maybe"})
        assert bad.status_code == 422
