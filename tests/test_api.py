from fastapi.testclient import TestClient

from solakit.main import create_app


def test_health_and_auth(config, auth):
    with TestClient(create_app(config=config)) as client:
        assert client.get("/health").json()["status"] == "degraded"
        assert (
            client.post(
                "/api/sessions/web/messages", json={"message": "Hi"}
            ).status_code
            == 401
        )
        assert client.get("/api/channels", headers=auth).json() == []
        assert (
            client.get("/", follow_redirects=False).headers["location"]
            == "/login?next=%2F"
        )
        assert client.post("/v1/runs", json={"message": "Hi"}).status_code == 404


def test_persistent_message_endpoint(config, auth, mock_model):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        assert (
            client.post("/api/sessions", json={"id": "web"}, headers=auth).status_code
            == 201
        )
        response = client.post(
            "/api/sessions/web/messages", json={"message": " Hello "}, headers=auth
        )
        assert response.json()["status"] == "completed"
        transcript = client.get("/api/sessions/web", headers=auth).json()["messages"]
        assert [m["role"] for m in transcript] == ["user", "assistant"]
        assert transcript[0]["content"] == "Hello"
        for payload in ({}, {"message": "   "}, {"message": "x" * 32001}):
            assert (
                client.post(
                    "/api/sessions/web/messages", json=payload, headers=auth
                ).status_code
                == 422
            )
        assert (
            client.post(
                "/api/sessions/missing/messages", json={"message": "Hi"}, headers=auth
            ).status_code
            == 404
        )


def test_misplaced_session_profile_and_unknown_message_fields_rejected(
    config, auth, mock_model
):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        response = client.post(
            "/api/sessions", headers=auth, json={"id": "poet", "profile": "poet"}
        )
        assert response.status_code == 422
        assert client.get("/api/sessions", headers=auth).json() == []
        assert (
            client.post(
                "/api/sessions",
                headers=auth,
                json={"id": "web", "default_profile": "interactive"},
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/api/sessions/web/messages",
                headers=auth,
                json={"message": "Hi", "typo": True},
            ).status_code
            == 422
        )


def test_session_creation_is_idempotent_and_rejects_conflicts(config, auth):
    with TestClient(create_app(config=config)) as client:
        payload = {"id": "same", "title": "Original"}
        first = client.post("/api/sessions", json=payload, headers=auth)
        second = client.post("/api/sessions", json=payload, headers=auth)
        assert first.status_code == 201
        assert second.status_code == 200
        assert first.json() == second.json()
        assert (
            client.post(
                "/api/sessions", json={**payload, "title": "Other"}, headers=auth
            ).status_code
            == 409
        )


def test_model_health_requires_operator_auth(config, auth, mock_model):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        assert client.get("/health/model").status_code == 401
        assert client.get("/health/model", headers=auth).status_code == 200


def test_existing_session_conflict_precedes_new_session_validation(config, auth):
    with TestClient(create_app(config=config)) as client:
        assert (
            client.post(
                "/api/sessions", json={"id": "existing"}, headers=auth
            ).status_code
            == 201
        )
        response = client.post(
            "/api/sessions",
            json={"id": "existing", "default_profile": "missing"},
            headers=auth,
        )
        assert response.status_code == 409
        response = client.post(
            "/api/sessions",
            json={"id": "new", "default_profile": "missing"},
            headers=auth,
        )
        assert response.status_code == 422
