"""The core artifact boundary survives workspace membership."""

import subprocess
import tomllib
from email.parser import Parser
from pathlib import Path
from zipfile import ZipFile


def test_core_wheel_excludes_pack_code_and_pack_dependencies(tmp_path):
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["uv", "build", "--offline", "--out-dir", str(tmp_path)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    with ZipFile(next(tmp_path.glob("solakit-*.whl"))) as wheel:
        assert not any(name.startswith("sola_packs/") for name in wheel.namelist())
        metadata = Parser().parsestr(
            wheel.read(
                next(
                    name
                    for name in wheel.namelist()
                    if name.endswith(".dist-info/METADATA")
                )
            ).decode()
        )
        assert not any(
            requirement.lower().replace("_", "-").startswith("solakit-packs-")
            for requirement in metadata.get_all("Requires-Dist", [])
        )
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert not any(
        dep.startswith("solakit-packs-") for dep in project["project"]["dependencies"]
    )
