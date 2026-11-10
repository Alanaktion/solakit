"""Regression coverage for the November 10 code review findings."""

import asyncio
import hashlib
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient
from test_execution import completion, model_list, setup
from test_tools import context
from test_ui import sign_in
from test_vision import fake_cdn

from solakit.channels.discord import DiscordAdapter, chunks
from solakit.core.config import Channel, load_config
from solakit.core.schemas import Message, Origin, Session
from solakit.core.search import SearchUnavailable
from solakit.core.secrets import Redactor
from solakit.core.store import Store
from solakit.main import create_app
from solakit.services.fs import Filesystem
from solakit.tools import ToolError, tier_roots
from solakit.ui.auth import UISessions


def test_git_metadata_write_paths_and_aliases_are_refused(config, tmp_path):
    repo = tmp_path / "repo"
    metadata = repo / ".git"
    metadata.mkdir(parents=True)
    (repo / "alias").symlink_to(metadata, target_is_directory=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (metadata / "escape").symlink_to(elsewhere, target_is_directory=True)
    profile = config.profiles["interactive"]
    profile.write = "anywhere"
    ctx = context(config)
    for path in (
        metadata / "config",
        repo / "alias/config",
        metadata / "escape/new",
        repo / ".GIT/config",
    ):
        with pytest.raises(ToolError, match="Git metadata"):
            ctx.path(str(path), write=True)
    assert ctx.path(str(repo / "README.md"), write=True) == repo / "README.md"
    assert ctx.path(str(metadata / "config")) == metadata / "config"


def test_default_roots_and_explicit_data_grants(config, tmp_path):
    repo = tmp_path / "repo"
    config.repositories = {"main": repo}
    profile = config.profiles["interactive"]
    profile.write = "workspace"
    ctx = context(config)
    assert tier_roots(profile, config, write=False) == [repo]
    for tier in ("workspace", "anywhere"):
        profile.read = profile.write = tier
        profile.workspace = [f"path:{tmp_path}"]
        for write in (False, True):
            with pytest.raises(ToolError, match="explicit"):
                ctx.path(str(config.paths.data / "app.db"), write=write)
    allowed = config.paths.data / "exports"
    profile.workspace = [f"path:{allowed}"]
    assert ctx.path(str(allowed / "notes"), write=True) == allowed / "notes"
    with pytest.raises(ToolError, match="explicit"):
        ctx.path(str(config.paths.data / "sessions"))
    profile.read = "none"
    profile.write = "none"
    with pytest.raises(ToolError):
        ctx.path(str(allowed / "notes"))


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://testserver:9999"},
        {"Origin": "https://foreign.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "none"},
    ],
)
def test_cross_origin_cookie_mutations_rejected(config, mock_model, headers):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        assert (
            client.post("/sessions", data={"id": "forged"}, headers=headers).status_code
            == 403
        )
        assert (
            client.post("/api/runs/missing/cancel", headers=headers).status_code == 403
        )
        assert not app.state.store.list_sessions()["sessions"]
        assert client.get("/api/profiles", headers=headers).status_code == 200
        # Bearer authorization does not acquire cookie-origin restrictions.
        response = client.post(
            "/api/sessions",
            json={"id": "bearer"},
            headers={**headers, "Authorization": "Bearer operator-test-token"},
        )
        assert response.status_code == 201


