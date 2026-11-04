import hashlib
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from solakit.core.prompt import read_prompt
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
