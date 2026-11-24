import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from solakit.records import (
    CompactionBoundary,
    Delivery,
    DeliveryState,
    InvalidTransition,
    Job,
    MemoryCommit,
    MemoryState,
    Message,
    ModelStep,
    Origin,
    Run,
    RunState,
    Session,
    SessionMode,
    ToolCall,
    ToolReceipt,
    new_id,
)
from solakit.runtime import DataDirectoryLock
from solakit.storage.backup import backup, restore
from solakit.storage.evidence import (
    CorruptionError,
    Entry,
    EvidenceLog,
    StorageError,
    canonical,
    digest,
    parse_entry,
)
from solakit.storage.legacy import import_fixture
from solakit.storage.search import SearchUnavailable, rebuild, search
from solakit.storage.store import Store, no_fault


@contextmanager
def opened(root: Path, *, fault: Callable[[str], None] = no_fault) -> Iterator[Store]:
    owner = DataDirectoryLock(root)
    owner.acquire()
    store = Store(root, fault=fault)
    try:
        asyncio.run(store.start())
        yield store
    finally:
        asyncio.run(store.stop())
        owner.release()


def test_sessions_admission_pages_events_and_restart(tmp_path: Path) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        sessions = [store.create_session(title=f"Topic {index}") for index in range(3)]
        session = sessions[0]
        ephemeral = store.create_session(mode=SessionMode.EPHEMERAL)
        assert store.list_sessions(limit=1) == [session]
        cursor = (session.created_at.isoformat(), session.id)
        assert store.list_sessions(after=cursor) == sessions[1:]
        assert ephemeral not in store.list_sessions()
        request_id = new_id()
        run = store.admit(
            session.id, request_id=request_id, content="Hello 雪 🌞", profile="default"
        )
        assert (
            store.admit(session.id, request_id=request_id, content="Hello 雪 🌞", profile="default")
            == run
        )
        with pytest.raises(ValueError, match="idempotency"):
            store.admit(session.id, request_id=request_id, content="Changed", profile="default")
        first = store.messages(session.id, limit=1)
        store.transition_run(run.id, RunState.RUNNING)
        message = Message(
            origin=Origin.INTERACTIVE,
            session_id=session.id,
            run_id=run.id,
            role="assistant",
            content="Unicode 雪 reply",
        )
        store.append(message)
        store.transition_run(run.id, RunState.COMPLETED)
        assert store.messages(session.id, after=first[0][0]) == [(4, message)]
        assert store.runs(session.id)[0][0] == 1
        assert store.runs(session.id, after=1) == []
        snapshot, high_water = store.snapshot(session.id)
        assert snapshot == session and high_water == 5
        assert [event.sequence for event in store.events(session.id)] == [1, 2, 3, 4, 5]
        assert [event.type for event in store.events(session.id, after=3)] == [
            "message.appended",
            "run.terminal",
        ]
        with pytest.raises(InvalidTransition):
            store.transition_run(run.id, RunState.COMPLETED)
        store.update_session(session.id, title="Renamed", archived=True)
        assert store.list_sessions(archived=True)[0].title == "Renamed"
        assert store.messages(session.id)[0] == first[0]
        with pytest.raises(ValueError, match="archived"):
            store.admit(session.id, request_id=new_id(), content="no", profile="default")
        store.update_session(session.id, archived=False)
        store.admit(ephemeral.id, request_id=new_id(), content="once", profile="default")
        with pytest.raises(ValueError, match="one run"):
            store.admit(ephemeral.id, request_id=new_id(), content="twice", profile="default")
    with opened(root) as store:
        assert store.request(request_id) == store.run(run.id)
        assert store.run(run.id).state == RunState.COMPLETED
        assert len(store.messages(session.id)) == 2
        assert store.search_status == "available"
        assert len(list(store.log.scan())) == 13
    assert os.stat(root / "state.db").st_mode & 0o777 == 0o600
    assert os.stat(root / "evidence").st_mode & 0o777 == 0o700


def test_session_title_search_survives_restart(tmp_path: Path) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        titles = ["Planning", "Straße café 雪", "100%_done", "it's a title", ""]
        sessions = [store.create_session(title=title) for title in titles]
        for query, expected in [
            (" PLAN ", [sessions[0]]),
            ("STRASSE CAFÉ", [sessions[1]]),
            ("雪", [sessions[1]]),
            ("%_", [sessions[2]]),
            ("it's", [sessions[3]]),
            ("' OR 1=1 --", []),
            ("missing", []),
            ("   ", sessions),
        ]:
            assert store.list_sessions(q=query) == expected
        store.update_session(sessions[0].id, title="Renamed")
        assert store.list_sessions(q="planning") == []
    with opened(root) as store:
        assert store.list_sessions(q="STRASSE") == [sessions[1]]
        assert store.list_sessions(q="renamed")[0].id == sessions[0].id


