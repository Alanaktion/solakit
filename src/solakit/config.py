"""Strict operator TOML and immutable, traceable authority snapshots."""

import tomllib
from contextlib import suppress
from fnmatch import fnmatchcase
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, field_validator, model_validator

from solakit.records import Origin, SchemaVersion, SessionMode, Value
from solakit.registry import RegistryError, load_registry

Count = Annotated[int, Field(ge=1, le=256)]
Seconds = Annotated[int, Field(ge=1, le=86400)]
Name = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$", max_length=128)]
ToolGrant = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_*?][a-zA-Z0-9_.*?-]*$", max_length=128)]
AgentName = Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]
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


class ModelEndpoint(Value):
    pin: str
    base_url: str
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


class ModelSettings(ModelEndpoint):
    pin: str = ""
    base_url: str = "http://127.0.0.1:8000/v1"
    image_inputs: bool = False
    context_tokens: Annotated[int, Field(ge=1024, le=1048576)] = 32768


class VisionSettings(ModelEndpoint):
    pin: Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]


class ImageSettings(Value):
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8188"
    timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 15
    poll_seconds: Annotated[int, Field(ge=1, le=60)] = 2
    max_checks: Annotated[int, Field(ge=1, le=600)] = 300

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        return ModelSettings.endpoint(value).rstrip("/")


class CheckoutSettings(Value):
    repository: str | None = None


class GitRepository(Value):
    path: Annotated[Path, Field(strict=False)]
    branch: Annotated[str, Field(min_length=1, max_length=255)]

    @field_validator("path", mode="before")
    @classmethod
    def nonempty_path(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("checkout path must not be empty")
        return value


LetterAuthor = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
LetterBranch = Annotated[str, Field(min_length=1, max_length=255)]


class LettersSettings(CheckoutSettings):
    branch: LetterBranch | None = None
    directory: str = "."
    author: LetterAuthor | None = None
    peer: LetterAuthor | None = None

    @field_validator("directory")
    @classmethod
    def relative_directory(cls, value: str) -> str:
        path = Path(value)
        if (
            not value
            or "\0" in value
            or path.is_absolute()
            or any(part == ".." or part.casefold() == ".git" for part in path.parts)
        ):
            raise ValueError("letters.directory must be repository-relative")
        return path.as_posix()

    @model_validator(mode="after")
    def distinct_authors(self) -> LettersSettings:
        if self.author is not None and self.author == self.peer:
            raise ValueError("letters authors must be distinct")
        return self


class LettersCheckout(GitRepository):
    directory: str
    author: LetterAuthor
    peer: LetterAuthor


class GitSettings(Value):
    repositories: dict[Name, GitRepository] = Field(default_factory=dict)


GIT_READ = frozenset({"git.status", "git.diff"})
GIT_WRITE = frozenset({"git.pull", "git.publish"})
LETTERS_READ = frozenset({"letters.inbox", "letters.read"})
LETTERS_WRITE = frozenset({"letters.send", "letters.reply"})
LETTERS_TOOLS = LETTERS_READ | LETTERS_WRITE
CONTENT_WRITE = frozenset({"blog.create", "blog.publish", "dreams.create"})


def same_path(path: Path, other: Path) -> bool:
    if path == other:
        return True
    with suppress(FileNotFoundError):
        return path.samefile(other)
    return False


def checkout_reserved(path: Path, reserved: frozenset[Path]) -> bool:
    return any(same_path(path, other) for other in reserved)


def _git_tools(
    repositories: dict[str, GitRepository], reserved: frozenset[Path], origin: Origin
) -> frozenset[str]:
    tools = GIT_READ if repositories else frozenset()
    if origin == Origin.INTERACTIVE and any(
        not checkout_reserved(repo.path, reserved) for repo in repositories.values()
    ):
        tools |= GIT_WRITE
    return tools


def letters_tools(
    checkout: LettersCheckout | None, roots: tuple[Path, ...], origin: Origin
) -> frozenset[str]:
    if checkout is None or not any(checkout.path.is_relative_to(root) for root in roots):
        return frozenset()
    if origin == Origin.INTERACTIVE:
        return LETTERS_TOOLS
    return LETTERS_READ if origin == Origin.SCHEDULED else frozenset()


class WebSearchSettings(Value):
    api_key_env: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] | None = Field(
        default=None, repr=False
    )


