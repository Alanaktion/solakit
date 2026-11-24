import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from solakit.adapters.memory import OpenViking
from solakit.adapters.model import ModelAdapter
from solakit.cli import main
from solakit.storage.store import Store


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


@pytest.mark.parametrize("host", ["127.0.0.1", "100.64.1.20"])
def test_serve_uses_configured_single_worker_and_ignores_proxy_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    import uvicorn

    config = config_file(tmp_path)
    with config.open("a") as target:
        target.write(
            f'\n[http]\nhost = "{host}"\nport = 9876\nallowed_origins = ["http://{host}:9876"]\n'
        )
    options: dict[str, Any] = {}

    def serve(app: FastAPI, **kwargs: Any) -> None:
        options.update(kwargs)
        assert app.state.context.state == "stopped"

    monkeypatch.setattr(uvicorn, "run", serve)
    monkeypatch.setattr(sys, "argv", ["sola", "serve", "--config", str(config)])
    main()
    assert options == {
        "host": host,
        "port": 9876,
        "workers": 1,
        "proxy_headers": False,
        "access_log": False,
    }
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("bad_args", [False, True])
def test_doctor_uses_synthetic_requests_and_scratch_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bad_args: bool,
) -> None:
    import httpx2

    config = config_file(tmp_path)
    (tmp_path / "private").write_text("PRIVATE IDENTITY")
    config.write_text('soul_file = "private"\n' + config.read_text())
    posts = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "fixture-qwen"}]})
        body = json.loads(request.content)
        posts.append(body)
        if body["stream"]:
            question = body["messages"][-1]["content"]
            reply = (
                question.splitlines()[0].split()[-1].rstrip(".")
                if "access code" in question
                else "ready"
            )
            delta = {"content": reply, "reasoning_content": "PRIVATE REASONING"}
            chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": "stop"}]}
            return httpx2.Response(200, content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")
        last = body["messages"][-1]
        if last["role"] == "user":
            finish = "tool_calls"
            message = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "lookup_code",
                            "arguments": json.dumps({"locker": "3" if bad_args else 3}),
                        },
                    }
                ],
            }
        else:
            assert last["tool_call_id"] == "call-1"
            finish = "stop"
            message = {"role": "assistant", "content": f"The code is {last['content']}."}
        return httpx2.Response(
            200, json={"choices": [{"finish_reason": finish, "message": message}]}
        )

    start = ModelAdapter.start

    async def mock_start(self: ModelAdapter) -> None:
        self.transport = httpx2.MockTransport(respond)
        await start(self)

    monkeypatch.setattr(ModelAdapter, "start", mock_start)
    monkeypatch.setattr(sys, "argv", ["sola", "doctor", "--json", "--config", str(config)])
    if bad_args:
        with pytest.raises(SystemExit) as exit:
            main()
        assert exit.value.code == 1
    else:
        main()
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["streaming"]["status"] == report["reasoning"]["status"] == "passed"
    assert report["tool_round_trip"]["status"] == ("failed" if bad_args else "passed")
    assert report["memory"]["status"] == report["vision"]["status"] == "disabled"
    assert report["repeated_prefix"]["status"] == "passed"
    assert posts[-1] == posts[-2]
    assert len(posts) == (5 if bad_args else 6)
    assert "PRIVATE IDENTITY" not in json.dumps(posts)
    assert "PRIVATE" not in output
    assert not (tmp_path / "data").exists()


def test_config_inspection_includes_inherited_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    old = config_file(tmp_path)
    new = tmp_path / "next.toml"
    new.write_text(old.read_text() + 'tools = ["files.read"]\n[profiles.reader]\n')
    old.write_text(old.read_text() + "tools = []\n[profiles.reader]\n")
    monkeypatch.setattr(sys, "argv", ["sola", "config", "explain", "reader", "--config", str(new)])
    main()
    explanation = json.loads(capsys.readouterr().out)
    assert explanation["visible_tools"] == ["files.read"]
    assert explanation["schema_bytes"] > 2
    monkeypatch.setattr(sys, "argv", ["sola", "config", "diff", str(old), str(new)])
    main()
    changes = json.loads(capsys.readouterr().out)
    assert changes["default"] == changes["reader"]
    assert changes["reader"]["before"]["visible_tools"] == []
    assert changes["reader"]["after"]["visible_tools"] == ["files.read"]
    assert not (tmp_path / "data").exists()


def test_quick_doctor_preserves_vision_result_on_model_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import httpx2

    config = config_file(tmp_path)
    with config.open("a") as target:
        target.write('[vision]\npin = "fixture-vision"\nbase_url = "http://vision.test/v1"\n')

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET" and request.url.path.endswith("/models")
        return httpx2.Response(200, json={"data": [{"id": "fixture-vision"}]})

    start = ModelAdapter.start

    async def mock_start(self: ModelAdapter) -> None:
        self.transport = httpx2.MockTransport(respond)
        await start(self)

    monkeypatch.setattr(ModelAdapter, "start", mock_start)
    monkeypatch.setattr(
        sys, "argv", ["sola", "doctor", "--quick", "--json", "--config", str(config)]
    )
    with pytest.raises(SystemExit) as exit:
        main()
    assert exit.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["model"] == {"status": "failed", "error": "model_mismatch"}
    assert report["vision"] == {"status": "available", "pin": "fixture-vision"}
    assert report["streaming"]["status"] == report["tool_round_trip"]["status"] == "skipped"
    assert "repeated_prefix" not in report and "reasoning" not in report
    assert not (tmp_path / "data").exists()