def test_bulk_archive_is_atomic_idempotent_and_durable(tmp_path: Path) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        first = store.create_session(title="Keep title", mode=SessionMode.ISOLATED)
        second = store.create_session()
        job = Job(
            origin=Origin.SCHEDULED,
            profile="default",
            task="Keep job",
            schedule="cron:0 12 * * *",
            session_id=first.id,
        )
        store._commit((job,), details={"session_id": first.id})
        store.update_session(first.id, pinned=True)
        with pytest.raises(KeyError):
            store.archive_sessions([first.id, new_id()])
        assert not store.session(first.id).archived
        assert store.archive_sessions([first.id, second.id, first.id]) == 2
        assert store.archive_sessions([first.id, second.id]) == 0
        assert store.session(first.id).pinned
        assert store.session(first.id).mode == SessionMode.ISOLATED
        assert store.session(first.id).title == "Keep title"
    with opened(root) as store:
        assert store.job(job.id) == job
        assert store.list_sessions() == []
        assert len(store.list_sessions(archived=True)) == 2
        store.update_session(first.id, archived=False)
        store.admit(first.id, request_id=new_id(), content="restored", profile="default")


def test_session_catalog_uses_stable_creation_keys_and_recorded_admission_dates(
    tmp_path: Path,
) -> None:
    created = datetime(2026, 11, 23, tzinfo=UTC)
    activity = created + timedelta(days=1, microseconds=1)
    earlier = Session(id=f"{0:032x}", created_at=created, origin=Origin.SYSTEM)
    once = Session(
        id=f"{1:032x}",
        created_at=created,
        origin=Origin.SYSTEM,
        mode=SessionMode.EPHEMERAL,
        archived=True,
    )
    delegated = Session(
        id=f"{2:032x}",
        created_at=created,
        origin=Origin.DELEGATED,
        mode=SessionMode.ISOLATED,
        parent_session_id=earlier.id,
    )
    with opened(tmp_path / "data") as store:
        topics = (earlier, once, delegated)

        def prepare() -> None:
            store.db.executemany("INSERT INTO sessions VALUES (?,0)", [(s.id,) for s in topics])

        store._commit(topics, prepare=prepare)
        runs = tuple(
            Run(
                origin=Origin.SYSTEM,
                session_id=once.id,
                request_id=new_id(),
                profile=name,
                created_at=at,
                started_at=at,
                ended_at=at + timedelta(days=30),
                state=RunState.COMPLETED,
            )
            for name, at in (
                ("legacy", activity),
                ("default", activity - timedelta(microseconds=1)),
            )
        )
        store._commit(runs)

        def catalog(
            after: tuple[str, str] | None = None,
            *,
            profile: str | None = None,
            activity_before: datetime | None = None,
        ) -> tuple[list[tuple[Session, datetime]], bool]:
            return store.session_catalog(
                after=after,
                limit=1,
                archived=None,
                q="",
                profile=profile,
                mode=None,
                created_from=created,
                created_before=created + timedelta(days=1),
                activity_from=None,
                activity_before=activity_before,
            )

        assert catalog() == ([(delegated, created)], True)
        assert catalog((created.isoformat(), delegated.id)) == ([(once, activity)], True)
        assert catalog((created.isoformat(), once.id)) == ([(earlier, created)], False)
        assert catalog(profile="default", activity_before=activity) == ([], False)
        assert catalog(profile="default", activity_before=activity + timedelta(microseconds=1)) == (
            [(once, activity)],
            False,
        )


def test_restart_skips_archived_history_but_reads_still_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import solakit.storage.evidence as evidence

    root = tmp_path / "data"
    with opened(root) as store:
        archived = store.create_session()
        run = store.admit(
            archived.id, request_id=new_id(), content="old history", profile="default"
        )
        store.transition_run(run.id, RunState.CANCELLED)
        message = store.messages(archived.id)[0][1]
        reference = store.log.references[
            store.db.execute("SELECT entry_id FROM entities WHERE id=?", (message.id,)).fetchone()[
                0
            ]
        ]
        active = store.create_session()
        store.admit(active.id, request_id=new_id(), content="current history", profile="default")
        store.archive_sessions([archived.id])

    parsed: list[Entry] = []

    def counted(data: bytes) -> Entry:
        entry = parse_entry(data)
        parsed.append(entry)
        return entry

    monkeypatch.setattr(evidence, "parse_entry", counted)
    with opened(root) as store:
        assert not any(entry.event and entry.event.session_id == archived.id for entry in parsed)
        assert any(entry.event and entry.event.session_id == active.id for entry in parsed)
        assert store.search_status == "available"
        assert store.messages(archived.id)[0][1] == message
        assert search(root, "old history")
        segment = root / "evidence" / reference.segment
        with segment.open("r+b") as stream:
            stream.seek(reference.offset)
            raw = stream.read(reference.length)
            stream.seek(reference.offset)
            stream.write(raw.replace(b"old history", b"bad history"))
        with pytest.raises(CorruptionError, match="corrupt evidence offset"):
            store.messages(archived.id)


