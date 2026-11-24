import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from test_jobs import Clock, job
from test_run_loop import call as model_call
from test_run_loop import harness, reply
from test_tools import changed, opened

from solakit.config import EffectiveProfile, OperatorConfig, ShellSettings, resolve_profile
from solakit.records import ErrorCode, Origin, RunState, ToolReceipt, new_id
from solakit.tools.shell import MAX_STREAM_BYTES


@pytest.mark.parametrize(
    "case", ["disabled", "wildcard", "revoked", "disabled-later", "old", "revoked-at-launch"]
)
def test_shell_admission_and_dispatch_denials(tmp_path: Path, case: str) -> None:
    with opened(
        tmp_path,
        grants=["shell.*" if case == "wildcard" else "shell.exec"],
        shell_enabled=case != "disabled",
    ) as h:
        if case == "revoked":
            assert len(h.session.schemas()) == 1
            h.dispatcher.current = lambda _: changed(h.profile, shell_granted=False)
        elif case == "disabled-later":
            h.dispatcher.current = lambda _: changed(h.profile, shell=ShellSettings())
        elif case == "old":
            h.session.admitted = EffectiveProfile.model_validate(
                h.profile.model_dump(exclude={"shell", "shell_granted"})
            )
        if case == "revoked-at-launch":
            checked = 0

            def revoke(_: EffectiveProfile) -> EffectiveProfile:
                nonlocal checked
                checked += 1
                return changed(h.profile, shell_granted=False) if checked > 1 else h.profile

            h.dispatcher.current = revoke
        else:
            assert h.session.schemas() == ()
        receipt = h.dispatch("shell.exec", argv=["/bin/mkdir", "unexpected"])
        assert receipt.status == "denied" and receipt.effect == "none"
        assert receipt.failure and receipt.failure.code == ErrorCode.TOOL_DENIED
        assert not (h.root / "unexpected").exists()


def test_shell_denied_for_scheduled_run(profile: EffectiveProfile) -> None:
    async def check() -> None:
        selected = changed(profile, shell=ShellSettings(enabled=True), shell_granted=True)
        context, requests = harness(
            selected,
            [
                reply(calls=[model_call("shell.exec", '{"argv":["pwd"]}')]),
                reply("Command was denied."),
            ],
        )
        assert context.jobs and context.runs and context.store
        clock = Clock()
        context.jobs.clock = clock
        await context.start()
        try:
            definition = job(context, clock, origin=Origin.SCHEDULED, tools=None)
            occurrence = context.jobs.manual(definition.id, request_id=new_id())
            assert occurrence.run_id
            run = await context.runs.wait(occurrence.run_id, timeout=5)
            assert run.state == RunState.FAILED
            assert all(
                tool["function"]["name"] != "shell.exec"
                for request in requests
                for tool in request.get("tools", [])
            )
            receipt = next(
                record
                for _, record in context.store.run_records(run.id)
                if isinstance(record, ToolReceipt)
            )
            assert receipt.status == "denied" and receipt.effect == "none"
        finally:
            await context.stop()

    asyncio.run(check())


@pytest.mark.parametrize(
    ("parent", "child", "allowed"),
    [
        (["shell.exec"], None, True),
        (["shell.exec"], ["shell.exec"], True),
        (["shell.exec"], ["shell.*"], False),
        (["shell.*"], ["shell.exec"], False),
    ],
)
def test_shell_literal_grant_inheritance(
    tmp_path: Path, parent: list[str], child: list[str] | None, allowed: bool
) -> None:
    profile = resolve_profile(
        OperatorConfig.model_validate(
            {
                "model": {"pin": "fixture"},
                "shell": {"enabled": True},
                "defaults": {"workspace_roots": [str(tmp_path)], "tools": parent},
                "profiles": {"child": {"tools": child}, "leaf": {"extends": "child"}},
            }
        ),
        "leaf",
    )
    assert ("shell.exec" in profile.tools) is allowed
    assert profile.shell_granted is allowed


def test_shell_literal_arguments_cwd_stdin_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLA_TEST_CREDENTIAL", "private-fixture-value")
    monkeypatch.setenv("PATH", str(tmp_path))
    with opened(tmp_path, shell_enabled=True) as h:
        target = h.root / "relative folder"
        target.mkdir()
        receipt = h.dispatch("shell.exec", argv=["pwd"])
        assert receipt.status == "succeeded" and isinstance(receipt.result, str)
        assert json.loads(receipt.result)["stdout"] == f"{h.root}\n"
        literals = [";$HOME", "$(touch unexpected)", "*", "'", "", "café"]
        receipt = h.dispatch(
            "shell.exec",
            cwd=target.name,
            argv=[
                sys.executable,
                "-c",
                "import os, sys, json; print(json.dumps([os.getcwd(), sys.argv[1:], "
                "sys.stdin.read(), os.getenv('SOLA_TEST_CREDENTIAL'), os.getenv('PATH')])); "
                "print('diagnostic', file=sys.stderr)",
                *literals,
            ],
        )
        assert receipt.status == "succeeded" and receipt.effect == "confirmed"
        assert isinstance(receipt.result, str)
        result = json.loads(receipt.result)
        assert result["exit_code"] == 0 and result["stderr"] == "diagnostic\n"
        assert json.loads(result["stdout"]) == [
            str(target),
            literals,
            "",
            None,
            "/usr/bin:/bin:/usr/sbin:/sbin",
        ]
        assert not (target / "unexpected").exists()


