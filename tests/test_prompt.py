import hashlib
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from solakit.core.prompt import read_prompt, read_turn
from solakit.main import create_app

FIXTURES = Path(__file__).parent / "fixtures/prompt"


def test_golden_dry_run(config, auth, monkeypatch):
    config.paths.soul = FIXTURES / "SOUL.md"
    config.paths.fragments = FIXTURES
    app = create_app(config=config)
    with TestClient(app) as client:

        def forbidden(*args, **kwargs):
            raise AssertionError("inspection must not call external services")

        monkeypatch.setattr("httpx.AsyncClient.get", forbidden)
        monkeypatch.setattr("httpx.AsyncClient.post", forbidden)
        before = list(config.paths.data.rglob("*"))
        response = client.get(
            "/api/prompt",
            headers=auth,
            params={
                "profile": "interactive",
                "session": "golden",
                "origin": "api",
                "recall": "A remembered fact.",
                "at": "2026-11-04T19:00:00Z",
            },
        )
        assert response.status_code == 200
        data = response.json()
        golden = (FIXTURES / "golden.txt").read_bytes()
        assert data["prompt"].encode() == golden
        assert data["turn"].encode() == (FIXTURES / "golden-turn.txt").read_bytes()
        assert data["sha256"] == hashlib.sha256(golden).hexdigest()
        assert data["tokens"] is None
        assert data["tools"] == []
        assert list(config.paths.data.rglob("*")) == before
        assert (
            client.get(
                "/api/prompt", headers=auth, params={"profile": "../escape"}
            ).status_code
            == 422
        )


def test_live_soul_verbatim(config):
    root = config.paths.fragments
    (root / "profiles").mkdir(parents=True, exist_ok=True)
    (root / "profiles/interactive.md").write_text("")
    config.paths.soul.write_bytes(b"Persona\r\n  \n")
    kwargs = dict(
        profile="interactive",
        session="s",
        origin="api",
        at=datetime.fromisoformat("2026-11-04T19:00:00+00:00"),
    )
    first = read_prompt(config, **kwargs)
    assert first.startswith("Persona\r\n  \n\n\n---\n\n\n\n---\n\n")
    config.paths.soul.write_bytes(b"Changed live persona")
    assert read_prompt(config, **kwargs).startswith("Changed live persona")
    assert "<recall>" not in first


def test_empty_context_and_header_newline_remain_literal(config):
    from solakit.core.prompt import SEPARATOR

    kwargs = dict(
        profile="interactive",
        session="s",
        origin="api",
        at=datetime.fromisoformat("2026-11-04T19:00:00+00:00"),
    )
    absent = read_prompt(config, **kwargs)
    (config.paths.fragments / "context.md").write_bytes(b"")
    empty = read_prompt(config, **kwargs)
    assert absent.count(SEPARATOR) == 2
    assert empty.count(SEPARATOR) == 3
    assert SEPARATOR + SEPARATOR in empty
    (config.paths.fragments / "recall-header.md").write_bytes(b"Header\n")
    _, turn = read_turn(config, recall="record", **kwargs)
    assert "Header\n\n<recall>\nrecord\n</recall>" in turn


def test_front_page_live_literal_and_breakdown(config):
    from solakit.core.prompt import SEPARATOR, inspection, validate_prompt_files

    kwargs = dict(
        profile="interactive",
        session="s",
        origin="api",
        at=datetime.fromisoformat("2026-11-04T19:00:00+00:00"),
    )
    config.memory.enabled = False
    path = config.paths.fragments / "memory.md"
    absent = read_prompt(config, **kwargs)
    path.write_bytes(b"")
    empty = read_prompt(config, **kwargs)
    assert len(empty) == len(absent) + len(SEPARATOR)
    path.write_bytes(b"Synthetic current fact\r\n  \n")
    (config.paths.fragments / "context.md").write_text("Synthetic rule")
    (config.paths.fragments / "recall-header.md").write_text("Header")
    first, turn = read_turn(config, recall="Synthetic recall", **kwargs)
    sections = first.split(SEPARATOR)
    assert sections[:3] == [
        config.paths.soul.read_bytes().decode(),
        "Synthetic current fact\r\n  \n",
        "Synthetic rule",
    ]
    assert "Header\n<recall>\nSynthetic recall\n</recall>" in turn
    info = inspection(first, [])
    assert [f["name"] for f in info["fragments"]] == [
        "soul",
        "memory",
        "context",
        "profile",
        "run",
    ]
    for fragment, text in zip(info["fragments"], sections, strict=True):
        assert fragment["chars"] == len(text)
        assert fragment["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert sum(f["chars"] for f in info["fragments"]) + (len(sections) - 1) * len(
        SEPARATOR
    ) == len(first)
    path.write_text("Changed synthetic fact")
    second = read_prompt(config, **kwargs)
    assert "Changed synthetic fact" in second
    assert "<recall>" not in second
    validate_prompt_files(config)
    path.write_bytes(b"\xff")
    import pytest

    with pytest.raises(ValueError, match="memory.md"):
        validate_prompt_files(config)
    path.unlink()
    validate_prompt_files(config)