def test_missing_archived_evidence_blocks_startup(tmp_path: Path) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        session = store.create_session()
        store.admit(session.id, request_id=new_id(), content="old", profile="default")
        store.archive_sessions([session.id])
    path = root / "evidence/00000001.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:1] + lines[2:]))
    with pytest.raises(CorruptionError):
        with opened(root):
            pass


@pytest.mark.parametrize(
    "stage", ["state_commit", "append", "fsync", "outbox_complete", "acknowledge"]
)
def test_bulk_archive_recovers_the_whole_batch(tmp_path: Path, stage: str) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        sessions = [store.create_session() for _ in range(2)]

        def fail(current: str) -> None:
            if current == stage:
                raise OSError("process stopped")

        store.fault = fail
        with pytest.raises(OSError, match="process stopped"):
            store.archive_sessions([session.id for session in sessions])
    with opened(root) as store:
        assert all(store.session(session.id).archived for session in sessions)
        assert len(list(store.log.scan())) == 4


@pytest.mark.parametrize(
    "stage", ["state_commit", "append", "fsync", "outbox_complete", "acknowledge"]
)
@pytest.mark.parametrize("second", [False, True])
def test_fault_boundaries_reconcile_once(tmp_path: Path, stage: str, second: bool) -> None:
    root = tmp_path / "data"
    request = new_id()
    with opened(root) as store:
        session = store.create_session()
        calls = 0

        def fault(current: str) -> None:
            nonlocal calls
            if current == stage:
                calls += 1
                if calls == (
                    2 if second and stage in ("append", "fsync", "outbox_complete") else 1
                ):
                    raise OSError("injected crash")

        store.fault = fault
        with pytest.raises(OSError, match="injected"):
            store.admit(session.id, request_id=request, content="retained", profile="default")
        with pytest.raises(StorageError):
            store.request(request)
    with opened(root) as store:
        recovered = store.request(request)
        assert recovered is not None
        assert (
            store.admit(session.id, request_id=request, content="retained", profile="default")
            == recovered
        )
        assert len(store.messages(session.id)) == 1
        assert len(list(store.log.scan())) == 3
        assert store.db.execute("SELECT count(*) FROM outbox WHERE delivered=0").fetchone()[0] == 0


@pytest.mark.parametrize(
    "stage", ["state_commit", "append", "fsync", "outbox_complete", "acknowledge"]
)
def test_actual_process_death(tmp_path: Path, stage: str) -> None:
    root = tmp_path / "data"
    request = new_id()
    with opened(root) as store:
        session = store.create_session()
    code = """
import asyncio, os, sys
from pathlib import Path
from typing import Any
from solakit.runtime import DataDirectoryLock
from solakit.storage.store import Store
root, session, request, stage = sys.argv[1:]
owner = DataDirectoryLock(Path(root)); owner.acquire()
store = Store(Path(root)); asyncio.run(store.start())
def fault(current):
    if current == stage: os._exit(73)
store.fault = fault
store.admit(session, request_id=request, content="process death", profile="default")
"""
    result = subprocess.run([sys.executable, "-c", code, str(root), session.id, request, stage])
    assert result.returncode == 73
    with opened(root) as store:
        assert store.request(request) is not None
        assert len(store.messages(session.id)) == 1
        assert len(list(store.log.scan())) == 3


def test_torn_tail_sealed_and_pending_entry_replayed(tmp_path: Path) -> None:
    root = tmp_path / "data"
    request = new_id()
    with opened(root) as store:
        session = store.create_session()

        def fail(stage: str) -> None:
            if stage == "state_commit":
                raise OSError("stop")

        store.fault = fail
        with pytest.raises(OSError):
            store.admit(session.id, request_id=request, content="survives", profile="default")
    segment = root / "evidence/00000001.jsonl"
    original = segment.read_bytes()
    tail = b'{"schema_version":1,"id":"torn'
    with segment.open("ab") as stream:
        stream.write(tail)
    with opened(root) as store:
        assert store.request(request) is not None
        assert store.log.active == "00000002.jsonl"
        assert len(list(store.log.scan())) == 3
        assert segment.read_bytes() == original + tail
        note = json.loads((root / "recovery/00000001.json").read_text())
        assert note["offset"] == len(original) and note["sha256"] == digest(tail)
        assert search(root, "survives")
    with opened(root) as store:
        assert len(list(store.log.scan())) == 3
    segment.write_bytes(original + tail + b"changed")
    with pytest.raises(CorruptionError, match="tail changed"):
        with opened(root):
            pass


