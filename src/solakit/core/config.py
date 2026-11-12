"""Validated operator configuration; secrets are references, never TOML values."""

import logging
import os
import re
import stat
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from string import Formatter
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from pydantic_core import PydanticCustomError


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


RESERVED_REQUEST_KEYS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "stream",
        "stream_options",
        "max_tokens",
        "max_completion_tokens",
        "n",
    }
)


class SamplingParams(StrictModel):
    """Known sampling keys; None means "server default" and is never sent."""

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    top_k: int | None = Field(default=None, ge=1, le=1000)
    min_p: float | None = Field(default=None, ge=0, le=1)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    repetition_penalty: float | None = Field(default=None, gt=0, le=2)


class RequestParams(SamplingParams):
    """[model.request]: defaults for every completion (profiles may override)."""

    parallel_tool_calls: bool | None = None
    # How thinking on/off reaches the server. "chat_template_kwargs" sends
    # {"chat_template_kwargs": {"enable_thinking": bool}} (Qwen/vLLM/oMLX style).
    thinking_control: Literal["chat_template_kwargs", "none"] = "chat_template_kwargs"
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def safe_extra(self) -> Self:
        import json

        forbidden = RESERVED_REQUEST_KEYS.intersection(self.extra_body)
        if forbidden:
            raise ValueError(
                "extra_body must not override request keys: "
                + ", ".join(sorted(forbidden))
            )
        if len(json.dumps(self.extra_body)) > 4096:
            raise ValueError("extra_body is limited to 4096 bytes of JSON")
        return self

    def wire(self, *, thinking: str | None = None, override=None) -> dict:
        """Exact extra request keys; empty when nothing is configured."""
        values = self.model_dump(
            include=set(SamplingParams.model_fields) | {"parallel_tool_calls"},
            exclude_none=True,
        )
        if override is not None:
            values.update(
                override.model_dump(
                    include=set(SamplingParams.model_fields) | {"parallel_tool_calls"},
                    exclude_none=True,
                )
            )
        result = {**values}
        for key, value in self.extra_body.items():
            if (
                key == "chat_template_kwargs"
                and isinstance(value, dict)
                and isinstance(result.get(key), dict)
            ):
                result[key] = {**result[key], **value}
            else:
                result[key] = value
        if thinking in ("on", "off") and self.thinking_control != "none":
            kwargs = dict(result.get("chat_template_kwargs") or {})
            kwargs["enable_thinking"] = thinking == "on"
            result["chat_template_kwargs"] = kwargs
        return result


class ProfileModel(SamplingParams):
    """[profiles.<name>.model]: per-profile thinking, output and sampling."""

    thinking: Literal["off", "on", "auto"] | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_reasoning_tokens: int | None = Field(default=None, ge=64)
    parallel_tool_calls: bool | None = None


class ModelConfig(StrictModel):
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: SecretRef | None = None
    model_id: str = "OsaurusAI/Qwen3.6-35B-A3B-mxfp4"
    concurrency: int = Field(default=1, ge=1)
    timeout_s: float = Field(default=120, gt=0)
    max_tokens: int = Field(default=4096, ge=1)
    trust_env: bool = False
    request: RequestParams = Field(default_factory=RequestParams)
    # Parse <tool_call> blocks the server left in content (fail-closed).
    tool_call_fallback: bool = False

    def resolve_api_key(self) -> str | None:
        return validate_api_token(self.api_key.resolve()) if self.api_key else None


class ServerConfig(StrictModel):
    cwd: Path | None = None
    host: str = "127.0.0.1"
    port: int = Field(default=8382, ge=1, le=65535)
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"


def check_memory_roots(roots: list[str]) -> None:
    for root in roots:
        if (
            not root.startswith("viking://")
            or "~" in root
            or "%" in root
            or any(p in {".", ".."} for p in root.split("/"))
        ):
            raise ValueError("memory roots must be canonical Viking URIs")


