"""Per-config LaunchAgents and their runtime entry point."""

import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep

from solakit.config import load_config, resolve_profile
from solakit.storage.evidence import publish

SERVICE_NOT_FOUND = 113  # launchctl's "Could not find specified service"
EXIT_TIMEOUT_SECONDS = 125  # HTTP drain and runtime cleanup can each take 60 seconds.


class ServiceError(RuntimeError):
    pass


def validate_env_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise ServiceError(f"cannot access environment file: {path}") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
        or not metadata.st_mode & stat.S_IRUSR
    ):
        raise ServiceError(f"environment file must be a private readable file owned by you: {path}")


def runtime_command(config: str) -> list[str]:
    return [sys.executable, "-EP", "-u", "-m", "solakit.service", config]


def launchctl(*args: str, allow_missing: bool = False) -> str | None:
    result = subprocess.run(
        ["/bin/launchctl", *args], capture_output=True, text=True, errors="replace"
    )
    if result.returncode == 0:
        return result.stdout
    if allow_missing and result.returncode == SERVICE_NOT_FOUND:
        return None
    detail = (result.stderr or result.stdout).strip()
    raise ServiceError(f"launchctl {args[0]} failed (exit {result.returncode}): {detail}")


def manage_service(action: str, config: Path, env_file: Path | None) -> None:
    if sys.platform != "darwin":
        raise ServiceError("service commands require macOS launchd")

    import fcntl

    uid = os.getuid()
    if uid == 0:
        raise ServiceError("run service commands as the macOS user who will run the agent")
    config = config.expanduser().resolve()
    label = f"org.solakit.runtime.{sha256(os.fsencode(config)).hexdigest()}"
    home = Path.home()
    plist = home / "Library" / "LaunchAgents" / f"{label}.plist"
    log_dir = home / "Library" / "Logs" / "sola" / label
    stdout, stderr = log_dir / "stdout.log", log_dir / "stderr.log"
    domain = f"gui/{uid}"
    target = f"{domain}/{label}"

    if action == "create":
        if plist.is_file():
            print(f"Already installed: {plist}")
            return
        if not config.is_file():
            raise ServiceError(f"configuration file does not exist: {config}")
        resolve_profile(load_config(config), base=config.parent)
        arguments = runtime_command(str(config))
        if env_file is not None:
            env_file = env_file.expanduser().absolute()
            validate_env_file(env_file)
            uv = shutil.which("uv")
            if uv is None:
                raise ServiceError("service create --env-file requires uv on PATH")
            arguments.extend([str(env_file), os.path.abspath(uv)])
        definition = {
            "Label": label,
            "ProgramArguments": arguments,
            "WorkingDirectory": str(config.parent),
            "Disabled": True,
            "KeepAlive": True,
            "Umask": 0o077,
            "ExitTimeOut": EXIT_TIMEOUT_SECONDS,
            "StandardOutPath": str(stdout),
            "StandardErrorPath": str(stderr),
        }
        plist.parent.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            publish(plist, plistlib.dumps(definition))
        except FileExistsError:
            print(f"Already installed: {plist}")
        else:
            print(f"Installed: {plist}")
        return

    if not plist.is_file():
        if action == "status":
            print(f"not created: {config}")
            return
        raise ServiceError(f"service has not been created for {config}; run sola service create")

    if action == "status":
        details = launchctl("print", target, allow_missing=True)
        pid = re.search(r"^\tpid = ([0-9]+)$", details or "", re.MULTILINE)
        state = "stopped" if details is None else "loaded"
        if pid:
            state = f"running (PID {pid[1]})"
        print(f"{state}: {config}\nstdout: {stdout}\nstderr: {stderr}")
        return

    with plist.open("rb") as installed:
        fcntl.flock(installed, fcntl.LOCK_EX)
        stopping = action == "stop"
        launchctl("disable" if stopping else "enable", target)
        loaded = launchctl("print", target, allow_missing=True) is not None
        if loaded and action != "start":
            launchctl("bootout", target, allow_missing=True)
            if not stopping:
                # launchctl bootout returns before launchd finishes unloading the service.
                deadline = monotonic() + EXIT_TIMEOUT_SECONDS + 5
                while launchctl("print", target, allow_missing=True) is not None:
                    if monotonic() >= deadline:
                        raise ServiceError(f"timed out waiting for launchd to unload {config}")
                    sleep(0.2)
            loaded = False
        if stopping:
            print(f"Stopped: {config}")
        else:
            if not loaded:
                launchctl("bootstrap", domain, str(plist))
            print(f"{action.capitalize()} requested: {config}")


def run_service(config: str, env_file: str | None = None, uv: str | None = None) -> None:
    if env_file is not None:
        if uv is None:
            raise SystemExit("recreate the LaunchAgent with an .env file for uv")
        try:
            validate_env_file(Path(env_file))
            # uv splits --env-file values on whitespace and treats backslashes as escapes.
            escaped_env_file = re.sub(r"([\\\s])", r"\\\1", env_file)
            os.execv(
                uv,
                [
                    uv,
                    "run",
                    "--quiet",  # dotenv parse warnings can include credential values.
                    "--no-project",
                    "--no-python-downloads",
                    "--python",
                    sys.executable,
                    "--env-file",
                    escaped_env_file,
                    "--",
                    *runtime_command(config),
                ],
            )
        except (OSError, ServiceError) as error:
            raise SystemExit(str(error)) from None

    from solakit.cli import main

    main(["serve", "--config", config])


if __name__ == "__main__":
    run_service(*sys.argv[1:])
