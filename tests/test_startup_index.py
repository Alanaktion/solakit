"""Warm startup reuses historical indexes but rechecks changed evidence."""

import json
import os
import sqlite3
from datetime import UTC, datetime

import pytest

from solakit.core.schemas import Message, Origin, Run, RunStatus, Session
from solakit.core.store import Store
from solakit.jobs.definitions import Definitions


def seed(store, identity="old"):
    store.create_session(
        Session(id=identity, archived_at=datetime(2020, 1, 1, tzinfo=UTC))
    )
    # Import evidence directly; runtime admission properly refuses archived writes.
    path = store.root / f"sessions/{identity}.jsonl"
    message = Message(
        role="user",
        content="ancient needle",
        origin=Origin(),
        run_id=identity,
        ts=datetime(2020, 1, 1, tzinfo=UTC),
    )
    path.write_text(message.model_dump_json() + "\n")
    store.save_run(
        Run(
            id=identity,
            session=identity,
            profile="interactive",
            origin=Origin(),
            status=RunStatus.completed,
            created_at=message.ts,
        )
    )
    return path, message


def test_thousand_unchanged_sessions_start_without_opening_evidence(
    config, monkeypatch
):
    store = Store(config.paths.data)
    for i in range(1000):
        identity = f"old-{i}"
        (store.root / f"sessions/{identity}.meta.json").write_text(
            Session(
                id=identity, archived_at=datetime(2020, 1, 1, tzinfo=UTC)
            ).model_dump_json()
        )
        (store.root / f"sessions/{identity}.jsonl").write_text(
            Message(
                role="user", content="ancient needle", origin=Origin(), run_id=identity
            ).model_dump_json()
            + "\n"
        )
    store.rebuild()

    def no_history(*args, **kwargs):
        raise AssertionError("Unchanged history must not be opened at startup")

    monkeypatch.setattr(Store, "_read_lines", no_history)
    monkeypatch.setattr(Store, "session", no_history)
    restarted = Store(config.paths.data)
    assert restarted.list_sessions()["total"] == 0
    assert restarted.list_sessions(status="archived")["total"] == 1000
    assert len(restarted.search("needle", limit=200)) == 200
    assert restarted.storage_health()["status"] == "ok"


def test_refresh_changed_added_deleted_sources_and_activity(config, monkeypatch):
    store = Store(config.paths.data)
    path, message = seed(store)
    seed(store, "untouched")
    store.rebuild()
    # Same-length edit with restored mtime still changes ctime/inode fingerprints.
    previous = path.stat()
    path.write_text(path.read_text().replace("ancient needle", "changed needle"))
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    reads = []
    original = Store._read_lines

    def observe(self, path):
        reads.append(path.name)
        return original(self, path)

    monkeypatch.setattr(Store, "_read_lines", observe)
    refreshed = Store(config.paths.data)
    assert "untouched.jsonl" not in reads and "untouched.events.jsonl" not in reads
    assert refreshed.search("changed")[0]["session_id"] == "old"
    assert refreshed.list_sessions(status="all", ids=["old"])["sessions"][0][
        "last_activity_at"
    ].startswith("2020-01-01")
    # Removed source units disappear from listings and full-text search.
    for filename in (
        "sessions/old.meta.json",
        "sessions/old.jsonl",
        "runs/old.events.jsonl",
    ):
        (store.root / filename).unlink()
    seed(refreshed, "new")
    refreshed = Store(config.paths.data)
    assert not refreshed.search("changed")
    assert {s["id"] for s in refreshed.list_records("sessions")} == {"new", "untouched"}
    assert {r["id"] for r in refreshed.list_records("runs")} == {"new", "untouched"}


