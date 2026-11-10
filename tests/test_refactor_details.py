"""Review D9–D12: scheduling defaults, text bounds, code roots and glob paths."""

import asyncio
import sys
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_tools import context

import solakit.core.config as config_module
from solakit.core.config import (
    Config,
    ConfigLoadError,
    Job,
    Profile,
    code_roots,
    load_config,
)
from solakit.jobs import render_task
from solakit.main import create_app
from solakit.services.process import execute


def job_fields(**changes):
    return {
        "id": "inherited",
        "profile": "interactive",
        "bind_session": "ephemeral",
        "task": "{timezone}",
        **changes,
    }


def test_config_jobs_inherit_timezone_before_local_time_validation():
    config = Config(
        timezone="Asia/Tokyo", jobs=[job_fields(run_at="2027-11-07T01:30:00")]
    )
    job = config.jobs[0]
    assert job.timezone == "Asia/Tokyo"
    assert job.run_at == "2027-11-06T16:30:00+00:00"
    assert render_task(job, datetime.fromisoformat(job.run_at)) == "Asia/Tokyo"
    explicit = Config(
        timezone="Asia/Tokyo", jobs=[job_fields(cron="0 9 * * *", timezone="UTC")]
    )
    assert explicit.jobs[0].timezone == "UTC"
    instance = Job(**job_fields(cron="0 9 * * *"))
    assert (
        Config(timezone="Asia/Tokyo", jobs=[instance]).jobs[0].timezone == "Asia/Tokyo"
    )
    with pytest.raises(ValidationError):
        Config(
            timezone="America/New_York", jobs=[job_fields(run_at="2027-11-07T01:30:00")]
        )


def test_managed_job_defaults_resolve_before_validation_and_survive_restart(
    config, mock_model, auth
):
    config.timezone = "Asia/Tokyo"
    with TestClient(create_app(config, mock_model)) as client:
        response = client.post(
            "/api/jobs", headers=auth, json=job_fields(run_at="2027-11-07T01:30:00")
        )
        assert response.status_code == 201, response.text
        assert response.json()["timezone"] == "Asia/Tokyo"
        assert response.json()["run_at"] == "2027-11-06T16:30:00+00:00"
        cron = client.post(
            "/api/jobs", headers=auth, json=job_fields(id="cron", cron="0 9 * * *")
        )
        assert cron.status_code == 201
        assert cron.json()["timezone"] == "Asia/Tokyo"
        explicit = client.post(
            "/api/jobs",
            headers=auth,
            json=job_fields(id="explicit", cron="0 9 * * *", timezone="UTC"),
        )
        assert explicit.json()["timezone"] == "UTC"
        config.timezone = "Europe/Paris"
        changed = client.patch(
            "/api/jobs/inherited", headers=auth, json={"task": "updated"}
        )
        assert changed.json()["timezone"] == "Asia/Tokyo"
        schema = client.get("/openapi.json").json()["components"]["schemas"][
            "JobCreate"
        ]
        assert "default" not in schema["properties"]["timezone"]
    from solakit.core.model import ModelClient

    restarted_model = ModelClient(
        config.model,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"data": [{"id": config.model.model_id}]}
            )
        ),
    )
    with TestClient(create_app(config, restarted_model)) as client:
        assert (
            client.get("/api/jobs/inherited", headers=auth).json()["timezone"]
            == "Asia/Tokyo"
        )


def test_process_limits_count_characters_and_leave_environment_unchanged(tmp_path):
    environment = {"LANG": "C.UTF-8"}
    result = asyncio.run(
        execute(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write('é🙂Z'.encode()); "
                "sys.stderr.buffer.write('界'.encode())",
            ],
            tmp_path,
            5,
            2,
            environment=environment,
        )
    )
    assert result["stdout"] == "é🙂"
    assert result["stderr"] == "界"
    assert result["stdout_complete"] is False
    assert result["stderr_complete"] is True
    assert environment == {"LANG": "C.UTF-8"}
    boundary = asyncio.run(
        execute(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(('a'*65535 + '🙂').encode())",
            ],
            tmp_path,
            5,
            65536,
        )
    )
    assert boundary["stdout"] == "a" * 65535 + "🙂"
    assert boundary["stdout_complete"] is True


def test_code_roots_distinguish_checkout_from_installed_interpreter(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    package = source / "src/solakit"
    module = package / "core/config.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    (source / "pyproject.toml").write_text('[project]\nname = "solakit"\n')
    assert code_roots(module) == (source,)
    monkeypatch.setattr(config_module, "__file__", str(module))
    path = tmp_path / "config.toml"
    path.write_text(f'[paths]\ndata = "{source / "data"}"\n')
    with pytest.raises(ConfigLoadError, match="outside the code tree"):
        load_config(path)
    installed = tmp_path / "venv/lib/python3.12/site-packages/solakit/core/config.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("")
    assert code_roots(installed) == (installed.parent.parent,)
    monkeypatch.setattr(config_module, "__file__", str(installed))
    data = tmp_path / "venv/lib/python3.12/operator-data"
    path.write_text(f'[paths]\ndata = "{data}"\n')
    assert load_config(path).paths.data == data
    path.write_text(f'[paths]\ndata = "{installed.parent / "data"}"\n')
    with pytest.raises(ConfigLoadError, match="outside the code tree"):
        load_config(path)


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("docs/*.md", {"docs/root.md"}),
        ("docs/?.md", set()),
        ("docs/[r]*.md", {"docs/root.md"}),
        ("docs/**/*.md", {"docs/root.md", "docs/a/deep.md", "docs/a/b/deeper.md"}),
        ("**/root.md", {"docs/root.md"}),
        (
            "*.md",
            {
                "top.md",
                "docs/root.md",
                "docs/a/deep.md",
                "docs/a/b/deeper.md",
                "other/docs/shadow.md",
            },
        ),
    ],
)
def test_find_path_globs_obey_directory_boundaries(config, tmp_path, pattern, expected):
    workspace = tmp_path / "workspace"
    for name in (
        "top.md",
        "docs/root.md",
        "docs/a/deep.md",
        "docs/a/b/deeper.md",
        "other/docs/shadow.md",
        "docs/a/skip.txt",
    ):
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    config.profiles["interactive"] = Profile(workspace=[f"path:{workspace}"])
    ctx = context(config)
    result = asyncio.run(ctx.fs.find(pattern, str(workspace)))
    assert {m["path"] for m in result["matches"]} == expected
