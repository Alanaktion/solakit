"""Local services do not inherit proxy/CA environment settings accidentally."""

import asyncio

import pytest

from solakit.core.config import MemoryConfig, ModelConfig
from solakit.core.memory import MemoryClient
from solakit.core.model import ModelClient


def test_malformed_no_proxy_does_not_break_default_clients(config, monkeypatch):
    from fastapi.testclient import TestClient

    from solakit.main import create_app

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "[::1],[fd8b::1],127.0.0.1")
    memory = MemoryClient(MemoryConfig())
    asyncio.run(memory.close())
    with TestClient(create_app(config=config)) as client:
        assert client.get("/health").json()["status"] == "degraded"


@pytest.mark.parametrize("trust_env", [False, True])
def test_proxy_and_ca_environment_trust_is_an_explicit_setting(monkeypatch, trust_env):
    observed = []

    def client(**kwargs):
        observed.append(kwargs)
        return object()

    monkeypatch.setattr("httpx.AsyncClient", client)
    ModelClient(ModelConfig(trust_env=trust_env))
    MemoryClient(MemoryConfig(trust_env=trust_env))
    assert [kwargs["trust_env"] for kwargs in observed] == [trust_env, trust_env]
