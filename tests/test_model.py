import asyncio
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from solakit.core.config import ModelConfig, SecretRef, ServerConfig
from solakit.core.model import ModelClient, ModelUnavailable
from solakit.core.startup import ConfigurationError, prepare
from solakit.main import create_app


@pytest.mark.parametrize("source", ["none", "env", "file"])
def test_model_bearer_auth_used_for_preflight_and_completion(
    config, monkeypatch, source
):
    key = "model-fixture-secret"
    monkeypatch.setenv("MODEL_TEST_KEY", key)
    # Inherited provider credentials are never sent unless explicitly referenced.
    monkeypatch.setenv("OPENAI_API_KEY", "unreferenced-credential")
    authenticated = source != "none"
    if source == "env":
        config.model.api_key = SecretRef(env="MODEL_TEST_KEY")
    elif source == "file":
        path = config.paths.soul.parent / "model-token"
        path.write_text(key + "\n")
        path.chmod(0o600)
        config.model.api_key = SecretRef(file=path)
    requests = []

    def backend(request):
        requests.append(request)
        if authenticated:
            assert request.headers["Authorization"] == f"Bearer {key}"
        else:
            assert "Authorization" not in request.headers
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})
        return httpx.Response(
            200,
            json={
                "model": config.model.model_id,
                "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            },
        )

    model = ModelClient(config.model, transport=httpx.MockTransport(backend))

    async def exercise():
        await model.preflight()
        await model.complete([{"role": "user", "content": "hello"}], [])
        assert model.available
        assert [r.url.path for r in requests] == ["/v1/models", "/v1/chat/completions"]
        await model.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure, expected",
    [
        ("connect", "connection failed"),
        ("timeout", "timed out"),
        (401, "HTTP 401"),
        (403, "HTTP 403"),
        (500, "HTTP 500"),
    ],
)
def test_backend_failure_diagnostics_are_actionable_and_redacted(
    config, monkeypatch, caplog, failure, expected
):
    key = "never-log-this-model-token"
    monkeypatch.setenv("MODEL_TEST_KEY", key)
    config.model.api_key = SecretRef(env="MODEL_TEST_KEY")

    def backend(request):
        if failure == "connect":
            raise httpx.ConnectError(f"connection refused: {key}")
        if failure == "timeout":
            raise httpx.ReadTimeout(f"backend timeout: {key}")
        return httpx.Response(failure, text=f"response body must not be logged: {key}")

    model = ModelClient(config.model, transport=httpx.MockTransport(backend))

    async def exercise():
        with pytest.raises(ModelUnavailable):
            await model.preflight()
        assert not model.available
        await model.close()

    with caplog.at_level(logging.DEBUG, logger="solakit.core.model"):
        asyncio.run(exercise())
    assert expected in caplog.text
    assert "http://127.0.0.1:8000/v1/models" in caplog.text
    assert "auth_configured=True" in caplog.text
    assert "trust_env=False" in caplog.text
    assert key not in caplog.text
    assert "response body must not be logged" not in caplog.text
    if failure in {401, 403}:
        assert "[model].api_key" in caplog.text


def test_model_key_is_validated_and_registered_for_run_diagnostics(
    config, monkeypatch, caplog
):
    key = "fixture-secret-model-key"
    monkeypatch.setenv("MODEL_TEST_KEY", key)
    config.model.api_key = SecretRef(env="MODEL_TEST_KEY")
    config.server.log_level = "debug"
    prepared = prepare(config)
    assert prepared.model_key == key
    assert key not in repr(prepared)

    def backend(request):
        assert request.headers["Authorization"] == f"Bearer {key}"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})
        raise RuntimeError(f"provider failed with credential {key}")

    model = ModelClient(config.model, transport=httpx.MockTransport(backend))
    with caplog.at_level(logging.DEBUG, logger="solakit"):
        with TestClient(create_app(config=config, model=model)) as client:
            auth = {"Authorization": "Bearer operator-test-token"}
            client.post("/api/sessions", headers=auth, json={"id": "test"})
            response = client.post(
                "/api/sessions/test/messages", headers=auth, json={"message": "hello"}
            )
            assert response.json()["status"] == "failed"
            trace = response.json()["diagnostics"][0]["traceback"]
            assert "[redacted]" in trace
            assert key not in trace
    assert "Model completion POST" in caplog.text
    assert key not in caplog.text


def test_missing_model_secret_fails_offline_check_clearly(config, monkeypatch):
    monkeypatch.delenv("MISSING_MODEL_KEY", raising=False)
    config.model.api_key = SecretRef(env="MISSING_MODEL_KEY")
    with pytest.raises(ConfigurationError, match="Model API token unavailable"):
        prepare(config)
    monkeypatch.setenv("MISSING_MODEL_KEY", "invalid\ntoken")
    with pytest.raises(ConfigurationError, match="Model API token unavailable"):
        prepare(config)


def test_default_model_and_server_ports_do_not_conflict():
    assert ModelConfig().base_url == "http://127.0.0.1:8000/v1"
    assert ServerConfig().port == 8382


def test_debug_diagnostics_remove_url_credentials_and_queries(caplog):
    model = ModelClient(
        ModelConfig(), transport=httpx.MockTransport(lambda request: None)
    )
    with caplog.at_level(logging.DEBUG, logger="solakit.core.model"):
        model._failure(
            "preflight GET",
            "models",
            httpx.ConnectError(
                "Cannot connect http://user:private-password@localhost:8000/v1?api_key=query-secret"
            ),
        )
    assert "private-password" not in caplog.text
    assert "query-secret" not in caplog.text
    assert "http://localhost:8000/v1" in caplog.text
    asyncio.run(model.close())
