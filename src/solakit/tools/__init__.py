"""Configured tool discovery, typed arguments, and access enforcement."""

import asyncio
import importlib
import inspect
import json
import logging
import re
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from solakit.core.config import EFFECTS, Config, Profile
from solakit.core.io import durable
from solakit.core.schemas import Session
from solakit.core.store import Store
from solakit.tools.arguments import argument_error, validate_arguments
from solakit.tools.distributions import SYNC_HINT, identity, installed


class ToolError(Exception):
    """Expected tool failure safe to return to the model."""


class ToolResult(BaseModel):
    content: str
    error: bool = False
    repairable: bool = False
    content_complete: bool = True
    artifacts: list[str] = Field(default_factory=list)


def truncate_output(content: str, limit: int, *, complete: bool) -> tuple[str, int]:
    """Count Unicode characters, including the marker within the output budget.

    A bounded source cannot promise its full length. Report an explicit lower
    bound rather than presenting the observed prefix as the original output.
    """
    if complete and len(content) <= limit:
        return content, 0
    retained = min(len(content), limit)
    while True:
        omitted = len(content) - retained
        count = str(omitted) if complete else f">={omitted}"
        marker = f"\n[truncated {count} chars]"
        budget = min(len(content), max(0, limit - len(marker)))
        if budget == retained:
            return content[:retained] + marker, omitted
        retained = budget


@dataclass
class ToolContext:
    session: Session
    profile: str
    config: Config
    store: Store
    run_id: str
    memory_client: Any = None
    deliver: Any = None
    runtime_provider: Any = None
    job_manager: Any = None
    channel_poster: Any = None
    nested_submit: Any = None
    stage_memory: Any = None
    configured_tool_names: frozenset[str] = frozenset()
    tool_view: Any = None
    activate_tools: Any = None
    active_tools: Any = None
    tool_allowlist: frozenset[str] | None = None

    @property
    def fs(self):
        from solakit.services.fs import Filesystem

        return Filesystem(self)

    def git(self, repository: str):
        from solakit.services.git import Git

        return Git(self, repository)

    def collection(self, alias: str):
        from solakit.services.collections import Collection

        return Collection(self, alias)

    def runtime(self) -> dict:
        if self.runtime_provider is None:
            raise ToolError("Live runtime status is not available in this context")
        status = self.runtime_provider(self.profile)
        if self.tool_allowlist is not None and "granted_tools" in status:
            status = {
                **status,
                "granted_tools": [
                    name
                    for name in status["granted_tools"]
                    if name in self.tool_allowlist
                ],
            }
        return status

    def path(self, path: str, *, write: bool = False) -> Path:
        """Resolve symlinks and enforce the actual path against explicit roots."""
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() and self.config.server.cwd is not None:
            candidate = self.config.server.cwd.expanduser() / candidate
        candidate = candidate.resolve()
        profile = self.config.profiles[self.profile]
        roots = tier_roots(profile, self.config, write=write)
        if not any(root and candidate.is_relative_to(root) for root in roots):
            raise ToolError("Path is outside profile filesystem scopes")
        return candidate


def scope_root(scope: str, config: Config) -> Path | None:
    if scope.startswith("repo:"):
        path = config.repositories.get(scope[5:])
        return path.expanduser().resolve() if path else None
    if scope.startswith("collection:"):
        resource = config.collections.get(scope.removeprefix("collection:"))
        if resource is None:
            return None
        root = config.repositories[resource.repository].expanduser().resolve()
        directory = (root / resource.directory).resolve()
        if not directory.is_relative_to(root):
            raise ValueError("Collection directory escapes repository")
        return directory
    if scope.startswith("path:"):
        path = Path(scope[5:]).expanduser()
        if not path.is_absolute():
            raise ValueError("filesystem scopes must use absolute paths")
        return path.resolve()
    raise ValueError(f"unknown filesystem scope: {scope}")


def tier_roots(profile: Profile, config: Config, *, write: bool) -> list[Path]:
    def roots(tier, entries, *, default_workspace=True):
        if tier == "anywhere":
            return [Path("/")]
        if tier == "none":
            return []
        if entries is None:
            entries = profile.workspace
        if not entries and default_workspace:
            return [
                config.paths.data.expanduser().resolve(),
                *(p.expanduser().resolve() for p in config.repositories.values()),
            ]
        result = []
        for entry in entries or []:
            root = scope_root(entry, config)
            if root is None:
                raise ValueError(f"Unknown workspace resource: {entry}")
            result.append(root)
        return result

    writes = roots(
        profile.write,
        profile.write_workspace,
        default_workspace=profile.write_workspace is None,
    )
    result = writes if write else roots(profile.read, profile.workspace) + writes
    return [Path("/")] if Path("/") in result else list(dict.fromkeys(result))


