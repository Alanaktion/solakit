import asyncio
import fcntl
import json
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from scripts import migration
from solakit.backup import _copy, backup, restore
from solakit.config import MemorySettings
from solakit.evidence import CorruptionError, Entry, canonical, digest
from solakit.jobs import CONTROL_ID
from solakit.memory import MemoryService, OpenViking
from solakit.records import (
    ErrorCode,
    Failure,
    ModelStep,
    Occurrence,
    Origin,
    RunState,
    utc_now,
)
from solakit.redaction import Redactor
from solakit.runtime import DataDirectoryLock, OwnershipError
from solakit.search import rebuild, search
from solakit.store import Store

AT = "2026-11-16T12:00:00+00:00"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value))


def write_lines(path: Path, values: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(canonical(v) + b"\r\n" for v in values))


@pytest.fixture
def copied(tmp_path: Path) -> Iterator[tuple[Path, Path, Path]]:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / ".runtime.lock").touch()
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "source.py").write_text("# legacy source\n")
    subprocess.run(["git", "-C", str(repo), "add", "source.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    (repo / "source.py").write_text("# modified legacy source\n")
    (repo / "untracked.txt").write_text("keep this hash\n")
    meta = {
        "id": "topic",
        "title": "Original title",
        "memory_scope": "isolated",
        "created_at": AT,
        "pinned": True,
        "archived_at": AT,
        "future": {"unknown": "preserve"},
    }
    write_json(legacy / "sessions/topic.meta.json", meta)
    run = {
        "id": "run",
        "session": "topic",
        "created_at": AT,
        "finished_at": AT,
        "status": "completed",
        "profile": "interactive",
        "origin": {"job": "once"},
        "prompt_sha256": digest(b"prompt\r\n"),
        "memory_commit": {"state": "uncertain"},
    }
    write_json(legacy / "runs/run.json", run)
    write_lines(legacy / "runs/run.events.jsonl", [{**run, "status": "running"}, run])
    (legacy / "runs/run.prompt.txt").write_bytes(b"prompt\r\n")
    write_json(legacy / "runs/run.tool-schemas.json", [{"type": "function"}])

    def message(role: str, content: str | None, **extra: Any) -> dict[str, Any]:
        return {
            "role": role,
            "content": content,
            "run_id": "run",
            "ts": AT,
            "origin": {"channel": "old"},
            **extra,
        }

    write_lines(legacy / "sessions/topic.jsonl.2", [message("user", "archive two")])
    write_lines(legacy / "sessions/topic.jsonl.10", [message("assistant", "archive ten")])
    write_lines(
        legacy / "sessions/topic.jsonl",
        [
            message(
                "assistant", None, tool_calls=[{"id": "call", "function": {"name": "old.tool"}}]
            ),
            message("tool", "tool pairing needle", tool_call_id="call"),
            message("user", "current user"),
            message("assistant", "current assistant"),
        ],
    )
    write_lines(legacy / "pack-operations/effects.jsonl", [{"state": "uncertain", "id": "intent"}])
    write_json(legacy / "ui-sessions.json", {"login": "PRIVATE_LOGIN_TOKEN"})
    (legacy / "unknown.bin").write_bytes(b"\x00\xff\r\n")
    with sqlite3.connect(legacy / "app.db") as db:
        db.execute("CREATE TABLE job_definitions(id TEXT PRIMARY KEY,payload TEXT,deleted INTEGER)")
        db.execute("CREATE TABLE messages_fts(text TEXT)")
        db.execute("INSERT INTO messages_fts VALUES ('DERIVED_INDEX_MUST_NOT_BE_IMPORTED')")
        for old, deleted in (("once", 0), ("deleted", 1), ("uncertain", 0)):
            job = {
                "id": old,
                "profile": "interactive",
                "task": "Legacy {date}",
                "run_at": AT,
                "cron": None,
                "bind_session": "topic",
                "enabled": True,
                "timezone": "America/Denver",
                "tools": ["mail.send"],
                "future": True,
            }
            db.execute(
                "INSERT INTO job_definitions VALUES (?,?,?)", (old, json.dumps(job), deleted)
            )
            if old != "uncertain":
                write_lines(
                    legacy / f"jobs/{old}.jsonl",
                    [
                        {"id": old, "consumed": True, "active": False, "next_fire": None},
                    ],
                )
    snap = tmp_path / "snapshot"
    yield legacy, repo, snap


def make_snapshot(copied: tuple[Path, Path, Path]) -> tuple[Path, Path]:
    legacy, repo, snap = copied
    migration.snapshot(legacy, snap, legacy_repo=repo)
    return snap, snap.parent / "candidate"


def test_import_verify_idempotency_and_restore(copied: tuple[Path, Path, Path]) -> None:
    legacy, repo, _ = copied
    original = {str(p.relative_to(legacy)): migration.metadata(p) for p in migration.tree(legacy)}
    snap, data = make_snapshot(copied)
    manifest = migration.validate_snapshot(snap)
    assert manifest["repository"]["files"]["source.py"] == migration.metadata(repo / "source.py")
    assert "untracked.txt" in manifest["repository"]["files"]
    assert manifest["repository"]["status"]
    result = asyncio.run(migration.import_snapshot(snap, data))
    assert result["status"] == "imported"
    assert result["tool_pairing_anomalies"] == 0
    assert migration.verify(snap, data)["status"] == "verified"
    before = {p.name: migration.metadata(p) for p in (data / "evidence").iterdir()}
    assert asyncio.run(migration.import_snapshot(snap, data))["new_records"] == 0
    assert {p.name: migration.metadata(p) for p in (data / "evidence").iterdir()} == before
    sid = migration.identity(str(legacy), "session", "topic")
    rid = migration.identity(str(legacy), "run", "run")
    store = Store(data)
    asyncio.run(store.start())
    try:
        assert store.session(sid).archived and store.session(sid).pinned
        assert [m.content for _, m in store.messages(sid)] == [
            "archive two",
            "archive ten",
            "current user",
            "current assistant",
        ]
        assert store.run(rid).state == RunState.COMPLETED
        assert [r.index for _, r in store.run_records(rid) if isinstance(r, ModelStep)] == [1, 2]
        assert all(
            j.consumed
            and j.paused
            and not j.enabled
            and j.tools == ()
            and j.workspace_roots == ()
            and j.delivery == "none"
            for j in store.jobs(include_deleted=True)
        )
        assert sum(j.deleted for j in store.jobs(include_deleted=True)) == 1
        assert json.loads(
            store.db.execute("SELECT body FROM entities WHERE id=?", (CONTROL_ID,)).fetchone()[0]
        )["paused"]
        assert store.db.execute("SELECT count(*) FROM login_sessions").fetchone()[0] == 0
        assert not store.queued_runs()
        memory = MemoryService(
            store,
            OpenViking(MemorySettings(), Redactor()),
            lambda admitted: pytest.fail("historical memory must stay inactive"),
        )
        asyncio.run(memory.tick())
        assert not store.db.execute(
            "SELECT 1 FROM entities WHERE kind IN ('receipt','memory','delivery')"
        ).fetchone()
        backup(store, data.parent / "backup")
    finally:
        asyncio.run(store.stop())
    hits = search(data, "tool pairing needle", session_id=sid)
    assert len(hits) == 1 and hits[0]["kind"] == "legacy"
    assert not search(data, "tool pairing needle", shared_only=True)
    assert not search(data, "DERIVED_INDEX_MUST_NOT_BE_IMPORTED")
    assert not search(data, "PRIVATE_LOGIN_TOKEN")
    assert b"PRIVATE_LOGIN_TOKEN" not in (data / "imports/phase13-report.json").read_bytes()
    assert (data / "imports/phase13-source/legacy/ui-sessions.json").read_bytes() == (
        legacy / "ui-sessions.json"
    ).read_bytes()
    restored = data.parent / "restored"
    restore(data.parent / "backup", restored)
    rebuild(restored)
    assert migration.verify(snap, restored)["status"] == "verified"
    assert (
        migration.reconcile(restored, data.parent / "before-rollback.json")["rollback"]
        == "before_new_writes"
    )
    assert {
        str(p.relative_to(legacy)): migration.metadata(p) for p in migration.tree(legacy)
    } == original
    assert (data / "imports/phase13-source/legacy/unknown.bin").stat().st_mode & 0o777 == 0o600


def test_resume_after_durable_commit(
    copied: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    snap, data = make_snapshot(copied)
    real_flush = Store._flush
    committed = False

    def fail_once(store: Store) -> None:
        nonlocal committed
        if (
            not committed
            and store.db.execute("SELECT 1 FROM imports WHERE source LIKE 'phase13:%'").fetchone()
        ):
            committed = True
            store.failed = True
            raise OSError("synthetic process death after commit")
        real_flush(store)

    with monkeypatch.context() as context:
        context.setattr(Store, "_flush", fail_once)
        with pytest.raises(OSError, match="synthetic"):
            asyncio.run(migration.import_snapshot(snap, data))
    assert committed
    asyncio.run(migration.import_snapshot(snap, data))
    assert migration.verify(snap, data)["status"] == "verified"


def test_rollback_after_new_writes_requires_reconciliation(copied: tuple[Path, Path, Path]) -> None:
    snap, data = make_snapshot(copied)
    asyncio.run(migration.import_snapshot(snap, data))
    store = Store(data)
    asyncio.run(store.start())
    try:
        session = store.create_session(title="New work")
        run = store.admit(
            session.id, request_id="a" * 32, content="new evidence", profile="default"
        )
        store.transition_run(
            run.id,
            RunState.INTERRUPTED,
            reason=Failure(code=ErrorCode.PROCESS_INTERRUPTED, message="stopped"),
        )
        job = store.jobs()[0]
        store._commit(
            (
                job.model_copy(update={"deleted": True}),
                Occurrence(
                    origin=Origin.SCHEDULED,
                    job_id=job.id,
                    due_at=utc_now(),
                    state="uncertain",
                ),
            )
        )
    finally:
        asyncio.run(store.stop())
    with pytest.raises(ValueError, match="new evidence"):
        migration.verify(snap, data)
    before = {str(p.relative_to(data)): migration.metadata(p) for p in migration.tree(data)}
    result = migration.reconcile(data, data.parent / "after-rollback.json")
    assert result["rollback"] == "reconciliation_required"
    report = json.loads((data.parent / "after-rollback.json").read_bytes())
    assert report["counts"]["job"] == 1 and report["counts"]["occurrence"] == 1
    assert report["counts"]["message"] == 1 and report["reverse_import"] == "unsupported"
    assert {str(p.relative_to(data)): migration.metadata(p) for p in migration.tree(data)} == before


def test_active_owner_and_unsafe_destinations_refused(copied: tuple[Path, Path, Path]) -> None:
    legacy, repo, snap = copied
    with (legacy / ".runtime.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="runtime is active"):
            migration.snapshot(legacy, snap, legacy_repo=repo)
    assert not snap.exists()
    with pytest.raises(ValueError, match="separate"):
        migration.snapshot(legacy, legacy / "snapshot", legacy_repo=repo)
    migration.snapshot(legacy, snap, legacy_repo=repo)
    with pytest.raises(ValueError, match="separate"):
        asyncio.run(migration.import_snapshot(snap, legacy / "candidate"))
    data = snap.parent / "candidate"
    asyncio.run(migration.import_snapshot(snap, data))
    owner = DataDirectoryLock(data)
    owner.acquire()
    try:
        with pytest.raises(OwnershipError):
            migration.verify(snap, data)
        with pytest.raises(OwnershipError):
            migration.reconcile(data, data.parent / "refused.json")
    finally:
        owner.release()


@pytest.mark.parametrize("damage", ["checksum", "extra", "symlink", "manifest_path"])
def test_snapshot_tampering_refused(copied: tuple[Path, Path, Path], damage: str) -> None:
    snap, data = make_snapshot(copied)
    if damage == "checksum":
        (snap / "legacy/unknown.bin").write_bytes(b"changed")
    elif damage == "extra":
        (snap / "unexpected").touch()
    elif damage == "symlink":
        (snap / "alias").symlink_to(snap / "legacy/unknown.bin")
    else:
        manifest = migration.object_json(snap / "manifest.json")
        manifest["files"]["../escaped"] = {"length": 0, "sha256": digest(b"")}
        write_json(snap / "manifest.json", manifest)
    with pytest.raises(ValueError):
        asyncio.run(migration.import_snapshot(snap, data))
    assert not data.exists()


def test_source_errors_are_preflighted_and_torn_tail_is_retained(
    copied: tuple[Path, Path, Path],
) -> None:
    legacy, repo, snap = copied
    path = legacy / "pack-operations/effects.jsonl"
    path.write_bytes(path.read_bytes() + b'{"state":')
    migration.snapshot(legacy, snap, legacy_repo=repo)
    data = snap.parent / "candidate"
    assert asyncio.run(migration.import_snapshot(snap, data))["anomalies"] == 1
    assert migration.verify(snap, data)["anomalies"] == 1
    assert (
        data / "imports/phase13-source/legacy/pack-operations/effects.jsonl"
    ).read_bytes() == path.read_bytes()
    (legacy / "sessions/topic.jsonl").write_bytes(b"invalid complete line\n")
    bad = snap.parent / "bad-snapshot"
    migration.snapshot(legacy, bad, legacy_repo=repo)
    with pytest.raises(ValueError, match="invalid complete"):
        asyncio.run(migration.import_snapshot(bad, snap.parent / "refused"))
    assert not (snap.parent / "refused").exists()


def test_new_writes_block_import_retry_without_recovery(copied: tuple[Path, Path, Path]) -> None:
    snap, data = make_snapshot(copied)
    asyncio.run(migration.import_snapshot(snap, data))
    store = Store(data)
    asyncio.run(store.start())
    try:
        session = store.create_session()
        run = store.admit(session.id, request_id="b" * 32, content="queued", profile="default")
    finally:
        asyncio.run(store.stop())
    with pytest.raises(ValueError, match="new evidence"):
        asyncio.run(migration.import_snapshot(snap, data))
    with sqlite3.connect(data / "state.db") as db:
        assert (
            json.loads(db.execute("SELECT body FROM entities WHERE id=?", (run.id,)).fetchone()[0])[
                "state"
            ]
            == "queued"
        )


def test_verifier_rejects_operational_tampering(copied: tuple[Path, Path, Path]) -> None:
    snap, data = make_snapshot(copied)
    asyncio.run(migration.import_snapshot(snap, data))
    with sqlite3.connect(data / "state.db") as db:
        db.execute(
            "UPDATE entities SET body=json_set(body,'$.enabled',json('true')) WHERE kind='job'"
        )
    with pytest.raises(CorruptionError):
        migration.verify(snap, data)


def test_snapshot_includes_private_config_and_identity(copied: tuple[Path, Path, Path]) -> None:
    legacy, repo, snap = copied
    config = legacy.parent / "config.toml"
    config.write_text('credential = "PRIVATE_CONFIG_SECRET"\n')
    fragments = legacy.parent / "fragments"
    fragments.mkdir()
    (fragments / "SOUL.md").write_bytes(b"identity\r\n")
    migration.snapshot(
        legacy, snap, legacy_repo=repo, includes={"config": config, "identity": fragments}
    )
    assert (snap / "extras/config/config.toml").read_bytes() == config.read_bytes()
    assert (snap / "extras/identity/SOUL.md").read_bytes() == (fragments / "SOUL.md").read_bytes()
    assert "PRIVATE_CONFIG_SECRET" not in (snap / "manifest.json").read_text()


def test_raw_envelope_scoping_does_not_change_fixture_behavior(tmp_path: Path) -> None:
    from solakit.search import _document

    assert _document(Entry(kind="legacy", record={"content": "old fixture"}))[1:3] == ("", "")


def test_conflicting_journals_and_inverted_times_are_reported(
    copied: tuple[Path, Path, Path],
) -> None:
    legacy, repo, snap = copied
    path = legacy / "runs/run.json"
    raw = migration.object_json(path)
    raw["finished_at"] = "2026-11-15T12:00:00+00:00"
    write_json(path, raw)
    migration.snapshot(legacy, snap, legacy_repo=repo)
    data = snap.parent / "candidate"
    result = asyncio.run(migration.import_snapshot(snap, data))
    assert result["anomalies"] == 2
    report = migration.object_json(data / "imports/phase13-report.json")
    assert {a["kind"] for a in report["anomalies"]} == {
        "run_journal_conflict",
        "run_timestamp_inversion",
    }
    assert migration.verify(snap, data)["status"] == "verified"
    store = Store(data)
    asyncio.run(store.start())
    try:
        run = store.run(report["id_map"]["run"]["run"])
        assert run.ended_at == run.created_at
    finally:
        asyncio.run(store.stop())


def test_status_conflict_and_missing_snapshot_never_resume(copied: tuple[Path, Path, Path]) -> None:
    legacy, repo, snap = copied
    raw = migration.object_json(legacy / "runs/run.json")
    write_lines(legacy / "runs/run.events.jsonl", [{**raw, "status": "running"}])
    write_lines(
        legacy / "sessions/topic.jsonl",
        [
            {"role": "user", "content": "orphan snapshot", "run_id": "topic", "ts": AT},
        ],
    )
    migration.snapshot(legacy, snap, legacy_repo=repo)
    data = snap.parent / "candidate"
    asyncio.run(migration.import_snapshot(snap, data))
    store = Store(data)
    asyncio.run(store.start())
    try:
        sid = migration.identity(str(legacy), "session", "topic")
        orphan = migration.identity(str(legacy), "run", "topic")
        assert sid != orphan
        assert store.run(orphan).state == RunState.INTERRUPTED
        assert (
            store.run(migration.identity(str(legacy), "run", "run")).state == RunState.INTERRUPTED
        )
        assert not store.queued_runs()
    finally:
        asyncio.run(store.stop())


def test_copy_interruption_resumes_without_publishing_partial_file(
    copied: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    snap, data = make_snapshot(copied)
    copy = _copy

    def fail_once(source: Path, target: Path) -> dict[str, object]:
        if target.name == ".phase13-copy":
            target.write_bytes(b"partial")
            raise OSError("synthetic copy interruption")
        return copy(source, target)

    with monkeypatch.context() as context:
        context.setattr(migration, "_copy", fail_once)
        with pytest.raises(OSError, match="interruption"):
            asyncio.run(migration.import_snapshot(snap, data))
    asyncio.run(migration.import_snapshot(snap, data))
    assert migration.verify(snap, data)["status"] == "verified"


def test_artifact_pairing_across_archive_boundary(copied: tuple[Path, Path, Path]) -> None:
    legacy, repo, snap = copied
    call = {"role": "assistant", "run_id": "run", "ts": AT, "tool_calls": [{"id": "cross"}]}
    write_lines(legacy / "sessions/topic.jsonl.10", [call])
    write_lines(
        legacy / "sessions/topic.jsonl",
        [
            {
                "role": "tool",
                "content": "result",
                "run_id": "run",
                "ts": AT,
                "tool_call_id": "cross",
            },
        ],
    )
    migration.snapshot(legacy, snap, legacy_repo=repo)
    manifest = migration.validate_snapshot(snap)
    report = migration.audit(snap, manifest, migration.catalog(snap, manifest))
    assert not report["tool_pairing"]
    assert report["artifact_references"] == [
        {"run": "run", "file": "legacy/runs/run.prompt.txt", "status": "matched"},
    ]


def test_cli_inventory_and_redacted_validation_error(
    copied: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    snap, data = make_snapshot(copied)
    report = data.parent / "before.json"
    monkeypatch.setattr(sys, "argv", ["migration", "inventory", str(snap), str(report)])
    migration.main()
    assert json.loads(capsys.readouterr().out)["status"] == "inventoried"
    assert report.is_file()
    (snap / "legacy/sessions/topic.meta.json").write_bytes(b'{"invalid":"PRIVATE_SECRET"}')
    monkeypatch.setattr(sys, "argv", ["migration", "import", str(snap), str(data)])
    with pytest.raises(SystemExit) as error:
        migration.main()
    assert error.value.code == 2
    assert "PRIVATE_SECRET" not in capsys.readouterr().out


def test_runs_follow_transcript_order_instead_of_identifier_order(
    copied: tuple[Path, Path, Path],
) -> None:
    legacy, repo, snap = copied
    run = migration.object_json(legacy / "runs/run.json")
    for old in ("z-first", "a-second"):
        write_json(legacy / f"runs/{old}.json", {**run, "id": old})
    write_lines(
        legacy / "sessions/topic.jsonl.2",
        [
            {"role": "user", "content": "first", "run_id": "z-first", "ts": AT},
        ],
    )
    write_lines(
        legacy / "sessions/topic.jsonl.10",
        [
            {"role": "user", "content": "second", "run_id": "a-second", "ts": AT},
        ],
    )
    migration.snapshot(legacy, snap, legacy_repo=repo)
    data = snap.parent / "candidate"
    asyncio.run(migration.import_snapshot(snap, data))
    store = Store(data)
    asyncio.run(store.start())
    try:
        sid = migration.identity(str(legacy), "session", "topic")
        assert [r.id for _, r in store.runs(sid)] == [
            migration.identity(str(legacy), "run", old) for old in ("z-first", "a-second", "run")
        ]
    finally:
        asyncio.run(store.stop())


@pytest.mark.parametrize("kind", ["artifact", "login"])
def test_rollback_detects_new_state_without_conversation_entries(
    copied: tuple[Path, Path, Path],
    kind: str,
) -> None:
    snap, data = make_snapshot(copied)
    asyncio.run(migration.import_snapshot(snap, data))
    if kind == "artifact":
        (data / "artifacts" / ("c" * 32)).write_bytes(b"unlinked but preserved")
    else:
        with sqlite3.connect(data / "state.db") as db:
            db.execute("INSERT INTO login_sessions VALUES ('hash','csrf',100,0)")
    result = migration.reconcile(data, data.parent / "rollback.json")
    assert result["new_entries"] == 0 and result["rollback"] == "reconciliation_required"


def test_snapshot_captures_committed_wal_without_touching_source(
    copied: tuple[Path, Path, Path],
) -> None:
    legacy, repo, snap = copied
    db = sqlite3.connect(legacy / "app.db")
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO messages_fts VALUES ('committed WAL content')")
        db.commit()
        before = {str(p.relative_to(legacy)): migration.metadata(p) for p in migration.tree(legacy)}
        assert "app.db-wal" in before
        migration.snapshot(legacy, snap, legacy_repo=repo)
        assert {
            str(p.relative_to(legacy)): migration.metadata(p) for p in migration.tree(legacy)
        } == before
        with sqlite3.connect(snap / "legacy/app.db") as copied_db:
            assert copied_db.execute("SELECT count(*) FROM messages_fts").fetchone()[0] == 2
        assert not (snap / "legacy/app.db-wal").exists()
        migration.validate_snapshot(snap)
    finally:
        db.close()


def test_jobs_bound_to_old_ephemeral_sessions_stay_disabled(
    copied: tuple[Path, Path, Path],
) -> None:
    legacy, repo, snap = copied
    path = legacy / "sessions/topic.meta.json"
    raw = migration.object_json(path)
    write_json(path, {**raw, "memory_scope": "ephemeral"})
    migration.snapshot(legacy, snap, legacy_repo=repo)
    data = snap.parent / "candidate"
    asyncio.run(migration.import_snapshot(snap, data))
    store = Store(data)
    asyncio.run(store.start())
    try:
        assert all(j.session_id is None and not j.enabled for j in store.jobs())
    finally:
        asyncio.run(store.stop())
