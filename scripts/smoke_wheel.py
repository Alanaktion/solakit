"""Build and install the core wheel in a disposable clean environment."""

import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def main() -> None:
    checkout = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix="sola-wheel-") as temporary:
        root = Path(temporary)
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(root)], check=True)
        (wheel,) = root.glob("*.whl")
        with ZipFile(wheel) as archive:
            assert all(name.startswith(("solakit/", "solakit-")) for name in archive.namelist())
            assert (
                archive.read("solakit/image-workflow.json")
                == (checkout / "examples/image-workflow.json").read_bytes()
            )
            for asset in (
                "index.html",
                "chat.css",
                "chat.js",
                "app.js",
                "turn.js",
                "job.js",
                "artifact.js",
                "settings.js",
                "evidence.js",
                "client.js",
                "ui.js",
                "markdown.js",
                "lit-core.min.js",
            ):
                assert archive.read(f"solakit/web/{asset}")
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
        assert json.loads(command.stdout)["phase"] == "api-cli-events"
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
        pack_dist = root / "pack-dist"
        subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                "--project",
                str(checkout / "examples/phase5_pack"),
                "--out-dir",
                str(pack_dist),
            ],
            check=True,
        )
        (pack_wheel,) = pack_dist.glob("*.whl")
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "--no-deps", str(pack_wheel)],
            check=True,
        )
        subprocess.run([str(python), str(checkout / "scripts/pack_smoke.py")], cwd=root, check=True)
        selected = root / "selected-workspace"
        subprocess.run(
            ["uv", "sync", "--frozen", "--all-packages", "--no-dev"],
            cwd=checkout,
            env=os.environ | {"UV_PROJECT_ENVIRONMENT": str(selected)},
            check=True,
        )
        subprocess.run(
            [str(selected / "bin/python"), str(checkout / "scripts/pack_smoke.py")],
            cwd=root,
            check=True,
        )
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
    print("Clean wheel, CLI, offline imports, pack exclusion and external pack dispatch passed.")


if __name__ == "__main__":
    main()