@pytest.mark.parametrize(
    "damage", ["interior", "conflict", "missing", "state", "offset", "missing-state"]
)
def test_corruption_blocks_recovery(tmp_path: Path, damage: str) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        session = store.create_session()
        store.admit(session.id, request_id=new_id(), content="original", profile="default")
    segment = root / "evidence/00000001.jsonl"
    lines = segment.read_bytes().splitlines(keepends=True)
    if damage == "interior":
        segment.write_bytes(b"bad json\n" + b"".join(lines[1:]))
    elif damage == "conflict":
        other = json.loads(lines[0])
        other["record"]["title"] = "different"
        with segment.open("ab") as stream:
            stream.write(canonical(other) + b"\n")
    elif damage == "missing":
        segment.write_bytes(b"".join(lines[:-1]))
    elif damage == "missing-state":
        (root / "state.db").unlink()
    else:
        with sqlite3.connect(root / "state.db") as db:
            if damage == "state":
                db.execute("UPDATE entities SET body='{}' WHERE kind='session'")
            else:
                db.execute("UPDATE outbox SET reference='{}'")
    with pytest.raises(CorruptionError):
        with opened(root):
            pass


@pytest.mark.parametrize("archived", [False, True])
def test_lineage_recovery_and_terminal_lifecycles(tmp_path: Path, archived: bool) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        session = store.create_session()
        run = store.admit(session.id, request_id=new_id(), content="input", profile="default")
        store.transition_run(run.id, RunState.RUNNING)
        fields: dict[str, Any] = dict(
            origin=Origin.INTERACTIVE, session_id=session.id, run_id=run.id
        )
        model = ModelStep(**fields, index=1, model="fixture")
        store.append(model)
        with pytest.raises(ValueError, match="index"):
            store.append(ModelStep(**fields, index=1, model="fixture"))
        call = ToolCall(
            **fields,
            model_step_id=model.id,
            provider_tool_call_id="provider",
            name="fixture",
            arguments={},
        )
        store.append(call)
        message = Message(**fields, role="assistant", content="pending delivery")
        store.append(message)
        delivery = Delivery(**fields, message_id=message.id)
        memory = MemoryCommit(**fields)
        store.append(delivery)
        store.transition_delivery(delivery.id, DeliveryState.SENDING)
        store.append(memory)
        store.transition_memory(memory.id, MemoryState.RUNNING)
        other = store.create_session()
        other_run = store.admit(other.id, request_id=new_id(), content="other", profile="default")
        with pytest.raises(ValueError, match="lineage"):
            store.append(
                Message(**(fields | {"session_id": other.id}), role="assistant", content="cross")
            )
        with pytest.raises(ValueError, match="crosses"):
            store.append(
                ToolCall(
                    **(fields | {"session_id": other.id, "run_id": other_run.id}),
                    model_step_id=model.id,
                    provider_tool_call_id="cross",
                    name="fixture",
                    arguments={},
                )
            )
        with pytest.raises(ValueError, match="crosses"):
            store.append(
                CompactionBoundary(
                    **fields,
                    through_message_id=store.messages(other.id)[0][1].id,
                    summary_message_id=message.id,
                    source_message_ids=(store.messages(other.id)[0][1].id,),
                )
            )
        if archived:
            store.archive_sessions([session.id])
    with opened(root) as store:
        assert store.run(run.id).state == RunState.INTERRUPTED
        records = [record for _, record in store.run_records(run.id)]
        assert (
            next(record for record in records if isinstance(record, Delivery)).state
            == DeliveryState.UNCERTAIN
        )
        assert (
            next(record for record in records if isinstance(record, MemoryCommit)).state
            == MemoryState.AMBIGUOUS
        )
        assert any(isinstance(record, ToolCall) for record in records)
        receipt = next(record for record in records if isinstance(record, ToolReceipt))
        assert receipt.status == "uncertain" and receipt.effect == "uncertain"
        with pytest.raises(InvalidTransition):
            store.transition_delivery(delivery.id, DeliveryState.DELIVERED)


@pytest.mark.parametrize("identity", ["../secret", "A" * 32, "", "a" * 33, "/tmp/path"])
def test_invalid_ids_never_form_paths(tmp_path: Path, identity: str) -> None:
    with opened(tmp_path / "data") as store:
        for operation in (
            lambda: store.session(identity),
            lambda: store.request(identity),
            lambda: store.artifact(identity, b"no"),
        ):
            with pytest.raises(ValueError):
                operation()


