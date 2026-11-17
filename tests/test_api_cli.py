import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from solakit.cli import main
from solakit.memory import OpenViking
from solakit.model import ModelAdapter
from solakit.store import Store


def config_file(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        'data_dir = "data"\n[model]\npin = "fixture-qwen"\n[defaults]\nworkspace_roots = ["."]\n'
    )
    return config


@pytest.mark.parametrize("failure", [False, True])
def test_check_live_is_health_only_with_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: bool,
) -> None:
    calls: list[str] = []

    async def start(self: ModelAdapter) -> None:
        calls.append("model.start")

    async def verify(self: ModelAdapter) -> dict[str, Any]:
        calls.append("model.verify")
        if failure:
            raise RuntimeError("PRIVATE_PROVIDER_ERROR")
        return {"pin": "fixture-qwen"}

    async def stop(self: ModelAdapter) -> None:
        calls.append("model.stop")

    async def memory_stop(self: OpenViking) -> None:
        calls.append("memory.stop")

    async def forbidden(self: Store) -> None:
        pytest.fail("live check must not open operational state or start background work")

    monkeypatch.setattr(ModelAdapter, "start", start)
    monkeypatch.setattr(ModelAdapter, "verify", verify)
    monkeypatch.setattr(ModelAdapter, "stop", stop)
    monkeypatch.setattr(OpenViking, "stop", memory_stop)
    monkeypatch.setattr(Store, "start", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sola", "check", "--live", "--explain", "--config", str(config_file(tmp_path))],
    )
    if failure:
        with pytest.raises(SystemExit) as exit:
            main()
        assert exit.value.code == 2
    else:
        main()
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["live"]["model"]["status"] == ("failed" if failure else "available")
    assert report["live"]["memory"]["status"] == "disabled"
    assert "PRIVATE_PROVIDER_ERROR" not in output
    assert calls == ["model.start", "model.verify", "memory.stop", "model.stop"]
    assert not (tmp_path / "data").exists()


def test_serve_uses_configured_single_worker_and_ignores_proxy_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    config = config_file(tmp_path)
    with config.open("a") as target:
        target.write(
            '\n[http]\nhost = "127.0.0.1"\nport = 9876\nallowed_origins = ["http://127.0.0.1:9876"]\n'
        )
    options: dict[str, Any] = {}

    def serve(app: FastAPI, **kwargs: Any) -> None:
        options.update(kwargs)
        assert app.state.context.state == "stopped"

    monkeypatch.setattr(uvicorn, "run", serve)
    monkeypatch.setattr(sys, "argv", ["sola", "serve", "--config", str(config)])
    main()
    assert options == {
        "host": "127.0.0.1",
        "port": 9876,
        "workers": 1,
        "proxy_headers": False,
        "access_log": False,
    }
    assert not (tmp_path / "data").exists()
