import asyncio
import json
import sys

import pytest

from solakit.core.config import Profile
from solakit.core.schemas import Session
from solakit.core.store import Store
from solakit.tools import Registry, ToolContext, ToolError, shell


@pytest.fixture
def fixture(config, tmp_path):
    config.enabled_tools = ["shell"]
    config.profiles = {
        "maintenance": Profile(
            tools=["shell.exec", "shell.run"],
            write_scopes=[f"path:{tmp_path}"],
            capabilities=["shell.exec"],
        )
    }
    store = Store(config.paths.data)
    session = store.create_session(Session(id="shell", default_profile="maintenance"))
    ctx = ToolContext(session, "maintenance", config, store, "run")
    return Registry(config), ctx, tmp_path


def test_argv_literal_shell_pipeline_and_nonzero_status(fixture):
    registry, ctx, directory = fixture

    async def exercise():
        literal = "; touch should-not-exist"
        result = await registry.dispatch(
            "shell.exec",
            json.dumps(
                {
                    "argv": ["/bin/echo", literal],
                    "directory": str(directory),
                }
            ),
            ctx,
        )
        assert json.loads(result.content)["stdout"].strip() == literal
        assert not (directory / "should-not-exist").exists()
        result = await registry.dispatch(
            "shell.run",
            json.dumps(
                {
                    "command": "printf moon | tr a-z A-Z",
                    "directory": str(directory),
                }
            ),
            ctx,
        )
        assert json.loads(result.content)["stdout"] == "MOON"
        result = await registry.dispatch(
            "shell.run",
            json.dumps(
                {
                    "command": "printf failure >&2; exit 7",
                    "directory": str(directory),
                }
            ),
            ctx,
        )
        data = json.loads(result.content)
        assert result.error and data["exit_code"] == 7 and data["stderr"] == "failure"

    asyncio.run(exercise())


def test_timeouts_kill_descendants_and_output_is_bounded(fixture):
    registry, ctx, directory = fixture

    async def exercise():
        result = await registry.dispatch(
            "shell.run",
            json.dumps(
                {
                    "command": "(sleep 0.4; printf late > late.txt) & wait",
                    "directory": str(directory),
                    "timeout_s": 0.03,
                }
            ),
            ctx,
        )
        assert result.error and json.loads(result.content)["timed_out"]
        await asyncio.sleep(0.45)
        assert not (directory / "late.txt").exists()
        ctx.config.profiles["maintenance"].tool_output_max_chars = 2048
        result = await registry.dispatch(
            "shell.exec",
            json.dumps(
                {
                    "argv": [sys.executable, "-c", "print('x'*100000)"],
                    "directory": str(directory),
                }
            ),
            ctx,
        )
        assert not result.content_complete
        assert len(result.content) <= 2048 and "[truncated " in result.content

    asyncio.run(exercise())


def test_grants_cwd_and_environment_hygiene(fixture, monkeypatch):
    registry, ctx, directory = fixture
    monkeypatch.setenv("SOLA_FAKE_API_KEY", "do-not-inherit")

    async def exercise():
        result = await registry.dispatch(
            "shell.exec",
            json.dumps(
                {
                    "argv": [
                        sys.executable,
                        "-c",
                        "import os; print(os.getenv('SOLA_FAKE_API_KEY', 'missing'))",
                    ],
                    "directory": str(directory),
                }
            ),
            ctx,
        )
        assert json.loads(result.content)["stdout"].strip() == "missing"
        result = await registry.dispatch(
            "shell.run",
            json.dumps(
                {
                    "command": "echo denied",
                    "directory": "/",
                }
            ),
            ctx,
        )
        assert result.error and "outside profile" in result.content
        ctx.config.profiles["maintenance"].effects = []
        result = await registry.dispatch(
            "shell.run",
            json.dumps(
                {
                    "command": "echo denied",
                    "directory": str(directory),
                }
            ),
            ctx,
        )
        assert result.error
        with pytest.raises(ToolError, match="Missing effect"):
            await shell.run(ctx, "echo denied", str(directory))

    asyncio.run(exercise())


def test_cancellation_kills_process_group_before_return(fixture):
    registry, ctx, directory = fixture

    async def exercise():
        task = asyncio.create_task(
            registry.dispatch(
                "shell.run",
                json.dumps(
                    {
                        "command": (
                            "printf started > started.txt; "
                            "(sleep 0.4; printf late > cancelled.txt) & wait"
                        ),
                        "directory": str(directory),
                    }
                ),
                ctx,
            )
        )
        async with asyncio.timeout(2):
            while not (directory / "started.txt").exists():
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.45)
        assert not (directory / "cancelled.txt").exists()

    asyncio.run(exercise())


def test_profile_grants_control_shell_access(config, tmp_path):
    config.enabled_tools = ["shell"]
    config.profiles["interactive"] = Profile(
        tools=["shell.run"],
        write_scopes=[f"path:{tmp_path}"],
        capabilities=["shell.exec"],
    )
    registry = Registry(config)
    store = Store(config.paths.data)
    session = store.create_session(Session(id="interactive"))
    ctx = ToolContext(session, "interactive", config, store, "run")

    async def exercise():
        request = json.dumps({"command": "printf allowed", "directory": str(tmp_path)})
        result = await registry.dispatch("shell.run", request, ctx)
        assert not result.error and json.loads(result.content)["stdout"] == "allowed"
        config.profiles["interactive"].tools = []
        assert (await registry.dispatch("shell.run", request, ctx)).error

    asyncio.run(exercise())


@pytest.mark.parametrize("module", ["shell", "solakit.tools.shell"])
def test_builtin_module_names(config, module):
    config.enabled_tools = [module]
    registry = Registry(config)
    assert {"shell.exec", "shell.run"} <= registry.tools.keys()
