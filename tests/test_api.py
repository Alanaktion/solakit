import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from starlette.types import Message as ASGIMessage
from test_run_loop import call, harness, reply, wire

from solakit.api import create_app
from solakit.auth import COOKIE, AuthService
from solakit.client import MAX_STREAMS, PAGE_BYTES, ClientService
from solakit.config import EffectiveProfile, HTTPSettings
from solakit.evidence import canonical
from solakit.records import Message, Origin, RunState, new_id
from solakit.runtime import compose

BASE = "http://127.0.0.1:8765"
PASSWORD = "synthetic-operator-secret"


@pytest.fixture
def app(profile: EffectiveProfile) -> FastAPI:
    async def respond(body: dict[str, Any]) -> bytes:
        return wire(reply())

    context, _ = harness(profile, respond)
    return create_app(context)


@pytest.fixture
def client(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SOLA_OPERATOR_PASSWORD", PASSWORD)
    with TestClient(app, base_url=BASE) as client:
        yield client


def login(client: TestClient, *, bearer: bool = False) -> dict[str, Any]:
    response = client.post(
        "/api/v1/auth/login",
        headers={"Origin": BASE},
        json={"password": PASSWORD, "transport": "bearer" if bearer else "cookie"},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    if bearer:
        client.headers["Authorization"] = "Bearer " + body["token"]
    else:
        client.headers.update({"Origin": BASE, "X-CSRF-Token": body["csrf_token"]})
    return body


@pytest.mark.parametrize("state", ["absent", "invalid", "expired", "revoked"])
def test_cookie_authentication_states(client: TestClient, app: FastAPI, state: str) -> None:
    if state != "absent":
        login(client)
    if state == "invalid":
        client.cookies.clear()
        client.cookies.set(COOKIE, "invalid")
    elif state in ("expired", "revoked"):
        assert client.portal
        statement = "UPDATE login_sessions SET " + (
            "expires_at=0" if state == "expired" else "revoked=1"
        )
        client.portal.call(lambda: app.state.context.store.db.execute(statement))
    response = client.get("/api/v1/sessions")
    assert response.status_code == 401
    assert PASSWORD not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "https://evil.invalid",
        "http://sibling.localhost:8765",
        "http://127.0.0.1:8766",
    ],
)
def test_cookie_mutations_require_exact_origin(client: TestClient, origin: str | None) -> None:
    login(client)
    del client.headers["Origin"]
    headers = {"Origin": origin} if origin is not None else {}
    response = client.post("/api/v1/sessions", json={}, headers=headers)
    assert response.status_code == 403
    assert client.get("/api/v1/sessions").json()["items"] == []


