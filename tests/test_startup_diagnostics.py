import asyncio
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from solakit.core.config import MemoryConfig, SecretRef
from solakit.core.memory import MemoryClient
from solakit.main import create_app


@pytest.mark.parametrize("mode", ["ready", "denied", "timeout", "invalid", "deadline"])
def test_read_only_memory_startup_diagnostics(
    config, mock_model, monkeypatch, caplog, mode
):
    monkeypatch.setenv("TEST_MEMORY_KEY", "memory-test-credential")
    roots = ["viking://user/sola/memories", "viking://user/sola/notes"]
    config.memory = MemoryConfig(
        enabled=True,
        base_url="http://memory:1933",
        api_key=SecretRef(env="TEST_MEMORY_KEY"),
        roots=roots,
    )
    (config.paths.fragments / "recall-header.md").write_text("Recall\n")
    if mode == "deadline":
        config.memory.timeout_s = 0.01
    requests = []

    async def backend(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/v1/fs/stat"
        assert request.headers["X-API-Key"] == "memory-test-credential"
        if mode == "deadline":
            await asyncio.sleep(1)
        if mode == "timeout":
            raise httpx.ReadTimeout("memory-test-credential", request=request)
        if mode == "denied":
            return httpx.Response(401, json={"detail": "memory-test-credential"})
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "result": {"isDir": True} if mode == "ready" else [],
            },
        )

    monkeypatch.setattr(
        "solakit.main.MemoryClient",
        lambda cfg, key, **kw: MemoryClient(
            cfg, key, transport=httpx.MockTransport(backend)
        ),
    )
    with caplog.at_level(logging.INFO):
        with TestClient(create_app(config=config, model=mock_model)) as client:
            health = client.get("/health").json()
            expected = "ready" if mode == "ready" else "unavailable"
            assert health["memory"] == expected
            assert health["memory_checked_at"]
            assert health["status"] == ("ok" if mode == "ready" else "degraded")
    assert [r.url.params["uri"] for r in requests] == (
        roots if mode == "ready" else roots[:1]
    )
    assert f"Memory: {expected}" in caplog.text
    assert "read-only startup check" in caplog.text
    assert "Tools: modules=none; loaded=none" in caplog.text
    assert "Profile interactive: interactive — tools: none (0)" in caplog.text
    assert "Model: ready" in caplog.text
    assert "Storage: ok" in caplog.text
    assert "memory-test-credential" not in caplog.text
    assert "operator-test-token" not in caplog.text
