"""UI integration checks: API parity, opaque auth, and safe presentation."""

import httpx
from fastapi.testclient import TestClient

from solakit.core.model import ModelClient
from solakit.core.schemas import Message, Origin, Run, RunStatus
from solakit.main import create_app
from solakit.ui.auth import COOKIE, LIFETIME, UISessions


def sign_in(client):
    response = client.post(
        "/login", data={"token": "operator-test-token"}, follow_redirects=False
    )
    assert response.status_code == 303
    return response


def new_session(client, identity="evening"):
    response = client.post(
        "/sessions",
        data={
            "id": identity,
            "default_profile": "interactive",
            "memory_scope": "ephemeral",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response


def test_login_cookie_api_logout_and_expired_fragment(config, mock_model, auth):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        assert client.get("/", follow_redirects=False).headers["location"] == "/login"
        assert client.get("/api/profiles").status_code == 401
        assert client.get("/api/profiles", headers=auth).json() == list(config.profiles)
        bad = client.post("/login", data={"token": "incorrect-secret"})
        assert bad.status_code == 401
        assert "incorrect-secret" not in bad.text
        assert COOKIE not in client.cookies
        response = sign_in(client)
        cookie = response.headers["set-cookie"]
        for flag in ("HttpOnly", "SameSite=lax", "Path=/", "Max-Age=604800"):
            assert flag in cookie
        assert "Secure" not in cookie
        assert "operator-test-token" not in cookie + client.get("/").text
        assert client.get("/api/profiles").status_code == 200
        opaque = client.cookies[COOKIE]
        client.post("/logout")
        client.cookies.set(COOKIE, opaque)
        assert client.get("/api/profiles").status_code == 401
        assert (
            client.get("/ui/status", headers={"HX-Request": "true"}).headers[
                "hx-redirect"
            ]
            == "/login"
        )
        assert client.get("/api/profiles", headers=auth).status_code == 200


def test_https_cookie_and_restart(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app, base_url="https://testserver") as client:
        assert "Secure" in sign_in(client).headers["set-cookie"]
        opaque = client.cookies[COOKIE]
        assert client.get("/").status_code == 200
    assert not app.state.ui_sessions.valid(opaque)


def test_session_expiry_and_login_lockout():
    clock = [100.0]
    sessions = UISessions(clock=lambda: clock[0])
    for _ in range(5):
        assert sessions.login("wrong", "right", "peer") == (None, 401)
    assert sessions.login("right", "right", "peer") == (None, 429)
    clock[0] += 60
    identity, status = sessions.login("right", "right", "peer")
    assert status == 200 and sessions.valid(identity)
    clock[0] += LIFETIME
    assert not sessions.valid(identity)


def test_login_lockout_http(config, mock_model):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        for _ in range(5):
            assert client.post("/login", data={"token": "bad"}).status_code == 401
        response = client.post("/login", data={"token": "operator-test-token"})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"
        assert COOKIE not in client.cookies


def test_session_create_chat_and_api_parity(config, mock_model):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        sign_in(client)
        assert "No conversations yet" in client.get("/").text
        new_session(client)
        page = client.get("/sessions/evening")
        assert page.status_code == 200
        assert 'name="profile"' not in page.text
        assert '"timeout":0' in page.text
        fragment = client.post(
            "/sessions/evening/messages",
            data={"message": "<script>alert(1)</script>", "profile": "ignored"},
            headers={"HX-Request": "true"},
        )
        assert fragment.status_code == 200
        assert fragment.headers["hx-trigger"] == "chatCompleted"
        assert "Hello back" in fragment.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in fragment.text
        assert "<script>" not in fragment.text
        assert "<!doctype" not in fragment.text
        record = client.get("/api/sessions/evening").json()
        assert record["memory_scope"] == "ephemeral"
        assert [m["role"] for m in record["messages"]] == ["user", "assistant"]
        assert record["messages"][0]["content"] == "<script>alert(1)</script>"
        run = client.get("/api/runs").json()[0]
        assert run["origin"]["channel"] == "api"
        assert run["profile"] == "interactive"
        assert "tokens: —" in fragment.text
        assert "Hello back" in client.get("/sessions/evening").text


def test_validation_and_missing_session(config, mock_model):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        sign_in(client)
        response = client.post(
            "/sessions", data={"id": '<img src=x onerror="alert(1)">'}
        )
        assert response.status_code == 422
        assert "<img src=x" not in response.text
        assert "&lt;img" in response.text
        assert client.get("/api/sessions").json() == []
        assert client.get("/sessions/missing").status_code == 404
        new_session(client)
        response = client.post(
            "/sessions/evening/messages",
            data={"message": " "},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 422
        assert 'role="alert"' in response.text
        assert client.get("/api/sessions/evening").json()["messages"] == []


def test_backend_down_honest_failure(config):
    def unavailable(request):
        raise httpx.ConnectError("Backend offline", request=request)

    model = ModelClient(config.model, transport=httpx.MockTransport(unavailable))
    with TestClient(create_app(config=config, model=model)) as client:
        sign_in(client)
        page = client.get("/")
        assert page.status_code == 200
        assert "degraded" in page.text and "unavailable" in page.text
        new_session(client)
        response = client.post(
            "/sessions/evening/messages",
            data={"message": "Hello"},
            headers={"HX-Request": "true"},
        )
        assert "Model unavailable" in response.text
        assert 'data-run-status="model_unavailable"' in response.text
        assert 'class="message message-assistant"' not in response.text
        assert client.get("/api/sessions/evening").json()["messages"] == []
        assert "hx-trigger" not in response.headers


def test_ui_redacts_credentials_without_rewriting_api_history(config, mock_model):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        sign_in(client)
        new_session(client)
        fragment = client.post(
            "/sessions/evening/messages",
            data={"message": "My token operator-test-token"},
            headers={"HX-Request": "true"},
        )
        assert "operator-test-token" not in fragment.text
        assert "[redacted]" in fragment.text
        assert "operator-test-token" not in client.get("/sessions/evening").text
        assert (
            client.get("/api/sessions/evening").json()["messages"][0]["content"]
            == "My token operator-test-token"
        )


def test_tool_cards_use_redacted_evidence_only(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        new_session(client)
        run = Run(
            session="evening",
            profile="interactive",
            origin=Origin(),
            status=RunStatus.completed,
        )
        app.state.store.save_run(run)
        app.state.store.append_message(
            "evening",
            Message(
                role="assistant",
                run_id=run.id,
                origin=Origin(),
                tool_calls=[
                    {
                        "id": "call1",
                        "type": "function",
                        "function": {
                            "name": "files.read",
                            "arguments": '{"PASSWORD":"raw-secret"}',
                        },
                    }
                ],
            ),
        )
        app.state.store.append_message(
            "evening",
            Message(
                role="tool",
                content="raw-tool-result",
                tool_call_id="call1",
                run_id=run.id,
                origin=Origin(),
            ),
        )
        app.state.store.log_tool(
            run.id,
            {
                "name": "files.read",
                "args": {"PASSWORD": "raw-secret"},
                "result": {"content": "<b>recorded result</b>", "error": False},
                "ms": 1,
            },
        )
        page = client.get("/sessions/evening")
        assert page.status_code == 200
        assert '<details class="tool-call">' in page.text
        assert "raw-secret" not in page.text
        assert "raw-tool-result" not in page.text
        assert "[redacted]" in page.text
        assert "&lt;b&gt;recorded result&lt;/b&gt;" in page.text


def test_local_assets_theme_and_minimal_navigation(config, mock_model):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        page = client.get("/login")
        assert "🌙" in page.text
        assert "Content-Security-Policy" in page.headers
        assert page.headers["cache-control"] == "no-store"
        assert "https://" not in page.text
        for path in (
            "app.js",
            "app.css",
            "moon.svg",
            "vendor/htmx.min.js",
            "vendor/LICENSE",
        ):
            assert client.get("/static/" + path).status_code == 200
        assert "prefers-color-scheme: dark" in client.get("/static/app.css").text
        sign_in(client)
        page = client.get("/")
        assert 'href="/prompt"' not in page.text
        assert 'href="/jobs"' not in page.text
