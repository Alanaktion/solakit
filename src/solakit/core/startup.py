"""Offline startup checks shared by the application and operator CLI."""

from dataclasses import dataclass, field

from pydantic import ValidationError

from solakit.core.config import Config, ConfigLoadError, load_config
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
    discord_token: str | None = field(default=None, repr=False)
    model_key: str | None = field(default=None, repr=False)


def prepare(config: Config | None = None) -> Prepared:
    try:
        config = config if config is not None else load_config()
    except ConfigLoadError as exc:
        raise ConfigurationError(str(exc)) from None
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
    model_key = None
    if config.model.api_key:
        try:
            model_key = config.model.resolve_api_key()
        except (OSError, ValueError):
            raise ConfigurationError(
                "Model API token unavailable or invalid; check its secret reference, "
                "printable ASCII value and file mode 0600"
            ) from None
    memory_key = None
    if config.memory.enabled and config.memory.api_key:
        try:
            memory_key = config.memory.api_key.resolve()
        except (OSError, ValueError):
            raise ConfigurationError(
                "Memory secret unavailable; check its env/file reference and mode 0600"
            ) from None
    discord_enabled = any(c.enabled and c.type == "discord" for c in config.channels)
    if (
        discord_enabled or any(j.enabled for j in config.jobs)
    ) and not config.live_ready:
        raise ConfigurationError(
            "Live channels/jobs require live_ready after the memory probe "
            "and acceptance suite"
        )
    discord_token = None
    if discord_enabled:
        if config.discord_token is None:
            raise ConfigurationError(
                "Enabled Discord requires a bot-token secret reference"
            )
        try:
            discord_token = config.discord_token.resolve()
        except (OSError, ValueError):
            raise ConfigurationError(
                "Discord secret unavailable; check its reference"
            ) from None
    return Prepared(config, registry, token, memory_key, discord_token, model_key)