def test_same_origin_and_legacy_cookie_requests(config, mock_model):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        sign_in(client)
        for index, headers in enumerate(
            ({}, {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
        ):
            assert (
                client.post(
                    "/api/sessions", json={"id": f"allowed-{index}"}, headers=headers
                ).status_code
                == 201
            )


def test_worker_retirement_and_reenqueue(config):
    def handler(request):
        return (
            model_list(config)
            if request.url.path.endswith("/models")
            else completion(config, "done")
        )

    async def exercise():
        execution = setup(config, handler)
        try:
            for index in range(8):
                identity = f"ephemeral-{index}"
                execution.store.create_session(
                    Session(id=identity, memory_scope="ephemeral")
                )
                # Enqueue together to cover FIFO and retirement after draining.
                results = await asyncio.gather(
                    *(execution.submit(identity, str(i)) for i in range(3))
                )
                assert all(run.status == "completed" for run in results)
                async with execution.submit_lock:
                    assert not execution.queues
                    assert not execution.workers
                    assert not execution.session_locks
            assert (await execution.submit(identity, "again")).status == "completed"
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_read_cancellation_does_not_wait_for_thread(config, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    fs = Filesystem(context(config))

    def slow_read(*args):
        entered.set()
        try:
            release.wait(5)
            return "done", None
        finally:
            finished.set()

    monkeypatch.setattr(fs, "_read", slow_read)

    async def exercise():
        task = asyncio.create_task(fs.read("ignored"))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.5)
            assert not finished.is_set()
        finally:
            release.set()

    asyncio.run(exercise())
    assert finished.is_set()


def test_grep_bounds_regex_and_marks_skipped_lines(config, tmp_path):
    config.profiles["interactive"].workspace = [f"path:{tmp_path}"]
    fs = Filesystem(context(config))
    path = tmp_path / "text.txt"

    async def exercise():
        path.write_text("a" * 4095 + "!\n")
        with pytest.raises(ToolError, match="time budget"):
            await asyncio.wait_for(fs.grep("(a+)+$", path, regex=True), 2)
        path.write_text("needle" + "a" * 4096 + "\nneedle 42\n")
        result = await fs.grep(r"needle \d+", path, regex=True, context=0)
        assert result["matches"] == 1
        assert result["more"]
        assert result["lines"] == ["text.txt:2: needle 42"]
        assert (await fs.grep("needle 42", path, context=0))["matches"] == 1

    asyncio.run(exercise())


def busy_error():
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


def test_transient_search_failure_can_recover(config, monkeypatch):
    import solakit.core.search as search

    store = Store(config.paths.data)
    store.create_session(Session(id="s"))
    store.append_message(
        "s", Message(origin=Origin(), run_id="run", role="user", content="needle")
    )
    connect = search.sqlite3.connect

    def unavailable(*args, **kwargs):
        raise busy_error()

    with monkeypatch.context() as patch:
        patch.setattr(search.sqlite3, "connect", unavailable)
        with pytest.raises(SearchUnavailable):
            store.search("needle")
    assert len(store.search("needle")) == 1
    assert store.storage_health()["status"] == "ok"
    assert search.sqlite3.connect is connect


def test_transient_index_write_repairs_missing_rows(config, monkeypatch):
    store = Store(config.paths.data)
    store.create_session(Session(id="s"))

    def unavailable(*args):
        raise busy_error()

    with monkeypatch.context() as patch:
        patch.setattr(store, "_insert_search_message", unavailable)
        store.append_message(
            "s",
            Message(
                origin=Origin(), run_id="run", role="user", content="missed needle"
            ),
        )
        store.append_message(
            "s",
            Message(
                origin=Origin(), run_id="run", role="user", content="another needle"
            ),
        )
    assert store.storage_health()["status"] == "degraded"
    assert len(store.search("needle")) == 2
    assert store.storage_health()["status"] == "ok"
    assert not store._fts_dirty_sessions


def test_discord_chunks_exclude_blank_separators():
    fence = "```\n" + "x" * 1992 + "\n```"
    assert len(fence) == 2000
    assert chunks(fence + "\n\n" + fence) == [fence, fence]
    assert chunks(" \n\t") == []


def test_all_secret_file_paths_are_config_relative(tmp_path, monkeypatch):
    directory = tmp_path / "configuration"
    directory.mkdir()
    path = directory / "config.toml"
    path.write_text("""
operator_token = {file = "operator"}
discord_token = {file = "discord"}
[model]
api_key = {file = "model"}
[memory]
api_key = {file = "memory"}
[tool_apis]
brave_api_key = {file = "brave"}
cloudflare_api_token = {file = "cloudflare"}
github_token = {file = "github"}
""")
    monkeypatch.chdir(tmp_path)
    config = load_config(path)
    for reference, name in (
        (config.operator_token, "operator"),
        (config.discord_token, "discord"),
        (config.model.api_key, "model"),
        (config.memory.api_key, "memory"),
        (config.tool_apis.brave_api_key, "brave"),
        (config.tool_apis.cloudflare_api_token, "cloudflare"),
        (config.tool_apis.github_token, "github"),
    ):
        assert reference.file == directory / name


@pytest.mark.parametrize(
    "saved", ["{", "[]", "{}", '{"token_hash": 1}', '{"token_hash":"x","sessions":[]}']
)
def test_damaged_ui_session_files_require_fresh_login(tmp_path, saved, caplog):
    path = tmp_path / "ui-sessions.json"
    path.write_text(saved)
    sessions = UISessions()
    sessions.load(path, "private-token")
    assert not sessions.sessions
    assert sessions.login("private-token", "private-token", "local")[1] == 200
    assert "private-token" not in caplog.text
    assert "browser login required" in caplog.text


@pytest.mark.parametrize("expiry", [True, "later", float("nan"), float("inf"), None])
def test_invalid_saved_expiries_invalidate_all_sessions(tmp_path, expiry):
    path = tmp_path / "ui-sessions.json"
    path.write_text(
        json.dumps(
            {
                "token_hash": hashlib.sha256(b"token").hexdigest(),
                "sessions": {"a" * 64: expiry, "b" * 64: 10**12},
            }
        )
    )
    sessions = UISessions()
    sessions.load(path, "token")
    assert not sessions.sessions


def test_redactor_matches_longest_secret_first():
    redactor = Redactor(["abc", "abcDEF", "abc"])
    assert redactor.redact("abcDEF abc") == "[redacted] [redacted]"


def test_discord_download_does_not_trust_proxy_environment(config, monkeypatch):
    fake_cdn(monkeypatch, b"image")
    from solakit.channels import discord

    client = discord.aiohttp.ClientSession
    options = []

    def recording_client(**kwargs):
        options.append(kwargs)
        return client(**kwargs)

    monkeypatch.setattr(discord.aiohttp, "ClientSession", recording_client)
    adapter = DiscordAdapter(
        Channel(id="discord", type="discord", channel_id="1", session="s"), "token"
    )
    assert (
        asyncio.run(adapter.download("https://cdn.discordapp.com/attachments/a/b"))
        == b"image"
    )
    assert options[0]["trust_env"] is False


def test_trusted_proxy_scheme_and_login_failure_buckets(config, mock_model):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app = create_app(config=config, model=mock_model)
    proxied = ProxyHeadersMiddleware(app, trusted_hosts=["testclient"])
    with TestClient(proxied) as client:
        first = {"X-Forwarded-For": "192.0.2.1", "X-Forwarded-Proto": "https"}
        second = {**first, "X-Forwarded-For": "192.0.2.2"}
        for _ in range(5):
            assert (
                client.post(
                    "/login", data={"token": "wrong"}, headers=first
                ).status_code
                == 401
            )
        assert (
            client.post("/login", data={"token": "wrong"}, headers=first).status_code
            == 429
        )
        signed = client.post(
            "/login",
            data={"token": "operator-test-token"},
            headers=second,
            follow_redirects=False,
        )
        assert signed.status_code == 303
        assert "Secure" in signed.headers["set-cookie"]


def test_delegated_children_release_session_locks(config):
    from test_delegation import setup as delegation_setup

    async def exercise():
        execution, _, _ = delegation_setup(config)
        try:
            run = await execution.submit("parent", "Delegate a task")
            assert run.status == "completed"
            async with execution.submit_lock:
                assert not execution.session_locks
                assert not execution.workers
                assert not execution.queues
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_sqlite_lock_contention_retries_once():
    from solakit.core.search import sqlite_retry

    attempts = []

    def operation():
        attempts.append(None)
        if len(attempts) == 1:
            raise busy_error()
        return "recovered"

    assert sqlite_retry(operation) == "recovered"
    assert len(attempts) == 2


def test_last_cancelled_queue_item_retires_worker(config):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            if request.url.path.endswith("/models"):
                return model_list(config)
            entered.set()
            await release.wait()
            return completion(config, "done")

        execution = setup(config, handler)
        try:
            first = await execution.enqueue("s", "running")
            await entered.wait()
            second = await execution.enqueue("s", "cancelled in queue")
            worker = execution.workers["s"]
            await execution.cancel(second.run.id)
            release.set()
            await first.future
            await asyncio.wait_for(asyncio.shield(worker), 1)
            assert worker.done()
            assert not execution.workers
            assert not execution.queues
            assert not execution.session_locks
        finally:
            release.set()
            await execution.close()

    asyncio.run(exercise())


def test_unreadable_ui_session_file_can_be_replaced(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "ui-sessions.json"
    path.write_text("unreadable")
    read = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self == path:
            raise PermissionError("private detail")
        return read(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", unreadable)
        sessions = UISessions()
        sessions.load(path, "token")
    assert not sessions.sessions
    assert json.loads(path.read_text())["sessions"] == {}


def test_case_alias_cannot_bypass_private_data_boundary(config):
    ctx = context(config)
    alias = config.paths.data.with_name(config.paths.data.name.upper())
    if not alias.exists():
        pytest.skip("case-sensitive filesystem")
    config.profiles["interactive"].read = "anywhere"
    config.profiles["interactive"].write = "anywhere"
    for write in (False, True):
        with pytest.raises(ToolError, match="explicit"):
            ctx.path(str(alias / "app.db"), write=write)
