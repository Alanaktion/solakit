import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import ValidationError

from scripts.migrate_config import migrate
from solakit.config import (
    ConfigError,
    EffectiveProfile,
    MemorySettings,
    load_config,
    resolve_profile,
)
from solakit.evidence import canonical, digest
from solakit.memory import MemoryError, MemoryService, OpenViking
from solakit.records import (
    MemoryCommit,
    MemoryState,
    Message,
    ModelStep,
    Run,
    RunState,
    SessionMode,
    ToolCall,
    new_id,
    utc_now,
)
from solakit.redaction import Redactor
from solakit.store import Store


def enabled(profile: EffectiveProfile) -> EffectiveProfile:
    return profile.model_copy(
        update={
            "memory": MemorySettings(
                enabled=True,
                account="test",
                roots=("viking://user/default/memories",),
            )
        }
    )


def completed(store: Store, profile: EffectiveProfile) -> Run:
    session = store.create_session(mode=profile.mode)
    run = store.admit(
        session.id,
        request_id=new_id(),
        content="A distinctive fact λ.",
        profile=profile.name,
        admission=profile.model_dump(mode="json"),
    )
    run = store.transition_run(run.id, RunState.RUNNING)
    store.append(
        Message(
            origin=run.origin,
            session_id=session.id,
            run_id=run.id,
            role="assistant",
            content="Acknowledged.",
        )
    )
    return store.transition_run(run.id, RunState.COMPLETED)


def tracker(
    store: Store,
    profile: EffectiveProfile,
    requests: list[httpx2.Request],
    version: str = "0.5.5",
) -> MemoryService:
    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/health":
            return httpx2.Response(200, json={"status": "ok", "version": version})
        if request.url.path.endswith("/commit"):
            result: dict[str, Any] = {"task_id": "provider-task"}
        elif "/tasks/" in request.url.path:
            result = {"status": "completed"}
        elif request.url.path.endswith("/find"):
            result = {
                "memories": [{"uri": profile.memory.roots[0] + "/profile.md", "content": "Fact"}]
            }
        elif request.url.path == "/api/v1/sessions":
            session = json.loads(request.content)["session_id"]
            result = {
                "session_id": session,
                "uri": f"viking://user/default/sessions/{session}",
                "user": {"account_id": "test", "user_id": "default"},
            }
        else:
            result = {}
        return httpx2.Response(200, json={"status": "ok", "result": result})

    adapter = OpenViking(profile.memory, Redactor())
    adapter.transport = httpx2.MockTransport(respond)
    return MemoryService(store, adapter, lambda admitted: profile)


def commit(store: Store, run: Run) -> MemoryCommit:
    row = store.db.execute(
        "SELECT id FROM entities WHERE kind='memory' AND run_id=?", (run.id,)
    ).fetchone()
    return store._load("memory", row[0], MemoryCommit)


@pytest.mark.parametrize("version", ["0.5.5", "0.5.6", "0.5.6.1"])
def test_exact_staging_dedupe_and_scope(profile: EffectiveProfile, version: str) -> None:
    async def scenario() -> None:
        selected = enabled(profile)
        store = Store(selected.data_dir)
        requests: list[httpx2.Request] = []
        service = tracker(store, selected, requests, version)
        await store.start()
        await service.adapter.start()
        try:
            session = store.create_session()
            run = store.admit(
                session.id,
                request_id=new_id(),
                content="Remember",
                profile=selected.name,
                admission=selected.model_dump(mode="json"),
            )
            store.transition_run(run.id, RunState.RUNNING)
            step = ModelStep(
                origin=run.origin, session_id=session.id, run_id=run.id, index=1, model="fixture"
            )
            store.append(step)
            call = ToolCall(
                origin=run.origin,
                session_id=session.id,
                run_id=run.id,
                model_step_id=step.id,
                provider_tool_call_id="note",
                name="memory.remember",
                arguments={},
            )
            store.append(call)
            exact = "  precise\r\nλ 🐈\n "
            note = service.stage(run.id, call.id, exact)
            assert note.content == exact and not requests
            with pytest.raises(ValueError, match="already staged"):
                service.stage(run.id, call.id, exact)
            with pytest.raises(ValidationError):
                service.stage(run.id, new_id(), "x" * 2001)
            run = store.transition_run(run.id, RunState.COMPLETED)
            service.enqueue(run)
            service.enqueue(run)
            await service.tick()
            work = commit(store, run)
            assert work.state == MemoryState.COMPLETED
            assert work.note_ids == (note.id,)
            assert work.payload[-1]["content"] == exact
            assert digest(canonical(work.payload)) == work.payload_sha256
            batch = next(
                json.loads(r.content) for r in requests if r.url.path.endswith("/messages/batch")
            )
            assert batch["messages"][-1]["content"] == exact
            assert all(r.headers["X-OpenViking-Account"] == "test" for r in requests)
            assert sum(r.url.path.endswith("/commit") for r in requests) == 1
            with pytest.raises(MemoryError):
                await service.adapter.recall(
                    selected.model_copy(update={"memory_mode": "off"}), "secret"
                )
        finally:
            await service.adapter.stop()
            await store.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("state", [MemoryState.PENDING, MemoryState.RUNNING, MemoryState.ACCEPTED])
