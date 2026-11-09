"""Typed seams the runtime injects into ToolContext (documentation for pack
authors and type checkers; runtime behaviour does not depend on them)."""

from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol


class JobManagerPort(Protocol):
    jobs: dict[str, Any]

    def job(self, job_id: str) -> Any: ...
    def get(self, job_id: str) -> dict: ...
    async def create(self, job: Any, *, authorize=None) -> dict: ...
    async def update(self, job_id: str, changes: dict, *, authorize=None) -> dict: ...
    async def trigger(self, job_id: str, *, authorize=None) -> dict: ...
    async def pause(self, job_id: str, paused: bool, *, authorize=None) -> dict: ...
    async def delete(self, job_id: str, *, authorize=None) -> dict: ...


class ChannelPoster(Protocol):
    async def post(self, channel: str, text: str, operation_id: str, **kwargs): ...


class NestedSubmit(Protocol):
    def __call__(
        self, session_id: str, text: str, *, profile: str, origin: Any, on_started=None
    ) -> Awaitable[Any]: ...


MemoryStager = Callable[[str], Awaitable[Any]]
ToolActivation = Callable[[Iterable[str]], None]
RuntimeStatus = Callable[[str], dict]
