"""Import a stopped legacy runtime directly into a new v2 data directory."""

import argparse
import asyncio
import fcntl
import json
import os
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from solakit.engine.jobs import CONTROL_ID
from solakit.records import (
    ErrorCode,
    Failure,
    Job,
    Message,
    ModelStep,
    Origin,
    Run,
    RunState,
    SchedulerControl,
    Session,
    SessionMode,
    utc_now,
)
from solakit.runtime import DataDirectoryLock, OwnershipError
from solakit.storage.backup import _copy
from solakit.storage.evidence import Entry, StorageError, canonical, checked_json
from solakit.storage.search import rebuild
from solakit.storage.store import Store, StoredRecord


def identity(source: str, kind: str, old: str) -> str:
    return uuid5(NAMESPACE_URL, canonical(["sola-phase13-v1", source, kind, old]).decode()).hex


def timestamp(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise ValueError("legacy timestamps require a UTC offset")
    return instant.astimezone(UTC)


def json_lines(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    if not path.exists():
        return
    with path.open("rb") as stream:
        for line, raw in enumerate(stream, 1):
            try:
                value = checked_json(raw)
            except ValueError:
                # shortcut: torn final JSON stays in the raw copy; repair it manually if needed.
                if not raw.endswith(b"\n"):
                    return
                raise ValueError(f"invalid JSON at {path.name}:{line}") from None
            if not isinstance(value, dict):
                raise ValueError(f"expected a JSON object at {path.name}:{line}")
            yield line, value


@contextmanager
def legacy_lock(source: Path) -> Iterator[None]:
    fd = os.open(source / ".runtime.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("stop the legacy runtime before importing") from None
        wal = source / "app.db-wal"
        if wal.exists() and wal.stat().st_size:
            raise ValueError("legacy SQLite WAL remains; close/checkpoint the old database first")
        yield
    finally:
        os.close(fd)


def records(root: Path, source: Path) -> Iterator[StoredRecord | Entry]:
    def mapped(kind: str, old: str) -> str:
        return identity(str(source), kind, old)

    sessions = {
        p.name.removesuffix(".meta.json"): json.loads(p.read_bytes())
        for p in sorted((root / "sessions").glob("*.meta.json"))
    }
    runs = {
        p.stem: json.loads(p.read_bytes())
        for p in sorted((root / "runs").glob("*.json"))
        if "." not in p.stem
    }
    transcripts = sorted(
        (
            p
            for p in (root / "sessions").glob("*.jsonl*")
            if p.name.split(".jsonl", 1)[1].removeprefix(".").isdigit() or p.name.endswith(".jsonl")
        ),
        key=lambda p: (
            p.name.split(".jsonl", 1)[0],
            int(p.suffix[1:]) if p.suffix != ".jsonl" else 2**63,
        ),
    )
    order: dict[str, int] = {}
    for path in transcripts:
        session = path.name.split(".jsonl", 1)[0]
        for _, raw in json_lines(path):
            old = raw["run_id"]
            order.setdefault(old, len(order))
            runs.setdefault(
                old,
                {
                    "session": session,
                    "created_at": raw["ts"],
                    "profile": "legacy",
                    "status": "interrupted",
                },
            )
            if runs[old]["session"] != session:
                raise ValueError("legacy run belongs to multiple sessions")
    for old, raw in sessions.items():
        yield Session(
            id=mapped("session", old),
            origin=Origin.SYSTEM,
            created_at=timestamp(raw["created_at"]),
            title=raw.get("title", ""),
            mode=SessionMode(raw.get("memory_scope", "shared")),
            archived=bool(raw.get("archived_at")),
            pinned=raw.get("pinned")
            if raw.get("pinned") is not None
            else bool(raw.get("bound_channel")),
        )
    for old, raw in sorted(
        runs.items(),
        key=lambda item: (
            item[1]["session"],
            order.get(item[0], len(order)),
            item[1]["created_at"],
            item[0],
        ),
    ):
        state = {
            "completed": RunState.COMPLETED,
            "cancelled": RunState.CANCELLED,
            "queued": RunState.INTERRUPTED,
            "running": RunState.INTERRUPTED,
            "interrupted": RunState.INTERRUPTED,
        }.get(raw["status"], RunState.FAILED)
        started = timestamp(raw["created_at"])
        ended = timestamp(raw["finished_at"]) if raw.get("finished_at") else utc_now()
        yield Run(
            id=mapped("run", old),
            request_id=mapped("request", old),
            session_id=mapped("session", raw["session"]),
            origin=Origin.SYSTEM,
            profile=raw["profile"],
            created_at=started,
            started_at=started,
            ended_at=max(started, ended),
            state=state,
            reason=None
            if state == RunState.COMPLETED
            else Failure(
                code=ErrorCode.PROCESS_INTERRUPTED
                if state == RunState.INTERRUPTED
                else ErrorCode.TOOL_FAILED,
                message="Legacy outcome: " + raw["status"],
            ),
        )
    indices: Counter[str] = Counter()
    for path in transcripts:
        sid = mapped("session", path.name.split(".jsonl", 1)[0])
        for line, raw in json_lines(path):
            content, role = raw.get("content"), raw["role"]
            if (
                role not in ("user", "assistant")
                or not isinstance(content, str)
                or raw.get("kind", "message") != "message"
            ):
                continue
            rid = mapped("run", raw["run_id"])
            key = f"{path.name}:{line}"
            created = timestamp(raw["ts"])
            yield Message(
                id=mapped("message", key),
                origin=Origin.SYSTEM,
                created_at=created,
                session_id=sid,
                run_id=rid,
                role=role,
                content=content,
            )
            if role == "assistant":
                indices[rid] += 1
                yield ModelStep(
                    id=mapped("model", key),
                    origin=Origin.SYSTEM,
                    created_at=created,
                    session_id=sid,
                    run_id=rid,
                    index=indices[rid],
                    model="legacy-unrecorded",
                    content=content,
                )
    with closing(
        sqlite3.connect((source / "app.db").as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as db:
        for old, payload, deleted in db.execute(
            "SELECT id,payload,deleted FROM job_definitions ORDER BY id"
        ):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", old):
                raise ValueError("invalid legacy job identifier")
            raw = json.loads(payload)
            states = list(json_lines(root / "jobs" / (old + ".jsonl")))
            cursor = states[-1][1] if states else {}
            bound = raw.get("bind_session")
            if bound not in sessions or sessions[bound].get("memory_scope") == "ephemeral":
                bound = None
            yield Job(
                id=mapped("job", old),
                origin=Origin.SCHEDULED,
                profile=raw["profile"],
                task=raw["task"],
                timezone=raw.get("timezone", "America/Denver"),
                schedule="once:" + raw["run_at"] if raw.get("run_at") else "cron:" + raw["cron"],
                session_id=mapped("session", bound) if bound else None,
                mode=SessionMode(sessions[bound].get("memory_scope", "shared"))
                if bound
                else SessionMode.EPHEMERAL,
                enabled=False,
                deleted=bool(deleted),
                consumed=bool(
                    cursor.get("consumed")
                    or cursor.get("active")
                    or (raw.get("run_at") and not states)
                ),
                tools=(),
                workspace_roots=(),
                delivery="none",
            )
            yield Entry(
                kind="legacy",
                record={"id": old, "payload": raw, "deleted": deleted, "cursor": cursor},
                details={"source": "app.db:job_definitions"},
            )
    yield SchedulerControl(id=CONTROL_ID, origin=Origin.SYSTEM, paused=True)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = str(path.relative_to(root))
        values = (
            json_lines(path)
            if ".jsonl" in path.name
            else (
                iter([(1, json.loads(path.read_bytes()))]) if path.suffix == ".json" else iter([])
            )
        )
        for line, raw in values:
            details: dict[str, Any] = {"source": relative, "line": line}
            old = path.name.split(".")[0]
            if path.parent.name == "sessions" and old in sessions:
                details["session_id"] = mapped("session", old)
            raw_run = (
                old
                if path.parent.name == "runs"
                else raw.get("run_id")
                if isinstance(raw, dict)
                else None
            )
            if isinstance(raw_run, str) and raw_run in runs:
                details.update(
                    run_id=mapped("run", raw_run),
                    session_id=mapped("session", runs[raw_run]["session"]),
                )
            yield Entry(
                id=mapped("raw", f"{relative}:{line}"),
                kind="legacy",
                record=raw if isinstance(raw, dict) else {"value": raw},
                details=details,
            )


def write(store: Store, batch: list[StoredRecord | Entry]) -> None:
    with store.db:
        for record in batch:
            if isinstance(record, Session):
                store.db.execute("INSERT INTO sessions VALUES (?,0)", (record.id,))
            entry = record if isinstance(record, Entry) else store._entry(record, {})
            store.db.execute(
                "INSERT INTO outbox(id,body) VALUES (?,?)", (entry.id, entry.encoded())
            )
    store._flush()


async def import_legacy(source: Path, destination: Path) -> dict[str, int]:
    source, destination = source.resolve(), destination.resolve()
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("source and destination must be separate directories")
    if not (source / "sessions").is_dir() or not (source / "app.db").is_file():
        raise ValueError("legacy directory must contain sessions/ and app.db")
    if (source / "app.db").is_symlink():
        raise ValueError("legacy database must not be a symlink")
    with legacy_lock(source):
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        owner = DataDirectoryLock(destination)
        owner.acquire()
        store = Store(destination)
        try:
            raw = destination / "imports/legacy"
            for child in sorted(source.iterdir()):
                if child.name in (
                    "backups",
                    ".runtime.lock",
                    "ui-sessions.json",
                    "calibration.json",
                    "doctor.json",
                ) or child.name.startswith("app.db"):
                    continue
                if child.is_symlink():
                    raise ValueError("legacy data must not contain symlinks")
                for path in sorted(child.rglob("*")) if child.is_dir() else [child]:
                    if path.is_symlink():
                        raise ValueError("legacy data must not contain symlinks")
                    if path.is_file():
                        _copy(path, raw / path.relative_to(source))
                    elif not path.is_dir():
                        raise ValueError("legacy data must contain only files and directories")
            await store.start()
            counts: Counter[str] = Counter()
            batch: list[StoredRecord | Entry] = []
            for record in records(raw, source):
                counts[record.kind if isinstance(record, Entry) else type(record).__name__] += 1
                batch.append(record)
                if len(batch) >= 200:
                    write(store, batch)
                    batch = []
            if batch:
                write(store, batch)
            rebuild(destination)
            return dict(counts)
        finally:
            await store.stop()
            owner.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source", type=Path, default=Path.home() / ".config/solakit")
    args = parser.parse_args()
    try:
        result = asyncio.run(import_legacy(args.source, args.destination))
    except ValidationError:
        parser.exit(2, "Invalid legacy record; inspect the raw import files.\n")
    except KeyError, TypeError:
        parser.exit(2, "Malformed legacy record; inspect the raw import files.\n")
    except (OSError, ValueError, sqlite3.Error, StorageError, OwnershipError) as error:
        parser.exit(2, f"Import failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
