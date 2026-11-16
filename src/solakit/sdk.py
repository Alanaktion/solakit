"""Supported SDK v1 for trusted, explicitly configured Python tool packs."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue

from solakit.records import ErrorCode, Origin, SessionMode, Value

__all__ = [
    "SDK_VERSION",
    "ErrorCode",
    "Origin",
    "SessionMode",
    "ToolArguments",
    "ToolResult",
    "ToolError",
    "Files",
    "Services",
    "ToolContext",
    "Tool",
]

SDK_VERSION = 1


class ToolArguments(Value):
    """Strict object arguments: no coercion and no unknown fields."""


class ToolResult(Value):
    data: JsonValue = None


class ToolError(Exception):
    """Recoverable error; message must be safe to show to the model/operator."""

    def __init__(self, message: str, code: ErrorCode = ErrorCode.TOOL_FAILED) -> None:
        super().__init__(message)
        self.code = code


class Files(Protocol):
    def read_text(self, path: str) -> str: ...

    def write_text(
        self, path: str, content: str, *, overwrite: bool = False
    ) -> dict[str, JsonValue]: ...


class Services(Protocol):
    def transcript_search(self, query: str, *, limit: int = 20) -> JsonValue: ...

    def status(self) -> JsonValue: ...

    async def recall(self, query: str) -> JsonValue: ...


@dataclass(frozen=True)
class ToolContext:
    session_id: str
    run_id: str
    origin: Origin
    mode: SessionMode
    files: Files
    services: Services
    begin_effect: Callable[[tuple[str, ...]], None]
    confirm_effect: Callable[[], None]


@dataclass(frozen=True)
class Tool[A: ToolArguments]:
    name: str
    description: str
    arguments: type[A]
    handler: Callable[[ToolContext, A], Awaitable[ToolResult]]
    consequential: bool = False
    origins: frozenset[Origin] = frozenset(Origin)
