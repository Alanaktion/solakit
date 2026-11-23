"""Explicit registration and schema disclosure, separate from tool implementations."""

import importlib
import inspect
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from pydantic import JsonValue

from solakit.sdk import SDK_VERSION, Origin, Tool, ToolArguments
from solakit.tools import TOOLS

if TYPE_CHECKING:
    from solakit.config import EffectiveProfile


class RegistryError(ValueError):
    pass


class Registry:
    def __init__(self, tools: Iterable[Tool[Any]] = ()) -> None:
        self.tools: dict[str, Tool[Any]] = {}
        self.schemas: dict[str, dict[str, JsonValue]] = {}
        self.code_roots: list[str] = []
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool[Any]) -> None:
        if (
            not isinstance(tool, Tool)
            or not isinstance(tool.name, str)
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tool.name)
        ):
            raise RegistryError("tool requires a valid explicit name")
        if (
            not isinstance(tool.description, str)
            or not tool.description.strip()
            or type(tool.consequential) is not bool
            or not isinstance(tool.origins, frozenset)
            or any(not isinstance(origin, Origin) for origin in tool.origins)
        ):
            raise RegistryError(f"invalid tool declaration: {tool.name}")
        if tool.name in self.tools:
            raise RegistryError(f"duplicate tool: {tool.name}")
        if not inspect.iscoroutinefunction(tool.handler):
            raise RegistryError(f"tool handler must be async: {tool.name}")
        try:
            if (
                not issubclass(tool.arguments, ToolArguments)
                or tool.arguments.model_config.get("strict") is not True
            ):
                raise ValueError
            schema = tool.arguments.model_json_schema()
            if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise RegistryError(f"invalid argument schema: {tool.name}") from error
        self.tools[tool.name] = tool
        self.schemas[tool.name] = {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": schema,
            },
        }

    def load(self, modules: Iterable[str]) -> None:
        for name in modules:
            try:
                module = importlib.import_module(name)
            except ImportError as error:
                raise RegistryError(
                    f"cannot load configured module {name}: dependency unavailable"
                ) from error
            if (
                type(getattr(module, "SDK_VERSION", None)) is not int
                or module.SDK_VERSION != SDK_VERSION
            ):
                raise RegistryError(f"unsupported SDK version in module: {name}")
            declared = getattr(module, "TOOLS", None)
            if not isinstance(declared, (tuple, list)):
                raise RegistryError(f"module must declare TOOLS: {name}")
            for tool in declared:
                self.register(tool)
            location = getattr(module, "__file__", None)
            if location:
                self.code_roots.append(location)

    def eligible(self, profile: EffectiveProfile) -> frozenset[str]:
        return frozenset(
            name
            for name in profile.tools
            if (tool := self.tools.get(name)) is not None
            and profile.origin in tool.origins
            and (profile.memory_mode != "off" or not name.startswith("memory."))
        )

    def visible(
        self, executable: frozenset[str], *, deferred: frozenset[str] = frozenset()
    ) -> tuple[dict[str, JsonValue], ...]:
        return tuple(
            self.schemas[name] for name in sorted(executable - deferred) if name in self.tools
        )

    def disclose(
        self, names: frozenset[str], executable: frozenset[str]
    ) -> tuple[dict[str, JsonValue], ...]:
        """Disclosure accepts an authority ceiling; it never changes it."""
        return self.visible(names & executable)


def load_registry(modules: Iterable[str] = ()) -> Registry:
    registry = Registry(TOOLS)
    registry.load(modules)
    return registry