class ProfileMemory(StrictModel):
    """[profiles.<name>.memory]: isolated OpenViking user/roots for one profile.

    Every recall, read, commit and compaction summary from this profile's runs
    uses this user and these roots instead of the global [memory] ones."""

    user: str = Field(pattern=r"^[A-Za-z0-9_-]+$", max_length=128)
    roots: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_roots(self) -> Self:
        check_memory_roots(self.roots)
        return self


class MemoryConfig(StrictModel):
    front_page_max_chars: int = Field(default=8192, ge=256)
    enabled: bool = False
    base_url: str | None = None
    api_key: SecretRef | None = None
    roots: list[str] = Field(default_factory=list)
    user: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$", max_length=128)
    timeout_s: float = Field(default=30, gt=0)
    compaction_timeout_s: float = Field(default=300, gt=0, allow_inf_nan=False)
    trust_env: bool = False
    recall_limit: int = Field(default=5, ge=1, le=20)
    recall_window: int = Field(default=3, ge=0, le=100)
    recall_query_max_chars: int = Field(default=1000, ge=1, le=100000)
    recall_skip_trivial: bool = True
    recall_trivial_phrases: list[str] = Field(
        default_factory=lambda: [
            "thanks",
            "thank you",
            "ok",
            "okay",
            "yes",
            "yeah",
            "lol",
            "nice",
            "cool",
            "got it",
        ]
    )
    remember_max_chars: int = Field(default=2000, ge=1, le=32000)

    @model_validator(mode="after")
    def valid_endpoint(self) -> Self:
        if self.enabled and (not self.base_url or not self.roots):
            raise ValueError("enabled memory requires base_url and canonical URI roots")
        check_memory_roots(self.roots)
        return self


class PathsConfig(StrictModel):
    data: Path = Path("~/sola-data")
    soul: Path = Path("SOUL.md")
    fragments: Path = Path("fragments")


EFFECTS = frozenset({"push", "send", "print", "shell", "operate"})
LEGACY_EFFECTS = {
    "git.push": "push",
    "email.reply": "send",
    "letters.reply": "send",
    "printer.print": "print",
    "shell.exec": "shell",
    "jobs.manage": "operate",
    "jobs.run": "operate",
    "jobs.manage.all": "operate",
    "sessions.search.all": "operate",
    "image.generate": None,
}