class ShellSettings(Value):
    enabled: bool = False


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


class BackgroundSettings(Value):
    timezone: str = "America/Denver"
    quiet_start: Annotated[int, Field(ge=0, le=23)] = 22
    quiet_end: Annotated[int, Field(ge=0, le=23)] = 8
    topic_cooldown_seconds: Annotated[int, Field(ge=1, le=86400)] = 3600
    hourly_runs: Count = 4
    daily_runs: Annotated[int, Field(ge=1, le=10000)] = 24
    max_active: Count = 1
    max_pending: Count = 8
    notification_ttl_seconds: Seconds = 86400

    @field_validator("timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ValueError, ZoneInfoNotFoundError:
            raise ValueError("background timezone must be an IANA timezone") from None
        return value


class HTTPSettings(Value):
    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 8765
    allowed_origins: tuple[str, ...] = ("http://127.0.0.1:8765", "http://localhost:8765")
    secure_cookie: bool = False
    password_env: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] = Field(
        default="SOLA_OPERATOR_PASSWORD", repr=False
    )
    session_seconds: Annotated[int, Field(ge=60, le=2592000)] = 86400

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def origins(cls, values: object) -> tuple[str, ...]:
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError("explicit allowed origins are required")
        for value in values:
            if not isinstance(value, str):
                raise ValueError("invalid origin")
            ModelSettings.endpoint(value)
            url = urlsplit(value)
            if url.path or url.netloc != url.netloc.lower():
                raise ValueError("origins must be canonical scheme and authority only")
        return tuple(dict.fromkeys(values))

    @model_validator(mode="after")
    def transport(self) -> HTTPSettings:
        direct_http_networks = tuple(
            ip_network(cidr)
            for cidr in (
                "10.0.0.0/8",
                "172.16.0.0/12",
                "192.168.0.0/16",
                "100.64.0.0/10",
                "fc00::/7",
            )
        )
        for origin in self.allowed_origins:
            url = urlsplit(origin)
            try:
                address = ip_address(url.hostname or "")
                local = address.is_loopback or any(
                    address in network for network in direct_http_networks
                )
            except ValueError:
                local = url.hostname == "localhost"
            if not local and (url.scheme != "https" or not self.secure_cookie):
                raise ValueError(
                    "origins outside loopback, private LAN, and Tailnet IP ranges "
                    "require HTTPS and secure cookies"
                )
        return self


class OperatorConfig(Value):
    schema_version: SchemaVersion = 1
    agent_name: AgentName = "Sola"
    data_dir: str = "~/.local/share/sola"
    soul_file: str | None = None
    curated_memory_file: str | None = None
    shutdown_timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 5
    model: ModelSettings = Field(default_factory=ModelSettings)
    vision: VisionSettings | None = None
    memory: MemorySettings = Field(default_factory=MemorySettings)
    images: ImageSettings = Field(default_factory=ImageSettings)
    blog: CheckoutSettings = Field(default_factory=CheckoutSettings)
    git: GitSettings = Field(default_factory=GitSettings)
    letters: LettersSettings = Field(default_factory=LettersSettings)
    web_search: WebSearchSettings = Field(default_factory=WebSearchSettings)
    shell: ShellSettings = Field(default_factory=ShellSettings)
    tool_modules: list[Name] = Field(default_factory=list)
    defaults: ProfileSettings = Field(default_factory=ProfileSettings)
    profiles: dict[Name, ProfileSettings] = Field(default_factory=dict)
    proactive_budgets: BudgetOverrides = Field(default_factory=BudgetOverrides)
    background: BackgroundSettings = Field(default_factory=BackgroundSettings)
    http: HTTPSettings = Field(default_factory=HTTPSettings)

    @model_validator(mode="after")
    def image_route(self) -> OperatorConfig:
        if self.vision is not None and self.model.image_inputs:
            raise ValueError("vision requires model.image_inputs = false")
        return self


