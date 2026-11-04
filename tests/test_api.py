from fastapi.testclient import TestClient

from solakit.main import create_app
from solakit.runtime import Runtime


def test_health_and_schema():
    with TestClient(create_app()) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert "/v1/runs" in client.get("/openapi.json").json()["paths"]


def test_unconfigured_agent():
    with TestClient(create_app()) as client:
        response = client.post("/v1/runs", json={"message": "Hello"})
        assert response.status_code == 503
        assert response.json() == {"detail": "No agent configured"}


def test_agent_runs():
    class TestAgent:
        async def run(self, message: str) -> str:
            return f"Received: {message}"

    with TestClient(create_app(Runtime(TestAgent()))) as client:
        response = client.post("/v1/runs", json={"message": " Hello "})
        assert response.status_code == 200
        assert response.json() == {"response": "Received: Hello"}


def test_invalid_messages():
    with TestClient(create_app()) as client:
        for payload in ({}, {"message": "   "}, {"message": "x" * 32001}):
            assert client.post("/v1/runs", json=payload).status_code == 422
