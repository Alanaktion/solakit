"""UI integration checks: API parity, opaque auth, and safe presentation."""

import httpx
from fastapi.testclient import TestClient

from solakit.core.model import ModelClient
from solakit.core.schemas import Message, Origin, Run, RunStatus
from solakit.main import create_app
from solakit.ui.auth import COOKIE, LIFETIME, UISessions


def send_and_observe(client, path, **kwargs):
    """Plain forms accept then redirect; wait through the read-only observer."""
    response = client.post(path, follow_redirects=False, **kwargs)
    if response.status_code != 303:
        return response
    target = response.headers["location"]
    client.get(target + "/events")
    return client.get(target)


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
        fragment = send_and_observe(
            client,
            "/sessions/evening/messages",
            data={"message": "<script>alert(1)</script>", "profile": "ignored"},
            headers={"HX-Request": "true"},
        )
        assert fragment.status_code == 200
        assert "hx-trigger" not in fragment.headers
        assert "Hello back" in fragment.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in fragment.text
        assert "<script>" not in fragment.text
        assert "<!doctype" in fragment.text
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
        response = send_and_observe(
            client,
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
        response = send_and_observe(
            client,
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
        fragment = send_and_observe(
            client,
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
        assert '<details class="tool-call" data-key=' in page.text
        assert "raw-secret" not in page.text
        assert "raw-tool-result" not in page.text
        assert "[redacted]" in page.text
        assert "&lt;b&gt;recorded result&lt;/b&gt;" in page.text


def test_local_assets_theme_and_navigation(config, mock_model):
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
        for path in ("/prompt", "/runs", "/operations", "/tools"):
            assert f'href="{path}"' in page.text
            assert client.get(path).status_code == 200


def test_tools_catalog_distinguishes_loaded_from_granted(config, mock_model):
    config.enabled_tools = ["files"]
    config.profiles["interactive"].tools = ["files.read"]
    with TestClient(create_app(config=config, model=mock_model)) as client:
        assert client.get("/api/tools").status_code == 401
        assert (
            client.get("/tools", follow_redirects=False).headers["location"] == "/login"
        )
        sign_in(client)
        catalog = client.get("/api/tools").json()
        assert catalog["modules"] == ["files"]
        assert catalog["profiles"]["interactive"] == ["files.read"]
        read = next(tool for tool in catalog["tools"] if tool["name"] == "files.read")
        write = next(tool for tool in catalog["tools"] if tool["name"] == "files.write")
        assert read["profiles"] == ["interactive"]
        assert write["profiles"] == []
        assert "path" in read["parameters"]["properties"]
        page = client.get("/tools")
        assert page.status_code == 200
        assert "files.read" in page.text and "files.write" in page.text
        assert "No description provided" not in page.text
        new_session(client)
        assert "1 tool" in client.get("/sessions/evening").text


def test_empty_tools_page(config, mock_model):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        sign_in(client)
        response = client.get("/tools")
        assert response.status_code == 200
        assert "No tools loaded." in response.text
        assert "No tools granted" in response.text


def test_real_tool_call_visible_in_chat_fragment(config):
    import json

    from solakit.tools import ToolContext, ToolResult, tool

    completions = []

    def backend(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})
        body = json.loads(request.content)
        completions.append(body)
        message = {"role": "assistant", "content": "Finished"}
        if len(completions) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "test-call",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": '{"text":"<script>example</script>"}',
                        },
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "model": config.model.model_id,
                "choices": [{"message": message}],
            },
        )

    @tool(name="echo", profiles=["interactive"])
    async def echo(ctx: ToolContext, text: str) -> ToolResult:
        return ToolResult(content=text)

    model = ModelClient(config.model, transport=httpx.MockTransport(backend))
    app = create_app(config=config, model=model)
    with TestClient(app) as client:
        app.state.registry.register(echo)
        config.profiles["interactive"].tools = ["echo"]
        app.state.registry.validate()
        sign_in(client)
        new_session(client)
        response = send_and_observe(
            client,
            "/sessions/evening/messages",
            data={"message": "Use echo"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert '<details class="tool-call" data-key=' in response.text
        assert "echo" in response.text and "Finished" in response.text
        assert "&lt;script&gt;example&lt;/script&gt;" in response.text
        assert "<script>example</script>" not in response.text
        assert len(completions) == 2
        assert completions[1]["messages"][-1]["tool_call_id"] == "test-call"
        assert (
            '<details class="tool-call" data-key='
            in client.get("/sessions/evening").text
        )
        client.post("/sessions/evening/archive")
        archived = client.get("/sessions/evening/archives/1")
        assert archived.status_code == 200
        assert '<details class="tool-call" data-key=' in archived.text
        assert "&lt;script&gt;example&lt;/script&gt;" in archived.text
        assert 'id="composer"' not in archived.text


def test_ui_archive_confirmation_browsing_and_secret_masking(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        assert (
            client.post("/sessions/evening/archive", follow_redirects=False).headers[
                "location"
            ]
            == "/login"
        )
        sign_in(client)
        new_session(client)
        send_and_observe(
            client,
            "/sessions/evening/messages",
            data={
                "message": "Archived <script>text</script> operator-test-token",
            },
        )
        failed = Run(
            session="evening",
            profile="interactive",
            origin=Origin(),
            status=RunStatus.failed,
            error="Earlier failed attempt",
        )
        app.state.store.save_run(failed)
        confirmation = client.get("/sessions/evening/archive")
        assert confirmation.status_code == 200
        assert "Archive and start fresh" in confirmation.text
        assert client.get("/api/sessions/evening").json()["messages"]
        result = client.post("/sessions/evening/archive", follow_redirects=False)
        assert result.status_code == 303
        current = client.get("/sessions/evening")
        assert "A new conversation." in current.text
        assert "Earlier failed attempt" not in current.text
        assert 'id="composer"' in current.text
        assert "/sessions/evening/archives/1" in current.text
        archived = client.get("/sessions/evening/archives/1")
        assert archived.status_code == 200
        assert "Archive 1 · read only" in archived.text
        assert 'id="composer"' not in archived.text
        assert "&lt;script&gt;text&lt;/script&gt;" in archived.text
        assert "operator-test-token" not in archived.text
        assert "[redacted]" in archived.text
        assert (
            client.get("/api/sessions/evening/archives/1")
            .json()[0]["content"]
            .endswith("operator-test-token")
        )
        assert client.get("/sessions/evening/archives/99").status_code == 404
        assert client.get("/sessions/evening/archives/0").status_code == 404


def test_session_patch_and_rename_ui_preserve_identity_evidence(config, mock_model):
    import json

    from solakit.core.schemas import Session
    from solakit.core.store import Store

    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        assert (
            client.patch("/api/sessions/old", json={"title": "New"}).status_code == 401
        )
        assert (
            client.get("/sessions/old/rename", follow_redirects=False).headers[
                "location"
            ]
            == "/login"
        )
        assert (
            client.post(
                "/sessions/old/rename", data={"title": "New"}, follow_redirects=False
            ).headers["location"]
            == "/login"
        )
        sign_in(client)
        store = app.state.store
        store.create_session(Session(id="old"))
        store.append_message(
            "old",
            Message(
                role="user", content="Original history", origin=Origin(), run_id="r"
            ),
        )
        store.set_session_archived("old", True)
        metadata = store.root / "sessions/old.meta.json"
        before = json.loads(metadata.read_text())
        before["imported_from"] = "legacy"
        metadata.write_text(json.dumps(before))
        transcript = store.root / "sessions/old.jsonl"
        evidence = transcript.read_bytes()
        response = client.patch(
            "/api/sessions/old", json={"title": "  Moonlight & notes  "}
        )
        assert response.status_code == 200
        assert response.json()["title"] == "Moonlight & notes"
        after = json.loads(metadata.read_text())
        assert {key: value for key, value in after.items() if key != "title"} == {
            key: value for key, value in before.items() if key != "title"
        }
        assert transcript.read_bytes() == evidence
        assert (
            client.get(
                "/api/sessions", params={"status": "all", "q": "Moonlight"}
            ).json()[0]["id"]
            == "old"
        )
        for payload in (
            {},
            {"title": None},
            {"title": 1},
            {"title": "x" * 201},
            {"title": "Good", "id": "changed"},
            {"archived_at": None},
        ):
            assert client.patch("/api/sessions/old", json=payload).status_code == 422
        assert (
            client.patch("/api/sessions/missing", json={"title": "New"}).status_code
            == 404
        )
        page = client.get("/sessions/old")
        assert "Rename session" in page.text
        page = client.get("/sessions/old/rename")
        assert 'value="Moonlight &amp; notes"' in page.text
        bad = client.post("/sessions/old/rename", data={"title": "x" * 201})
        assert bad.status_code == 422 and 'role="alert"' in bad.text
        assert "x" * 201 in bad.text
        renamed = client.post(
            "/sessions/old/rename",
            data={"title": "<A new name>"},
            follow_redirects=False,
        )
        assert (
            renamed.status_code == 303
            and renamed.headers["location"] == "/sessions/old"
        )
        assert "&lt;A new name&gt;" in client.get("/sessions/old").text
        assert (
            client.patch("/api/sessions/old", json={"title": "  "}).json()["title"]
            == ""
        )
        assert transcript.read_bytes() == evidence
    restarted = Store(config.paths.data)
    assert restarted.session("old").title == ""
    assert restarted.session("old").archived_at
    assert restarted.search("Original")[0]["session_id"] == "old"
