import asyncio
import fcntl
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.migration import identity, import_legacy
from solakit.config import MemorySettings
from solakit.jobs import CONTROL_ID
from solakit.memory import MemoryService, OpenViking
from solakit.records import ModelStep, RunState
from solakit.redaction import Redactor
from solakit.search import search
from solakit.store import Store

AT = "2026-11-16T12:00:00+00:00"


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture
def legacy(tmp_path: Path) -> Path:
    root = tmp_path / "legacy"
    write(
        root / "sessions/topic.meta.json",
        {
            "id": "topic",
            "title": "Original title",
            "memory_scope": "isolated",
            "created_at": AT,
            "pinned": True,
            "archived_at": AT,
            "future": "keep me",
        },
    )
    (root / ".runtime.lock").touch()
    write(
        root / "runs/z-first.json",
        {
            "id": "z-first",
            "session": "topic",
            "profile": "interactive",
            "status": "completed",
            "created_at": AT,
            "finished_at": "2026-11-15T12:00:00+00:00",
            "memory_commit": {"state": "uncertain"},
        },
    )
    write(root / "runs/z-first.tool-schemas.json", [{"type": "function"}])
    (root / "runs/z-first.prompt.txt").write_bytes(b"original prompt\r\n")
    for name, role, content, run in [
        ("topic.jsonl.2", "user", "archive two", "z-first"),
        ("topic.jsonl.10", "assistant", "archive ten", "z-first"),
        ("topic.jsonl", "user", "current turn", "a-second"),
    ]:
        path = root / "sessions" / name
        path.write_bytes(
            (
                json.dumps({"role": role, "content": content, "run_id": run, "ts": AT}) + "\r\n"
            ).encode()
        )
    with (root / "sessions/topic.jsonl").open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "role": "tool",
                    "content": "tool evidence needle",
                    "run_id": "a-second",
                    "tool_call_id": "call",
                    "ts": AT,
                }
            )
            + "\n"
        )
    write(root / "pack-operations/effects.jsonl", {"state": "uncertain", "id": "intent"})
    (root / "pack-operations/pack.py").write_text('raise AssertionError("never load me")\n')
    write(root / "ui-sessions.json", {"login": "PRIVATE_LOGIN_TOKEN"})
    (root / "unknown.bin").write_bytes(b"\x00\xff\r\n")
    with sqlite3.connect(root / "app.db") as db:
        db.execute("CREATE TABLE job_definitions(id TEXT PRIMARY KEY,payload TEXT,deleted INTEGER)")
        db.execute("CREATE TABLE messages_fts(text TEXT)")
        db.execute("INSERT INTO messages_fts VALUES ('DERIVED_INDEX_NEEDLE')")
        for name, deleted in (("once", 0), ("deleted", 1)):
            payload = {
                "id": name,
                "profile": "interactive",
                "task": "Old task",
                "run_at": AT,
                "bind_session": "topic",
                "enabled": True,
                "tools": ["mail.send"],
            }
            db.execute(
                "INSERT INTO job_definitions VALUES (?,?,?)", (name, json.dumps(payload), deleted)
            )
            write(root / f"jobs/{name}.jsonl", {"consumed": True, "active": False})
    return root


def test_direct_import_preserves_history_and_does_not_replay(legacy: Path, tmp_path: Path) -> None:
    before = hashes(legacy)
    destination = tmp_path / "v2"
    counts = asyncio.run(import_legacy(legacy, destination))
    assert counts["Session"] == 1 and counts["Run"] == 2 and counts["Job"] == 2
    assert hashes(legacy) == before
    store = Store(destination)
    asyncio.run(store.start())
    sid = identity(str(legacy), "session", "topic")
    try:
        session = store.session(sid)
        assert session.title == "Original title" and session.archived and session.pinned
        assert [m.content for _, m in store.messages(sid)] == [
            "archive two",
            "archive ten",
            "current turn",
        ]
        runs = [r for _, r in store.runs(sid)]
        assert [r.id for r in runs] == [
            identity(str(legacy), "run", old) for old in ("z-first", "a-second")
        ]
        assert runs[0].state == RunState.COMPLETED and runs[0].ended_at == runs[0].created_at
        assert runs[1].state == RunState.INTERRUPTED and not store.queued_runs()
        steps = [r for _, r in store.run_records(runs[0].id) if isinstance(r, ModelStep)]
        assert [r.index for r in steps] == [1]
        jobs = store.jobs(include_deleted=True)
        assert all(
            j.consumed
            and not j.enabled
            and not j.tools
            and not j.workspace_roots
            and j.delivery == "none"
            for j in jobs
        )
        assert sum(j.deleted for j in jobs) == 1
        assert json.loads(
            store.db.execute("SELECT body FROM entities WHERE id=?", (CONTROL_ID,)).fetchone()[0]
        )["paused"]
        memory = MemoryService(
            store,
            OpenViking(MemorySettings(), Redactor()),
            lambda admitted: pytest.fail("historical memory cannot enqueue"),
        )
        asyncio.run(memory.tick())
        assert not store.db.execute(
            "SELECT 1 FROM entities WHERE kind IN ('receipt','memory','delivery')"
        ).fetchone()
        assert store.db.execute("SELECT count(*) FROM login_sessions").fetchone()[0] == 0
    finally:
        asyncio.run(store.stop())
    assert len(search(destination, "tool evidence needle", session_id=sid)) == 1
    assert not search(destination, "tool evidence needle", shared_only=True)
    assert not search(destination, "DERIVED_INDEX_NEEDLE")
    assert not search(destination, "PRIVATE_LOGIN_TOKEN")
    raw = destination / "imports/legacy"
    for name in ("unknown.bin", "runs/z-first.prompt.txt", "sessions/topic.meta.json"):
        assert (raw / name).read_bytes() == (legacy / name).read_bytes()
        assert (raw / name).stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        asyncio.run(import_legacy(legacy, destination))


def test_active_runtime_and_source_destination_overlap_refused(
    legacy: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "v2"
    with (legacy / ".runtime.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="stop the legacy runtime"):
            asyncio.run(import_legacy(legacy, destination))
    assert not destination.exists()
    with pytest.raises(ValueError, match="separate"):
        asyncio.run(import_legacy(legacy, legacy / "v2"))
    (legacy / "app.db-wal").write_bytes(b"uncheckpointed")
    with pytest.raises(ValueError, match="WAL remains"):
        asyncio.run(import_legacy(legacy, destination))
    assert not destination.exists()


def test_torn_tail_preserved_and_complete_bad_json_rejected(legacy: Path, tmp_path: Path) -> None:
    path = legacy / "pack-operations/effects.jsonl"
    path.write_bytes(path.read_bytes() + b'\n{"torn":')
    destination = tmp_path / "v2"
    asyncio.run(import_legacy(legacy, destination))
    assert (
        destination / "imports/legacy/pack-operations/effects.jsonl"
    ).read_bytes() == path.read_bytes()
    path.write_bytes(b"invalid complete JSON\n")
    with pytest.raises(ValueError, match="effects.jsonl:1"):
        asyncio.run(import_legacy(legacy, tmp_path / "failed"))
    assert path.read_bytes() == b"invalid complete JSON\n"


def test_source_symlinks_are_not_followed(legacy: Path, tmp_path: Path) -> None:
    (legacy / "alias").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        asyncio.run(import_legacy(legacy, tmp_path / "v2"))
