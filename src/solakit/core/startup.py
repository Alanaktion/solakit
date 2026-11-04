"""Offline startup checks shared by the application and operator CLI."""

from dataclasses import dataclass, field

from pydantic import ValidationError

from solakit.core.config import Config, load_config
from solakit.core.prompt import validate_prompt_files
from solakit.tools import Registry


class ConfigurationError(ValueError):
    """An actionable configuration error without echoed secret input values."""


@dataclass(frozen=True)
class Prepared:
    config: Config
    registry: Registry
    operator_token: str = field(repr=False)
    memory_key: str | None = field(default=None, repr=False)


def prepare(config: Config | None = None) -> Prepared:
    try:
        config = config if config is not None else load_config()
    except ValidationError as exc:
        fields = [
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['type']}"
            for issue in exc.errors(include_input=False, include_url=False)
        ]
        raise ConfigurationError(
            "Invalid configuration: " + "; ".join(fields)
        ) from None
    except (OSError, ValueError):
        raise ConfigurationError(
            "Cannot load configuration; check its path, TOML syntax and data directory"
        ) from None
    except KeyError:
        raise ConfigurationError("Invalid timezone in configuration") from None
    try:
        validate_prompt_files(config)
        registry = Registry(config)
    except (OSError, ValueError, ImportError) as exc:
        raise ConfigurationError(str(exc)) from None
    try:
        token = config.operator_token.resolve()
    except (OSError, ValueError):
        raise ConfigurationError(
            "Operator secret unavailable; check its env/file reference and mode 0600"
        ) from None
    memory_key = None
    if config.memory.enabled and config.memory.api_key:
        try:
            memory_key = config.memory.api_key.resolve()
        except (OSError, ValueError):
            raise ConfigurationError(
                "Memory secret unavailable; check its env/file reference and mode 0600"
            ) from None
    return Prepared(config, registry, token, memory_key)