class EffectiveProfile(Value):
    name: str
    agent_name: AgentName = "Sola"
    origin: Origin
    mode: SessionMode
    data_dir: Path
    model_pin: str
    model_image_inputs: bool = False
    vision: VisionSettings | None = None
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
    images: ImageSettings = Field(default_factory=ImageSettings)
    blog_repository: Path | None = None
    git_repositories: dict[str, GitRepository] = Field(default_factory=dict)
    git_reserved: frozenset[Path] = Field(default_factory=frozenset)
    letters: LettersCheckout | None = None
    web_search: WebSearchSettings = Field(default_factory=WebSearchSettings)
    shell: ShellSettings = Field(default_factory=ShellSettings)
    shell_granted: bool = False
    background: BackgroundSettings = Field(default_factory=BackgroundSettings)
    http: HTTPSettings = Field(default_factory=HTTPSettings)
    proactive_budgets: BudgetOverrides = Field(default_factory=BudgetOverrides)
    tool_modules: tuple[str, ...] = ()
    # Read old admissions without exposing or applying their redundant loading field.
    legacy_loaded_tools: frozenset[str] = Field(
        default_factory=frozenset, validation_alias="loaded_tools", exclude=True, repr=False
    )
    granted_tools: frozenset[str]
    tools: frozenset[str]
    memory_mode: MemoryMode
    fragments: tuple[Path, ...]
    budgets: Budgets
    sources: tuple[tuple[str, str], ...]

    def explain(self) -> dict[str, object]:
        values = self.model_dump(mode="json", exclude={"sources", "model_api_key_env"})
        for key in ("granted_tools", "tools"):
            values[key] = sorted(values[key])
        values["model_api_key_env"] = "<redacted>" if self.model_api_key_env else None
        if self.vision and self.vision.api_key_env:
            values["vision"]["api_key_env"] = "<redacted>"
        values["memory"]["api_key_env"] = "<redacted>" if self.memory.api_key_env else None
        values["web_search"]["api_key_env"] = "<redacted>" if self.web_search.api_key_env else None
        values["http"]["password_env"] = "<redacted>"
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
    sources = {
        key: "builtin"
        for key in EffectiveProfile.model_fields
        if key not in ("sources", "legacy_loaded_tools")
    }
    budgets = Budgets()
    for key in Budgets.model_fields:
        sources[f"budgets.{key}"] = "builtin"
    roots: tuple[Path, ...] = ()
    grants: frozenset[str] = frozenset()
    shell_granted = False
    memory: MemoryMode = "optional"
    fragments: tuple[Path, ...] = ()
    try:
        registered = frozenset(load_registry(config.tool_modules).tools)
    except RegistryError as error:
        raise ConfigError(str(error)) from None
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
                for name in registered
                if fnmatchcase(name, pattern)
            ) | frozenset(
                pattern for pattern in settings.tools if not any(c in pattern for c in "*?")
            )
            if child and not selected_tools <= grants:
                raise ConfigError("profile tools exceed inherited authority")
            shell_granted = "shell.exec" in settings.tools and (not child or shell_granted)
            grants = selected_tools
            sources["granted_tools"] = source + ".tools"
            sources["shell_granted"] = source + ".tools"
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
    if origin in (Origin.SCHEDULED, Origin.PROACTIVE):
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
        ("agent_name", "agent_name"),
        ("data_dir", "data_dir"),
        ("model_pin", "model.pin"),
        ("model_image_inputs", "model.image_inputs"),
        ("vision", "vision"),
        ("model_base_url", "model.base_url"),
        ("model_api_key_env", "model.api_key_env"),
        ("model_context_tokens", "model.context_tokens"),
        ("model_connect_timeout_seconds", "model.connect_timeout_seconds"),
        ("model_generation_timeout_seconds", "model.generation_timeout_seconds"),
        ("soul_file", "soul_file"),
        ("curated_memory_file", "curated_memory_file"),
        ("memory", "memory"),
        ("images", "images"),
        ("blog_repository", "blog"),
        ("git_repositories", "git"),
        ("git_reserved", "letters"),
        ("letters", "letters"),
        ("web_search", "web_search"),
        ("shell", "shell"),
        ("background", "background"),
        ("http", "http"),
        ("proactive_budgets", "proactive_budgets"),
        ("tool_modules", "tool_modules"),
        ("shutdown_timeout_seconds", "shutdown_timeout_seconds"),
    ):
        top, _, nested = setting.partition(".")
        explicit = top in config.model_fields_set
        if nested:
            explicit = nested in config.model.model_fields_set
        sources[field] = setting if explicit else "builtin"
    sources.update(
        name="selection",
        origin="admission",
        mode="admission",
        tools="registered tools ∩ granted_tools",
    )
    sources.pop("budgets")
    tools = grants & registered
    repositories = {}
    for alias, repo in config.git.repositories.items():
        root = _paths([str(repo.path)], base, directories=True)[0]
        if any(root.is_relative_to(parent) for parent in roots):
            repositories[alias] = GitRepository(path=root, branch=repo.branch)
    letter_settings = config.letters
    reserved = frozenset(
        _paths([letter_settings.repository], base, directories=True)
        if letter_settings.repository is not None
        else ()
    )
    letters = None
    if reserved and letter_settings.branch and letter_settings.author and letter_settings.peer:
        checkout = next(iter(reserved))
        directory = checkout
        for part in Path(letter_settings.directory).parts:
            directory /= part
            if directory.is_symlink():
                raise ConfigError("letters.directory cannot contain symlinked components")
        letters = LettersCheckout(
            path=checkout,
            branch=letter_settings.branch,
            directory=letter_settings.directory,
            author=letter_settings.author,
            peer=letter_settings.peer,
        )
    blog = (
        _paths([config.blog.repository], base, directories=True)[0]
        if (config.blog.repository is not None)
        else None
    )
    if blog is not None and checkout_reserved(blog, reserved):
        tools -= CONTENT_WRITE
    tools -= LETTERS_TOOLS - letters_tools(letters, roots, origin)
    tools -= (GIT_READ | GIT_WRITE) - _git_tools(repositories, reserved, origin)
    if not config.web_search.api_key_env:
        tools -= {"web.search"}
    if not (config.shell.enabled and shell_granted and origin == Origin.INTERACTIVE):
        tools -= {"shell.exec"}
    return EffectiveProfile(
        name=name,
        agent_name=config.agent_name,
        origin=origin,
        mode=mode,
        data_dir=data_dir,
        model_pin=config.model.pin,
        model_image_inputs=config.model.image_inputs,
        vision=config.vision,
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
        images=config.images,
        blog_repository=blog,
        git_repositories=repositories,
        git_reserved=reserved,
        letters=letters,
        web_search=config.web_search,
        shell=config.shell,
        shell_granted=shell_granted,
        background=config.background,
        http=config.http,
        proactive_budgets=config.proactive_budgets,
        tool_modules=tuple(config.tool_modules),
        granted_tools=grants,
        tools=tools,
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
    if current.images != admitted.images:
        raise ConfigError("image service changed; a new admission is required")
    if current.blog_repository != admitted.blog_repository:
        raise ConfigError("blog repository changed; a new admission is required")
    roots = tuple(
        dict.fromkeys(
            inner
            for a in admitted.workspace_roots
            for b in current.workspace_roots
            for inner in (a, b)
            if inner.is_relative_to(a) and inner.is_relative_to(b)
        )
    )
    tools = current.tools & admitted.tools & current.granted_tools & admitted.granted_tools
    repositories = {
        alias: repo
        for alias, repo in admitted.git_repositories.items()
        if current.git_repositories.get(alias) == repo
        and any(repo.path.is_relative_to(root) for root in roots)
    }
    reserved = current.git_reserved | admitted.git_reserved
    tools -= (GIT_READ | GIT_WRITE) - _git_tools(repositories, reserved, admitted.origin)
    letters = admitted.letters if admitted.letters == current.letters else None
    tools -= LETTERS_TOOLS - letters_tools(letters, roots, admitted.origin)
    if admitted.blog_repository is not None and checkout_reserved(
        admitted.blog_repository, reserved
    ):
        tools -= CONTENT_WRITE
    if not admitted.web_search.api_key_env or current.web_search != admitted.web_search:
        tools -= {"web.search"}
    shell_granted = current.shell_granted and admitted.shell_granted
    if not (
        shell_granted
        and current.shell.enabled
        and admitted.shell.enabled
        and admitted.origin == Origin.INTERACTIVE
        and roots
    ):
        tools -= {"shell.exec"}
    changes = {
        "model_image_inputs": current.model_image_inputs and admitted.model_image_inputs,
        "workspace_roots": roots,
        "git_repositories": repositories,
        "git_reserved": reserved,
        "letters": letters,
        "granted_tools": current.granted_tools & admitted.granted_tools,
        "shell_granted": shell_granted,
        "tools": tools,
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
