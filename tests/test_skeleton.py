import json
import subprocess
import sys

from fastapi.testclient import TestClient

from solakit.api import create_app


def test_health() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "phase": "skeleton"}


def test_cli_offline() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from solakit.cli import main; "
            "sys.argv = ['sola', 'check', '--explain']; main(); "
            "assert 'fastapi' not in sys.modules; assert 'openai' not in sys.modules",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout)["runtime_configured"] is False