def test_shell_denies_invalid_or_managed_working_directories(tmp_path: Path) -> None:
    with opened(tmp_path, shell_enabled=True) as h:
        protected = h.root / "managed"
        protected.mkdir()
        (h.root / "linked").symlink_to(protected, target_is_directory=True)
        (h.root / ".git").mkdir()
        (h.root / "file").touch()
        h.dispatcher.protected += (protected,)
        for cwd in (
            "linked",
            "linked/child",
            str(protected),
            ".git",
            "missing",
            "file",
            "..",
            str(tmp_path),
        ):
            receipt = h.dispatch("shell.exec", argv=["pwd"], cwd=cwd)
            assert receipt.status == "denied" and receipt.effect == "none", (cwd, receipt)


def test_shell_pins_validated_directory_through_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = asyncio.create_subprocess_exec
    with opened(tmp_path, shell_enabled=True) as h:
        directory, moved = h.root / "project", h.root / "renamed"
        directory.mkdir()

        async def swap(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            directory.rename(moved)
            directory.symlink_to(h.profile.data_dir, target_is_directory=True)
            return await spawn(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", swap)
        receipt = h.dispatch("shell.exec", argv=["pwd"], cwd=directory.name)
        assert receipt.status == "succeeded" and isinstance(receipt.result, str)
        assert json.loads(receipt.result)["stdout"] == f"{moved}\n"


def test_shell_stream_caps_exit_failure_and_spawn_failure(tmp_path: Path) -> None:
    with opened(tmp_path, shell_enabled=True) as h:
        receipt = h.dispatch(
            "shell.exec",
            argv=[
                sys.executable,
                "-c",
                "import sys; print('x' * 200_000); print('y' * 200_000, file=sys.stderr)",
            ],
        )
        assert receipt.status == "succeeded" and isinstance(receipt.result, str)
        output = json.loads(receipt.result)
        assert output["stdout"] == "x" * MAX_STREAM_BYTES and output["stdout_truncated"]
        assert output["stderr"] == "y" * MAX_STREAM_BYTES and output["stderr_truncated"]

        secret = "fixture-secret-file-content"
        h.dispatcher.redactor.register(secret)
        (h.root / "diagnostic").write_text(secret)
        receipt = h.dispatch(
            "shell.exec",
            argv=[
                sys.executable,
                "-c",
                "import sys; sys.stderr.write(open('diagnostic').read()); sys.exit(126)",
            ],
        )
        assert receipt.status == "failed" and receipt.effect == "confirmed"
        assert (
            receipt.failure
            and receipt.failure.message == "Local program exited with code 126: <redacted>"
        )
        assert all(secret.encode() not in path.read_bytes() for path in h.store.log.paths())

        receipt = h.dispatch("shell.exec", argv=["./nonexistent-fixture-program"])
        assert receipt.status == "failed" and receipt.effect == "none"
        assert receipt.failure and receipt.failure.message == "Local program could not be started"

        for argv in ([], [""], ["a\x00b"], ["a" * (32 * 1024)], ["pwd"] * 129):
            receipt = h.dispatch("shell.exec", argv=argv)
            assert receipt.failure and receipt.failure.code == ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("operation", ["exit", "cancel", "timeout"])
def test_shell_reaps_group_and_records_outcome(tmp_path: Path, operation: str) -> None:
    with opened(
        tmp_path,
        shell_enabled=True,
        budgets={"tool_timeout_seconds": 1 if operation == "timeout" else 5},
    ) as h:
        pids = h.root / "pids"
        call = h.call(
            "shell.exec",
            argv=[
                sys.executable,
                "-c",
                "import os, subprocess, time; from pathlib import Path; "
                "child = subprocess.Popen(['/bin/sleep', '60']); "
                "Path('pids').write_text(f'{os.getpid()} {child.pid}\\n')"
                + ("; time.sleep(60)" if operation != "exit" else ""),
            ],
        )

        async def check() -> None:
            running = asyncio.create_task(h.session.dispatch(call))
            async with asyncio.timeout(5):
                while not pids.exists() or not pids.read_text().endswith("\n"):
                    assert not running.done()
                    await asyncio.sleep(0.01)
            records = [record for _, record in h.store.run_records(h.step.run_id)]
            assert call in records
            if operation == "cancel":
                assert not any(isinstance(record, ToolReceipt) for record in records)
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
            else:
                await running
            receipt = next(
                record
                for _, record in h.store.run_records(h.step.run_id)
                if isinstance(record, ToolReceipt)
            )
            assert receipt.tool_call_id == call.id
            if operation == "exit":
                assert receipt.status == "succeeded" and receipt.effect == "confirmed"
            else:
                assert receipt.status == "uncertain" and receipt.effect == "uncertain"
                assert receipt.failure and receipt.failure.code == ErrorCode.EFFECT_UNCERTAIN
                assert "Inspect the workspace before another attempt" in receipt.failure.message

            def group_exists() -> bool:
                try:
                    os.killpg(int(pids.read_text().split()[0]), 0)
                except ProcessLookupError:
                    return False
                return True

            async with asyncio.timeout(5):
                while group_exists():
                    await asyncio.sleep(0.01)

        asyncio.run(check())
