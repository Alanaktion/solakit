import json
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from solakit.api import create_app
from solakit.config import EffectiveProfile
from solakit.runtime import compose


def test_health(profile: EffectiveProfile) -> None:
    context = compose(profile)
    with TestClient(create_app(context)) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "running", "phase": "records-configuration"}
    assert context.state == "stopped"


def test_cli_offline(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; import solakit.config as config; "
            "config.DEFAULT_CONFIG = Path(sys.argv[1]); from solakit.cli import main; "
            "sys.argv = ['sola', 'check', '--explain']; main(); "
            "assert 'fastapi' not in sys.modules; assert 'openai' not in sys.modules",
            str(tmp_path / "missing.toml"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout)["runtime_configured"] is False