def test_refresh_failure_rolls_back_signature_and_retries(config, monkeypatch):
    store = Store(config.paths.data)
    path, message = seed(store)
    store.rebuild()
    path.write_text(
        message.model_copy(update={"content": "replacement needle"}).model_dump_json()
        + "\n"
    )
    original = Store._message_search_rows
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            raise RuntimeError("interrupted refresh")

        patch.setattr(Store, "_message_search_rows", fail)
        with pytest.raises(RuntimeError, match="interrupted refresh"):
            Store(config.paths.data)
    assert store.search("ancient")  # Prior committed snapshot remains usable.
    recovered = Store(config.paths.data)
    assert recovered.search("replacement") and not recovered.search("ancient")
    assert original is Store._message_search_rows


def test_cached_health_and_repaired_evidence(config):
    store = Store(config.paths.data)
    path, message = seed(store)
    path.write_text("{invalid}\n")
    store = Store(config.paths.data)
    issues = store.storage_health()
    assert issues["status"] == "degraded"
    assert Store(config.paths.data).storage_health() == issues
    path.write_text(message.model_dump_json() + "\n")
    repaired = Store(config.paths.data)
    assert repaired.storage_health()["status"] == "ok"
    assert repaired.search("needle")


@pytest.mark.parametrize("damage", ["version", "fts"])
def test_cache_invalidation_rebuilds_without_losing_authoritative_jobs(config, damage):
    from solakit.core.config import Job

    store = Store(config.paths.data)
    seed(store)
    store.rebuild()
    definitions = Definitions(store)
    definitions.load([])
    definitions.save(
        Job(
            id="kept",
            cron="0 0 * * *",
            task="Keep",
            enabled=False,
            profile="interactive",
            bind_session="ephemeral",
        )
    )
    with sqlite3.connect(store.root / "app.db") as db:
        if damage == "version":
            db.execute("UPDATE index_meta SET value='old' WHERE key='version'")
        else:
            db.execute("DROP TABLE messages_fts")
    recovered = Store(config.paths.data)
    assert recovered.search("needle")
    assert Definitions(recovered).load([])[0].id == "kept"


def test_warm_restart_recovers_only_pending_work_and_delivery(config, monkeypatch):
    store = Store(config.paths.data)
    seed(store)
    origin = Origin()
    for identity, status in (
        ("pending", RunStatus.running),
        ("delivery", RunStatus.completed),
    ):
        store.save_run(
            Run(
                id=identity,
                session="old",
                profile="interactive",
                origin=origin,
                status=status,
            )
        )
    store.log_delivery("delivery", {"stage": "delivered", "message_id": "123"})
    store.rebuild()
    reads = []
    original = Store._read_lines

    def observe(self, path):
        reads.append(path.name)
        return original(self, path)

    monkeypatch.setattr(Store, "_read_lines", observe)
    restarted = Store(config.paths.data)
    assert "old.events.jsonl" not in reads and "old.jsonl" not in reads
    with sqlite3.connect(store.root / "app.db") as db:
        runs = {
            identity: json.loads(payload)
            for identity, payload in db.execute("SELECT id,payload FROM runs")
        }
    assert runs["pending"]["status"] == "interrupted"
    assert runs["delivery"]["delivery_status"] == "delivered"
    assert "delivery.deliveries.jsonl" in reads
    assert restarted.search("needle")


def test_lifecycle_metadata_edits_do_not_reindex_old_history(config, monkeypatch):
    store = Store(config.paths.data)
    seed(store)
    store.rebuild()
    store.set_session_archived("old", False)

    def no_history(*args, **kwargs):
        raise AssertionError("Metadata-only edits must reuse indexed history")

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_read_lines", no_history)
        restored = Store(config.paths.data)
    assert restored.list_sessions()["total"] == 1
    assert restored.search("needle")
    # Bad metadata removes cached addresses, and repairing it indexes again.
    metadata = restored.root / "sessions/old.meta.json"
    original = metadata.read_bytes()
    metadata.write_text("{invalid}")
    damaged = Store(config.paths.data)
    assert not damaged.search("needle")
    assert damaged.storage_health()["status"] == "degraded"
    metadata.write_bytes(original)
    repaired = Store(config.paths.data)
    assert repaired.search("needle")
    assert repaired.storage_health()["status"] == "ok"