@dataclass(frozen=True)
class Manifest:
    name: str
    profiles: tuple[str, ...]
    writes: tuple[str, ...]
    timeout: float | None
    manages_timeout: bool = False
    summary: str | None = None
    keywords: tuple[str, ...] = ()
    effect: str | None = None


def tool(
    *,
    name: str,
    profiles: list[str],
    writes: list[str] = (),
    timeout: float | None = None,
    effect: str | None = None,
    manages_timeout: bool = False,
    summary: str | None = None,
    keywords: tuple[str, ...] = (),
):
    def decorate(function: Callable) -> Callable:
        function.__sola_tool__ = Manifest(
            name,
            tuple(profiles),
            tuple(writes),
            timeout,
            manages_timeout,
            summary,
            tuple(keywords),
            effect,
        )
        return function

    return decorate


@dataclass
class Registered:
    function: Callable
    manifest: Manifest
    arguments: type[BaseModel]

    def schema(self) -> dict:
        description = inspect.getdoc(self.function) or ""
        if not inspect.iscoroutinefunction(self.function):
            description += (
                "\nRuns in a worker thread. Timeout does not stop execution; "
                "effects may continue. Inspect state or receipts before retrying."
            )
        return {
            "type": "function",
            "function": {
                "name": self.manifest.name,
                "description": description,
                "parameters": self.arguments.model_json_schema(),
            },
        }