def test_restart_before_during_and_after_acceptance(
    profile: EffectiveProfile, state: MemoryState
) -> None:
    async def scenario() -> None:
        selected = enabled(profile)
        store = Store(selected.data_dir)
        requests: list[httpx2.Request] = []
        service = tracker(store, selected, requests)
        await store.start()
        run = completed(store, selected)
        service.enqueue(run)
        work = commit(store, run)
        if state != MemoryState.PENDING:
            store.transition_memory(work.id, MemoryState.RUNNING)
        if state == MemoryState.ACCEPTED:
            store.transition_memory(work.id, state, provider_commit_id="provider-task")
        await store.stop()
        store = Store(selected.data_dir)
        service = tracker(store, selected, requests)
        await store.start()
        await service.adapter.start()
        try:
            await service.tick()
            work = commit(store, run)
            assert work.state == (
                MemoryState.AMBIGUOUS if state == MemoryState.RUNNING else MemoryState.COMPLETED
            )
            assert sum(r.url.path.endswith("/commit") for r in requests) == (
                state == MemoryState.PENDING
            )
            if state == MemoryState.ACCEPTED:
                assert any(r.url.path.endswith("/tasks/provider-task") for r in requests)
        finally:
            await service.adapter.stop()
            await store.stop()

    asyncio.run(scenario())


def test_outage_backoff_ambiguous_submission_and_backlog(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        selected = enabled(profile)
        selected = selected.model_copy(
            update={"memory": selected.memory.model_copy(update={"backlog_cap": 1})}
        )
        store = Store(selected.data_dir)
        requests: list[httpx2.Request] = []
        service = tracker(store, selected, requests)
        await store.start()
        await service.adapter.start()
        try:
            run = completed(store, selected)
            service.enqueue(run)
            second = completed(store, selected)
            service.enqueue(second)
            assert commit(store, second).state == MemoryState.FAILED
            assert service.adapter.client
            await service.adapter.stop()
            service.adapter.transport = httpx2.MockTransport(lambda r: httpx2.Response(503))
            await service.adapter.start()
            await service.tick()
            work = commit(store, run)
            assert work.state == MemoryState.PENDING and work.attempts == 1
            assert work.next_check > utc_now()
            await service.tick()
            assert commit(store, run).attempts == 1
            store.transition_memory(
                work.id, MemoryState.PENDING, next_check=utc_now() - timedelta(seconds=1)
            )
            await service.adapter.stop()

            def lost(request: httpx2.Request) -> httpx2.Response:
                requests.append(request)
                if request.url.path == "/health":
                    return httpx2.Response(200, json={"status": "ok", "version": "0.5.5"})
                if request.url.path.endswith("/commit"):
                    raise httpx2.ReadTimeout("acceptance lost")
                session = json.loads(request.content).get("session_id")
                result = (
                    {
                        "session_id": session,
                        "uri": f"viking://user/default/sessions/{session}",
                        "user": {"account_id": "test", "user_id": "default"},
                    }
                    if session
                    else {}
                )
                return httpx2.Response(200, json={"status": "ok", "result": result})

            service.adapter.transport = httpx2.MockTransport(lost)
            await service.adapter.start()
            await service.tick()
            assert commit(store, run).state == MemoryState.AMBIGUOUS
            count = len(requests)
            await service.tick()
            assert len(requests) == count
        finally:
            await service.adapter.stop()
            await store.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "uri",
    [
        "viking://user/other/memories/leak.md",
        "viking://user/default/memories/../secret",
        "viking://user/default/memories/%2e%2e/secret",
        "viking://user/default/memories/secret?other",
        "viking://user/default/memories/secret#other",
    ],
)
def test_recall_rejects_provider_scope_escape(profile: EffectiveProfile, uri: str) -> None:
    async def scenario() -> None:
        selected = enabled(profile)
        adapter = OpenViking(selected.memory, Redactor())
        adapter.verified = True
        adapter.transport = httpx2.MockTransport(
            lambda r: httpx2.Response(
                200,
                json={
                    "status": "ok",
                    "result": {"memories": [{"uri": uri, "content": "secret"}]},
                },
            )
        )
        await adapter.start()
        try:
            with pytest.raises(MemoryError):
                await adapter.recall(selected, "query")
        finally:
            await adapter.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", [SessionMode.ISOLATED, SessionMode.EPHEMERAL])
