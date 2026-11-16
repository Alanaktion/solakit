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
        command = subprocess.run(
            [str(environment / "bin/sola"), "check", "--explain"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(command.stdout)["phase"] == "skeleton"
        subprocess.run(
            [
                str(python),
                "-c",
                (
                    "import importlib.util, sys; import solakit; "
                    "assert importlib.util.find_spec('packs') is None; "
                    "assert importlib.util.find_spec('evals') is None; "
                    "from solakit.cli import main; sys.argv = ['sola', 'check']; main(); "
                    "assert 'fastapi' not in sys.modules; assert 'openai' not in sys.modules"
                ),
            ],
            cwd=root,
            check=True,
        )
    print("Clean wheel, CLI, offline imports, and pack exclusion passed.")


if __name__ == "__main__":
    main()
