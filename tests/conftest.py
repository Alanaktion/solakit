import pytest

from solakit.core.config import Config, PathsConfig, SecretRef


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLA_TEST_TOKEN", "operator-test-token")
    return Config(
        operator_token=SecretRef(env="SOLA_TEST_TOKEN"),
        paths=PathsConfig(
            data=tmp_path / "data",
            soul=tmp_path / "SOUL.md",
            fragments=tmp_path / "fragments",
        ),
    )


@pytest.fixture
def auth():
    return {"Authorization": "Bearer operator-test-token"}