def test_index_failure_does_not_negate_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import solakit.storage.search as projection

    root = tmp_path / "data"
    with opened(root) as store:
        session = store.create_session()

        def broken(log: EvidenceLog) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(projection, "update_index", broken)
        run = store.admit(
            session.id, request_id=new_id(), content="durable indexing outage", profile="default"
        )
        assert store.search_status == "degraded"
        assert store.request(run.request_id) == run
        with pytest.raises(SearchUnavailable, match="stale"):
            search(root, "durable")
    monkeypatch.undo()
    with opened(root) as store:
        assert store.request(run.request_id) is not None
        assert store.search_status == "available"
        assert search(root, "durable")


def test_append_fsync_failure_is_unacknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        session = store.create_session()
        request = new_id()

        def full(fd: int) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(os, "fsync", full)
        with pytest.raises(OSError, match="disk full"):
            store.admit(session.id, request_id=request, content="retain", profile="default")
    monkeypatch.undo()
    with opened(root) as store:
        assert store.request(request) is not None
        assert len(store.messages(session.id)) == 1


def test_search_rebuild_is_independent_deterministic_and_readonly(tmp_path: Path) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        session = store.create_session()
        store.admit(
            session.id, request_id=new_id(), content='café 雪 quoted "OR" word', profile="default"
        )
        with store.db:
            store.db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, enabled INTEGER)")
            store.db.execute("INSERT INTO jobs VALUES ('fixture-job',1)")
            store.db.execute("CREATE TABLE tombstones(id TEXT PRIMARY KEY)")
            store.db.execute("INSERT INTO tombstones VALUES ('never-resurrect')")
        before = search(root, "café")
        assert before and search(root, "雪")
        assert search(root, '" OR "')
        assert search(root, "café", session_id=new_id()) == []
    authoritative = {
        str(path.relative_to(root)): digest(path.read_bytes())
        for path in root.rglob("*")
        if path.is_file() and path.name != "search.db"
    }
    (root / "search.db").unlink()
    with pytest.raises(SearchUnavailable):
        search(root, "café")
    assert rebuild(root) == 3
    assert search(root, "café") == before
    assert rebuild(root) == 3
    assert search(root, "café") == before
    assert authoritative == {
        str(path.relative_to(root)): digest(path.read_bytes())
        for path in root.rglob("*")
        if path.is_file() and path.name != "search.db"
    }
    # No state database is needed at all to rebuild or search.
    (root / "state.db").rename(root / "state.saved")
    assert rebuild(root) == 3
    assert search(root, "café") == before
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from solakit.cli import main
sys.argv = ["sola", "search", "café", "--data-dir", sys.argv[1]]
main()
assert "fastapi" not in sys.modules
assert "openai" not in sys.modules
assert "solakit.runtime" not in sys.modules
""",
            str(root),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["results"] == before


def test_stale_search_offsets_and_corrupt_sources(tmp_path: Path) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        session = store.create_session()
        store.admit(session.id, request_id=new_id(), content="needle", profile="default")
    with sqlite3.connect(root / "search.db") as db:
        row = db.execute(
            "SELECT entry_id,reference FROM refs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        ref = json.loads(row[1])
        ref["offset"] += 1
        db.execute("UPDATE refs SET reference=? WHERE entry_id=?", (json.dumps(ref), row[0]))
    with pytest.raises(CorruptionError, match="offset"):
        search(root, "needle")
    rebuild(root)
    segment = root / "evidence/00000001.jsonl"
    segment.write_bytes(segment.read_bytes().replace(b"needle", b"damage"))
    with pytest.raises(CorruptionError, match="fingerprint"):
        search(root, "needle")


def test_backup_restore_state_evidence_raw_imports_and_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "data"
    fixture = tmp_path / "legacy.jsonl"
    raw = (
        b'{"archived":true,"messages":[{"role":"tool","tool_call_id":"old",'
        b'"content":"legacy needle"}],"unknown":{"keep":1}}\n'
    )
    fixture.write_bytes(raw)
    with opened(root) as store:
        session = store.create_session()
        run = store.admit(
            session.id, request_id=new_id(), content="backup needle", profile="default"
        )
        artifact_id = new_id()
        store.artifact(artifact_id, b"request bytes")
        assert import_fixture(store, fixture) == 1
        assert import_fixture(store, fixture) == 0
        second = tmp_path / "duplicate" / "legacy.jsonl"
        second.parent.mkdir()
        second.write_text('{"content":"different source same filename","extra":true}\n')
        assert import_fixture(store, second) == 1
        assert (root / "imports" / digest(raw)).read_bytes() == raw
        assert any(entry.record.get("unknown") == {"keep": 1} for _, entry in store.log.scan())
        with store.db:
            store.db.execute("CREATE TABLE jobs (id TEXT, enabled INTEGER)")
            store.db.execute("INSERT INTO jobs VALUES ('kept',1)")
            store.db.execute("CREATE TABLE tombstones (id TEXT)")
            store.db.execute("INSERT INTO tombstones VALUES ('gone')")
        destination = tmp_path / "checkpoint"
        backup(store, destination)
        assert not (destination / "search.db").exists()
    restored = tmp_path / "restored"
    restore(destination, restored)
    with opened(restored) as store:
        assert store.request(run.request_id) == run
        assert store.session(session.id) == session
        assert store.db.execute("SELECT * FROM jobs").fetchall()[0][0] == "kept"
        assert store.db.execute("SELECT * FROM tombstones").fetchall()[0][0] == "gone"
        assert (restored / "artifacts" / artifact_id).read_bytes() == b"request bytes"
        assert (restored / "imports" / digest(raw)).read_bytes() == raw
        assert import_fixture(store, fixture) == 0
        assert len(search(restored, "needle")) == 2
    with pytest.raises(ValueError, match="new destination"):
        restore(destination, restored)
    (destination / "state.db").write_bytes(b"tampered")
    with pytest.raises(CorruptionError, match="validation"):
        restore(destination, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_artifact_conflict_and_page_bounds(tmp_path: Path) -> None:
    with opened(tmp_path / "data") as store:
        session = store.create_session()
        identity = new_id()
        assert store.artifact(identity, b"exact") == store.artifact(identity, b"exact")
        with pytest.raises(CorruptionError):
            store.artifact(identity, b"different")
        for limit in (0, 101, True):
            with pytest.raises(ValueError):
                store.messages(session.id, limit=limit)
        with pytest.raises(ValueError):
            store.events(session.id, after=-1)
        with pytest.raises(ValueError):
            search(store.root, "")


def test_duplicate_keys_and_unsupported_schema_are_corruption(tmp_path: Path) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        store.create_session()
    segment = root / "evidence/00000001.jsonl"
    data = segment.read_bytes()
    segment.write_bytes(data.replace(b'{"details":', b'{"schema_version":999,"details":', 1))
    with pytest.raises(CorruptionError):
        list(EvidenceLog(root).scan())
    segment.write_bytes(
        Entry(kind="legacy", record={"preserved": True})
        .encoded()
        .replace(b'"schema_version":1', b'"schema_version":999')
    )
    with pytest.raises(CorruptionError):
        list(EvidenceLog(root).scan())


def test_partial_write_preserves_tail_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        session = store.create_session()
        request = new_id()
        segment = root / "evidence/00000001.jsonl"
        original = segment.read_bytes()

        def partial(entry: Entry, fault: Callable[[str], None]) -> None:
            with segment.open("ab") as stream:
                stream.write(entry.encoded()[:37])
            raise OSError("disk full during write")

        monkeypatch.setattr(store.log, "append", partial)
        with pytest.raises(OSError, match="disk full"):
            store.admit(session.id, request_id=request, content="partial write", profile="default")
        preserved = segment.read_bytes()
        assert preserved.startswith(original) and len(preserved) == len(original) + 37
    with opened(root) as store:
        assert store.request(request) is not None
        assert segment.read_bytes() == preserved
        assert len(store.messages(session.id)) == 1
        assert len(list(store.log.scan())) == 3


def test_failed_rebuild_preserves_previous_projection(tmp_path: Path) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        store.create_session(title="valid")
    previous = (root / "search.db").read_bytes()
    segment = root / "evidence/00000001.jsonl"
    with segment.open("ab") as stream:
        stream.write(b"invalid complete line\n")
    with pytest.raises(CorruptionError):
        rebuild(root)
    assert (root / "search.db").read_bytes() == previous
    assert not list(root.glob(".search-*.db"))


@pytest.mark.parametrize("damage", ["routing", "sequence", "admission", "session-row"])
def test_operational_routing_corruption_is_detected(tmp_path: Path, damage: str) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        session = store.create_session()
        store.admit(session.id, request_id=new_id(), content="evidence", profile="default")
    with sqlite3.connect(root / "state.db") as db:
        if damage == "routing":
            db.execute("UPDATE entities SET session_id=? WHERE kind='message'", (new_id(),))
        elif damage == "sequence":
            db.execute("UPDATE entities SET sequence=100 WHERE kind='message'")
        elif damage == "admission":
            db.execute("UPDATE admissions SET fingerprint='wrong'")
        else:
            db.execute("DELETE FROM sessions")
    with pytest.raises(CorruptionError):
        with opened(root):
            pass


def test_incremental_index_only_parses_new_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import solakit.storage.search as projection

    with opened(tmp_path / "data") as store:
        session = store.create_session()
        store.admit(session.id, request_id=new_id(), content="first", profile="default")
        parse = parse_entry
        parsed = []

        def counted(data: bytes) -> Entry:
            entry = parse(data)
            parsed.append(entry.kind)
            return entry

        monkeypatch.setattr(projection, "parse_entry", counted)
        store.admit(session.id, request_id=new_id(), content="second", profile="default")
        assert parsed == ["run", "message"]
        assert len(search(store.root, "second")) == 1


def test_maintenance_commands_and_cli_status(tmp_path: Path) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        session = store.create_session()
        store.admit(session.id, request_id=new_id(), content="offline needle", profile="default")
    script = str(Path("scripts/storage.py").resolve())
    checkpoint = tmp_path / "checkpoint"
    restored = tmp_path / "restored"
    fixture = tmp_path / "legacy.jsonl"
    fixture.write_text('{"content":"legacy imported"}\n')
    for arguments, status in (
        (["rebuild", "--data-dir", str(root)], "rebuilt"),
        (["import-fixture", "--data-dir", str(root), str(fixture)], "imported"),
        (["backup", "--data-dir", str(root), str(checkpoint)], "backed_up"),
        (["restore", str(checkpoint), str(restored)], "restored"),
    ):
        result = subprocess.run(
            [sys.executable, script, *arguments], check=True, capture_output=True, text=True
        )
        assert json.loads(result.stdout)["status"] == status
    config = tmp_path / "offline.toml"
    config.write_text(f'data_dir = "{root}"\n')
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from solakit.cli import main; main()",
            "search",
            "needle",
            "--config",
            str(config),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["status"] == "available"
    (root / "search.db").write_bytes(b"invalid database")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from solakit.cli import main; main()",
            "search",
            "needle",
            "--data-dir",
            str(root),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2 and json.loads(result.stdout)["status"] == "corrupt"
    absent = tmp_path / "absent"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from solakit.cli import main; main()",
            "search",
            "needle",
            "--data-dir",
            str(absent),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2 and json.loads(result.stdout)["status"] == "unavailable"
    assert not absent.exists()


@pytest.mark.parametrize("archived", [False, True])
def test_restore_checks_state_evidence_consistency_before_publication(
    tmp_path: Path, archived: bool
) -> None:
    root = tmp_path / "data"
    checkpoint = tmp_path / "checkpoint"
    with opened(root) as store:
        session = store.create_session()
        if archived:
            store.archive_sessions([session.id])
        backup(store, checkpoint)
    with sqlite3.connect(checkpoint / "state.db") as db:
        db.execute("DELETE FROM sessions")
    db.close()
    manifest = json.loads((checkpoint / "manifest.json").read_bytes())
    data = (checkpoint / "state.db").read_bytes()
    manifest["files"]["state.db"] = {"length": len(data), "sha256": digest(data)}
    (checkpoint / "manifest.json").write_bytes(canonical(manifest))
    with pytest.raises(CorruptionError, match="routing"):
        restore(checkpoint, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


def test_sealed_tail_metadata_is_checked_on_reads(tmp_path: Path) -> None:
    root = tmp_path / "data"
    with opened(root) as store:
        session = store.create_session()
        store.admit(session.id, request_id=new_id(), content="before tail", profile="default")
    segment = root / "evidence/00000001.jsonl"
    with segment.open("ab") as stream:
        stream.write(b"incomplete")
    with opened(root) as store:
        assert search(root, "before tail")
        (root / "recovery/00000001.json").write_text("invalid note")
        with pytest.raises(CorruptionError):
            store.messages(session.id)
        with pytest.raises(CorruptionError):
            search(root, "before tail")


def test_legacy_unknown_identity_fields_remain_searchable(tmp_path: Path) -> None:
    fixture = tmp_path / "legacy.jsonl"
    fixture.write_text(
        '{"id":{"opaque":1},"session_id":["old"],"run_id":42,'
        '"content":"import text","unknown":"retained token"}\n'
    )
    with opened(tmp_path / "data") as store:
        assert import_fixture(store, fixture) == 1
        assert store.search_status == "available"
        assert search(store.root, "import text")
        assert search(store.root, "retained token")


def test_invalid_session_cursor_rejected(tmp_path: Path) -> None:
    with opened(tmp_path / "data") as store:
        for timestamp in ("not a timestamp", "2026-01-01T00:00:00", "2026-01-01T01:00:00+01:00"):
            with pytest.raises(ValueError):
                store.list_sessions(after=(timestamp, new_id()))


def test_copied_inventory_fixtures_preserve_archives_and_tool_links(tmp_path: Path) -> None:
    import shutil

    fixtures = Path("docs/migration/fixtures/legacy-format")
    copied = tmp_path / "copied"
    shutil.copytree(fixtures, copied)
    sources = [
        copied / name
        for name in (
            "sessions/sample.meta.json",
            "sessions/sample.jsonl.1",
            "sessions/sample.jsonl",
            "runs/sample-run.events.jsonl",
            "runs/sample-run.tools.jsonl",
            "jobs/consumed-once.jsonl",
            "pack-operations/sample.jsonl",
        )
    ]
    with opened(tmp_path / "data") as store:
        counts = [import_fixture(store, path) for path in sources]
        assert counts == [1, 1, 3, 1, 1, 1, 1]
        assert all(import_fixture(store, path) == 0 for path in sources)
        entries = [entry for _, entry in store.log.scan()]
        assert entries[0].record["legacy_extra"] == {"preserve": True}
        assert entries[1].details["source_name"] == "sample.jsonl.1"
        calls = entries[2].record["tool_calls"]
        assert isinstance(calls, list) and isinstance(calls[0], dict)
        assert calls[0]["id"] == entries[3].record["tool_call_id"]
        for path in sources:
            assert (
                store.root / "imports" / digest(path.read_bytes())
            ).read_bytes() == path.read_bytes()
        duplicate = copied / "other" / "sample.jsonl"
        duplicate.parent.mkdir()
        shutil.copyfile(copied / "sessions/sample.jsonl", duplicate)
        assert import_fixture(store, duplicate) == 3
        bad = copied / "runs/damaged.events.jsonl"
        with pytest.raises(ValueError):
            import_fixture(store, bad)
        assert (store.root / "imports" / digest(bad.read_bytes())).read_bytes() == bad.read_bytes()
        assert store.search_status == "available"
        assert search(store.root, "runtime.status")


def test_restore_accepts_canonical_alias_to_backup_root(tmp_path: Path) -> None:
    root = tmp_path / "data"
    checkpoint = tmp_path / "checkpoint"
    with opened(root) as store:
        session = store.create_session()
        backup(store, checkpoint)
    alias = tmp_path / "backup-alias"
    alias.symlink_to(checkpoint, target_is_directory=True)
    restore(alias, tmp_path / "restored")
    with opened(tmp_path / "restored") as store:
        assert store.session(session.id) == session


def test_compaction_keeps_sources_and_checks_summary_provenance(tmp_path: Path) -> None:
    with opened(tmp_path / "data") as store:
        session = store.create_session()
        run = store.admit(session.id, request_id=new_id(), content="original", profile="default")
        source = store.messages(session.id)[0][1]
        summary = Message(
            origin=run.origin,
            session_id=session.id,
            run_id=run.id,
            role="assistant",
            content="summary",
        )
        store.append(summary)
        boundary = CompactionBoundary(
            origin=run.origin,
            session_id=session.id,
            run_id=run.id,
            through_message_id=source.id,
            summary_message_id=summary.id,
            source_message_ids=(source.id,),
        )
        store.append(boundary)
        assert [message for _, message in store.messages(session.id)] == [source, summary]
        with pytest.raises(ValueError, match="summary"):
            store.append(
                CompactionBoundary.model_validate(
                    boundary.model_dump()
                    | {
                        "id": new_id(),
                        "summary_message_id": source.id,
                    }
                )
            )
        other = store.admit(session.id, request_id=new_id(), content="next", profile="default")
        with pytest.raises(ValueError, match="crosses"):
            store.append(
                CompactionBoundary.model_validate(
                    boundary.model_dump()
                    | {
                        "id": new_id(),
                        "run_id": other.id,
                    }
                )
            )


def test_receipts_link_to_intents_once(tmp_path: Path) -> None:
    with opened(tmp_path / "data") as store:
        session = store.create_session()
        run = store.admit(session.id, request_id=new_id(), content="intent", profile="default")
        model = ModelStep(
            origin=run.origin, session_id=session.id, run_id=run.id, index=1, model="fixture"
        )
        store.append(model)
        call = ToolCall(
            origin=run.origin,
            session_id=session.id,
            run_id=run.id,
            model_step_id=model.id,
            provider_tool_call_id="provider-id",
            name="fixture",
            arguments={"value": "雪"},
        )
        store.append(call)
        receipt = ToolReceipt(
            origin=run.origin,
            session_id=session.id,
            run_id=run.id,
            tool_call_id=call.id,
            status="succeeded",
            result="recorded",
        )
        store.append(receipt)
        with pytest.raises(ValueError, match="already has a receipt"):
            store.append(ToolReceipt.model_validate(receipt.model_dump() | {"id": new_id()}))
        assert [event.type for event in store.events(session.id)][-2:] == [
            "tool.intent",
            "tool.receipt",
        ]
        query_plan = store.db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM outbox WHERE delivered=0 ORDER BY ordinal"
        ).fetchall()
        assert any("outbox_pending" in row[3] for row in query_plan)
