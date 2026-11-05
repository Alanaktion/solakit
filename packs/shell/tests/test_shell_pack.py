import asyncio
import json
import sys

import pytest
from sola_packs import shell

from solakit.tools import ToolError


@pytest.fixture
def fixture(pack_context, tmp_path):
    registry, ctx = pack_context(
        ["sola_packs.shell"],
        ["shell.exec", "shell.run"],
        profile="maintenance",
        write=[f"path:{tmp_path}"],
        capabilities=["shell.exec"],
    )
    return registry, ctx, tmp_path


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
        ctx.config.profiles["maintenance"].capabilities = []
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
        ctx.profile = "interactive"
        with pytest.raises(ToolError, match="maintenance-only"):
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


def test_nonmaintenance_profile_cannot_grant_shell_tools(pack_context, tmp_path):
    with pytest.raises(ValueError, match="not available"):
        pack_context(
            ["sola_packs.shell"],
            ["shell.run"],
            profile="interactive",
            write=[f"path:{tmp_path}"],
            capabilities=["shell.exec"],
        )
