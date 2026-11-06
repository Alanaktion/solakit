"""Configured tool discovery, typed arguments, and enforced capabilities."""

import asyncio
import importlib
import inspect
import json
import re
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from solakit.core.config import Config, Profile
from solakit.core.io import durable
from solakit.core.schemas import Session
from solakit.core.store import Store
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
    nested_submit: Any = None
    stage_memory: Any = None
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
        candidate = Path(path).expanduser().resolve()
        profile = self.config.profiles[self.profile]
        scopes = (
            profile.write_scopes
            if write
            else (profile.read_scopes + profile.write_scopes)
        )
        roots = [scope_root(scope, self.config) for scope in scopes]
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


@dataclass(frozen=True)
class Manifest:
    name: str
    profiles: tuple[str, ...]
    writes: tuple[str, ...]
    timeout: float | None
    manages_timeout: bool = False


def tool(
    *,
    name: str,
    profiles: list[str],
    writes: list[str] = (),
    timeout: float | None = None,
    manages_timeout: bool = False,
):
    def decorate(function: Callable) -> Callable:
        function.__sola_tool__ = Manifest(
            name, tuple(profiles), tuple(writes), timeout, manages_timeout
        )
        return function

    return decorate


@dataclass
class Registered:
    function: Callable
    manifest: Manifest
    arguments: type[BaseModel]

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.manifest.name,
                "description": inspect.getdoc(self.function) or "",
                "parameters": self.arguments.model_json_schema(),
            },
        }


class Registry:
    def __init__(self, config: Config):
        self.config = config
        self.tools: dict[str, Registered] = {}
        self.runtime_status = None
        self.job_manager = None
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

    def allowed(self, name: str, profile: str) -> bool:
        registered = self.tools.get(name)
        cfg = self.config.profiles[profile]
        return (
            registered is not None
            and (name in cfg.tools or "*" in cfg.tools)
            and (
                profile in registered.manifest.profiles
                or "*" in registered.manifest.profiles
            )
        )

    def tool_names(self, profile: str) -> list[str]:
        """Expand the all-tools grant without changing operator configuration."""
        names = []
        for name in self.config.profiles[profile].tools:
            if name == "*":
                names.extend(name for name in self.tools if self.allowed(name, profile))
            else:
                names.append(name)
        return list(dict.fromkeys(names))

    def check_writes(self, registered: Registered, profile: Profile) -> None:
        for scope in registered.manifest.writes:
            if scope == "filesystem":
                if not profile.write_scopes:
                    raise ValueError("filesystem tool requires explicit write roots")
            elif scope.startswith(("repo:", "path:", "collection:")):
                if scope not in profile.write_scopes:
                    raise ValueError(f"missing filesystem grant: {scope}")
            elif scope not in profile.capabilities:
                raise ValueError(f"missing capability: {scope}")

    def validate(self) -> None:
        for name, profile in self.config.profiles.items():
            for scope in profile.read_scopes + profile.write_scopes:
                if scope_root(scope, self.config) is None:
                    raise ValueError(f"unknown resource scope: {scope}")
            for tool_name in profile.tools:
                if tool_name != "*" and not self.allowed(tool_name, name):
                    raise ValueError(f"tool {tool_name} is not available to {name}")
            for tool_name in self.tool_names(name):
                self.check_writes(self.tools[tool_name], profile)

    def catalog(self) -> dict:
        """Loaded tools and effective grants, shared by diagnostics and API."""
        return {
            "modules": list(self.config.enabled_tools),
            "distributions": list(self.distributions),
            "profiles": {
                name: [schema["function"]["name"] for schema in self.schemas(name)]
                for name in sorted(self.config.profiles)
            },
            "tools": [
                {
                    "name": name,
                    "description": inspect.getdoc(registered.function) or "",
                    "parameters": registered.schema()["function"]["parameters"],
                    "profiles": [
                        profile
                        for profile in sorted(self.config.profiles)
                        if self.allowed(name, profile)
                    ],
                    "writes": list(registered.manifest.writes),
                }
                for name, registered in sorted(self.tools.items())
            ],
        }

    def effective_tools(
        self, profile: str, tools: list[str] | None = None
    ) -> list[str]:
        """A task may narrow profile grants, never replace or expand them."""
        selected = None if tools is None else set(tools)
        return [
            name
            for name in self.tool_names(profile)
            if self.allowed(name, profile) and (selected is None or name in selected)
        ]

    def schemas(self, profile: str, tools: list[str] | None = None) -> list[dict]:
        return [
            self.tools[name].schema() for name in self.effective_tools(profile, tools)
        ]

    async def dispatch(self, name: str, arguments: str, ctx: ToolContext) -> ToolResult:
        start = time.monotonic()
        result = ToolResult(content="Tool execution failed", error=True)
        parsed = None
        detail = None
        repairable = False
        try:
            if ctx.tool_allowlist is not None and name not in ctx.tool_allowlist:
                repairable = True
                raise ToolError("Tool is not permitted for this run")
            if not self.allowed(name, ctx.profile):
                repairable = True
                raise ToolError("Tool is not permitted in this profile")
            registered = self.tools[name]
            ctx.runtime_provider = self.runtime_status
            ctx.job_manager = self.job_manager
            cfg = self.config.profiles[ctx.profile]
            self.check_writes(registered, cfg)
            repairable = True
            parsed = json.loads(arguments)
            values = registered.arguments.model_validate(parsed).model_dump()
            repairable = False
            timeout = min(
                registered.manifest.timeout or cfg.tool_timeout_s, cfg.tool_timeout_s
            )
            # Trusted tools that own a multi-stage operation can enforce their
            # own bound and return operation-specific timeout receipts/evidence.
            async with asyncio.timeout(
                None if registered.manifest.manages_timeout else timeout
            ):
                if inspect.iscoroutinefunction(registered.function):
                    returned = await registered.function(ctx=ctx, **values)
                else:
                    returned = await asyncio.to_thread(
                        registered.function, ctx=ctx, **values
                    )
            result = (
                returned
                if isinstance(returned, ToolResult)
                else ToolResult(content=str(returned))
            )
        except (ToolError, ValidationError, ValueError, TimeoutError) as exc:
            if isinstance(exc, TimeoutError):
                message = "Tool timed out"
            elif isinstance(exc, ToolError):
                message = str(exc)
            elif isinstance(exc, ValidationError):
                # Locations can contain attacker-supplied extra keys. Only expose
                # declared field names and numeric array positions, never values,
                # custom validator messages or arbitrary extra-key text.
                fields = registered.arguments.model_fields
                issues = []
                for issue in exc.errors(include_input=False, include_url=False)[:8]:
                    location = (
                        ".".join(
                            str(part)
                            if isinstance(part, int) or part in fields
                            else "<argument>"
                            for part in issue["loc"]
                        )
                        or "arguments"
                    )
                    issues.append(f"{location}: {issue['type']}")
                message = "Invalid tool arguments: " + "; ".join(issues)
            elif isinstance(exc, json.JSONDecodeError):
                message = "Invalid tool arguments: expected a JSON object"
            else:
                message = "Invalid tool arguments or capability grant"
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
