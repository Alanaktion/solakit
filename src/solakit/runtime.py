"""One explicit owner for runtime resources and the shared inference permit."""

import asyncio
import fcntl
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from solakit.config import EffectiveProfile
from solakit.evidence import ensure_directory
from solakit.store import Store

if TYPE_CHECKING:
    from solakit.blog import BlogService
    from solakit.dispatch import Dispatcher
    from solakit.images import ImageService
    from solakit.jobs import JobService
    from solakit.memory import MemoryService
    from solakit.model import ModelAdapter
    from solakit.run_loop import RunLoop


class LifecycleError(RuntimeError):
    pass


class OwnershipError(LifecycleError):
    pass


class DataDirectoryLock:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        ensure_directory(self.directory)
        fd = os.open(
            self.directory / ".owner.lock",
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise OwnershipError("data directory already has an active owner") from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


class Resource(Protocol):
    """Adapters own their tasks/handles; stop must also tolerate a failed start."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


@dataclass
class ApplicationContext:
    profile: EffectiveProfile
    resources: tuple[Resource, ...] = ()
    store: Store | None = None
    model: ModelAdapter | None = None
    tools: Dispatcher | None = None
    runs: RunLoop | None = None
    memory: MemoryService | None = None
    jobs: JobService | None = None
    images: ImageService | None = None
    blog: BlogService | None = None
    inference: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1), init=False)
    state: str = field(default="stopped", init=False)
    _active: list[Resource] = field(default_factory=list, init=False, repr=False)
    _lifecycle: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _cleanup: asyncio.Task[list[Exception]] | None = field(default=None, init=False, repr=False)
    _owner: DataDirectoryLock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._owner = DataDirectoryLock(self.profile.data_dir)

    async def start(self) -> None:
        async with self._lifecycle:
            if self.state == "running":
                return
            if self.state != "stopped":
                raise LifecycleError("stop must finish successfully before restart")
            self.state = "starting"
            try:
                self._owner.acquire()
                for resource in self.resources:
                    self._active.append(resource)
                    await resource.start()
            except BaseException as startup_error:
                try:
                    await self._stop()
                except Exception as cleanup_error:
                    raise BaseExceptionGroup(
                        "startup and cleanup failed", [startup_error, cleanup_error]
                    ) from None
                raise
            self.state = "running"

    async def _close_resources(self) -> list[Exception]:
        errors = []
        for resource in reversed(tuple(self._active)):
            try:
                await resource.stop()
            except Exception as error:
                errors.append(error)
            else:
                self._active.remove(resource)
        return errors

    async def _stop(self) -> None:
        self.state = "stopping"
        if self._cleanup is not None and not self._cleanup.done():
            self.state = "failed"
            raise LifecycleError("previous shutdown is still cancelling; ownership retained")
        self._cleanup = asyncio.create_task(self._close_resources())
        try:
            done, _ = await asyncio.wait(
                (self._cleanup,), timeout=self.profile.shutdown_timeout_seconds
            )
            if not done:
                self._cleanup.cancel()
                raise LifecycleError("shutdown deadline exceeded; ownership retained")
            errors = self._cleanup.result()
            if errors:
                raise ExceptionGroup("resource shutdown failed; ownership retained", errors)
        except BaseException:
            self._cleanup.cancel()
            self.state = "failed"
            raise
        self._owner.release()
        self.state = "stopped"

    async def stop(self) -> None:
        async with self._lifecycle:
            if self.state != "stopped":
                await self._stop()


def compose(
    profile: EffectiveProfile,
    *,
    resources: tuple[Resource, ...] = (),
    current_profile: Callable[[EffectiveProfile], EffectiveProfile] | None = None,
) -> ApplicationContext:
    """Resources are supplied in dependency order, closed in reverse order."""
    from solakit.blog import BlogService
    from solakit.dispatch import Dispatcher
    from solakit.images import ImageService
    from solakit.jobs import JobService
    from solakit.memory import MemoryService, OpenViking
    from solakit.model import ModelAdapter
    from solakit.redaction import Redactor
    from solakit.registry import load_registry
    from solakit.run_loop import RunLoop

    store = Store(profile.data_dir)
    context = ApplicationContext(profile=profile, store=store)
    redactor = Redactor()
    context.model = ModelAdapter(profile, store, context.inference, redactor=redactor)

    def current(admitted: EffectiveProfile) -> EffectiveProfile:
        from solakit.config import ConfigError

        if admitted.name != context.profile.name:
            raise ConfigError("profile requires an operator configuration resolver")
        return context.profile.model_copy(
            update={
                "origin": admitted.origin,
                "mode": admitted.mode,
                "memory_mode": context.profile.memory_mode
                if admitted.mode.value == "shared"
                else "off",
            }
        )

    context.tools = Dispatcher(
        store,
        load_registry(profile.tool_modules),
        current_profile or current,
        redactor=redactor,
    )
    context.memory = MemoryService(
        store,
        OpenViking(profile.memory, redactor),
        context.tools.current,
    )
    context.tools.memory = context.memory
    context.images = ImageService(store, profile.images)
    context.tools.images = context.images
    context.blog = BlogService()
    context.tools.blog = context.blog
    context.runs = RunLoop(store, context.model, context.tools, memory=context.memory)
    context.jobs = JobService(store, context.runs, lambda: context.profile)
    context.tools.jobs = context.jobs
    context.runs.jobs = context.jobs
    context.resources = (
        store,
        context.model,
        context.memory,
        context.images,
        *resources,
        context.runs,
        context.jobs,
    )
    return context
