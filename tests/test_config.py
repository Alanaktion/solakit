import logging

import pytest
from pydantic import ValidationError

from solakit.core.config import Config, SecretRef, load_config
from solakit.core.secrets import RedactingFilter, Redactor


def test_invalid_configuration(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[model]\nconcurrency = 0\n")
    with pytest.raises(ValidationError):
        load_config(path)
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "channels": [
                    {
                        "id": "x",
                        "type": "api",
                        "channel_id": "x",
                        "session": "x",
                        "profile": "missing",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        Config.model_validate({"typo": True})


def test_profile_context_budget_can_be_configured(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[profiles.interactive]\ncontext_budget = 196608\n")

    config = load_config(path)

    assert config.profiles["interactive"].context_budget == 196608


def test_secret_references(tmp_path, monkeypatch):
    with pytest.raises(ValidationError):
        SecretRef(env="X", file=tmp_path / "x")
    monkeypatch.delenv("SOLA_MISSING", raising=False)
    with pytest.raises(ValueError):
        SecretRef(env="SOLA_MISSING").resolve()
    path = tmp_path / "token"
    path.write_text("secret\n")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        SecretRef(file=path).resolve()
    path.chmod(0o600)
    assert SecretRef(file=path).resolve() == "secret"


def test_redaction():
    redactor = Redactor(["actual-secret"])
    assert redactor.redact({"API_KEY": "x", "nested": ["actual-secret"]}) == {
        "API_KEY": "[redacted]",
        "nested": ["[redacted]"],
    }
    record = logging.LogRecord("test", 20, "", 1, "value %s", ("actual-secret",), None)
    RedactingFilter(redactor).filter(record)
    assert record.getMessage() == "value [redacted]"


def test_prompt_files_checked_before_startup_resources(config, monkeypatch):
    from fastapi.testclient import TestClient

    from solakit.core.config import Profile
    from solakit.core.startup import ConfigurationError
    from solakit.main import create_app

    config.profiles["blog"] = Profile()

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "startup must validate prompt files before allocating services"
        )

    monkeypatch.setattr("solakit.main.Store", forbidden)
    monkeypatch.setattr("solakit.main.ModelClient", forbidden)
    with pytest.raises(ConfigurationError, match=r"profiles/blog\.md"):
        with TestClient(create_app(config=config)):
            pass
    assert not config.paths.data.exists()


@pytest.mark.parametrize("which", ["soul", "profile", "context", "recall_header"])
def test_prompt_inputs_must_be_readable_utf8(config, which):
    from solakit.core.prompt import validate_prompt_files

    if which == "soul":
        path = config.paths.soul
    elif which == "profile":
        path = config.paths.fragments / "profiles/interactive.md"
    elif which == "context":
        path = config.paths.fragments / "context.md"
    else:
        from solakit.core.config import MemoryConfig

        config.memory = MemoryConfig(
            enabled=True,
            base_url="http://memory",
            roots=["viking://user/sola/memories"],
        )
        path = config.paths.fragments / "recall-header.md"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match="readable UTF-8"):
        validate_prompt_files(config)


def test_startup_diagnostics_do_not_echo_invalid_secret_values(tmp_path, monkeypatch):
    from solakit.core.startup import ConfigurationError, prepare

    path = tmp_path / "invalid.toml"
    path.write_text('[operator_token]\nvalue = "must-not-echo-this-secret"\n')
    monkeypatch.setenv("SOLA_CONFIG", str(path))
    with pytest.raises(ConfigurationError) as error:
        prepare()
    assert "operator_token.value" in str(error.value)
    assert "must-not-echo-this-secret" not in str(error.value)


def test_secret_key_suffix_matching_is_intentional():
    assert Redactor().redact({"monkey": "value", "key_count": 2}) == {
        "monkey": "[redacted]",
        "key_count": 2,
    }


def test_config_load_errors_identify_path_and_position_without_contents(
    tmp_path, monkeypatch
):
    from solakit.core.startup import ConfigurationError, prepare

    path = tmp_path / "missing.toml"
    monkeypatch.setenv("SOLA_CONFIG", str(path))
    with pytest.raises(ConfigurationError, match="file not found") as error:
        prepare()
    assert str(path) in str(error.value)
    path.write_text('secret = "DO_NOT_ECHO" invalid')
    with pytest.raises(ConfigurationError, match="Invalid TOML") as error:
        prepare()
    assert str(path) in str(error.value)
    assert "line 1, column" in str(error.value)
    assert "DO_NOT_ECHO" not in str(error.value)