class Registry:
    def __init__(self, config: Config):
        self.config = config
        self.tools: dict[str, Registered] = {}
        self.runtime_status = None
        self.job_manager = None
        self.channel_poster = None
        self.distributions: list[dict] = []
        for module in config.enabled_tools:
            if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module):
                raise ValueError(
                    "Tool modules must be Python module names or dotted paths"
                )
            path = module if "." in module else f"solakit.tools.{module}"
            provider = installed(path)
            try:
                imported = importlib.import_module(path)
            except ModuleNotFoundError as exc:
                raise ValueError(
                    f"enabled_tools references {path} but required module "
                    f"{exc.name} is unavailable; {SYNC_HINT}"
                ) from None
            except ImportError:
                raise ValueError(
                    f"enabled_tools references {path} but its imports failed; "
                    f"check installed distributions and dependencies; {SYNC_HINT}"
                ) from None
            self.distributions.append(identity(path, imported, provider))
            for function in vars(imported).values():
                manifest = getattr(function, "__sola_tool__", None)
                if isinstance(manifest, Manifest):
                    self.register(function, manifest)
        self.validate()

    def register(self, function: Callable, manifest: Manifest | None = None) -> None:
        manifest = manifest or function.__sola_tool__
        if manifest.effect is not None and manifest.effect not in EFFECTS:
            raise ValueError(f"Unknown tool effect: {manifest.effect}")
        if any(w != "filesystem" for w in manifest.writes):
            raise ValueError(
                "writes accepts only filesystem; use effect for outward actions"
            )
        if manifest.name.startswith("tools."):
            raise ValueError("tools.* is reserved for runtime disclosure")
        if manifest.summary is not None and (
            len(manifest.summary) > 100
            or "\n" in manifest.summary
            or "\r" in manifest.summary
        ):
            raise ValueError("Tool summary must be one line of at most 100 characters")
        if manifest.name in self.tools:
            raise ValueError(f"duplicate tool: {manifest.name}")
        hints = get_type_hints(function, include_extras=True)
        fields = {}
        for name, parameter in inspect.signature(function).parameters.items():
            if name == "ctx":
                continue
            if name not in hints:
                raise ValueError(f"tool argument missing type: {name}")
            fields[name] = (
                hints[name],
                ... if parameter.default is parameter.empty else parameter.default,
            )
        arguments = create_model(
            manifest.name, __config__=ConfigDict(extra="forbid", strict=True), **fields
        )
        self.tools[manifest.name] = Registered(function, manifest, arguments)

    def expand_grants(
        self, entries: list[str], profile: str, *, strict: bool = True
    ) -> list[str]:
        available = sorted(
            n
            for n, t in self.tools.items()
            if profile in t.manifest.profiles or "*" in t.manifest.profiles
        )
        families = sorted({n.split(".")[0] for n in available})
        result = []
        for entry in entries:
            if entry == "*":
                matches = available
            elif re.fullmatch(r"[A-Za-z_]\w*\.\*", entry):
                family = entry[:-2]
                matches = [n for n in available if n.split(".")[0] == family]
                if not matches and strict:
                    raise ValueError(
                        f"Profile {profile}: unknown tool family: {family} "
                        f"(known: {', '.join(families)})"
                    )
            elif "*" in entry:
                raise ValueError(f"Invalid tool grant pattern: {entry}")
            elif entry in available:
                matches = [entry]
            elif not strict:
                matches = []
            else:
                raise ValueError(f"tool {entry} is not available to {profile}")
            result.extend(matches)
        return list(dict.fromkeys(result))

    def allowed(self, name: str, profile: str) -> bool:
        return name in self.tool_names(profile)

    def tool_names(self, profile: str) -> list[str]:
        return self.expand_grants(self.config.profiles[profile].tools, profile)

    def check_access(self, registered: Registered, profile: Profile) -> None:
        if registered.manifest.writes and profile.write == "none":
            raise ToolError(
                f"{registered.manifest.name} requires filesystem write tier"
            )
        effect = registered.manifest.effect
        if effect and effect not in profile.effects:
            raise ToolError(f"{registered.manifest.name} requires effect '{effect}'")

    def validate(self) -> None:
        for name, profile in self.config.profiles.items():
            for entry in profile.workspace + (profile.write_workspace or []):
                if scope_root(entry, self.config) is None:
                    raise ValueError(
                        f"Profile {name}: unknown workspace resource: {entry}"
                    )
            if "shell" in profile.effects and profile.write == "none":
                logging.getLogger(__name__).warning(
                    "Profile %s: shell effect with write=none cannot use shell tools",
                    name,
                )
            self.view(name)
            for tool_name in self.tool_names(name):
                try:
                    self.check_access(self.tools[tool_name], profile)
                except ToolError as exc:
                    raise ValueError(f"Profile '{name}' grants {exc}") from exc

    def access_summary(self, profile: str) -> dict:
        cfg = self.config.profiles[profile]
        return {
            "tools": self.tool_names(profile),
            "read": cfg.read,
            "write": cfg.write,
            "read_roots": [str(p) for p in tier_roots(cfg, self.config, write=False)],
            "write_roots": [str(p) for p in tier_roots(cfg, self.config, write=True)],
            "effects": list(cfg.effects),
            "resources": list(cfg.resources),
        }

    def summary_access(self, name: str) -> str:
        access = self.access_summary(name)
        return (
            f"{name} — tools: {', '.join(self.config.profiles[name].tools) or 'none'} "
            f"({len(access['tools'])}), "
            f"read: {access['read']} ({', '.join(access['read_roots'])}), "
            f"write: {access['write']} ({', '.join(access['write_roots'])}), "
            f"effects: {', '.join(access['effects']) or '—'}; "
            f"resources: {', '.join(access['resources']) or '—'}"
        )

    def catalog(self) -> dict:
        """Loaded tools and effective grants, shared by diagnostics and API."""
        return {
            "profile_toml": {
                n: f"[profiles.{n}]\n"
                + "\n".join(
                    f"{key} = {json.dumps(value, ensure_ascii=False)}"
                    for key, value in self.config.profiles[n]
                    .model_dump(
                        include={
                            "tools",
                            "read",
                            "write",
                            "workspace",
                            "write_workspace",
                            "effects",
                            "resources",
                        }
                    )
                    .items()
                    if value is not None
                )
                for n in self.config.profiles
            },
            "profile_config": {
                n: self.config.profiles[n].model_dump(
                    include={
                        "tools",
                        "read",
                        "write",
                        "workspace",
                        "write_workspace",
                        "effects",
                        "resources",
                    }
                )
                for n in self.config.profiles
            },
            "summaries": {n: self.summary_access(n) for n in self.config.profiles},
            "access": {n: self.access_summary(n) for n in self.config.profiles},
            "disclosure": {
                name: self.view(name).diagnostics(
                    self.config.profiles[name].context_budget
                )
                for name in self.config.profiles
            },
            "modules": list(self.config.enabled_tools),
            "distributions": list(self.distributions),
            "profiles": {
                name: [schema["function"]["name"] for schema in self.schemas(name)]
                for name in sorted(self.config.profiles)
            },
            "tools": [
                {
                    "name": name,
                    "family": name.split(".")[0],
                    "summary": self.summary(name),
                    "keywords": list(registered.manifest.keywords),
                    "description": inspect.getdoc(registered.function) or "",
                    "parameters": registered.schema()["function"]["parameters"],
                    "profiles": [
                        profile
                        for profile in sorted(self.config.profiles)
                        if self.allowed(name, profile)
                    ],
                    "writes": list(registered.manifest.writes),
                    "effect": registered.manifest.effect,
                }
                for name, registered in sorted(self.tools.items())
            ],
        }

    def effective_tools(
        self, profile: str, tools: list[str] | None = None
    ) -> list[str]:
        """A task may narrow profile grants, never replace or expand them."""
        selected = (
            None
            if tools is None
            else set(self.expand_grants(tools, profile, strict=False))
        )
        return [
            name
            for name in self.tool_names(profile)
            if self.allowed(name, profile) and (selected is None or name in selected)
        ]

    def schemas(self, profile: str, tools: list[str] | None = None) -> list[dict]:
        return [
            self.tools[name].schema() for name in self.effective_tools(profile, tools)
        ]

    def summary(self, name: str) -> str:
        item = self.tools[name]
        return (
            item.manifest.summary
            or (inspect.getdoc(item.function) or name).splitlines()[0][:100]
        )

    def view(self, profile, tools=None, mode="inherit", resident=None, *, strict=True):
        from solakit.tools.disclosure import resolve

        return resolve(self, profile, tools, mode, resident, strict=strict)

    async def dispatch(self, name: str, arguments: str, ctx: ToolContext) -> ToolResult:
        start = time.monotonic()
        result = ToolResult(content="Tool execution failed", error=True)
        parsed = None
        detail = None
        repairable = False
        execution_started = False
        values = None
        try:
            if (
                ctx.tool_view is not None
                and ctx.tool_view.mode == "deferred"
                and name in ("tools.search", "tools.load")
            ):
                from solakit.tools.disclosure import disclose

                result = disclose(name, arguments, ctx)
                return result
            if ctx.tool_allowlist is not None and name not in ctx.tool_allowlist:
                repairable = True
                raise ToolError(
                    "Tool is not permitted for this run. Choose a tool from "
                    "this run's granted schemas; changing arguments cannot "
                    "grant access."
                )
            if not self.allowed(name, ctx.profile):
                repairable = True
                raise ToolError(
                    "Tool is not permitted in this profile. Choose a tool from "
                    "this run's granted schemas; changing arguments cannot "
                    "grant access."
                )
            registered = self.tools[name]
            if (
                ctx.tool_view is not None
                and ctx.tool_view.mode == "deferred"
                and name not in ctx.active_tools
            ):
                ctx.activate_tools([name])
                repairable = False
                raise ToolError(
                    f"{name} was not loaded; its full schema is now loaded "
                    "for this run. It was not executed — call it again."
                )
            ctx.runtime_provider = self.runtime_status
            ctx.job_manager = self.job_manager
            ctx.channel_poster = self.channel_poster
            cfg = self.config.profiles[ctx.profile]
            self.check_access(registered, cfg)
            repairable = True
            parsed = json.loads(arguments)
            values = validate_arguments(registered.arguments, parsed).model_dump()
            repairable = False
            timeout = min(
                registered.manifest.timeout or cfg.tool_timeout_s, cfg.tool_timeout_s
            )
            # Trusted tools that own a multi-stage operation can enforce their
            # own bound and return operation-specific timeout receipts/evidence.
            async with asyncio.timeout(
                None if registered.manifest.manages_timeout else timeout
            ):
                execution_started = True
                if inspect.iscoroutinefunction(registered.function):
                    returned = await registered.function(ctx=ctx, **values)
                else:
                    returned = await asyncio.to_thread(
                        registered.function, ctx=ctx, **values
                    )
            candidate = (
                returned
                if isinstance(returned, ToolResult)
                else ToolResult(content=str(returned))
            )
            candidate.artifacts = [str(ctx.path(path)) for path in candidate.artifacts]
            result = candidate
        except (ToolError, ValidationError, ValueError, TimeoutError) as exc:
            if isinstance(exc, TimeoutError):
                message = (
                    "Tool timed out. Completion is unknown; inspect the current "
                    "state or operation receipt before retrying an action with "
                    "side effects."
                )
            elif isinstance(exc, ToolError):
                message = str(exc)
                if not execution_started:
                    message += " The tool was not executed."
            elif not execution_started and repairable:
                message = argument_error(registered.arguments, exc)
            else:
                detail = traceback.format_exc()
                message = (
                    "Tool failed. Inspect current state or operation receipts "
                    "before retrying side effects."
                )
            result = ToolResult(content=message, error=True, repairable=repairable)
        except Exception:
            detail = traceback.format_exc()
        finally:
            limit = self.config.profiles[ctx.profile].tool_output_max_chars
            observed_chars = len(result.content)
            source_complete = result.content_complete
            result.content, omitted = truncate_output(
                result.content, limit, complete=source_complete
            )
            result.content_complete = source_complete and omitted == 0
            await durable(
                ctx.store.log_tool,
                ctx.run_id,
                {
                    "name": name,
                    "args": parsed if parsed is not None else arguments,
                    "validated_args": values,
                    "ms": round((time.monotonic() - start) * 1000),
                    "output_chars": observed_chars if source_complete else None,
                    "output_observed_chars": observed_chars,
                    "omitted_chars": omitted if source_complete else None,
                    "omitted_chars_min": omitted,
                    "result": result.model_dump(),
                    "traceback": detail,
                },
            )
        return result