def test_csrf_cookie_flags_and_mixed_auth(client: TestClient) -> None:
    result = login(client)
    assert "token" not in result
    assert client.get("/api/v1/auth/session").json()["transport"] == "cookie"
    assert client.post("/api/v1/sessions", json={}).status_code == 201
    del client.headers["X-CSRF-Token"]
    assert client.post("/api/v1/sessions", json={}).status_code == 403
    assert (
        client.post("/api/v1/sessions", json={}, headers={"X-CSRF-Token": "bad"}).status_code == 403
    )
    token = client.cookies[COOKIE]
    response = client.get("/api/v1/sessions", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 400 and response.json()["error"]["code"] == "ambiguous_auth"
    client.cookies.clear()
    login_response = client.post(
        "/api/v1/auth/login", headers={"Origin": BASE}, json={"password": PASSWORD}
    )
    cookie = login_response.headers.get_list("set-cookie")[0]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    assert "Max-Age=" in cookie


def test_bearer_no_origin_logout_and_revoke_all(client: TestClient) -> None:
    result = login(client, bearer=True)
    assert not client.cookies
    assert "Origin" not in client.headers
    assert client.post("/api/v1/sessions", json={}).status_code == 201
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/sessions").status_code == 401
    del client.headers["Authorization"]
    login(client, bearer=True)
    assert client.delete("/api/v1/auth/sessions").status_code == 204
    assert client.get("/api/v1/sessions").status_code == 401
    assert result["token"] not in client.get("/health").text


def test_host_forwarded_duplicate_and_url_credentials(client: TestClient) -> None:
    login(client, bearer=True)
    assert (
        client.get(
            "/api/v1/sessions",
            headers={"Host": "evil.invalid", "X-Forwarded-Host": "127.0.0.1:8765"},
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/v1/sessions",
            headers={
                "Forwarded": 'host="evil.invalid";proto=https',
                "X-Forwarded-Host": "evil.invalid",
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/sessions",
            json={},
            headers={"Origin": "null", "X-Forwarded-Host": "127.0.0.1:8765"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/sessions", headers=[("Host", "127.0.0.1:8765"), ("Host", "evil.invalid")]
        ).status_code
        == 400
    )
    assert client.get("/api/v1/sessions?access_token=synthetic-private").status_code == 400
    assert (
        client.get("/api/v1/sessions", headers={"Cookie": f"{COOKIE}=a; {COOKIE}=b"}).status_code
        == 400
    )


def test_login_origin_throttle_and_secret_safe_validation(client: TestClient, app: FastAPI) -> None:
    assert client.post("/api/v1/auth/login", json={"password": PASSWORD}).status_code == 403
    assert (
        client.post(
            "/api/v1/auth/login", headers={"Origin": "null"}, json={"password": PASSWORD}
        ).status_code
        == 403
    )
    for _ in range(5):
        response = client.post(
            "/api/v1/auth/login", headers={"Origin": BASE}, json={"password": "synthetic-private"}
        )
        assert response.status_code == 401 and "synthetic-private" not in response.text
    assert (
        client.post(
            "/api/v1/auth/login", headers={"Origin": BASE}, json={"password": PASSWORD}
        ).status_code
        == 429
    )
    assert client.portal
    client.portal.call(
        lambda: app.state.context.store.db.execute("UPDATE login_throttle SET window_start=0")
    )
    login(client)
    response = client.post(
        "/api/v1/runs", json={"session_id": "synthetic-private", "content": PASSWORD}
    )
    assert response.status_code == 422
    assert PASSWORD not in response.text and "synthetic-private" not in response.text
    assert client.get("/api/v1/no-such-route").json()["error"]["code"] == "http_error"
    assert client.post("/api/v1/sessions", content=b"x" * 131073).status_code == 413


def test_persistent_auth_and_schema_upgrade(
    profile: EffectiveProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLA_OPERATOR_PASSWORD", PASSWORD)
    context = compose(profile)
    with TestClient(create_app(context), base_url=BASE) as first:
        login(first)
        token = first.cookies[COOKIE]
        assert first.portal

        def old_schema() -> None:
            db = context.store.db  # type: ignore[union-attr]
            with db:
                db.execute("DROP TABLE login_sessions")
                db.execute("DROP TABLE login_throttle")
                db.execute("PRAGMA user_version=1")

        first.portal.call(old_schema)
    # Simulate an actual Phase 9 database, then preserve a new login across owners.
    with TestClient(create_app(compose(profile)), base_url=BASE) as second:
        assert (
            second.get("/api/v1/sessions", headers={"Cookie": f"{COOKIE}={token}"}).status_code
            == 401
        )
        login(second)
        saved = second.cookies[COOKIE]
    with TestClient(create_app(compose(profile)), base_url=BASE) as third:
        assert (
            third.get("/api/v1/sessions", headers={"Cookie": f"{COOKIE}={saved}"}).status_code
            == 200
        )
    database = (profile.data_dir / "state.db").read_bytes()
    assert saved.encode() not in database and PASSWORD.encode() not in database
    assert hashed_token(saved).encode() in database
    for path in (profile.data_dir / "evidence").glob("*.jsonl"):
        assert (
            saved.encode() not in path.read_bytes() and PASSWORD.encode() not in path.read_bytes()
        )


def hashed_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def test_api_runs_tools_idempotency_artifacts_and_search(
    profile: EffectiveProfile, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SOLA_OPERATOR_PASSWORD", PASSWORD)
    context, requests = harness(
        profile,
        [
            reply(calls=[call("files.read", json.dumps({"path": str(tmp_path / "source.txt")}))]),
            reply("The fixture says hello."),
        ],
    )
    (tmp_path / "source.txt").write_text("hello")
    with TestClient(create_app(context), base_url=BASE) as client:
        login(client, bearer=True)
        session = client.post(
            "/api/v1/sessions", json={"mode": "isolated", "title": "Fixture"}
        ).json()
        key = new_id()
        body = {"session_id": session["id"], "content": "Read source.txt and tell me what it says."}
        headers = {"Idempotency-Key": key}
        result = client.post("/api/v1/runs?wait=5", json=body, headers=headers)
        assert result.status_code == 200, result.text
        run = result.json()["run"]
        assert run["state"] == "completed"
        assert client.get(result.headers["location"]).json()["id"] == run["id"]
        retry = client.post("/api/v1/runs", json=body, headers=headers)
        assert retry.json()["run"]["id"] == run["id"]
        assert (
            client.post(
                "/api/v1/runs", json=body | {"content": "different"}, headers=headers
            ).status_code
            == 409
        )
        assert client.post("/api/v1/runs", json=body).status_code == 422
        messages = client.get(f"/api/v1/sessions/{session['id']}/messages").json()["items"]
        assert [item["record"]["role"] for item in messages] == ["user", "tool", "assistant"]
        assert len(requests) == 2
        prompts = client.get(f"/api/v1/runs/{run['id']}/prompts").json()["items"]
        assert len(prompts) == 2
        artifact = prompts[0]["record"]["body"]
        path = f"/api/v1/runs/{run['id']}/{artifact['path']}"
        part = client.get(path + "?limit=64")
        assert part.status_code == 200 and len(part.content) == 64
        assert part.headers["x-artifact-sha256"] == artifact["sha256"]
        assert part.headers["x-next-offset"] == "64"
        rest = client.get(path + "?offset=64").content
        assert hashlib.sha256(part.content + rest).hexdigest() == artifact["sha256"]
        assert client.get(path + "?limit=262145").status_code == 422
        assert client.get(path + "?offset=99999999").status_code == 416
        other = client.post("/api/v1/sessions", json={}).json()["id"]
        other_run = client.post(
            "/api/v1/runs?wait=5",
            json={"session_id": other, "content": "other"},
            headers={"Idempotency-Key": new_id()},
        ).json()["run"]
        assert client.get(f"/api/v1/runs/{other_run['id']}/{artifact['path']}").status_code == 404
        assert client.get("/api/v1/search?q=hello").json()["items"]
        assert (
            client.post("/api/v1/prompt", json={"content": "Hello", "hashes": True}).status_code
            == 200
        )
        assert client.get("/api/v1/profiles").json()["items"][0]["name"] == "default"
        assert client.get("/api/v1/tools").json()["items"]
        assert client.get("/api/v1/memory").status_code == 200
        assert client.get("/api/v1/health").json()["accepting"]
        assert client.portal
        client.portal.call(
            lambda: setattr(
                context, "profile", context.profile.model_copy(update={"tools": frozenset()})
            )
        )
        assert (
            client.post("/api/v1/runs", json=body, headers=headers).json()["run"]["id"] == run["id"]
        )
    assert context.state == "stopped"


def test_bulk_archive_endpoint(client: TestClient) -> None:
    path = "/api/v1/sessions/archive"
    assert client.post(path, json={"session_ids": [new_id()]}).status_code == 401
    login(client)
    identities = [client.post("/api/v1/sessions", json={}).json()["id"] for _ in range(2)]
    assert (
        client.post(
            path, json={"session_ids": identities}, headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    for invalid in ([], ["bad"], [new_id()] * 101):
        assert client.post(path, json={"session_ids": invalid}).status_code == 422
    assert client.post(path, json={"session_ids": [identities[0], new_id()]}).status_code == 404
    assert len(client.get("/api/v1/sessions").json()["items"]) == 2
    response = client.post(path, json={"session_ids": identities + identities})
    assert response.status_code == 200
    assert response.json() == {"archived": 2}
    assert client.post(path, json={"session_ids": identities}).json() == {"archived": 0}
    assert client.get("/api/v1/sessions").json()["items"] == []
    assert len(client.get("/api/v1/sessions?archived=true").json()["items"]) == 2
    assert (
        client.patch(f"/api/v1/sessions/{identities[0]}", json={"archived": False}).status_code
        == 200
    )


def test_session_title_search_filters_before_pagination(client: TestClient) -> None:
    login(client, bearer=True)
    path = "/api/v1/sessions"
    client.post(path, json={"title": "Unrelated topic"})
    matches = [
        client.post(path, json={"title": f"Travel plans {i}"}).json()["id"] for i in range(3)
    ]
    archived = client.post(path, json={"title": "Travel archive"}).json()["id"]
    client.patch(f"{path}/{archived}", json={"archived": True})
    ephemeral = client.post(path, json={"title": "Travel once", "mode": "ephemeral"}).json()["id"]
    params: dict[str, str | int] = {"q": " TRAVEL ", "limit": 2}
    first = client.get(path, params=params).json()
    assert [item["id"] for item in first["items"]] == matches[:2]
    second = client.get(path, params=params | {"after": first["next"]}).json()
    assert [item["id"] for item in second["items"]] == matches[2:]
    last = client.get(path, params=params | {"after": second["next"]}).json()
    assert last == {"items": [], "next": second["next"]}
    archived_page = client.get(path, params={"q": "travel", "archived": "true"}).json()
    assert [item["id"] for item in archived_page["items"]] == [archived]
    included = client.get(path, params={"q": "travel", "include_ephemeral": "true"}).json()
    assert [item["id"] for item in included["items"]] == matches + [ephemeral]
    assert client.get(path, params={"q": "missing"}).json()["items"] == []
    assert client.get(path, params={"q": "x" * 257}).status_code == 422
    assert client.get(path, params={"q": "travel", "after": "bad"}).status_code == 422


def test_session_pinned_filter_preserves_creation_cursor_and_title_search(
    client: TestClient,
) -> None:
    login(client, bearer=True)
    path = "/api/v1/sessions"
    identities = [client.post(path, json={"title": f"Topic {i}"}).json()["id"] for i in range(4)]
    for identity in identities[1::2]:
        assert client.patch(f"{path}/{identity}", json={"pinned": True}).status_code == 200
    params: dict[str, str | int] = {"pinned": "true", "limit": 1}
    first = client.get(path, params=params).json()
    assert [item["id"] for item in first["items"]] == identities[1:2]
    second = client.get(path, params=params | {"after": first["next"]}).json()
    assert [item["id"] for item in second["items"]] == identities[3:4]
    unpinned = client.get(path, params={"pinned": "false"}).json()
    assert [item["id"] for item in unpinned["items"]] == identities[::2]
    assert len(client.get(path).json()["items"]) == 4
    assert client.get(path, params={"pinned": "true", "q": "Topic 0"}).json()["items"] == []
    client.patch(f"{path}/{identities[3]}", json={"archived": True})
    archived = client.get(path, params={"pinned": "true", "archived": "true"}).json()
    assert [item["id"] for item in archived["items"]] == identities[3:4]
    client.patch(f"{path}/{identities[1]}", json={"pinned": False})
    assert client.get(path, params={"pinned": "true"}).json()["items"] == []
    assert client.get(path, params={"pinned": "invalid"}).status_code == 422


def test_session_pagination_is_separate_from_snapshot(client: TestClient, app: FastAPI) -> None:
    login(client)
    identities = [
        client.post("/api/v1/sessions", json={"title": str(i)}).json()["id"] for i in range(4)
    ]
    first = client.get("/api/v1/sessions?limit=2").json()
    assert [s["id"] for s in first["items"]] == identities[:2]
    second = client.get("/api/v1/sessions", params={"limit": 2, "after": first["next"]}).json()
    assert [s["id"] for s in second["items"]] == identities[2:]
    assert client.get("/api/v1/sessions?after=bad").status_code == 422
    assert client.get("/api/v1/sessions?limit=101").status_code == 422
    identity = identities[0]
    assert client.portal

    def populate() -> None:
        store = app.state.context.store
        run = store.admit(identity, request_id=new_id(), content="first", profile="default")
        for i in range(60):
            store.append(
                Message(
                    origin=Origin.INTERACTIVE,
                    session_id=identity,
                    run_id=run.id,
                    role="assistant",
                    content=str(i),
                )
            )

    client.portal.call(populate)
    snapshot = client.portal.call(app.state.service.snapshot, identity)
    listing = client.get(f"/api/v1/sessions/{identity}/messages?limit=2").json()
    assert len(snapshot["messages"]) == 50
    assert snapshot["messages"][0]["sequence"] > listing["items"][-1]["sequence"]
    next_page = client.get(
        f"/api/v1/sessions/{identity}/messages", params={"limit": 2, "after": listing["next"]}
    ).json()
    assert next_page["items"][0]["sequence"] > listing["next"]
    assert client.get(f"/api/v1/sessions/{identity}/events?cursor={new_id()}:0").status_code == 422


def test_jobs_shared_controls_and_occurrences(client: TestClient) -> None:
    login(client, bearer=True)
    identity = client.post("/api/v1/sessions", json={}).json()["id"]
    body = {"task": "Say hello", "schedule": "cron:0 12 * * *", "session_id": identity}
    created = client.post("/api/v1/jobs", json=body)
    assert created.status_code == 201, created.text
    job = created.json()
    assert not job["enabled"]
    assert (
        client.post("/api/v1/jobs", json=body | {"budgets": {"model_calls": True}}).status_code
        == 422
    )
    path = "/api/v1/jobs/" + job["id"]
    assert client.patch(path, json={"paused": True}).json()["paused"]
    assert client.patch(path, json={"paused": False, "enabled": True}).json()["enabled"]
    assert client.patch(path, json={"generation": 99}).status_code == 422
    assert client.patch(path, json={"tools": ["ungranted"]}).status_code == 409
    assert client.put("/api/v1/jobs/control", json={"paused": True}).json()["paused"]
    assert client.get("/api/v1/jobs/control").json()["paused"]
    assert client.put("/api/v1/jobs/control", json={"paused": False}).json()["paused"] is False
    headers = {"Idempotency-Key": new_id()}
    occurrence = client.post(path + "/run", headers=headers).json()
    assert occurrence["run_id"]
    assert client.post(path + "/run", headers=headers).json()["id"] == occurrence["id"]
    assert client.get(path + "/occurrences").json()["items"][0]["id"] == occurrence["id"]
    assert client.get("/api/v1/jobs?limit=1").json()["items"][0]["id"] == job["id"]
    assert client.delete(path).json()["deleted"]
    assert client.get("/api/v1/jobs").json()["items"] == []


def test_sse_replay_preview_loss_gap_revocation_and_cleanup(
    profile: EffectiveProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLA_OPERATOR_PASSWORD", PASSWORD)

    async def scenario() -> None:
        context = compose(profile)
        await context.start()
        assert context.store and context.model
        service = ClientService(context)
        auth = AuthService(context.store, profile.http)
        token, _, _ = auth.login(PASSWORD)
        logged = auth.authenticate(None, "Bearer " + token)
        session = context.store.create_session()
        run = context.runs.submit(session.id, profile=profile, content="Hello", request_id=new_id())  # type: ignore[union-attr]
        context.runs.cancel(run.id)  # type: ignore[union-attr]
        try:
            stream = service.events(session.id, 0, auth, logged)
            assert b"event: snapshot" in await anext(stream)
            seen = [await anext(stream) for _ in range(3)]
            assert sum(b"event: run.terminal" in event for event in seen) == 1
            last = int(seen[-1].split(b"\n")[0].split(b":")[-1])
            context.model.previews.update(session.id, run.id, 1, "first preview")
            preview = await anext(stream)
            assert b"event: preview" in preview and b"id:" not in preview
            await stream.aclose()
            assert service.streams == 0
            context.model.previews.latest.clear()
            replay = service.events(session.id, last, auth, logged)
            assert b'"replace_previews":true' in await anext(replay)
            auth.logout(logged)
            assert b"event: auth.expired" in await anext(replay)
            await replay.aclose()
            token, _, _ = auth.login(PASSWORD)
            logged = auth.authenticate(None, "Bearer " + token)
            # Retention/sparse replay is modelled without changing immutable evidence.
            original = context.store.events
            context.store.events = lambda *a, **kw: []  # type: ignore[method-assign]
            gap = service.events(session.id, 0, auth, logged)
            assert b"event: snapshot" in await anext(gap)
            assert b"event: reset" in await anext(gap)
            await gap.aclose()
            context.store.events = original  # type: ignore[method-assign]
            future = service.events(session.id, 999999, auth, logged)
            assert b"event: reset" in await anext(future)
            await future.aclose()
            assert service.streams == 0
            assert len(canonical(service.snapshot(session.id))) < 65536
        finally:
            await context.stop()

    asyncio.run(scenario())


def test_preview_and_page_caps(client: TestClient, app: FastAPI) -> None:
    login(client, bearer=True)
    assert client.portal

    def fill() -> None:
        for i in range(1000):
            app.state.context.model.previews.update(new_id(), new_id(), i, "猫" * 10000)

    client.portal.call(fill)
    assert len(app.state.context.model.previews.latest) == 8
    assert all(
        len(preview["text"].encode()) <= 8192
        for preview in app.state.context.model.previews.latest.values()
    )
    service = app.state.service
    service.streams = MAX_STREAMS
    identity = client.post("/api/v1/sessions", json={}).json()["id"]
    assert client.get(f"/api/v1/sessions/{identity}/events").status_code == 429
    service.streams = 0
    assert PAGE_BYTES == 512 * 1024


def test_openapi_contract(client: TestClient, app: FastAPI) -> None:
    assert client.get("/openapi.json").status_code == 401
    login(client, bearer=True)
    schema = client.get("/openapi.json").json()
    assert schema == app.openapi()
    assert schema["components"]["securitySchemes"]["OperatorCookie"]["name"] == COOKIE
    assert schema["paths"]["/api/v1/runs"]["post"]["parameters"][-1]["name"] == "Idempotency-Key"
    assert "202" in schema["paths"]["/api/v1/runs"]["post"]["responses"]
    fixture = json.loads(Path("docs/contracts/examples/openapi.json").read_text())
    assert schema == fixture


def test_image_attachment_scope_idempotency_and_unsupported(
    client: TestClient, app: FastAPI
) -> None:
    from test_images import png

    from solakit.records import Artifact

    login(client, bearer=True)
    topic = client.post("/api/v1/sessions", json={}).json()["id"]
    source = client.post(
        "/api/v1/runs?wait=5",
        json={"session_id": topic, "content": "Hello"},
        headers={"Idempotency-Key": new_id()},
    ).json()["run"]["id"]
    assert client.portal

    def seed_image() -> str:
        store = app.state.context.store
        artifact = Artifact.model_validate(
            store.artifact(new_id(), png(64, 64)) | {"media_type": "image/png"}
        )
        store.append(
            Message(
                origin=Origin.INTERACTIVE,
                session_id=topic,
                run_id=source,
                role="user",
                content="Synthetic input image",
                image=artifact,
            )
        )
        return artifact.path.split("/")[-1]

    identity = client.portal.call(seed_image)
    image = {"run_id": source, "artifact_id": identity}
    key = {"Idempotency-Key": new_id()}
    body = {"session_id": topic, "content": "Describe the image.", "image": image}
    assert client.post("/api/v1/runs", json=body, headers=key).status_code == 422

    def enable() -> None:
        app.state.context.profile = app.state.context.profile.model_copy(
            update={"model_image_inputs": True}
        )

    client.portal.call(enable)
    first = client.post("/api/v1/runs?wait=5", json=body, headers=key)
    assert first.status_code == 200 and first.json()["run"]["state"] == "completed", first.text
    assert (
        client.post("/api/v1/runs", json=body, headers=key).json()["run"]["id"]
        == first.json()["run"]["id"]
    )
    assert client.post("/api/v1/runs", json=body | {"image": None}, headers=key).status_code == 409
    other = client.post("/api/v1/sessions", json={"mode": "isolated"}).json()["id"]
    assert (
        client.post(
            "/api/v1/runs", json=body | {"session_id": other}, headers={"Idempotency-Key": new_id()}
        ).status_code
        == 403
    )
    prompts = client.get(f"/api/v1/runs/{first.json()['run']['id']}/prompts").json()["items"]
    reference = prompts[0]["record"]["body"]
    raw = client.get(f"/api/v1/runs/{first.json()['run']['id']}/{reference['path']}").content
    messages = json.loads(raw)["messages"]
    assert any(
        isinstance(m["content"], list) and m["content"][-1]["type"] == "image_url" for m in messages
    )


def test_http_disconnect_wait_timeout_reconnect_and_completion(
    profile: EffectiveProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    import httpx2
    import uvicorn

    monkeypatch.setenv("SOLA_OPERATOR_PASSWORD", PASSWORD)

    async def scenario() -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        selected = profile.model_copy(update={"http": HTTPSettings(allowed_origins=(origin,))})
        context = compose(selected)
        assert context.model
        entered, finish = asyncio.Event(), asyncio.Event()

        class DelayedStream(httpx2.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                body = wire(reply("The durable answer."))
                first, rest = body.split(b"\n\n", 1)
                yield first + b"\n\n"
                entered.set()
                await finish.wait()
                yield rest

        async def respond(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/models"):
                return httpx2.Response(200, json={"data": [{"id": selected.model_pin}]})
            return httpx2.Response(
                200, stream=DelayedStream(), headers={"content-type": "text/event-stream"}
            )

        context.model.transport = httpx2.MockTransport(respond)
        app = create_app(context)
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="critical", access_log=False, proxy_headers=False)
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if serving.done():
                        await serving
                    await asyncio.sleep(0.01)
            async with httpx2.AsyncClient(base_url=origin, trust_env=False) as client:
                logged = await client.post(
                    "/api/v1/auth/login",
                    headers={"Origin": origin},
                    json={"password": PASSWORD, "transport": "bearer"},
                )
                client.headers["Authorization"] = "Bearer " + logged.json()["token"]
                session = (await client.post("/api/v1/sessions", json={})).json()["id"]
                key = new_id()
                body = {"session_id": session, "content": "A long synthetic request."}
                receipt = await client.post(
                    "/api/v1/runs?wait=0.02", json=body, headers={"Idempotency-Key": key}
                )
                assert receipt.status_code == 202
                run = receipt.json()["run"]["id"]
                await asyncio.wait_for(entered.wait(), 2)
                async with client.stream(
                    "GET", f"/api/v1/sessions/{session}/events?cursor={session}:0"
                ) as stream:
                    assert stream.status_code == 200
                    async for line in stream.aiter_lines():
                        if line == "event: preview":
                            break
                waiting = asyncio.create_task(client.get(f"/api/v1/runs/{run}?wait=30"))
                await asyncio.sleep(0.02)
                waiting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
                finish.set()
                completed = (await client.get(f"/api/v1/runs/{run}?wait=5")).json()
                assert completed["state"] == "completed"
                seen: list[str] = []
                terminal = 0
                async with client.stream(
                    "GET", f"/api/v1/sessions/{session}/events?cursor={session}:0"
                ) as stream:
                    async for line in stream.aiter_lines():
                        if line.startswith("id: "):
                            seen.append(line[4:])
                        if line == "event: run.terminal":
                            terminal += 1
                        if terminal and not line:
                            break
                assert terminal == 1 and len(seen) == len(set(seen))
                retry = await client.post(
                    "/api/v1/runs", json=body, headers={"Idempotency-Key": key}
                )
                assert retry.json()["run"]["id"] == run
                messages = (await client.get(f"/api/v1/sessions/{session}/messages")).json()[
                    "items"
                ]
                assert [m["record"]["role"] for m in messages] == ["user", "assistant"]
                async with asyncio.timeout(2):
                    while app.state.service.streams:
                        await asyncio.sleep(0.01)
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 5)
            listener.close()
        assert context.state == "stopped"

    asyncio.run(scenario())


def test_artifact_integrity_and_bounded_record_pages(client: TestClient, app: FastAPI) -> None:
    from solakit.records import Artifact, ModelStep

    login(client, bearer=True)
    session = client.post("/api/v1/sessions", json={}).json()["id"]
    run = client.post(
        "/api/v1/runs?wait=5",
        json={"session_id": session, "content": "Hello"},
        headers={"Idempotency-Key": new_id()},
    ).json()["run"]["id"]
    reference = client.get(f"/api/v1/runs/{run}/prompts").json()["items"][0]["record"]["body"]
    path = app.state.context.profile.data_dir / reference["path"]
    original = path.read_bytes()
    path.write_bytes(b"x" * len(original))
    response = client.get(f"/api/v1/runs/{run}/{reference['path']}")
    assert response.status_code == 503 and b"x" * 100 not in response.content
    path.write_bytes(original)
    assert client.portal

    def append_large() -> None:
        store = app.state.context.store
        raw = b"synthetic model response"
        artifact = Artifact.model_validate(store.artifact(new_id(), raw))
        store.append(
            ModelStep(
                origin=Origin.INTERACTIVE,
                session_id=session,
                run_id=run,
                index=99,
                model="fixture-qwen",
                content="x" * 100000,
                response=artifact,
            )
        )
        for _ in range(20):
            store.append(
                Message(
                    origin=Origin.INTERACTIVE,
                    session_id=session,
                    run_id=run,
                    role="assistant",
                    content="x" * 60000,
                )
            )

    client.portal.call(append_large)
    response = client.get(f"/api/v1/runs/{run}/records?limit=100")
    assert len(response.content) <= PAGE_BYTES + 2048
    items = response.json()["items"]
    large = next(item for item in items if item["truncated"])
    assert large["record"]["artifacts"]["response"]["sha256"]
    assert len(items) < 25
    next_page = client.get(
        f"/api/v1/runs/{run}/records", params={"after": response.json()["next"], "limit": 100}
    )
    assert next_page.json()["items"][0]["sequence"] > items[-1]["sequence"]


def test_secure_cookie_and_remote_configuration(
    profile: EffectiveProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv("SOLA_OPERATOR_PASSWORD", PASSWORD)
    with pytest.raises(ValidationError):
        HTTPSettings(allowed_origins=("http://sola.example",))
    with pytest.raises(ValidationError):
        HTTPSettings(allowed_origins=("https://sola.example",))
    selected = profile.model_copy(
        update={"http": HTTPSettings(allowed_origins=("https://sola.example",), secure_cookie=True)}
    )
    with TestClient(create_app(compose(selected)), base_url="https://sola.example") as client:
        response = client.post(
            "/api/v1/auth/login",
            headers={"Origin": "https://sola.example"},
            json={"password": PASSWORD},
        )
        assert response.status_code == 200
        assert "Secure" in response.headers.get_list("set-cookie")[0]
        assert client.get("/api/v1/auth/session").status_code == 200


@pytest.mark.parametrize("origin", ["http://192.168.1.20:8382", "http://[fd12::1]:8382"])
def test_lan_cookie_login_and_exact_host_origin_checks(
    profile: EffectiveProfile, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    monkeypatch.setenv("SOLA_OPERATOR_PASSWORD", PASSWORD)
    selected = profile.model_copy(update={"http": HTTPSettings(allowed_origins=(origin,))})
    with TestClient(create_app(compose(selected)), base_url=origin) as client:
        response = client.post(
            "/api/v1/auth/login", headers={"Origin": origin}, json={"password": PASSWORD}
        )
        assert response.status_code == 200
        assert "; Secure" not in response.headers.get_list("set-cookie")[0]
        assert client.get("/api/v1/auth/session").status_code == 200
        assert client.post("/api/v1/sessions", json={}).status_code == 403
        client.headers.update({"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]})
        assert client.post("/api/v1/sessions", json={}).status_code == 201
        for foreign in ("http://192.168.1.21:8382", origin.replace(":8382", ":8383")):
            assert client.get("/health", headers={"Host": foreign[7:]}).status_code == 400
            assert (
                client.post("/api/v1/sessions", json={}, headers={"Origin": foreign}).status_code
                == 403
            )
        assert (
            client.post("/api/v1/sessions", json={}, headers={"X-CSRF-Token": "wrong"}).status_code
            == 403
        )


def test_slow_sse_send_releases_slot_and_does_not_block_runs(
    profile: EffectiveProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    import solakit.api as boundary
    import solakit.client as views

    monkeypatch.setenv("SOLA_OPERATOR_PASSWORD", PASSWORD)
    monkeypatch.setattr(boundary, "SEND_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(views, "HEARTBEAT_SECONDS", 0)

    async def respond(body: dict[str, Any]) -> bytes:
        return wire(reply())

    async def scenario() -> None:
        context, _ = harness(profile, respond)
        app = create_app(context)
        await context.start()
        assert context.store and context.runs
        token, _, _ = app.state.auth.login(PASSWORD)
        identity = context.store.create_session().id
        blocked = asyncio.Event()

        async def receive() -> dict[str, Any]:
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(message: ASGIMessage) -> None:
            if message["type"] == "http.response.body":
                blocked.set()
                await asyncio.Event().wait()

        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/api/v1/sessions/{identity}/events",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1:8765"),
                (b"authorization", ("Bearer " + token).encode()),
            ],
            "server": ("127.0.0.1", 8765),
            "client": ("127.0.0.1", 1234),
        }
        request = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.wait_for(blocked.wait(), 1)
            assert app.state.service.streams == 1
            run = context.runs.submit(
                identity, profile=context.profile, content="Hello", request_id=new_id()
            )
            assert (await context.runs.wait(run.id, timeout=2)).state == RunState.COMPLETED
            with pytest.raises(ClientDisconnect):
                await request
            assert app.state.service.streams == 0
            logged = app.state.auth.authenticate(None, "Bearer " + token)
            stream = app.state.service.events(identity, None, app.state.auth, logged)
            assert b"event: snapshot" in await anext(stream)
            assert await anext(stream) == b": heartbeat\n\n"
            await stream.aclose()
        finally:
            request.cancel()
            await context.stop()

    asyncio.run(scenario())


def test_omitted_profile_uses_server_selection(
    profile: EffectiveProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLA_OPERATOR_PASSWORD", PASSWORD)
    context, _ = harness(profile.model_copy(update={"name": "short"}), [reply()])
    with TestClient(create_app(context), base_url=BASE) as client:
        login(client, bearer=True)
        session = client.post("/api/v1/sessions", json={}).json()["id"]
        receipt = client.post(
            "/api/v1/runs?wait=5",
            json={"session_id": session, "content": "Hello"},
            headers={"Idempotency-Key": new_id()},
        )
        assert receipt.status_code == 200
        assert receipt.json()["run"]["profile"] == "short"
        assert client.get("/api/v1/tools").status_code == 200
        assert client.post("/api/v1/prompt", json={"content": "Hello"}).status_code == 200
        job = client.post(
            "/api/v1/jobs",
            json={"session_id": session, "task": "Hello", "schedule": "cron:0 12 * * *"},
        )
        assert job.status_code == 201 and job.json()["profile"] == "short"


def test_chat_shell_is_public_but_runtime_stays_authenticated(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "<sola-app></sola-app>" in response.text
    assert '<meta name="agent-name" content="Sola" />' in response.text
    assert response.text.index("/assets/chat.css") < response.text.index("/assets/user.css")
    assert PASSWORD not in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    for asset in (
        "chat.css",
        "chat.js",
        "app.js",
        "turn.js",
        "job.js",
        "artifact.js",
        "settings.js",
        "evidence.js",
        "client.js",
        "ui.js",
        "markdown.js",
        "lit-core.min.js",
        "lucide.min.js",
    ):
        response = client.get("/assets/" + asset)
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/v1/profiles").status_code == 401
    assert client.get("/assets/other.js").status_code == 401
    assert client.head("/assets/lit-core.min.js").status_code == 405
    assert client.post("/assets/lit-core.min.js").status_code == 401
    assert client.post("/assets/chat.js").status_code == 401
    assert client.get("/", headers={"Host": "evil.invalid"}).status_code == 400
    assert client.get("/", headers={"Origin": "https://evil.invalid"}).status_code == 403


def test_configured_agent_name_is_escaped_in_shell(client: TestClient, app: FastAPI) -> None:
    context = app.state.context
    context.profile = context.profile.model_copy(
        update={"agent_name": 'Luna 🌙 "><script>alert(1)</script>&'}
    )
    response = client.get("/")
    assert response.status_code == 200
    assert '<meta name="agent-name" content="Luna 🌙 &quot;&gt;&lt;script&gt;' in response.text
    assert (
        "<title>Luna 🌙 &quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;&amp; · Conversation"
        in response.text
    )
    assert "<script>alert(1)</script>" not in response.text


def test_optional_user_css(client: TestClient, app: FastAPI) -> None:
    path = app.state.context.profile.data_dir / "user.css"
    response = client.get("/assets/user.css")
    assert response.status_code == 200 and response.content == b""
    path.write_text(".brand { color: red; }", encoding="utf-8")
    response = client.get("/assets/user.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert response.headers["cache-control"] == "no-store"
    assert response.text == ".brand { color: red; }"
    path.write_text(".brand { color: blue; }", encoding="utf-8")
    assert client.get("/assets/user.css").text == ".brand { color: blue; }"
    path.unlink()
    assert client.get("/assets/user.css").content == b""
    assert client.post("/assets/user.css").status_code == 401
    assert client.get("/assets/state.db").status_code == 401


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_user_css_does_not_serve_links_or_directories(
    client: TestClient, app: FastAPI, tmp_path: Path, kind: str
) -> None:
    private = tmp_path / "private"
    private.write_text("private operator data")
    path = app.state.context.profile.data_dir / "user.css"
    if kind == "symlink":
        path.symlink_to(private)
    elif kind == "hardlink":
        os.link(private, path)
    else:
        path.mkdir()
    response = client.get("/assets/user.css")
    assert response.status_code == 404
    assert "private operator data" not in response.text


def test_recent_history_pages_backwards_without_changing_forward_clients(
    client: TestClient, app: FastAPI
) -> None:
    login(client)
    identity = client.post("/api/v1/sessions", json={}).json()["id"]

    def seed() -> None:
        store = app.state.context.store
        for i in range(60):
            run = store.admit(identity, request_id=new_id(), content=f"Turn {i}", profile="default")
            store.transition_run(run.id, RunState.CANCELLED)

    assert client.portal
    client.portal.call(seed)
    for kind in ("messages", "runs"):
        path = f"/api/v1/sessions/{identity}/{kind}"
        recent = client.get(path, params={"before": 0, "limit": 25}).json()
        assert len(recent["items"]) == 25
        sequences = [item["sequence"] for item in recent["items"]]
        assert sequences == sorted(sequences)
        assert recent["next"] == min(sequences)
        middle = client.get(path, params={"before": recent["next"], "limit": 25}).json()
        oldest = client.get(path, params={"before": middle["next"], "limit": 25}).json()
        assert len(oldest["items"]) == 10
        combined = oldest["items"] + middle["items"] + recent["items"]
        assert len({item["record"]["id"] for item in combined}) == 60
        assert client.get(path, params={"before": oldest["next"]}).json()["items"] == []
        forward = client.get(path, params={"limit": 100}).json()
        assert combined == forward["items"]
        assert client.get(path, params={"before": -1}).status_code == 422
        assert client.get(path, params={"before": 10, "after": 1}).status_code == 409
    run_id = forward["items"][-1]["record"]["id"]
    path = f"/api/v1/runs/{run_id}/records"
    recent = client.get(path, params={"before": 0, "limit": 1}).json()
    older = client.get(path, params={"before": recent["next"], "limit": 1}).json()
    assert older["items"][0]["sequence"] < recent["items"][0]["sequence"]
    assert client.get(path, params={"before": -1}).status_code == 422
    assert client.get(path, params={"before": 10, "after": 1}).status_code == 409


def test_large_chat_records_keep_attribution_and_tool_correlation() -> None:
    from solakit.client import record_view
    from solakit.records import ToolReceipt

    message = Message(
        origin=Origin.SCHEDULED,
        session_id=new_id(),
        run_id=new_id(),
        role="user",
        content="private scheduled input" * 5000,
    )
    view = record_view(1, message)
    assert view["truncated"]
    assert view["record"]["origin"] == "scheduled"
    assert view["record"]["role"] == "user"
    assert "content" not in view["record"]
    receipt = ToolReceipt(
        origin=Origin.INTERACTIVE,
        session_id=new_id(),
        run_id=new_id(),
        tool_call_id=new_id(),
        status="succeeded",
        result="long result" * 10000,
    )
    view = record_view(2, receipt)
    assert view["truncated"]
    assert view["record"]["tool_call_id"] == receipt.tool_call_id
    assert view["record"]["status"] == "succeeded"
    assert "result" not in view["record"]
