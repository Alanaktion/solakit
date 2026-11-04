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