def test_front_page_cap_check_and_runtime(
    config, auth, monkeypatch, capsys, mock_model
):
    import pytest

    from solakit import cli
    from solakit.core.prompt import check_front_page, read_prompt
    from solakit.core.startup import prepare

    config.memory.front_page_max_chars = 256
    path = config.paths.fragments / "memory.md"
    path.write_text("x" * 205)
    monkeypatch.setattr(cli, "prepare", lambda: prepare(config))
    monkeypatch.setattr("sys.argv", ["sola", "check"])
    cli.main()
    assert "Warning:" in capsys.readouterr().out
    path.write_text("x" * 257)
    with pytest.raises(ValueError, match=r"memory.md: 257 characters, cap 256"):
        check_front_page(config)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert "memory.md: 257 characters, cap 256" in capsys.readouterr().err
    with TestClient(create_app(config=config, model=mock_model)) as client:
        status = client.app.state.registry.runtime_status("interactive")
        assert status["front_page"] == {"chars": 257, "cap": 256, "over": True}
        response = client.get("/api/prompt", headers=auth)
        assert response.status_code == 200
        assert "x" * 257 in response.json()["prompt"]
        assert response.json()["fragments"][1]["name"] == "memory"
        assert (
            client.post(
                "/api/sessions", headers=auth, json={"id": "oversized"}
            ).status_code
            == 201
        )
        served = client.post(
            "/api/sessions/oversized/messages", headers=auth, json={"message": "Hello"}
        )
        assert served.status_code == 200
        assert served.json()["status"] == "completed"
        assert "x" * 257 in served.json()["prompt"]
        # Assembly does not cut an oversized operator file.
        assert "x" * 257 in read_prompt(
            config,
            profile="interactive",
            session="s",
            origin="api",
            at=datetime.now().astimezone(),
        )


def test_memory_present_golden(config, tmp_path):
    import shutil

    shutil.copytree(FIXTURES, tmp_path / "prompt")
    config.paths.fragments = tmp_path / "prompt"
    config.paths.soul = config.paths.fragments / "SOUL.md"
    (config.paths.fragments / "memory.md").write_bytes(b"Synthetic front page.\r\n  \n")
    result = read_prompt(
        config,
        profile="interactive",
        session="golden",
        origin="api",
        at=datetime.fromisoformat("2026-11-04T19:00:00+00:00"),
        recall="A remembered fact.",
    )
    assert result.encode() == (FIXTURES / "memory-present.txt").read_bytes()


def test_historical_fragment_evidence(config, auth, mock_model):
    import pytest

    path = config.paths.fragments / "memory.md"
    path.write_text("Synthetic fact with a separator\n\n---\n\ninside")
    with TestClient(create_app(config=config, model=mock_model)) as client:
        assert (
            client.post(
                "/api/sessions", headers=auth, json={"id": "front-page"}
            ).status_code
            == 201
        )
        response = client.post(
            "/api/sessions/front-page/messages", headers=auth, json={"message": "Hello"}
        )
        assert response.status_code == 200
        record = response.json()
        assert record["status"] == "completed"
        params = {"run_id": record["id"]}
        before = client.get("/api/prompt", headers=auth, params=params).json()
        assert [f["name"] for f in before["fragments"]] == [
            "soul",
            "memory",
            "profile",
            "run",
        ]
        assert before["fragments"][1]["chars"] == len(path.read_text())
        path.unlink()
        assert client.get("/api/prompt", headers=auth, params=params).json() == before
        with pytest.raises(FileExistsError):
            client.app.state.store.write_prompt(record["id"], "replacement")


def test_memory_present_legacy_layout_is_unchanged(config, tmp_path):
    import shutil

    shutil.copytree(FIXTURES, tmp_path / "prompt")
    config.paths.fragments = tmp_path / "prompt"
    config.paths.soul = config.paths.fragments / "SOUL.md"
    config.profiles["interactive"].prompt_layout = "legacy"
    (config.paths.fragments / "memory.md").write_bytes(b"Synthetic front page.\r\n  \n")
    prompt, turn = read_turn(
        config,
        profile="interactive",
        session="golden",
        origin="api",
        at=datetime.fromisoformat("2026-11-04T19:00:00+00:00"),
        recall="A remembered fact.",
    )
    assert turn is None
    assert prompt.encode() == (FIXTURES / "memory-present-legacy.txt").read_bytes()


def test_stable_layout_keeps_per_run_values_out_of_system_message(config):
    """Guard: nothing that varies per run may appear in the system message."""
    (config.paths.fragments / "recall-header.md").write_text("Recalled:")
    kwargs = dict(profile="interactive", session="s", origin="discord")
    a, turn_a = read_turn(
        config,
        at=datetime.fromisoformat("2026-11-04T19:00:01.123456+00:00"),
        recall="first recall",
        **kwargs,
    )
    b, turn_b = read_turn(
        config,
        at=datetime.fromisoformat("2026-11-05T08:31:59+00:00"),
        recall=None,
        **{**kwargs, "origin": "api"},
    )
    assert a == b
    for value in ("2026-11-0", "first recall", "discord", "origin"):
        assert value not in a
    assert "time: 2026-11-04T12:00-07:00" in turn_a
    assert "first recall" in turn_a and "<recall>" not in turn_b


def test_two_turns_share_byte_identical_request_prefix(config):
    import asyncio
    import json

    import httpx

    from solakit.core.loop import Execution
    from solakit.core.model import ModelClient
    from solakit.core.schemas import Session
    from solakit.core.store import Store
    from solakit.tools import Registry

    requests = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": config.model.model_id,
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            },
        )

    store = Store(config.paths.data)
    store.create_session(Session(id="s"))
    execution = Execution(
        config,
        store,
        Registry(config),
        ModelClient(config.model, transport=httpx.MockTransport(handler)),
    )

    async def exercise():
        first = await execution.submit("s", "first")
        await execution.submit("s", "second")
        await execution.close()
        return first

    first = asyncio.run(exercise())
    one, two = requests
    # The entire first request (system + its user turn) is a prefix of the second.
    assert two["messages"][: len(one["messages"])] == one["messages"]
    record = store.run(first.id)
    assert one["messages"][-1]["content"] == record["turn_context"] + "\n\nfirst"
    transcript = store.transcript("s")
    assert transcript[0]["content"] == "first"
    assert transcript[0]["turn_context"] == record["turn_context"]
