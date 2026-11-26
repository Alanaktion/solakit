import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from solakit import cli, service


def write_config(path: Path) -> None:
    path.write_text(
        'data_dir = "data"\n[model]\npin = "fixture-qwen"\n[defaults]\nworkspace_roots = ["."]\n'
    )


def test_service_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.toml"
    write_config(config)
    other = tmp_path / "other.toml"
    write_config(other)
    alias = tmp_path / "alias.toml"
    alias.symlink_to(config)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os, "getuid", lambda: 501)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", config)

    loaded: dict[str, str] = {}
    enabled: set[str] = set()
    unloading: set[str] = set()
    calls: list[str] = []
    failure = ""

    def launchctl(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        executable, action, target, *paths = argv
        assert executable == "/bin/launchctl" and target.startswith("gui/501")
        calls.append(action)
        if failure == action:
            return subprocess.CompletedProcess(argv, 112, "", "GUI domain unavailable")
        if action == "print":
            if target in unloading:
                unloading.remove(target)
                return subprocess.CompletedProcess(argv, 0, "\tstate = SIGTERMed\n", "")
            if target in loaded:
                return subprocess.CompletedProcess(argv, 0, loaded[target], "")
            return subprocess.CompletedProcess(argv, 113, "", "Could not find specified service")
        if action == "enable":
            enabled.add(target)
        elif action == "disable":
            enabled.discard(target)
        elif action == "bootstrap":
            definition = plistlib.loads(Path(paths[0]).read_bytes())
            service_target = f"{target}/{definition['Label']}"
            assert service_target in enabled and service_target not in loaded
            assert service_target not in unloading
            loaded[service_target] = "\tstate = running\n\tpid = 123\n"
        elif action == "bootout":
            del loaded[target]
            if target in enabled:
                unloading.add(target)
        else:
            pytest.fail(f"unexpected launchctl command {action}")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", launchctl)
    cli.main(["service", "status"])
    assert capsys.readouterr().out.startswith("not created:")
    with pytest.raises(SystemExit) as error:
        cli.main(["service", "restart"])
    assert error.value.code == 2
    assert "run sola service create" in capsys.readouterr().err
    cli.main(["service", "create"])
    plist_dir = tmp_path / "Library" / "LaunchAgents"
    (plist,) = plist_dir.glob("*.plist")
    definition = plistlib.loads(plist.read_bytes())
    assert definition["Disabled"] is True and definition["KeepAlive"] is True
    assert definition["ProgramArguments"] == [
        sys.executable,
        "-EP",
        "-u",
        "-m",
        "solakit.service",
        str(config.resolve()),
    ]
    assert definition["WorkingDirectory"] == str(config.resolve().parent)
    cli.main(["service", "create", "--config", alias.name])
    assert "Already installed:" in capsys.readouterr().out
    assert list(plist_dir.glob("*.plist")) == [plist]
    assert not calls

    cli.main(["service", "create", "--config", other.name])
    assert len(list(plist_dir.glob("*.plist"))) == 2
    failure = "bootstrap"
    with pytest.raises(SystemExit) as error:
        cli.main(["service", "start"])
    assert error.value.code == 2 and not loaded
    assert "launchctl bootstrap failed" in capsys.readouterr().err
    failure = ""
    cli.main(["service", "start", "--config", alias.name])
    cli.main(["service", "start"])
    cli.main(["service", "restart", "--config", other.name])
    assert len(loaded) == 2 and calls.count("bootstrap") == 3
    capsys.readouterr()
    cli.main(["service", "status"])
    status = capsys.readouterr().out
    assert status.startswith("running (PID 123):") and definition["StandardErrorPath"] in status

    target = f"gui/501/{definition['Label']}"
    failure = "bootout"
    with pytest.raises(SystemExit) as error:
        cli.main(["service", "restart"])
    assert error.value.code == 2
    assert calls[-3:] == ["enable", "print", "bootout"]
    assert "launchctl bootout failed" in capsys.readouterr().err
    failure = ""
    cli.main(["service", "restart", "--config", alias.name])
    assert calls[-6:] == ["enable", "print", "bootout", "print", "print", "bootstrap"]
    assert target in loaded and target in enabled and len(loaded) == 2
    assert capsys.readouterr().out.startswith("Restart requested:")

    loaded[target] = "\tstate = spawn scheduled\n"
    cli.main(["service", "status"])
    assert capsys.readouterr().out.startswith("loaded:")
    config.unlink()
    cli.main(["service", "stop"])
    cli.main(["service", "stop"])
    assert target not in enabled and len(loaded) == 1 and calls.count("bootout") == 3
    capsys.readouterr()
    cli.main(["service", "status"])
    assert capsys.readouterr().out.startswith("stopped:")

    failure = "print"
    with pytest.raises(SystemExit) as error:
        cli.main(["service", "status"])
    assert error.value.code == 2
    assert "launchctl print failed (exit 112): GUI domain unavailable" in capsys.readouterr().err


def test_uv_launch_requires_private_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "private file.env"
    env_file.write_text('export SOLA_OPERATOR_PASSWORD="fixture secret"\n')
    uv = "/absolute/bin/uv"
    commands: list[list[str]] = []

    def exec_uv(executable: str, argv: list[str]) -> None:
        assert executable == uv
        commands.append(argv)
        raise SystemExit(0)

    monkeypatch.setattr(os, "execv", exec_uv)
    env_file.chmod(0o644)
    with pytest.raises(SystemExit, match="private readable file"):
        service.run_service("fixture.toml", str(env_file), uv)
    assert not commands

    env_file.chmod(0o600)
    with pytest.raises(SystemExit) as error:
        service.run_service("fixture.toml", str(env_file), uv)
    assert error.value.code == 0
    (command,) = commands
    assert command[:3] == [uv, "run", "--quiet"]
    assert "--no-project" in command
    assert command[command.index("--env-file") + 1] == str(env_file).replace(" ", r"\ ")
    assert command[command.index("--") + 1 :] == service.runtime_command("fixture.toml")
    assert "fixture secret" not in " ".join(command)
