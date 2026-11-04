import pytest

from solakit.core.config import Config, PathsConfig, SecretRef


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLA_TEST_TOKEN", "operator-test-token")
    config = Config(
        operator_token=SecretRef(env="SOLA_TEST_TOKEN"),
        paths=PathsConfig(
            data=tmp_path / "data",
            soul=tmp_path / "SOUL.md",
            fragments=tmp_path / "fragments",
        ),
    )

    config.paths.soul.write_text("I am Sola.")
    (config.paths.fragments / "profiles").mkdir(parents=True)
    (config.paths.fragments / "profiles/interactive.md").write_text("")
    return config


@pytest.fixture
def mock_model(config):
    import httpx

    from solakit.core.model import ModelClient

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})
        return httpx.Response(
            200,
            json={
                "model": config.model.model_id,
                "choices": [
                    {"message": {"role": "assistant", "content": "Hello back"}}
                ],
            },
        )

    return ModelClient(config.model, transport=httpx.MockTransport(handler))


@pytest.fixture
def auth():
    return {"Authorization": "Bearer operator-test-token"}
