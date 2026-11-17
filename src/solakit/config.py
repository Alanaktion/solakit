"""Strict operator TOML and immutable, traceable authority snapshots."""

import tomllib
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator

from solakit.records import Origin, SchemaVersion, SessionMode, Value

Count = Annotated[int, Field(ge=1, le=256)]
Seconds = Annotated[int, Field(ge=1, le=86400)]
Name = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$", max_length=128)]
ToolGrant = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_*?][a-zA-Z0-9_.*?-]*$", max_length=128)]
MemoryMode = Literal["off", "optional", "required"]
DEFAULT_CONFIG = Path.home() / ".config/sola/config.toml"


class ConfigError(ValueError):
    """Safe for operator-facing output: never includes raw input values."""


class Budgets(Value):
    model_calls: Count = 16
    tool_calls: Count = 32
    active_seconds: Seconds = 900
    output_tokens: Annotated[int, Field(ge=1, le=32768)] = 4096
    tool_result_bytes: Annotated[int, Field(ge=1, le=1048576)] = 65536
    tool_timeout_seconds: Annotated[int, Field(ge=1, le=3600)] = 60


class BudgetOverrides(Value):
    model_calls: Count | None = None
    tool_calls: Count | None = None
    active_seconds: Seconds | None = None
    output_tokens: Annotated[int, Field(ge=1, le=32768)] | None = None
    tool_result_bytes: Annotated[int, Field(ge=1, le=1048576)] | None = None
    tool_timeout_seconds: Annotated[int, Field(ge=1, le=3600)] | None = None


class ProfileSettings(Value):
    extends: Name = "default"
    workspace_roots: list[str] | None = None
    tools: list[ToolGrant] | None = None
    memory_mode: MemoryMode | None = None
    fragments: list[str] | None = None
    budgets: BudgetOverrides = Field(default_factory=BudgetOverrides)


class ModelSettings(Value):
    pin: str = ""
    base_url: str = "http://127.0.0.1:8000/v1"
    context_tokens: Annotated[int, Field(ge=1024, le=1048576)] = 32768
    connect_timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 5
    generation_timeout_seconds: Annotated[int, Field(ge=1, le=3600)] = 120
    api_key_env: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] | None = Field(
        default=None, repr=False
    )

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        try:
            url = urlsplit(value)
            port = url.port
        except ValueError:
            raise ValueError("invalid model endpoint") from None
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or port == 0
        ):
            raise ValueError("model endpoint must not contain credentials, query, or fragment")
        return value


