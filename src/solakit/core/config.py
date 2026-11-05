"""Validated operator configuration; secrets are references, never TOML values."""

import os
import re
import stat
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from string import Formatter
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConfigLoadError(ValueError):
    """A safe operator-facing file/configuration diagnostic."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SecretRef(StrictModel):
    env: str | None = None
    file: Path | None = None

    @model_validator(mode="after")
    def one_source(self) -> Self:
        if bool(self.env) == bool(self.file):
            raise ValueError("secret requires exactly one env or file reference")
        return self

    def resolve(self) -> str:
        if self.env:
            value = os.environ.get(self.env, "")
        else:
            path = self.file.expanduser()
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("secret reference must be a regular file")
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise ValueError("secret file must have mode 0600")
            value = path.read_text().strip()
        if not value:
            raise ValueError("secret reference is empty or unavailable")
        return value


def validate_api_token(value: str) -> str:
    if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("API token must be printable ASCII without whitespace")
    return value


class ModelConfig(StrictModel):
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: SecretRef | None = None
    model_id: str = "OsaurusAI/Qwen3.6-35B-A3B-mxfp4"
    concurrency: int = Field(default=1, ge=1)
    timeout_s: float = Field(default=120, gt=0)
    max_tokens: int = Field(default=4096, ge=1)
    trust_env: bool = False

    def resolve_api_key(self) -> str | None:
        return validate_api_token(self.api_key.resolve()) if self.api_key else None


class ServerConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8382, ge=1, le=65535)
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"


class MemoryConfig(StrictModel):
    enabled: bool = False
    base_url: str | None = None
    api_key: SecretRef | None = None
    roots: list[str] = Field(default_factory=list)
    user: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$", max_length=128)
    timeout_s: float = Field(default=30, gt=0)
    trust_env: bool = False
    recall_limit: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def valid_endpoint(self) -> Self:
        if self.enabled and (not self.base_url or not self.roots):
            raise ValueError("enabled memory requires base_url and canonical URI roots")
        for root in self.roots:
            if (
                not root.startswith("viking://")
                or "~" in root
                or "%" in root
                or any(p in {".", ".."} for p in root.split("/"))
            ):
                raise ValueError("memory roots must be canonical Viking URIs")
        return self


class PathsConfig(StrictModel):
    data: Path = Path("~/sola-data")
    soul: Path = Path("SOUL.md")
    fragments: Path = Path("fragments")


class Profile(StrictModel):
    tools: list[str] = Field(default_factory=list)
    read_scopes: list[str] = Field(default_factory=list)
    write_scopes: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    max_steps: int = Field(default=40, ge=1)
    context_budget: int = Field(default=32000, ge=1)
    mid_run_compaction: bool = False
    tool_timeout_s: float = Field(default=120, gt=0)
    tool_output_max_chars: int = Field(default=20000, ge=32)
    repair_attempts: int = Field(default=2, ge=0)


class DelegationConfig(StrictModel):
    allowed_profiles: list[str] = Field(default_factory=list)
    timeout_s: float = Field(default=900, gt=0, allow_inf_nan=False)


class Channel(StrictModel):
    id: str
    type: Literal["discord", "api"]
    channel_id: str
    session: str
    profile: str = "interactive"
    guild_id: str | None = None
    trigger: Literal["all", "mention"] = "all"
    trigger_authors: list[str] = Field(default_factory=list)
    enabled: bool = False


class Delivery(StrictModel):
    to: str = ""
    mode: Literal["final", "artifact", "silent"] = "final"

    @model_validator(mode="after")
    def destination(self) -> Self:
        if self.mode != "silent" and not self.to:
            raise ValueError("Non-silent delivery requires a destination")
        return self


class Job(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    cron: str | None = None
    run_at: str | None = None
    timezone: str = "America/Denver"
    profile: str
    task: str
    bind_session: str
    deliver: Delivery | None = None
    catch_up: bool = False
    polling: bool = False
    enabled: bool = False

    @model_validator(mode="after")
    def valid_timezone(self) -> Self:
        try:
            zone = ZoneInfo(self.timezone)
        except (KeyError, ValueError) as exc:
            raise ValueError("Invalid job timezone") from exc
        from string import Formatter

        from apscheduler.triggers.cron import CronTrigger

        if (self.cron is None) == (self.run_at is None):
            raise ValueError("Choose exactly one of cron or run_at")
        if self.cron is not None:
            CronTrigger.from_crontab(self.cron, timezone=self.timezone)
        else:
            if "T" not in self.run_at and " " not in self.run_at:
                raise ValueError("run_at requires a date and time")
            instant = datetime.fromisoformat(self.run_at)
            if instant.tzinfo is None:
                candidates = {
                    instant.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                    for fold in (0, 1)
                    if instant.replace(tzinfo=zone, fold=fold)
                    .astimezone(UTC)
                    .astimezone(zone)
                    .replace(tzinfo=None)
                    == instant
                }
                if len(candidates) != 1:
                    raise ValueError(
                        "Ambiguous/nonexistent local time; specify a UTC offset"
                    )
                instant = candidates.pop()
            self.run_at = instant.astimezone(UTC).isoformat()
            if self.catch_up:
                raise ValueError("One-time jobs catch up automatically; omit catch_up")
        if self.catch_up and not self.polling:
            raise ValueError("catch_up is reserved for idempotent polling jobs")
        if not self.task.strip() or len(self.task) > 32000:
            raise ValueError("Job task must contain 1–32000 characters")
        for _, field, spec, conversion in Formatter().parse(self.task):
            if field is not None and (
                field not in {"date", "time", "timezone", "job_id"}
                or spec
                or conversion
            ):
                raise ValueError("Unsupported job task placeholder")
        return self


class CollectionConfig(StrictModel):
    repository: str
    directory: Path
    filename: str = "{date}-{slug}.md"
    layout: str | None = None

    @model_validator(mode="after")
    def safe_layout(self) -> Self:
        if self.directory.is_absolute() or ".." in self.directory.parts:
            raise ValueError("Collection directory must be repository-relative")
        if not self.directory.parts or any(
            part.casefold() == ".git" for part in self.directory.parts
        ):
            raise ValueError("Collection must use a directory below the repository")
        for template, fields in (
            (self.filename, {"date", "slug"}),
            (self.layout, {"date", "slug", "title", "body"}),
        ):
            if template is None:
                continue
            for _, field, spec, conversion in Formatter().parse(template):
                if field is not None and (field not in fields or spec or conversion):
                    raise ValueError("Unsupported collection template placeholder")
        sample = self.filename.format(date="2000-01-01", slug="entry")
        if not sample or Path(sample).name != sample or sample in {".", "..", ".git"}:
            raise ValueError("Collection filename must be one file name")
        return self


class Config(StrictModel):
    operator_token: SecretRef = Field(
        default_factory=lambda: SecretRef(env="SOLA_OPERATOR_TOKEN")
    )
    model: ModelConfig = Field(default_factory=ModelConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    delegation: DelegationConfig = Field(default_factory=DelegationConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    profiles: dict[str, Profile] = Field(
        default_factory=lambda: {"interactive": Profile(max_steps=60)}
    )
    channels: list[Channel] = Field(default_factory=list)
    jobs: list[Job] = Field(default_factory=list)
    repositories: dict[str, Path] = Field(default_factory=dict)
    collections: dict[str, CollectionConfig] = Field(default_factory=dict)
    enabled_tools: list[str] = Field(default_factory=list)
    timezone: str = "America/Denver"
    discord_token: SecretRef | None = None

    @model_validator(mode="after")
    def references(self) -> Self:
        ZoneInfo(self.timezone)
        for profile in self.delegation.allowed_profiles:
            if profile not in self.profiles:
                raise ValueError(f"unknown delegation profile: {profile}")
        for collection in self.collections.values():
            if collection.repository not in self.repositories:
                raise ValueError("Collection references unknown repository")
        for values in (self.channels, self.jobs):
            ids = [item.id for item in values]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate channel or job ID")
            for item in values:
                if item.profile not in self.profiles:
                    raise ValueError(f"unknown profile: {item.profile}")
        pins = [(c.type, c.channel_id) for c in self.channels]
        if len(pins) != len(set(pins)):
            raise ValueError("duplicate channel identity")
        if any(c.type != "api" and c.profile == "maintenance" for c in self.channels):
            raise ValueError("Maintenance is operator-API only")
        if any(j.profile == "maintenance" for j in self.jobs):
            raise ValueError("Maintenance cannot be scheduled")
        channel_ids = {c.id for c in self.channels}
        for job in self.jobs:
            if (
                job.deliver
                and job.deliver.mode != "silent"
                and job.deliver.to not in channel_ids
            ):
                raise ValueError("job delivery references unknown channel")
        return self


def load_config(path: Path | None = None) -> Config:
    """Resolve relative paths against the configuration directory."""
    path = path or Path(os.environ.get("SOLA_CONFIG", "config.toml"))
    try:
        source = path.read_text()
    except UnicodeDecodeError:
        raise ConfigLoadError(f"Configuration file must be UTF-8: {path}") from None
    except FileNotFoundError:
        raise ConfigLoadError(f"Configuration file not found: {path}") from None
    except OSError:
        raise ConfigLoadError(f"Configuration file is not readable: {path}") from None
    try:
        values = tomllib.loads(source)
    except tomllib.TOMLDecodeError as exc:
        position = re.search(
            r"\(at (?:line \d+, column \d+|end of document)\)", str(exc)
        )
        location = f" {position[0]}" if position else ""
        raise ConfigLoadError(f"Invalid TOML in {path}{location}") from None
    config = Config.model_validate(values)
    base = path.resolve().parent
    for name in ("data", "soul", "fragments"):
        value = getattr(config.paths, name).expanduser()
        setattr(config.paths, name, value if value.is_absolute() else base / value)
    config.repositories = {
        name: p.expanduser() if p.expanduser().is_absolute() else base / p
        for name, p in config.repositories.items()
    }
    code_root = Path(__file__).resolve().parents[3]
    if config.paths.data.resolve().is_relative_to(code_root):
        raise ConfigLoadError(
            "Runtime data directory must live outside the code tree: "
            f"{config.paths.data}"
        )
    return config
