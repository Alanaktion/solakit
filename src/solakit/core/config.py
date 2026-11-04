"""Validated operator configuration; secrets are references, never TOML values."""

import os
import stat
import tomllib
from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class ModelConfig(StrictModel):
    base_url: str = "http://127.0.0.1:8001/v1"
    model_id: str = "OsaurusAI/Qwen3.6-35B-A3B-mxfp4"
    concurrency: int = Field(default=1, ge=1)
    timeout_s: float = Field(default=120, gt=0)


class MemoryConfig(StrictModel):
    enabled: bool = False
    base_url: str | None = None
    api_key: SecretRef | None = None
    roots: list[str] = Field(default_factory=list)
    timeout_s: float = Field(default=30, gt=0)
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
    tool_timeout_s: float = Field(default=120, gt=0)
    tool_output_max_chars: int = Field(default=20000, ge=32)
    repair_attempts: int = Field(default=2, ge=0)


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
    to: str
    mode: Literal["final", "artifact", "silent"] = "final"


class Job(StrictModel):
    id: str
    cron: str
    timezone: str = "America/Denver"
    profile: str
    task: str
    bind_session: str
    deliver: Delivery | None = None
    catch_up: bool = False
    enabled: bool = False

    @model_validator(mode="after")
    def valid_timezone(self) -> Self:
        ZoneInfo(self.timezone)
        if len(self.cron.split()) != 5:
            raise ValueError("cron requires five fields")
        return self


class Config(StrictModel):
    operator_token: SecretRef = Field(
        default_factory=lambda: SecretRef(env="SOLA_OPERATOR_TOKEN")
    )
    model: ModelConfig = Field(default_factory=ModelConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    profiles: dict[str, Profile] = Field(
        default_factory=lambda: {"interactive": Profile(max_steps=60)}
    )
    channels: list[Channel] = Field(default_factory=list)
    jobs: list[Job] = Field(default_factory=list)
    repositories: dict[str, Path] = Field(default_factory=dict)
    enabled_tools: list[str] = Field(default_factory=list)
    timezone: str = "America/Denver"

    @model_validator(mode="after")
    def references(self) -> Self:
        ZoneInfo(self.timezone)
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
        channel_ids = {c.id for c in self.channels}
        for job in self.jobs:
            if job.deliver and job.deliver.to not in channel_ids:
                raise ValueError("job delivery references unknown channel")
        return self


def load_config(path: Path | None = None) -> Config:
    """Resolve relative paths against the configuration directory."""
    path = path or Path(os.environ.get("SOLA_CONFIG", "config.toml"))
    config = Config.model_validate(tomllib.loads(path.read_text()))
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
        raise ValueError("runtime data must live outside the code tree")
    return config
