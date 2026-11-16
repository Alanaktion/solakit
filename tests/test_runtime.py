import asyncio
import subprocess
import sys

import pytest

from solakit.config import EffectiveProfile
from solakit.runtime import LifecycleError, OwnershipError, compose


class FakeResource:
    def __init__(self, name: str, events: list[str], *, fail: bool = False) -> None:
        self.name = name
        self.events = events
        self.fail = fail

    async def start(self) -> None:
        self.events.append("start " + self.name)
        if self.fail:
            raise RuntimeError("fixture startup failed")

    async def stop(self) -> None:
        self.events.append("stop " + self.name)


def test_repeated_start_stop_and_shared_permit(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        events: list[str] = []
        context = compose(
            profile, resources=(FakeResource("store", events), FakeResource("worker", events))
        )
        for _ in range(2):
            await asyncio.gather(context.start(), context.start())
            assert context.state == "running"
            async with context.inference:
                assert context.inference.locked()
            await asyncio.gather(context.stop(), context.stop())
            assert context.state == "stopped"
        assert events == ["start store", "start worker", "stop worker", "stop store"] * 2

    asyncio.run(scenario())


def test_failed_start_closes_partial_resources(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        events: list[str] = []
        context = compose(
            profile,
            resources=(FakeResource("store", events), FakeResource("worker", events, fail=True)),
        )
        with pytest.raises(RuntimeError, match="fixture startup"):
            await context.start()
        assert events == ["start store", "start worker", "stop worker", "stop store"]
        assert context.state == "stopped"
        replacement = compose(profile)
        await replacement.start()
        await replacement.stop()

    asyncio.run(scenario())


def test_ownership_in_process_and_subprocess(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        context = compose(profile)
        second = compose(profile)
        await context.start()
        try:
            with pytest.raises(OwnershipError):
                await second.start()
            script = (
                "import sys; from pathlib import Path; "
                "from solakit.runtime import DataDirectoryLock; "
                "DataDirectoryLock(Path(sys.argv[1])).acquire()"
            )
            result = subprocess.run(
                [sys.executable, "-c", script, str(profile.data_dir)],
                capture_output=True,
                text=True,
            )
            assert result.returncode != 0 and "OwnershipError" in result.stderr
        finally:
            await context.stop()
        await second.start()
        await second.stop()
        assert (profile.data_dir / ".owner.lock").is_file()

    asyncio.run(scenario())


def test_shutdown_timeout_retains_ownership(profile: EffectiveProfile) -> None:
    class SlowResource:
        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            await asyncio.sleep(60)

    async def scenario() -> None:
        short = EffectiveProfile.model_validate(
            profile.model_dump() | {"shutdown_timeout_seconds": 1}
        )
        resource = SlowResource()
        context = compose(short, resources=(resource,))
        await context.start()
        started = asyncio.get_running_loop().time()
        with pytest.raises(LifecycleError, match="deadline"):
            await context.stop()
        assert asyncio.get_running_loop().time() - started < 2
        with pytest.raises(OwnershipError):
            await compose(profile).start()
        with pytest.raises(LifecycleError, match="before restart"):
            await context.start()
        await asyncio.sleep(0)

        async def recovered_stop() -> None:
            pass

        resource.stop = recovered_stop  # type: ignore[method-assign]
        await context.stop()
        assert context.state == "stopped"

    asyncio.run(scenario())


def test_cleanup_error_keeps_owner_and_closes_other_resources(profile: EffectiveProfile) -> None:
    class FailingStop(FakeResource):
        async def stop(self) -> None:
            await super().stop()
            if self.fail:
                raise RuntimeError("fixture cleanup failed")

    async def scenario() -> None:
        events: list[str] = []
        resource = FailingStop("worker", events)
        context = compose(profile, resources=(FakeResource("store", events), resource))
        await context.start()
        resource.fail = True
        with pytest.raises(ExceptionGroup, match="shutdown failed"):
            await context.stop()
        assert events[-2:] == ["stop worker", "stop store"]
        with pytest.raises(OwnershipError):
            await compose(profile).start()
        resource.fail = False
        await context.stop()
        assert events.count("stop store") == 1
        replacement = compose(profile)
        await replacement.start()
        await replacement.stop()

    asyncio.run(scenario())


def test_cancelled_start_cleans_up(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        events: list[str] = []
        entered = asyncio.Event()

        class StartingResource(FakeResource):
            async def start(self) -> None:
                entered.set()
                await asyncio.sleep(60)

        context = compose(profile, resources=(StartingResource("client", events),))
        startup = asyncio.create_task(context.start())
        await entered.wait()
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup
        assert events == ["stop client"] and context.state == "stopped"
        replacement = compose(profile)
        await replacement.start()
        await replacement.stop()

    asyncio.run(scenario())
