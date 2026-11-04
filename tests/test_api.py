from fastapi.testclient import TestClient

from solakit.main import create_app
from solakit.runtime import Runtime


def test_health_and_auth(config, auth):
    with TestClient(create_app(config=config)) as client:
        assert client.get("/health").json()["status"] == "degraded"
        assert client.post("/api/runs", json={"message": "Hi"}).status_code == 401
        assert client.get("/api/channels", headers=auth).json() == []
        assert client.get("/").status_code == 404
        assert client.post("/v1/runs", json={"message": "Hi"}).status_code == 404


def test_unconfigured_agent(config, auth):
    with TestClient(create_app(config=config)) as client:
        response = client.post("/api/runs", json={"message": "Hello"}, headers=auth)
        assert response.status_code == 503


def test_agent_runs(config, auth):
    class TestAgent:
        async def run(self, message: str) -> str:
            return f"Received: {message}"

    with TestClient(create_app(Runtime(TestAgent()), config=config)) as client:
        response = client.post("/api/runs", json={"message": " Hello "}, headers=auth)
        assert response.json() == {"response": "Received: Hello"}


def test_invalid_messages(config, auth):
    with TestClient(create_app(config=config)) as client:
        for payload in ({}, {"message": "   "}, {"message": "x" * 32001}):
            assert (
                client.post("/api/runs", json=payload, headers=auth).status_code == 422
            )