class MemorySettings(Value):
    enabled: bool = False
    base_url: str = "http://127.0.0.1:1933"
    account: Name = "default"
    user: Name = "default"
    roots: tuple[str, ...] = ()
    api_key_env: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] | None = Field(
        default=None, repr=False
    )
    timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 15
    backlog_cap: Annotated[int, Field(ge=1, le=10000)] = 128

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        return ModelSettings.endpoint(value)

    @field_validator("roots", mode="before")
    @classmethod
    def root_sequence(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("roots")
    @classmethod
    def canonical_roots(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        import re

        roots = tuple(dict.fromkeys(value.rstrip("/") for value in values))
        if any(
            not re.fullmatch(r"viking://user/[A-Za-z0-9_][A-Za-z0-9_.-]*/memories", r)
            for r in roots
        ):
            raise ValueError("memory roots must be canonical managed user memory roots")
        return roots


class OperatorConfig(Value):
    schema_version: SchemaVersion = 1
    data_dir: str = "~/.local/share/sola"
    soul_file: str | None = None
    curated_memory_file: str | None = None
    shutdown_timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 5
    model: ModelSettings = Field(default_factory=ModelSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    tool_modules: list[Name] = Field(default_factory=list)
    loaded_tools: list[Name] = Field(default_factory=list)
    defaults: ProfileSettings = Field(default_factory=ProfileSettings)
    profiles: dict[Name, ProfileSettings] = Field(default_factory=dict)
    proactive_budgets: BudgetOverrides = Field(default_factory=BudgetOverrides)


class EffectiveProfile(Value):
    name: str
    origin: Origin
    mode: SessionMode
    data_dir: Path
    model_pin: str
    model_base_url: str
    model_context_tokens: int = 32768
    model_connect_timeout_seconds: int = 5
    model_generation_timeout_seconds: int = 120
    soul_file: Path | None = None
    curated_memory_file: Path | None = None
    model_api_key_env: str | None = Field(repr=False)
    shutdown_timeout_seconds: int
    workspace_roots: tuple[Path, ...]
    memory: MemorySettings = Field(default_factory=MemorySettings)
    tool_modules: tuple[str, ...] = ()
    loaded_tools: frozenset[str]
    granted_tools: frozenset[str]
    tools: frozenset[str]
    memory_mode: MemoryMode
    fragments: tuple[Path, ...]
    budgets: Budgets
    sources: tuple[tuple[str, str], ...]

    def explain(self) -> dict[str, object]:
        values = self.model_dump(mode="json", exclude={"sources", "model_api_key_env"})
        for key in ("loaded_tools", "granted_tools", "tools"):
            values[key] = sorted(values[key])
        values["model_api_key_env"] = "<redacted>" if self.model_api_key_env else None
        values["memory"]["api_key_env"] = "<redacted>" if self.memory.api_key_env else None
        return {"values": values, "sources": dict(self.sources)}


def load_config(path: Path | None = None) -> OperatorConfig:
    path = path or DEFAULT_CONFIG
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        if path == DEFAULT_CONFIG:
            return OperatorConfig()
        raise ConfigError("configuration file does not exist") from None
    except OSError, ValueError:
        raise ConfigError("configuration file cannot be read as TOML") from None
    try:
        return OperatorConfig.model_validate(raw)
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
        raise ConfigError("invalid configuration fields: " + ", ".join(fields)) from None


def _paths(values: list[str], base: Path, *, directories: bool) -> tuple[Path, ...]:
    paths = []
    for value in values:
        if not value.strip():
            raise ConfigError("paths must not be empty")
        try:
            path = Path(value).expanduser()
            path = (base / path).resolve(strict=True)
            valid = path.is_dir() if directories else path.is_file()
        except OSError, RuntimeError, ValueError:
            valid = False
        if not valid:
            raise ConfigError("workspace roots/fragments must exist with the expected type")
        if path not in paths:
            paths.append(path)
    return tuple(paths)


def resolve_profile(
    config: OperatorConfig,
    name: str = "default",
    *,
    base: Path | None = None,
    origin: Origin = Origin.INTERACTIVE,
    mode: SessionMode = SessionMode.SHARED,
) -> EffectiveProfile:
    """Profiles inherit defaults and may only narrow authority and budgets."""
    base = (base or Path.cwd()).resolve()
    if config.memory.enabled and config.memory.roots != (
        f"viking://user/{config.memory.user}/memories",
    ):
        raise ConfigError("enabled memory requires exactly its selected user's managed root")
    chain: list[tuple[str, ProfileSettings]] = []
    seen: set[str] = set()
    parent = name
    while parent != "default":
        if parent in seen or parent not in config.profiles:
            raise ConfigError("profile inheritance has a cycle or unknown parent")
        seen.add(parent)
        settings = config.profiles[parent]
        chain.append((f"profiles.{parent}", settings))
        parent = settings.extends
    if "default" in config.profiles or config.defaults.extends != "default":
        raise ConfigError("default is reserved; defaults cannot extend another profile")
    sources = {key: "builtin" for key in EffectiveProfile.model_fields if key != "sources"}
    budgets = Budgets()
    for key in Budgets.model_fields:
        sources[f"budgets.{key}"] = "builtin"
    roots: tuple[Path, ...] = ()
    grants: frozenset[str] = frozenset()
    memory: MemoryMode = "optional"
    fragments: tuple[Path, ...] = ()
    for source, settings in [("defaults", config.defaults), *reversed(chain)]:
        child = source != "defaults"
        if settings.workspace_roots is not None:
            selected = _paths(settings.workspace_roots, base, directories=True)
            if child and any(not any(p.is_relative_to(root) for root in roots) for p in selected):
                raise ConfigError("profile workspace roots exceed inherited authority")
            roots = selected
            sources["workspace_roots"] = source + ".workspace_roots"
        if settings.tools is not None:
            selected_tools = frozenset(
                name
                for pattern in settings.tools
                for name in config.loaded_tools
                if fnmatchcase(name, pattern)
            ) | frozenset(
                pattern for pattern in settings.tools if not any(c in pattern for c in "*?")
            )
            if child and not selected_tools <= grants:
                raise ConfigError("profile tools exceed inherited authority")
            grants = selected_tools
            sources["granted_tools"] = source + ".tools"
        if settings.memory_mode is not None:
            if child and memory == "off" and settings.memory_mode != "off":
                raise ConfigError("profile cannot enable inherited disabled memory")
            memory = settings.memory_mode
            sources["memory_mode"] = source + ".memory_mode"
        if settings.fragments is not None:
            fragments = _paths(settings.fragments, base, directories=False)
            sources["fragments"] = source + ".fragments"
        changes = settings.budgets.model_dump(exclude_none=True)
        if child and any(value > getattr(budgets, key) for key, value in changes.items()):
            raise ConfigError("profile budgets exceed inherited limits")
        budgets = Budgets.model_validate(budgets.model_dump() | changes)
        sources.update({f"budgets.{key}": source + f".budgets.{key}" for key in changes})
    if not roots:
        raise ConfigError("runnable profiles require at least one existing workspace root")
    if not config.model.pin.strip():
        raise ConfigError("runnable profiles require an explicit model pin")
    if mode != SessionMode.SHARED:
        memory = "off"
        sources["memory_mode"] = f"session mode: {mode}"
    if origin == Origin.PROACTIVE:
        changes = {"model_calls": 8, "tool_calls": 12, "active_seconds": 600}
        changes.update(config.proactive_budgets.model_dump(exclude_none=True))
        values = {key: min(getattr(budgets, key), value) for key, value in changes.items()}
        for key, value in values.items():
            if value < getattr(budgets, key):
                sources[f"budgets.{key}"] = (
                    "proactive_budgets." + key
                    if key in config.proactive_budgets.model_fields_set
                    else "builtin proactive ceiling: " + key
                )
        budgets = Budgets.model_validate(budgets.model_dump() | values)
    try:
        data_dir = (base / Path(config.data_dir).expanduser()).resolve()
    except OSError, RuntimeError, ValueError:
        raise ConfigError("invalid data directory") from None
    if not config.data_dir.strip():
        raise ConfigError("data directory must not be empty")
    if data_dir.exists() and not data_dir.is_dir():
        raise ConfigError("data directory must be a directory")
    for field, setting in (
        ("data_dir", "data_dir"),
        ("model_pin", "model.pin"),
        ("model_base_url", "model.base_url"),
        ("model_api_key_env", "model.api_key_env"),
        ("model_context_tokens", "model.context_tokens"),
        ("model_connect_timeout_seconds", "model.connect_timeout_seconds"),
        ("model_generation_timeout_seconds", "model.generation_timeout_seconds"),
        ("soul_file", "soul_file"),
        ("curated_memory_file", "curated_memory_file"),
        ("memory", "memory"),
        ("loaded_tools", "loaded_tools"),
        ("tool_modules", "tool_modules"),
        ("shutdown_timeout_seconds", "shutdown_timeout_seconds"),
    ):
        top, _, nested = setting.partition(".")
        explicit = top in config.model_fields_set
        if nested:
            explicit = nested in config.model.model_fields_set
        sources[field] = setting if explicit else "builtin"
    sources.update(
        name="selection", origin="admission", mode="admission", tools="loaded_tools ∩ granted_tools"
    )
    sources.pop("budgets")
    return EffectiveProfile(
        name=name,
        origin=origin,
        mode=mode,
        data_dir=data_dir,
        model_pin=config.model.pin,
        model_base_url=config.model.base_url,
        model_context_tokens=config.model.context_tokens,
        model_connect_timeout_seconds=config.model.connect_timeout_seconds,
        model_generation_timeout_seconds=config.model.generation_timeout_seconds,
        soul_file=_paths([config.soul_file], base, directories=False)[0]
        if config.soul_file is not None
        else None,
        curated_memory_file=_paths([config.curated_memory_file], base, directories=False)[0]
        if config.curated_memory_file is not None
        else None,
        model_api_key_env=config.model.api_key_env,
        shutdown_timeout_seconds=config.shutdown_timeout_seconds,
        workspace_roots=roots,
        memory=config.memory,
        tool_modules=tuple(config.tool_modules),
        loaded_tools=frozenset(config.loaded_tools),
        granted_tools=grants,
        tools=grants & frozenset(config.loaded_tools),
        memory_mode=memory,
        fragments=fragments,
        budgets=budgets,
        sources=tuple(sorted(sources.items())),
    )


def restrict_to_admission(
    current: EffectiveProfile, admitted: EffectiveProfile
) -> EffectiveProfile:
    """Current revocations apply; new grants cannot widen an admitted run."""
    if (current.model_pin, current.model_base_url, current.data_dir) != (
        admitted.model_pin,
        admitted.model_base_url,
        admitted.data_dir,
    ):
        raise ConfigError("runtime/model identity changed; a new admission is required")
    if current.memory != admitted.memory:
        raise ConfigError("memory service/scope changed; a new admission is required")
    roots = tuple(
        dict.fromkeys(
            inner
            for a in admitted.workspace_roots
            for b in current.workspace_roots
            for inner in (a, b)
            if inner.is_relative_to(a) and inner.is_relative_to(b)
        )
    )
    changes = {
        "workspace_roots": roots,
        "loaded_tools": current.loaded_tools & admitted.loaded_tools,
        "granted_tools": current.granted_tools & admitted.granted_tools,
        "tools": (
            current.tools
            & admitted.tools
            & current.loaded_tools
            & admitted.loaded_tools
            & current.granted_tools
            & admitted.granted_tools
        ),
        "memory_mode": "off"
        if "off" in (current.memory_mode, admitted.memory_mode)
        else admitted.memory_mode,
        "budgets": Budgets(
            **{
                key: min(getattr(current.budgets, key), getattr(admitted.budgets, key))
                for key in Budgets.model_fields
            }
        ),
    }
    sources = dict(admitted.sources)
    for key in changes:
        if key == "budgets":
            for budget in Budgets.model_fields:
                sources[f"budgets.{budget}"] = "admission ∩ current grants"
        else:
            sources[key] = "admission ∩ current grants"
    return EffectiveProfile.model_validate(
        admitted.model_dump() | changes | {"sources": tuple(sorted(sources.items()))}
    )