class Profile(StrictModel):
    read: Literal["none", "workspace", "anywhere"] = "workspace"
    write: Literal["none", "workspace", "anywhere"] = "none"
    workspace: list[str] = Field(default_factory=list)
    write_workspace: list[str] | None = None
    effects: list[Literal["push", "send", "print", "shell", "operate"]] = Field(
        default_factory=list
    )
    resources: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def translate_access(cls, values):
        if not isinstance(values, dict):
            return values
        old = {"read_scopes", "write_scopes", "capabilities"}
        new = {"read", "write", "workspace", "write_workspace", "effects", "resources"}
        if not old.intersection(values):
            return values
        if new.intersection(values):
            raise ValueError(
                "Do not mix legacy scopes/capabilities with tiers/effects/resources"
            )
        values = dict(values)
        reads = TypeAdapter(list[str]).validate_python(values.get("read_scopes", []))
        writes = TypeAdapter(list[str]).validate_python(values.get("write_scopes", []))
        effects, resources, actions = [], [], {}
        for cap in TypeAdapter(list[str]).validate_python(
            values.get("capabilities", [])
        ):
            if cap in LEGACY_EFFECTS:
                if LEGACY_EFFECTS[cap]:
                    effects.append(LEGACY_EFFECTS[cap])
                continue
            parts = cap.split(":")
            if (
                len(parts) != 3
                or parts[0]
                not in {"mailbox", "letters", "printer", "calendar", "image"}
                or parts[2] not in {"read", "reply", "print", "generate"}
            ):
                raise PydanticCustomError(
                    "access_config",
                    "Unmapped legacy capability: {capability}",
                    {"capability": cap},
                )
            prefix, alias, action = parts
            resources.append(f"{prefix}:{alias}")
            actions.setdefault(prefix, {}).setdefault(alias, set()).add(action)
            if action in {"reply", "print"}:
                effects.append({"reply": "send", "print": "print"}[action])
        for prefix, aliases in actions.items():
            if len({tuple(sorted(a)) for a in aliases.values()}) > 1:
                logging.getLogger(__name__).warning(
                    "Legacy resource actions differ for %s aliases: %s; "
                    "split profiles to retain action separation",
                    prefix,
                    ", ".join(aliases),
                )
        values.update(
            workspace=list(dict.fromkeys(reads + writes)),
            write_workspace=writes,
            read="workspace" if reads or writes else "none",
            write="workspace" if writes else "none",
            effects=list(dict.fromkeys(effects)),
            resources=list(dict.fromkeys(resources)),
        )
        for key in old:
            values.pop(key, None)
        return values

    prompt_layout: Literal["stable", "legacy"] = "stable"
    # Optional isolated memory user/roots; None uses the global [memory] section.
    memory: ProfileMemory | None = None
    model: ProfileModel = Field(default_factory=ProfileModel)
    # Operator playbooks indexed in this profile's prompt (fragments/playbooks).
    playbooks: list[str] = Field(default_factory=list)
    # Plan 6 workflow guidance; all opt-in until evaluated.
    step_notice_at: int = Field(default=0, ge=0)
    wrapup: Literal["off", "status", "answer"] = "off"
    plan_reminder: bool = False
    context_tokens: int | None = Field(default=None, ge=256)
    tool_disclosure: Literal["eager", "deferred", "auto"] = "eager"
    tool_resident: list[str] = Field(default_factory=list)
    tool_auto_threshold: int = Field(default=12000, ge=0)
    tools: list[str] = Field(default_factory=list)
    max_steps: int = Field(default=40, ge=1)
    context_budget: int = Field(default=32000, ge=1)
    mid_run_compaction: bool = False
    tool_timeout_s: float = Field(default=120, gt=0)
    tool_output_max_chars: int = Field(default=20000, ge=32)
    repair_attempts: int = Field(default=2, ge=0)
    # None keeps the historical per-run count; a number counts repairs per
    # tool + error class (each up to repair_attempts) under this run ceiling.
    repair_ceiling: int | None = Field(default=None, ge=1)
    # lenient: read tools tolerate blank args, JSON-string arrays/objects and
    # one stray key (recorded); strict: only scalar/null/blank normalization.
    argument_mode: Literal["lenient", "strict"] = "lenient"
    tool_result_format: Literal["envelope", "text"] = "envelope"
    # Request-time stubbing of old, large tool results (evidence unchanged).
    tool_result_aging: Literal["off", "auto"] = "off"
    aging_keep_groups: int = Field(default=2, ge=0)
    aging_min_chars: int = Field(default=1500, ge=100)


class CompactionConfig(StrictModel):
    """openviking: OpenViking summaries only (historical behaviour). local: the
    pinned model summarizes. auto: OpenViking for shared sessions with memory
    enabled, the local model otherwise."""

    strategy: Literal["openviking", "local", "auto"] = "openviking"
    max_summary_tokens: int = Field(default=1024, ge=64, le=16384)
    max_input_chars: int = Field(default=60000, ge=2000)


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
    tools: list[str] | None = Field(default=None, max_length=256)
    tool_disclosure: Literal["inherit", "eager", "deferred", "auto"] = "inherit"
    tool_resident: list[str] | None = None
    task: str
    playbook: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    # Structured final answer: JSON validated against this schema (2 retries),
    # optionally rendered by a sandboxed Jinja template for delivery.
    output_schema: dict[str, Any] | None = None
    render: str | None = None
    # Trigger this job after a successful run (no cycles).
    then: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    bind_session: str
    sub_session: bool = False
    deliver: Delivery | None = None
    catch_up: bool = False
    polling: bool = False
    enabled: bool = False

    @model_validator(mode="after")
    def valid_timezone(self) -> Self:  # noqa: C901
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
        if self.output_schema is not None or self.render is not None:
            from solakit.core.structured import check_render, check_schema

            if self.output_schema is not None:
                check_schema(self.output_schema)
            if self.render is not None:
                if self.output_schema is None:
                    raise ValueError("render requires output_schema")
                check_render(self.render)
        if self.then is not None and self.then == self.id:
            raise ValueError("then must name another job")
        if self.sub_session and self.bind_session == "ephemeral":
            raise ValueError("Sub-sessions require a parent session or channel")
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


