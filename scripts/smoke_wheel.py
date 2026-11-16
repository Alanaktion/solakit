"""Build and install the core wheel in a disposable clean environment."""

import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def main() -> None:
    with TemporaryDirectory(prefix="sola-wheel-") as temporary:
        root = Path(temporary)
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(root)], check=True)
        (wheel,) = root.glob("*.whl")
        with ZipFile(wheel) as archive:
            assert all(name.startswith(("solakit/", "solakit-")) for name in archive.namelist())
        environment = root / "venv"
        subprocess.run(["uv", "venv", "--python", "3.15", str(environment)], check=True)
        python = environment / "bin/python"
        constraints = root / "constraints.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--no-hashes",
                "--output-file",
                str(constraints),
                "--quiet",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--constraint",
                str(constraints),
                str(wheel),
            ],
            check=True,
        )
        config = root / "config.toml"
        config.write_text(
            'data_dir = "data"\n[model]\npin = "fixture-qwen"\n'
            '[defaults]\nworkspace_roots = ["."]\n'
        )
        command = subprocess.run(
            [str(environment / "bin/sola"), "check", "--config", str(config), "--explain"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(command.stdout)["phase"] == "model-prompts"
        assert json.loads(command.stdout)["runtime_configured"] is True
        assert not (root / "data").exists()
        prompt = subprocess.run(
            [
                str(environment / "bin/sola"),
                "prompt",
                "Verbatim fixture",
                "--config",
                str(config),
                "--at",
                "2026-11-16T12:00:00Z",
                "--hashes",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        inspection = json.loads(prompt.stdout)
        assert inspection["request"]["messages"][-1]["content"] == "Verbatim fixture"
        assert inspection["manifest"]["recall"] == "omitted"
        assert not (root / "data").exists()
        search = subprocess.run(
            [str(environment / "bin/sola"), "search", "fixture", "--data-dir", str(root / "data")],
            cwd=root,
            capture_output=True,
            text=True,
        )
        assert search.returncode == 2 and json.loads(search.stdout)["status"] == "unavailable"
        assert not (root / "data").exists()
        subprocess.run(
            [
                str(python),
                "-c",
                (
                    "import importlib.util, sys; import solakit; "
                    "assert importlib.util.find_spec('packs') is None; "
                    "assert importlib.util.find_spec('evals') is None; "
                    "from solakit.cli import main; "
                    "sys.argv = ['sola', 'check', '--config', sys.argv[1]]; main(); "
                    "assert 'fastapi' not in sys.modules; assert 'openai' not in sys.modules"
                ),
                str(config),
            ],
            cwd=root,
            check=True,
        )
    print("Clean wheel, CLI, offline imports, and pack exclusion passed.")


if __name__ == "__main__":
    main()