def test_modes_and_failed_runs_never_commit(profile: EffectiveProfile, mode: SessionMode) -> None:
    async def scenario() -> None:
        selected = enabled(profile).model_copy(update={"mode": mode})
        store = Store(selected.data_dir)
        requests: list[httpx2.Request] = []
        service = tracker(store, selected, requests)
        await store.start()
        await service.adapter.start()
        try:
            run = completed(store, selected)
            record = await service.recall(run, selected, "private")
            assert record.status == "disabled" and record.query == ""
            service.enqueue(run)
            await service.tick()
            assert not requests
            assert (
                store.db.execute("SELECT count(*) FROM entities WHERE kind='memory'").fetchone()[0]
                == 0
            )
        finally:
            await service.adapter.stop()
            await store.stop()

    asyncio.run(scenario())


def test_config_scope_validation_and_toml(profile: EffectiveProfile, tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        MemorySettings(roots=("viking://user/default/memories/../other",))
    path = tmp_path / "config.toml"
    path.write_text(f'''data_dir = "{tmp_path / "new"}"
[model]
pin = "fixture"
[defaults]
workspace_roots = ["{tmp_path}"]
[memory]
enabled = true
roots = ["viking://user/default/memories/"]
''')
    assert resolve_profile(load_config(path)).memory.roots == ("viking://user/default/memories",)
    path.write_text(path.read_text().replace("user/default/", "user/other/"))
    with pytest.raises(ConfigError, match="selected user"):
        resolve_profile(load_config(path))


def test_migration_preserves_source_blocks_broadening_and_redacts(tmp_path: Path) -> None:
    source = tmp_path / "legacy.toml"
    source.write_text(f'''enabled_tools = ["files", "shell", "memory"]
discord_token = "private-secret"
[model]
model_id = "fixture"
api_key = "private-secret"
[paths]
data = "old-data"
[profiles.interactive]
workspace = ["{tmp_path}"]
read = "workspace"
write = "none"
tools = ["files.*", "shell.*", "memory.*"]
''')
    report = migrate(source, tmp_path / "migration", tmp_path / "data")
    config = load_config(tmp_path / "migration/config.v2.toml")
    assert config.defaults.tools == ["files.read", "memory.recall", "memory.remember"]
    assert config.defaults.memory_mode == "off"
    assert "private-secret" not in json.dumps(report)
    assert (tmp_path / "migration/legacy.source.toml").read_bytes() == source.read_bytes()
    candidate = tmp_path / "candidate.toml"
    candidate.write_text(
        (tmp_path / "migration/config.v2.toml")
        .read_text()
        .replace('"files.read",', '"files.read", "files.write",')
    )
    blocked = migrate(source, tmp_path / "blocked", tmp_path / "data", candidate=candidate)
    assert blocked["status"] == "blocked"
    assert "tools" in blocked["unresolved"] and "write_roots" in blocked["unresolved"]
    assert not (tmp_path / "blocked/config.v2.toml").exists()
    resolved = migrate(
        source,
        tmp_path / "resolved",
        tmp_path / "data",
        candidate=candidate,
        resolutions=frozenset({"tools", "write_roots"}),
    )
    assert resolved["status"] == "ready_for_review"
    with pytest.raises(FileExistsError):
        migrate(source, tmp_path / "resolved", tmp_path / "data")


def test_manual_reconciliation_checks_task_ownership(profile: EffectiveProfile) -> None:
    from scripts.memory_work import reconcile

    async def scenario() -> None:
        selected = enabled(profile)
        store = Store(selected.data_dir)
        service = tracker(store, selected, [])
        await store.start()
        try:
            run = completed(store, selected)
            service.enqueue(run)
            work = commit(store, run)
            store.transition_memory(work.id, MemoryState.RUNNING)
            store.transition_memory(work.id, MemoryState.AMBIGUOUS)
            resource = "other-session"

            def respond(request: httpx2.Request) -> httpx2.Response:
                if request.url.path == "/health":
                    return httpx2.Response(200, json={"status": "ok", "version": "0.5.5"})
                assert request.method == "GET"
                return httpx2.Response(
                    200,
                    json={
                        "status": "ok",
                        "result": {
                            "task_type": "session_commit",
                            "resource_id": resource,
                            "status": "completed",
                        },
                    },
                )

            service.adapter.transport = httpx2.MockTransport(respond)
            await service.adapter.start()
            with pytest.raises(ValueError, match="this commit"):
                await reconcile(service, work.id, "task")
            resource = "sola-" + work.id
            result = await reconcile(service, work.id, "task")
            assert result.state == MemoryState.ACCEPTED and result.provider_commit_id == "task"
        finally:
            await service.adapter.stop()
            await store.stop()

    asyncio.run(scenario())


def test_failed_run_retains_staged_note_without_submission(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        selected = enabled(profile)
        store = Store(selected.data_dir)
        requests: list[httpx2.Request] = []
        service = tracker(store, selected, requests)
        await store.start()
        try:
            session = store.create_session()
            run = store.admit(
                session.id,
                request_id=new_id(),
                profile=selected.name,
                content="input",
                admission=selected.model_dump(mode="json"),
            )
            store.transition_run(run.id, RunState.RUNNING)
            step = ModelStep(
                origin=run.origin, session_id=session.id, run_id=run.id, index=1, model="fixture"
            )
            store.append(step)
            call = ToolCall(
                origin=run.origin,
                session_id=session.id,
                run_id=run.id,
                model_step_id=step.id,
                provider_tool_call_id="note",
                name="memory.remember",
                arguments={},
            )
            store.append(call)
            note = service.stage(run.id, call.id, "retain exactly")
            run = store.transition_run(run.id, RunState.CANCELLED)
            service.enqueue(run)
            await service.tick()
            assert not requests
            assert (
                store.db.execute("SELECT id FROM entities WHERE kind='memory_note'").fetchone()[0]
                == note.id
            )
            assert not store.db.execute("SELECT 1 FROM entities WHERE kind='memory'").fetchone()
        finally:
            await store.stop()

    asyncio.run(scenario())


def test_migration_preserves_fragments_and_normalizes_candidate_paths(tmp_path: Path) -> None:
    source = tmp_path / "legacy.toml"
    fragments = tmp_path / "fragments"
    (fragments / "profiles").mkdir(parents=True)
    for name in ("memory.md", "context.md", "profiles/interactive.md"):
        (fragments / name).write_text("operator-owned\r\nλ")
    source.write_text(f'''enabled_tools = ["files"]
[model]
model_id = "fixture"
[paths]
fragments = "fragments"
[profiles.interactive]
workspace = ["{tmp_path}"]
write_workspace = ["{fragments}"]
read = "workspace"
write = "workspace"
tools = ["files.*"]
''')
    report = migrate(source, tmp_path / "draft", tmp_path / "data")
    assert report["status"] == "ready_for_review"
    config = load_config(tmp_path / "draft/config.v2.toml")
    assert config.defaults.tools == ["files.read"]
    assert config.curated_memory_file == str(fragments / "memory.md")
    assert config.defaults.fragments == [
        str(fragments / "context.md"),
        str(fragments / "profiles/interactive.md"),
    ]
    candidate = tmp_path / "candidate.toml"
    candidate.write_text(
        (tmp_path / "draft/config.v2.toml").read_text().replace(str(tmp_path) + "/", "")
    )
    # The generated data root is relative in this candidate; resolve it against the source location.
    report = migrate(source, tmp_path / "normalized", tmp_path / "data", candidate=candidate)
    assert report["status"] == "ready_for_review"
    normalized = load_config(tmp_path / "normalized/config.v2.toml")
    resolved = resolve_profile(normalized, base=tmp_path / "normalized")
    assert resolved.data_dir == tmp_path / "data"
    assert resolved.curated_memory_file == fragments / "memory.md"
    assert (tmp_path / "normalized/legacy.source.toml").stat().st_mode & 0o777 == 0o600


def test_accepted_observation_outage_is_bounded(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        selected = enabled(profile)
        store = Store(selected.data_dir)
        service = tracker(store, selected, [])
        await store.start()
        try:
            run = completed(store, selected)
            service.enqueue(run)
            work = commit(store, run)
            store.transition_memory(work.id, MemoryState.RUNNING)
            store.transition_memory(
                work.id, MemoryState.ACCEPTED, provider_commit_id="task", attempts=11
            )
            service.adapter.transport = httpx2.MockTransport(lambda r: httpx2.Response(503))
            await service.adapter.start()
            await service.tick()
            work = commit(store, run)
            assert work.state == MemoryState.FAILED and work.attempts == 12
            assert work.provider_commit_id == "task"
            assert work.reason
        finally:
            await service.adapter.stop()
            await store.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["wrong_account", "revoked_after_batch"])
def test_submission_stops_on_scope_mismatch_or_revocation(
    profile: EffectiveProfile, fault: str
) -> None:
    async def scenario() -> None:
        selected = enabled(profile)
        store = Store(selected.data_dir)
        requests: list[httpx2.Request] = []
        service = tracker(store, selected, requests)
        current = selected

        def respond(request: httpx2.Request) -> httpx2.Response:
            nonlocal current
            requests.append(request)
            if request.url.path == "/health":
                return httpx2.Response(200, json={"status": "ok", "version": "0.5.5"})
            assert not request.url.path.endswith("/commit")
            if request.url.path == "/api/v1/sessions":
                session = json.loads(request.content)["session_id"]
                result = {
                    "session_id": session,
                    "uri": f"viking://user/default/sessions/{session}",
                    "user": {
                        "account_id": "other" if fault == "wrong_account" else "test",
                        "user_id": "default",
                    },
                }
            else:
                current = selected.model_copy(update={"memory_mode": "off"})
                result = {}
            return httpx2.Response(200, json={"status": "ok", "result": result})

        service.adapter.transport = httpx2.MockTransport(respond)
        service.current = lambda admitted: current
        await store.start()
        await service.adapter.start()
        try:
            run = completed(store, selected)
            service.enqueue(run)
            await service.tick()
            assert commit(store, run).state == MemoryState.AMBIGUOUS
            assert sum(r.url.path.endswith("/messages/batch") for r in requests) == (
                fault == "revoked_after_batch"
            )
        finally:
            await service.adapter.stop()
            await store.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("version", "accepted"),
    [
        ("0.5.5", True),
        ("0.5.6", True),
        ("0.5.5.1", True),
        ("0.5.10", True),
        ("0.5.4.1", False),
        ("0.4.99", False),
        ("0.6.0", False),
        ("1.0.0", False),
        ("0.5.6rc1", False),
        ("0.5.6.dev1", False),
        ("0.5.6+local", False),
        ("0.5.05", False),
        ("0.5.6garbage", False),
        ("0.5", False),
        ("", False),
        (None, False),
        (5, False),
    ],
)
def test_version_policy(version: object, accepted: bool) -> None:
    async def scenario() -> None:
        adapter = OpenViking(MemorySettings(), Redactor())

        def respond(request: httpx2.Request) -> httpx2.Response:
            assert request.method == "GET" and request.url.path == "/health"
            return httpx2.Response(200, json={"status": "ok", "version": version})

        adapter.transport = httpx2.MockTransport(respond)
        await adapter.start()
        try:
            if accepted:
                assert (await adapter.verify())["version"] == version
                assert adapter.verified
            else:
                with pytest.raises(MemoryError, match=">=0.5.5,<0.6.0"):
                    await adapter.verify()
                assert not adapter.verified
        finally:
            await adapter.stop()

    asyncio.run(scenario())