class ToolApisConfig(StrictModel):
    brave_api_key: SecretRef | None = None
    cloudflare_account_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+$")
    cloudflare_api_token: SecretRef | None = None
    github_token: SecretRef | None = None


class Config(StrictModel):
    operator_token: SecretRef = Field(
        default_factory=lambda: SecretRef(env="SOLA_OPERATOR_TOKEN")
    )
    model: ModelConfig = Field(default_factory=ModelConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    delegation: DelegationConfig = Field(default_factory=DelegationConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    profile_defaults: dict[str, Any] = Field(default_factory=dict)
    channel_defaults: dict[str, Any] = Field(default_factory=dict)
    profiles: dict[str, Profile] = Field(
        default_factory=lambda: {"interactive": Profile(max_steps=60)}
    )
    channels: list[Channel] = Field(default_factory=list)
    jobs: list[Job] = Field(default_factory=list)
    repositories: dict[str, Path] = Field(default_factory=dict)
    collections: dict[str, CollectionConfig] = Field(default_factory=dict)
    enabled_tools: list[str] = Field(default_factory=list)
    # Named grant bundles: profiles/jobs grant "@name". Pure grant sugar,
    # expanded before access checks; entries may be names, families or @sets.
    toolsets: dict[str, list[str]] = Field(default_factory=dict)
    tool_apis: ToolApisConfig = Field(default_factory=ToolApisConfig)
    timezone: str = "America/Denver"
    discord_token: SecretRef | None = None

    @model_validator(mode="before")
    @classmethod
    def apply_entry_defaults(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        values = dict(values)
        timezone = values.get("timezone", cls.model_fields["timezone"].default)
        if isinstance(values.get("jobs"), list):
            values["jobs"] = [
                {
                    "timezone": timezone,
                    **(
                        job.model_dump(exclude_unset=True)
                        if isinstance(job, Job)
                        else job
                    ),
                }
                if isinstance(job, (dict, Job))
                else job
                for job in values["jobs"]
            ]
        profile_defaults = values.get("profile_defaults", {})
        channel_defaults = values.get("channel_defaults", {})

        # Preserve the historical implicit interactive profile unless profiles
        # were supplied explicitly, while still applying configured defaults to it.
        if (
            isinstance(profile_defaults, dict)
            and profile_defaults
            and "profiles" not in values
        ):
            values["profiles"] = {
                "interactive": {"max_steps": profile_defaults.get("max_steps", 60)}
            }
        if isinstance(profile_defaults, dict) and isinstance(
            values.get("profiles"), dict
        ):
            values["profiles"] = {
                name: (
                    {
                        **profile_defaults,
                        **(
                            profile.model_dump(exclude_unset=True)
                            if isinstance(profile, Profile)
                            else profile
                        ),
                    }
                    if isinstance(profile, (dict, Profile))
                    else profile
                )
                for name, profile in values["profiles"].items()
            }
        for name, profile in (
            values.get("profiles", {}).items()
            if isinstance(values.get("profiles", {}), dict)
            else []
        ):
            if isinstance(profile, dict) and {
                "read_scopes",
                "write_scopes",
                "capabilities",
            }.intersection(profile):
                logging.getLogger(__name__).warning(
                    "Profile %s: translating deprecated access keys; "
                    "inspect catalog summaries and profile_toml for replacement",
                    name,
                )
        if isinstance(channel_defaults, dict) and isinstance(
            values.get("channels"), list
        ):
            values["channels"] = [
                (
                    {
                        **channel_defaults,
                        **(
                            channel.model_dump(exclude_unset=True)
                            if isinstance(channel, Channel)
                            else channel
                        ),
                    }
                    if isinstance(channel, (dict, Channel))
                    else channel
                )
                for channel in values["channels"]
            ]
        return values

    @model_validator(mode="after")
    def references(self) -> Self:  # noqa: C901
        # Validate partial defaults even when no concrete entry happens to use them.
        Profile.model_validate(self.profile_defaults)
        if not self.memory.enabled:
            for name, profile in self.profiles.items():
                if profile.memory is not None:
                    raise ValueError(
                        f"Profile {name} overrides memory but [memory] is disabled"
                    )
        Channel.model_validate(
            {
                "id": "defaults-check",
                "type": "api",
                "channel_id": "defaults-check",
                "session": "defaults-check",
                **self.channel_defaults,
            }
        )
        ZoneInfo(self.timezone)
        for name, entries in self.toolsets.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
                raise ValueError(f"invalid toolset name: {name}")
            for entry in entries:
                if entry.startswith("@") and entry[1:] not in self.toolsets:
                    raise ValueError(f"toolset {name}: unknown toolset {entry}")
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
                    import difflib

                    close = difflib.get_close_matches(item.profile, list(self.profiles))
                    hint = f" (did you mean {close[0]}?)" if close else ""
                    raise ValueError(f"unknown profile: {item.profile}{hint}")
        pins = [(c.type, c.channel_id) for c in self.channels]
        if len(pins) != len(set(pins)):
            raise ValueError("duplicate channel identity")
        if any(c.type != "api" and c.profile == "maintenance" for c in self.channels):
            raise ValueError("Maintenance is operator-API only")
        if any(j.profile == "maintenance" for j in self.jobs):
            raise ValueError("Maintenance cannot be scheduled")
        channel_ids = {c.id for c in self.channels}
        for name, profile in self.profiles.items():
            for grant in profile.resources:
                if (
                    grant.startswith("channel:")
                    and grant.removeprefix("channel:") not in channel_ids
                ):
                    raise ValueError(f"Profile {name}: unknown channel grant: {grant}")
        for job in self.jobs:
            if (
                job.deliver
                and job.deliver.mode != "silent"
                and job.deliver.to not in channel_ids
            ):
                raise ValueError("job delivery references unknown channel")
        return self


def code_roots(module_file: Path | None = None) -> tuple[Path, ...]:
    """Protect the installed package, plus a verified source checkout if present."""
    package = (module_file or Path(__file__)).resolve().parent.parent
    candidate = package.parent.parent
    if package.parent.name == "src":
        try:
            project = tomllib.loads((candidate / "pyproject.toml").read_text())
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            pass
        else:
            metadata = project.get("project")
            if isinstance(metadata, dict) and metadata.get("name") == "solakit":
                return (candidate,)
    return (package,)


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
    if config.server.cwd is not None:
        value = config.server.cwd.expanduser()
        config.server.cwd = value if value.is_absolute() else base / value
    config.repositories = {
        name: p.expanduser() if p.expanduser().is_absolute() else base / p
        for name, p in config.repositories.items()
    }
    for reference in (
        config.operator_token,
        config.model.api_key,
        config.memory.api_key,
        config.discord_token,
        config.tool_apis.brave_api_key,
        config.tool_apis.cloudflare_api_token,
        config.tool_apis.github_token,
    ):
        if reference is not None and reference.file is not None:
            value = reference.file.expanduser()
            reference.file = value if value.is_absolute() else base / value
    if any(config.paths.data.resolve().is_relative_to(root) for root in code_roots()):
        raise ConfigLoadError(
            "Runtime data directory must live outside the code tree: "
            f"{config.paths.data}"
        )
    return config
