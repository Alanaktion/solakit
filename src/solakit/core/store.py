"""Append-only evidence, atomic metadata, and rebuildable SQLite indexes.

The jobs service also owns authoritative job_definitions in app.db. Rebuild
refreshes only derived tables and must preserve that independently managed data.

Only an unterminated final JSONL fragment may be omitted during recovery. Before
sealing such a fragment, persist its exact offset and hash as immutable recovery
metadata. Interior damage without that evidence fails closed. Original bytes
are never rewritten, and every omitted fragment is visible in storage health.
"""

import fcntl
import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from solakit.core.schemas import (
    MemoryNote,
    Message,
    Run,
    RunStatus,
    Session,
    SessionArchived,
    now,
)
from solakit.core.search import (
    FTS_INSERT,
    FTS_SCHEMA,
    SearchUnavailable,
    search_index,
    timestamp,
)
from solakit.core.secrets import Redactor

# Bump when derived schemas or evidence projection semantics change.
INDEX_VERSION = "1"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def safe_id(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError("invalid record identifier")
    return value


class StorageCorruption(RuntimeError):
    """An evidence file needs operator recovery before it can be used."""


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, payload: bytes) -> None:
    """Publish a complete, fsynced file; unfinished temporary files are not state."""
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class Store:
    def __init__(
        self, root: Path, redactor: Redactor | None = None, exclusive: bool = False
    ):
        self.root = root.expanduser().resolve()
        self.redactor = redactor or Redactor()
        self.lock = threading.RLock()
        self._line_counts: dict[str, tuple[tuple, int]] = {}
        self._issues: dict[tuple[str, int | None], dict] = {}
        for directory in (
            self.root,
            self.root / "sessions",
            self.root / "runs",
            self.root / "recovery",
            self.root / "jobs",
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ownership = None
        if exclusive:
            self.ownership = (self.root / ".runtime.lock").open("a")
            try:
                fcntl.flock(self.ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.ownership.close()
                raise RuntimeError("Another runtime owns this data directory") from None
        try:
            from solakit.core.compaction import recover_rotations

            recover_rotations(self)
            self.rebuild(incremental=True)
            self.recover()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.ownership is not None:
            self.ownership.close()
            self.ownership = None

    def _path(self, kind: str, record_id: str, suffix: str) -> Path:
        return self.root / kind / f"{safe_id(record_id)}{suffix}"

    def _issue(self, path: Path, kind: str, offset: int | None = None) -> None:
        relative = str(path.relative_to(self.root))
        self._issues[(relative, offset)] = {
            "file": relative,
            "kind": kind,
            "offset": offset,
        }

    def _corruption(self, path: Path, kind: str, offset: int | None = None):
        self._issue(path, kind, offset)
        return StorageCorruption(
            f"Storage recovery required: {path.relative_to(self.root)}"
        )

    def storage_health(self) -> dict:
        with self.lock:
            issues = list(self._issues.values())
        return {"status": "degraded" if issues else "ok", "issues": issues}

    def _recovery_path(self, path: Path, offset: int) -> Path:
        identity = hashlib.sha256(str(path.relative_to(self.root)).encode()).hexdigest()
        return self.root / "recovery" / f"{identity}.{offset}.json"

    def _is_recovered(self, path: Path, offset: int, fragment: bytes) -> bool:
        marker = self._recovery_path(path, offset)
        if not marker.exists():
            return False
        try:
            record = json.loads(marker.read_bytes())
            return record == {
                "file": str(path.relative_to(self.root)),
                "offset": offset,
                "sha256": hashlib.sha256(fragment).hexdigest(),
            }
        except (ValueError, UnicodeDecodeError):
            return False

    @staticmethod
    def _decode_record(fragment: bytes) -> dict:
        record = json.loads(fragment)
        if not isinstance(record, dict):
            raise ValueError("JSONL records must be objects")
        return record

    def _append(self, path: Path, record: dict) -> None:
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            # Never extend evidence already known to have unaccounted-for damage.
            relative = str(path.relative_to(self.root))
            if any(
                issue["file"] == relative
                and issue["kind"]
                in {
                    "corrupt_record",
                    "invalid_message",
                    "invalid_run",
                    "invalid_memory_note",
                    "missing_active_transcript",
                }
                for issue in self._issues.values()
            ):
                raise self._corruption(path, "corrupt_record")
            existed = path.exists()
            with path.open("a+b") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                offset, fragment = size, b""
                # Read only the last record, even for a large transcript.
                while offset:
                    count = min(offset, 8192)
                    offset -= count
                    stream.seek(offset)
                    fragment = stream.read(count) + fragment
                    newline = fragment.rfind(b"\n")
                    if newline >= 0:
                        offset += newline + 1
                        fragment = fragment[newline + 1 :]
                        break
                if fragment:
                    try:
                        self._decode_record(fragment)
                    except (ValueError, UnicodeDecodeError):
                        marker = self._recovery_path(path, offset)
                        recovery = {
                            "file": relative,
                            "offset": offset,
                            "sha256": hashlib.sha256(fragment).hexdigest(),
                        }
                        atomic_write(marker, json.dumps(recovery).encode())
                        self._issue(path, "recovered_tail", offset)
                stream.seek(0, os.SEEK_END)
                if fragment:
                    stream.write(b"\n")
                stream.write(payload.encode("utf-8") + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            if not existed:
                sync_directory(path.parent)

    def _read(self, path: Path) -> list[dict]:
        return [record for _, record in self._read_lines(path)]

    def _read_lines(self, path: Path) -> list[tuple[int, dict]]:
        with self.lock:
            if not path.exists():
                return []
            data = path.read_bytes()
            records, offset = [], 0
            for line_no, line in enumerate(data.splitlines(keepends=True), 1):
                terminated = line.endswith(b"\n")
                fragment = line[:-1] if terminated else line
                try:
                    records.append((line_no, self._decode_record(fragment)))
                except (ValueError, UnicodeDecodeError) as exc:
                    if self._is_recovered(path, offset, fragment):
                        self._issue(path, "recovered_tail", offset)
                    elif not terminated and offset + len(line) == len(data):
                        self._issue(path, "incomplete_tail", offset)
                    else:
                        raise self._corruption(path, "corrupt_record", offset) from exc
                offset += len(line)
            return records

    def _index(self, kind: str, record_id: str, payload: dict) -> None:
        with self.lock, closing(sqlite3.connect(self.root / "app.db")) as db, db:
            db.execute(
                f"INSERT OR REPLACE INTO {kind} VALUES (?, ?)",
                (record_id, json.dumps(payload)),
            )

    def _inventory(self):
        """Stat evidence once per directory; never open unchanged historical files."""
        units, recovery = {}, []
        patterns = {
            "sessions": (
                r"([A-Za-z0-9][A-Za-z0-9_-]{0,127})\."
                r"(?:meta\.json|jsonl(?:\.[0-9]+)?|compaction\.json|archive\.json)$"
            ),
            "runs": r"([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.(?:events|deliveries)\.jsonl$",
            "jobs": r"([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.jsonl$",
        }
        for kind, pattern in patterns.items():
            matcher = re.compile(pattern)
            with os.scandir(self.root / kind) as entries:
                for entry in entries:
                    match = matcher.fullmatch(entry.name)
                    if not match or not entry.is_file():
                        continue
                    stat = entry.stat()
                    units.setdefault((kind, match[1]), []).append(
                        [
                            entry.name,
                            stat.st_size,
                            stat.st_mtime_ns,
                            stat.st_ctime_ns,
                            stat.st_ino,
                            stat.st_dev,
                        ]
                    )
        with os.scandir(self.root / "recovery") as entries:
            for entry in entries:
                if entry.name.endswith(".json") and entry.is_file():
                    stat = entry.stat()
                    recovery.append(
                        [
                            entry.name,
                            stat.st_size,
                            stat.st_mtime_ns,
                            stat.st_ctime_ns,
                            stat.st_ino,
                            stat.st_dev,
                        ]
                    )
        return (
            {key: json.dumps(sorted(files)) for key, files in units.items()},
            json.dumps(sorted(recovery)),
        )

    def rebuild(self, *, incremental=False) -> None:
        """Refresh changed source units; explicit rebuild always rechecks everything.

        Source stats are a disposable cache, committed with the derived rows.
        Runtime writes intentionally leave old signatures: a crash or an index
        failure therefore forces changed evidence to be re-read on restart.
        """
        started = time.perf_counter()
        with self.lock, closing(sqlite3.connect(self.root / "app.db")) as db, db:
            sources, recovery = self._inventory()
            required = {
                "sessions",
                "runs",
                "jobs",
                "session_activity",
                "source_activity",
                "messages_fts",
                "index_sources",
                "index_meta",
            }
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            cached, metadata = {}, {}
            if required <= tables:
                metadata = dict(db.execute("SELECT key, value FROM index_meta"))
                if (
                    incremental
                    and metadata.get("version") == INDEX_VERSION
                    and metadata.get("recovery") == recovery
                ):
                    cached = {
                        (kind, identity): signature
                        for kind, identity, signature in db.execute(
                            "SELECT kind, id, signature FROM index_sources"
                        )
                    }
                else:
                    incremental = False
            else:
                incremental = False
            if incremental:
                try:
                    self._issues = {
                        (i["file"], i["offset"]): i
                        for i in json.loads(metadata.get("issues", "[]"))
                    }
                    for signature in cached.values():
                        json.loads(signature)
                except (ValueError, TypeError, KeyError):
                    incremental = False
                # An unhealthy derived index must be repaired, never cached as good.
                if any(i["file"] == "app.db" for i in self._issues.values()):
                    incremental = False
            if not incremental:
                self._issues.clear()
                cached = {}
            changed = {
                key
                for key in sources.keys() | cached.keys()
                if not incremental or sources.get(key) != cached.get(key)
            }
            db.execute("BEGIN")
            for kind in ("sessions", "runs", "jobs"):
                db.execute(
                    f"CREATE TABLE IF NOT EXISTS {kind} "
                    "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                )
                if not incremental:
                    db.execute(f"DELETE FROM {kind}")
            db.execute(
                "CREATE TABLE IF NOT EXISTS session_activity "
                "(id TEXT PRIMARY KEY, last_activity TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS source_activity "
                "(kind TEXT, id TEXT, session_id TEXT, at TEXT, PRIMARY KEY(kind,id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS index_sources "
                "(kind TEXT, id TEXT, signature TEXT, PRIMARY KEY(kind,id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS index_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            if not incremental:
                db.execute("DELETE FROM source_activity")
                db.execute("DELETE FROM index_sources")
            fts = True
            try:
                if not incremental:
                    db.execute("DROP TABLE IF EXISTS messages_fts")
                    db.execute(FTS_SCHEMA)
            except sqlite3.Error:
                self._issue(self.root / "app.db", "fts_index_unavailable")
                fts = False
            for kind, identity in sorted(changed):
                old_files = json.loads(cached.get((kind, identity), "[]"))
                new_files = json.loads(sources.get((kind, identity), "[]"))
                history_changed = (
                    not incremental
                    or [f for f in old_files if not f[0].endswith(".meta.json")]
                    != [f for f in new_files if not f[0].endswith(".meta.json")]
                    or not db.execute(
                        f"SELECT 1 FROM {kind} WHERE id=?", (identity,)
                    ).fetchone()
                    or not any(f[0].endswith(".meta.json") for f in new_files)
                )
                db.execute(f"DELETE FROM {kind} WHERE id=?", (identity,))
                if kind != "sessions" or history_changed:
                    db.execute(
                        "DELETE FROM source_activity WHERE kind=? AND id=?",
                        (kind, identity),
                    )
                if kind == "sessions" and fts and incremental and history_changed:
                    db.execute(
                        "DELETE FROM messages_fts WHERE session_id=?", (identity,)
                    )
                # Recheck previous issues for this unit, including deleted files.
                self._issues = {
                    key: issue
                    for key, issue in self._issues.items()
                    if not (
                        issue["file"].startswith(f"{kind}/{identity}.")
                        and (
                            kind != "sessions"
                            or history_changed
                            or issue["file"].endswith(".meta.json")
                        )
                    )
                }
                db.execute(
                    "DELETE FROM index_sources WHERE kind=? AND id=?", (kind, identity)
                )
                signature = sources.get((kind, identity))
                if signature is None:
                    continue
                db.execute(
                    "INSERT INTO index_sources VALUES (?, ?, ?)",
                    (kind, identity, signature),
                )
                activity = None
                try:
                    if kind == "sessions":
                        if not self._path(kind, identity, ".meta.json").exists():
                            continue
                        session = self.session(identity)
                        db.execute(
                            "INSERT INTO sessions VALUES (?, ?)",
                            (identity, session.model_dump_json()),
                        )
                        if not history_changed:
                            # Title/lifecycle edits never reread old transcripts.
                            continue
                        archives = [
                            item[0]
                            for item in json.loads(signature)
                            if re.fullmatch(
                                re.escape(identity) + r"\.jsonl\.[0-9]+", item[0]
                            )
                        ]
                        archives.sort(key=lambda name: int(name.rsplit(".", 1)[1]))
                        rows = self._message_search_rows(identity, archives=archives)
                        dates = [row[7] for row in rows if row[7] is not None]
                        if dates:
                            activity = (identity, max(dates))
                        if fts:
                            try:
                                db.executemany(FTS_INSERT, rows)
                            except sqlite3.Error:
                                self._issue(
                                    self.root / "app.db", "fts_index_unavailable"
                                )
                                fts = False
                    elif kind == "runs":
                        path = self._path(kind, identity, ".events.jsonl")
                        records = self._read(path)
                        if not records:
                            continue
                        try:
                            run = Run.model_validate(records[-1])
                        except ValidationError:
                            self._issue(path, "invalid_run")
                            continue
                        db.execute(
                            "INSERT INTO runs VALUES (?, ?)",
                            (run.id, run.model_dump_json()),
                        )
                        if records[-1].get("created_at"):
                            activity = (
                                run.session,
                                self._activity_timestamp(run.created_at),
                            )
                    else:
                        records = self._read(self._path(kind, identity, ".jsonl"))
                        if records:
                            db.execute(
                                "INSERT INTO jobs VALUES (?, ?)",
                                (identity, json.dumps(records[-1])),
                            )
                    if activity:
                        db.execute(
                            "INSERT INTO source_activity VALUES (?, ?, ?, ?)",
                            (kind, identity, *activity),
                        )
                except StorageCorruption:
                    # Invalid metadata also invalidates retained history addresses.
                    if kind == "sessions" and not history_changed:
                        db.execute(
                            "DELETE FROM source_activity WHERE kind=? AND id=?",
                            (kind, identity),
                        )
                        if fts:
                            db.execute(
                                "DELETE FROM messages_fts WHERE session_id=?",
                                (identity,),
                            )
                    # Keep other sessions usable; retain diagnostics across restarts.
            db.execute("DELETE FROM session_activity")
            db.execute(
                "INSERT INTO session_activity SELECT session_id, max(at) "
                "FROM source_activity GROUP BY session_id"
            )
            db.executemany(
                "INSERT OR REPLACE INTO index_meta VALUES (?, ?)",
                [
                    ("version", INDEX_VERSION),
                    ("recovery", recovery),
                    ("issues", json.dumps(list(self._issues.values()))),
                ],
            )

        logging.getLogger(__name__).info(
            "Storage index: %s, %d source units refreshed, %d reused, %.3fs",
            "incremental" if incremental else "full rebuild",
            len(changed),
            len(sources.keys() - changed),
            time.perf_counter() - started,
        )

    def recover(self) -> None:
        # Completed historical runs without delivery evidence need no recovery.
        with closing(sqlite3.connect(self.root / "app.db")) as db, db:
            candidates = db.execute(
                "SELECT payload FROM runs WHERE "
                "json_extract(payload, '$.status') IN ('queued', 'running') OR "
                "json_extract(payload, '$.delivery_status')='sending'"
            ).fetchall()
            seen = {json.loads(row[0])["id"] for row in candidates}
            for path in (self.root / "runs").glob("*.deliveries.jsonl"):
                identity = path.name.removesuffix(".deliveries.jsonl")
                if identity not in seen:
                    candidates.extend(
                        db.execute(
                            "SELECT payload FROM runs WHERE id=? AND "
                            "json_extract(payload, '$.delivery_status')='none'",
                            (identity,),
                        ).fetchall()
                    )
        for (payload,) in candidates:
            record = json.loads(payload)
            run = Run.model_validate(record)
            if run.status in (RunStatus.queued, RunStatus.running):
                run.status = RunStatus.interrupted
                run.finished_at = now()
                run.error = "Process stopped before run finalization"
                self.save_run(run)
            if run.delivery_status in {"none", "sending"}:
                deliveries = self.delivery_events(run.id)
                if deliveries:
                    acknowledged = next(
                        (
                            event
                            for event in reversed(deliveries)
                            if event["stage"] == "delivered"
                        ),
                        None,
                    )
                    run.delivery_status = (
                        "delivered" if acknowledged else "failed_or_uncertain"
                    )
                    if acknowledged:
                        run.delivery = {
                            k: v for k, v in acknowledged.items() if k != "stage"
                        }
                    self.save_run(run)

    def create_session(self, session: Session) -> Session:
        path = self._path("sessions", session.id, ".meta.json")
        with self.lock:
            if path.exists():
                return self.session(session.id)
            atomic_write(path, session.model_dump_json().encode("utf-8"))
            self._index("sessions", session.id, session.model_dump(mode="json"))
        return session

    def session(self, session_id: str) -> Session:
        path = self._path("sessions", session_id, ".meta.json")
        with self.lock:
            try:
                session = Session.model_validate_json(path.read_bytes())
                if session.id != session_id:
                    raise ValueError("metadata identity mismatch")
                return session
            except (ValueError, UnicodeDecodeError) as exc:
                raise self._corruption(path, "invalid_metadata") from exc

    @staticmethod
    def _activity_timestamp(value):
        return timestamp(value.replace(tzinfo=UTC) if value.tzinfo is None else value)

    @staticmethod
    def _activity(db, session_id, at):
        db.execute(
            "INSERT INTO session_activity VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET "
            "last_activity=max(last_activity, excluded.last_activity)",
            (session_id, at),
        )

    def _touch_session(self, session_id, at):
        try:
            with self.lock, closing(sqlite3.connect(self.root / "app.db")) as db, db:
                self._activity(db, session_id, self._activity_timestamp(at))
        except sqlite3.Error:
            self._issue(self.root / "app.db", "session_activity_index_unavailable")

    def rename_session(self, session_id: str, title: str) -> Session:
        with self.lock:
            path = self._path("sessions", session_id, ".meta.json")
            session = self.session(session_id)
            if session.title == title:
                return session
            metadata = json.loads(path.read_bytes())
            metadata["title"] = title
            atomic_write(path, json.dumps(metadata, ensure_ascii=False).encode())
            updated = self.session(session_id)
            self._index("sessions", session_id, updated.model_dump(mode="json"))
            return updated

    def set_session_archived(self, session_id, archived):
        with self.lock:
            path = self._path("sessions", session_id, ".meta.json")
            session = self.session(session_id)
            metadata = json.loads(path.read_bytes())
            if bool(session.archived_at) == archived:
                return session
            # Retain any importer-owned metadata fields as well as known fields.
            metadata["archived_at"] = now().isoformat() if archived else None
            atomic_write(path, json.dumps(metadata, ensure_ascii=False).encode())
            updated = self.session(session_id)
            self._index("sessions", session_id, updated.model_dump(mode="json"))
            return updated

    def list_sessions(
        self,
        *,
        status="active",
        q="",
        before=None,
        profile=None,
        exclude_source=None,
        roots_only=False,
        parent_session=None,
        limit=50,
        offset=0,
        ids=None,
    ):
        if status not in {"active", "archived", "all"}:
            raise ValueError("Invalid session status")
        if (
            type(limit) is not int
            or not 1 <= limit <= 500
            or type(offset) is not int
            or offset < 0
        ):
            raise ValueError("Use limit 1–500 and a nonnegative offset")
        if len(q) > 200:
            raise ValueError("Session query exceeds 200 characters")
        at = "coalesce(a.last_activity, json_extract(s.payload, '$.created_at'))"
        source = "sessions s LEFT JOIN session_activity a ON a.id=s.id"
        clauses, args = [], []
        if roots_only:
            clauses.append("json_extract(s.payload, '$.parent_session') IS NULL")
        if parent_session is not None:
            clauses.append("json_extract(s.payload, '$.parent_session') = ?")
            args.append(parent_session)
        if ids is not None:
            if not ids or len(ids) > 500:
                raise ValueError("Select 1–500 session IDs")
            clauses.append("s.id IN (" + ",".join("?" for _ in ids) + ")")
            args.extend(ids)
        if status != "all":
            clauses.append(
                "json_extract(s.payload, '$.archived_at') IS "
                + ("NOT NULL" if status == "archived" else "NULL")
            )
        if exclude_source is not None:
            clauses.append("coalesce(json_extract(s.payload, '$.source'), 'api') != ?")
            args.append(exclude_source)
        if q:
            clauses.append(
                "(instr(lower(s.id), lower(?))>0 OR "
                "instr(lower(coalesce(json_extract(s.payload, '$.title'), '')), "
                "lower(?))>0)"
            )
            args.extend([q, q])
        if before:
            clauses.append(f"julianday({at}) < julianday(?)")
            args.append(timestamp(before))
        if profile is not None:
            clauses.append("json_extract(s.payload, '$.default_profile')=?")
            args.append(profile)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.lock, closing(sqlite3.connect(self.root / "app.db")) as db, db:
            total = db.execute(
                f"SELECT count(*) FROM {source}" + where, args
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT s.payload, {at} FROM {source}"
                + where
                + f" ORDER BY julianday({at}) DESC, s.id LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            counts = dict(
                db.execute(
                    "SELECT CASE WHEN json_extract(payload, '$.archived_at') IS NULL "
                    "THEN 'active' ELSE 'archived' END, count(*) "
                    "FROM sessions GROUP BY 1"
                ).fetchall()
            )
        with self.lock, closing(sqlite3.connect(self.root / "app.db")) as db, db:
            profiles = [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT json_extract(payload, '$.default_profile') "
                    "FROM sessions ORDER BY 1"
                )
                if row[0]
            ]
        return {
            "profiles": profiles,
            "sessions": [
                {
                    "source": "api",
                    **json.loads(payload),
                    "last_activity_at": self._activity_timestamp(
                        datetime.fromisoformat(at)
                    ),
                }
                for payload, at in rows
            ],
            "total": total,
            "counts": {
                "active": counts.get("active", 0),
                "archived": counts.get("archived", 0),
            },
            "limit": limit,
            "offset": offset,
        }

    def list_runs(self, session_id=None, include_archived=False):
        clauses, args = [], []
        if session_id is not None:
            clauses.append("json_extract(r.payload, '$.session')=?")
            args.append(session_id)
        if not include_archived:
            clauses.append("json_extract(s.payload, '$.archived_at') IS NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.lock, closing(sqlite3.connect(self.root / "app.db")) as db, db:
            rows = db.execute(
                "SELECT r.payload FROM runs r LEFT JOIN sessions s ON "
                "s.id=json_extract(r.payload, '$.session')"
                + where
                + " ORDER BY r.rowid",
                args,
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def check_compaction(self, session_id: str) -> None:
        path = self._path("sessions", session_id, ".compaction.json")
        if path.exists():
            try:
                stage = json.loads(path.read_bytes())["stage"]
                if stage not in {"summarizing", "committed"}:
                    raise ValueError("Compaction requires recovery")
            except (ValueError, KeyError, TypeError) as exc:
                raise self._corruption(path, "compaction_recovery_required") from exc

    def context_started_at(self, session_id: str) -> str | None:
        """Last operator archival boundary; not a synthetic model message."""
        with self.lock:
            path = self._path("sessions", session_id, ".archive.json")
            if not path.exists():
                return None
            try:
                manifest = json.loads(path.read_bytes())
                if manifest["stage"] != "committed":
                    raise ValueError("Archive rotation is pending")
                return manifest["created_at"]
            except (ValueError, KeyError, TypeError) as exc:
                raise self._corruption(path, "archive_recovery_required") from exc

    def append_message(self, session_id: str, message: Message) -> None:
        with self.lock:
            if self.session(session_id).archived_at:
                raise SessionArchived(
                    "Session is archived; restore it before continuing"
                )
            # Refuse writes after a failed rotation.
            self.context_started_at(session_id)
            self.check_compaction(session_id)
            path = self._path("sessions", session_id, ".jsonl")
            record = message.model_dump(mode="json")
            before = path.stat() if path.exists() else None
            fingerprint = (
                (before.st_ino, before.st_size, before.st_mtime_ns) if before else None
            )
            cached = self._line_counts.get(session_id)
            self._append(path, record)
            self._touch_session(session_id, message.ts)
            try:
                if cached and cached[0] == fingerprint:
                    line_no = cached[1] + 1
                else:
                    with path.open("rb") as stream:
                        line_no = sum(1 for _ in stream)
                after = path.stat()
                self._line_counts[session_id] = (
                    (after.st_ino, after.st_size, after.st_mtime_ns),
                    line_no,
                )
                with closing(sqlite3.connect(self.root / "app.db")) as db, db:
                    db.execute(
                        FTS_INSERT,
                        self._message_search_row(session_id, "active", line_no, record),
                    )
            except (sqlite3.Error, OSError):
                self._issue(self.root / "app.db", "fts_index_unavailable")

    def transcript(self, session_id: str) -> list[dict]:
        with self.lock:
            self.session(session_id)
            self.context_started_at(session_id)
            self.check_compaction(session_id)
            path = self._path("sessions", session_id, ".jsonl")
            manifest = self._path("sessions", session_id, ".compaction.json")
            manual = self._path("sessions", session_id, ".archive.json")
            if not path.exists() and (manifest.exists() or manual.exists()):
                raise self._corruption(path, "missing_active_transcript")
            records = self._read(path)
            try:
                for record in records:
                    Message.model_validate(record)
            except ValidationError as exc:
                raise self._corruption(path, "invalid_message") from exc
            return records

    def archives(self, session_id: str) -> list[str]:
        self.session(session_id)
        paths = (self.root / "sessions").glob(f"{safe_id(session_id)}.jsonl.[0-9]*")
        prefix = f"{session_id}.jsonl."
        paths = [p for p in paths if p.name.removeprefix(prefix).isdigit()]
        return [p.name for p in sorted(paths, key=lambda p: int(p.suffix[1:]))]

    def all_messages(self, session_id: str) -> list[dict]:
        with self.lock:
            records = []
            for name in self.archives(session_id):
                records.extend(self._read(self.root / "sessions" / name))
            return records + self.transcript(session_id)

    @staticmethod
    def _message_search_row(session_id, archive, line_no, record):
        message = Message.model_validate(record)
        content = message.content or ""
        if message.role == "assistant":
            names = []
            for call in message.tool_calls or []:
                function = call.get("function")
                if isinstance(function, dict) and isinstance(function.get("name"), str):
                    names.append(function["name"])
            content += "\n" + "\n".join(names) if names else ""
        stamp = message.ts if "ts" in record else None
        return (
            content,
            message.role,
            message.kind,
            message.origin.channel,
            session_id,
            archive,
            line_no,
            timestamp(stamp.replace(tzinfo=stamp.tzinfo or UTC)) if stamp else None,
            message.run_id,
        )

    def _message_search_rows(self, session_id, *, archives=None):
        self.transcript(session_id)  # Validate active evidence and rotation state.
        rows = []
        names = self.archives(session_id) if archives is None else archives
        for name in [*names, f"{session_id}.jsonl"]:
            path = self.root / "sessions" / name
            for line_no, record in self._read_lines(path):
                try:
                    rows.append(
                        self._message_search_row(
                            session_id,
                            "active" if name == f"{session_id}.jsonl" else name,
                            line_no,
                            record,
                        )
                    )
                except ValidationError as exc:
                    raise self._corruption(path, "invalid_message") from exc

        return rows

    def reindex_messages(self, session_id: str) -> None:
        """Refresh addresses after durable rotation; index failures lose no evidence."""
        with self.lock:
            try:
                rows = self._message_search_rows(session_id)
                dates = [row[7] for row in rows if row[7] is not None]
                if dates:
                    self._touch_session(session_id, datetime.fromisoformat(max(dates)))
                with closing(sqlite3.connect(self.root / "app.db")) as db, db:
                    db.execute(
                        "DELETE FROM messages_fts WHERE session_id = ?", (session_id,)
                    )
                    db.executemany(FTS_INSERT, rows)
            except (sqlite3.Error, OSError, StorageCorruption):
                self._issue(self.root / "app.db", "fts_index_unavailable")

    def search(
        self,
        query: str,
        *,
        session_id: str | None = None,
        role: str | None = None,
        kind: str | None = None,
        after: str | datetime | None = None,
        before: str | datetime | None = None,
        limit: int = 50,
    ) -> list[dict]:
        with self.lock:
            if any(i["kind"] == "fts_index_unavailable" for i in self._issues.values()):
                raise SearchUnavailable(
                    "Search index unavailable; rebuild app.db from evidence"
                )
            try:
                return search_index(
                    self.root,
                    query,
                    session_id=session_id,
                    role=role,
                    kind=kind,
                    after=after,
                    before=before,
                    limit=limit,
                    redactor=self.redactor,
                )
            except SearchUnavailable:
                self._issue(self.root / "app.db", "fts_index_unavailable")
                raise

    def save_run(self, run: Run) -> None:
        with self.lock:
            record = self.redactor.redact(run.model_dump(mode="json"))
            self._append(self._path("runs", run.id, ".events.jsonl"), record)
            atomic_write(
                self._path("runs", run.id, ".json"), json.dumps(record).encode()
            )
            self._index("runs", run.id, record)
            # Recovery/observation may finish ancient imported runs today. Only
            # request admission and actual message timestamps count as activity.
            self._touch_session(run.session, run.created_at)

    def update_run(self, run_id: str, updates: dict) -> Run:
        """Patch finalized-run observations without overwriting concurrent fields."""
        with self.lock:
            events = self._read(self._path("runs", run_id, ".events.jsonl"))
            if not events:
                raise FileNotFoundError(run_id)
            run = Run.model_validate(events[-1] | updates)
            self.save_run(run)
            return run

    def pending_memory(self) -> list[dict]:
        with self.lock, closing(sqlite3.connect(self.root / "app.db")) as db, db:
            rows = db.execute(
                "SELECT payload FROM runs WHERE "
                "json_extract(payload, '$.memory_status') IN "
                "('accepted','pending','running','cancelling','status_unavailable') "
                "AND json_extract(payload, '$.status') NOT IN ('queued','running') "
                # Polling stops at the first unavailable status. Visit previously
                # unavailable runs last so one dead task cannot starve the rest.
                "ORDER BY "
                "json_extract(payload, '$.memory_status')='status_unavailable', "
                "rowid LIMIT 100"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def run(
        self, run_id: str, *, view: bool = False, include_messages: bool = True
    ) -> dict:
        path = self._path("runs", run_id, ".events.jsonl")
        events = self._read(path)
        if not events:
            raise FileNotFoundError(run_id)
        try:
            Run.model_validate(events[-1])
        except ValidationError as exc:
            with self.lock:
                raise self._corruption(path, "invalid_run") from exc
        record = events[-1].copy()
        if view:
            # Streaming presentation reads only the current run's contiguous tail,
            # never historical prompts, archives, or other runs' tool evidence.
            record["messages"] = self.run_tail(record) if include_messages else []
            record["tools"] = self._read(self._path("runs", run_id, ".tools.jsonl"))
            return record
        prompt = self._path("runs", run_id, ".prompt.txt")
        record["prompt"] = (
            prompt.read_bytes().decode("utf-8") if prompt.exists() else None
        )
        schemas = self._path("runs", run_id, ".tool-schemas.json")
        fragments = self._path("runs", run_id, ".prompt-fragments.json")
        record["prompt_fragments"] = (
            json.loads(fragments.read_bytes()) if fragments.exists() else None
        )
        turn = self._path("runs", run_id, ".turn.txt")
        record["turn_context"] = (
            turn.read_bytes().decode("utf-8") if turn.exists() else None
        )
        record["tool_schemas"] = None
        if record.get("tool_schema_sha256"):
            try:
                payload = schemas.read_bytes()
            except FileNotFoundError as exc:
                raise self._corruption(schemas, "missing_tool_schemas") from exc
            if hashlib.sha256(payload).hexdigest() != record["tool_schema_sha256"]:
                raise self._corruption(schemas, "tool_schema_hash_mismatch")
            record["tool_schemas"] = json.loads(payload)
        record["tool_catalog"] = None
        if record.get("tool_catalog_sha256"):
            catalog = self._path("runs", run_id, ".tool-catalog.json")
            try:
                payload = catalog.read_bytes()
            except FileNotFoundError as exc:
                raise self._corruption(catalog, "missing_tool_catalog") from exc
            if hashlib.sha256(payload).hexdigest() != record["tool_catalog_sha256"]:
                raise self._corruption(catalog, "tool_catalog_hash_mismatch")
            record["tool_catalog"] = json.loads(payload)
        record["messages"] = [
            m for m in self.all_messages(record["session"]) if m["run_id"] == run_id
        ]
        record["memory_events"] = self.memory_events(run_id)
        record["memory_notes"] = self.memory_notes(run_id)
        record["tools"] = self._read(self._path("runs", run_id, ".tools.jsonl"))
        record["reasoning"] = self.reasoning(run_id)
        record["deliveries"] = self.delivery_events(run_id)
        record["diagnostics"] = self._read(
            self._path("runs", run_id, ".diagnostics.jsonl")
        )
        return record

    def run_tail(self, run: dict) -> list[dict]:
        """Read backwards in bounded blocks, stopping at the preceding turn.

        Session execution is serialized. This projection is deliberately limited
        to active context; the full evidence endpoint still includes archives.
        """
        with self.lock:
            self.session(run["session"])
            self.check_compaction(run["session"])
            path = self._path("sessions", run["session"], ".jsonl")
            if not path.exists():
                return []
            result, remainder = [], b""
            with path.open("rb") as stream:
                position = stream.seek(0, os.SEEK_END)
                end = total = position
                while position:
                    size = min(position, 8192)
                    position -= size
                    stream.seek(position)
                    parts = (stream.read(size) + remainder).splitlines(keepends=True)
                    remainder = parts.pop(0) if position else b""
                    for line in reversed(parts):
                        offset = end - len(line)
                        end = offset
                        fragment = line.removesuffix(b"\n")
                        try:
                            message = self._decode_record(fragment)
                        except (ValueError, UnicodeDecodeError) as exc:
                            if self._is_recovered(path, offset, fragment):
                                self._issue(path, "recovered_tail", offset)
                                continue
                            if not line.endswith(b"\n") and offset + len(line) == total:
                                self._issue(path, "incomplete_tail", offset)
                                continue
                            raise self._corruption(
                                path, "corrupt_record", offset
                            ) from exc
                        try:
                            parsed = Message.model_validate(message)
                        except ValidationError as exc:
                            raise self._corruption(
                                path, "invalid_message", offset
                            ) from exc
                        if message["run_id"] != run["id"]:
                            if (
                                result
                                or parsed.ts.timestamp()
                                < datetime.fromisoformat(run["created_at"]).timestamp()
                            ):
                                return list(reversed(result))
                            continue
                        result.append(message)
            return list(reversed(result))

    def write_prompt(self, run_id: str, prompt: str) -> str:
        payload = prompt.encode("utf-8")
        # Never redact prompt bytes: this is the exact model input for audit.
        fragments = getattr(prompt, "fragments", None)
        if fragments is not None:
            self._write_run_input(
                run_id, ".prompt-fragments.json", json.dumps(fragments).encode()
            )
        return self._write_run_input(run_id, ".prompt.txt", payload)

    def write_turn_context(self, run_id: str, turn: str) -> str:
        # Exact request-time preamble sent before the user's verbatim text.
        return self._write_run_input(run_id, ".turn.txt", turn.encode("utf-8"))

    def write_tool_catalog(self, run_id: str, catalog_json: str) -> str:
        return self._write_run_input(
            run_id, ".tool-catalog.json", catalog_json.encode()
        )

    def write_tool_schemas(self, run_id: str, schemas: list[dict]) -> str:
        # Input evidence, not diagnostics: preserve schema property names/types.
        payload = json.dumps(schemas, ensure_ascii=False, sort_keys=True).encode(
            "utf-8"
        )
        return self._write_run_input(run_id, ".tool-schemas.json", payload)

    def _write_run_input(self, run_id: str, suffix: str, payload: bytes) -> str:
        with self._path("runs", run_id, suffix).open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(self.root / "runs")
        return hashlib.sha256(payload).hexdigest()

    def stage_memory_note(self, note: MemoryNote) -> None:
        # Notes, like transcript and prompt evidence, are intentionally verbatim.
        self._append(
            self._path("runs", note.run_id, ".notes.jsonl"),
            note.model_dump(mode="json"),
        )

    def memory_notes(self, run_id: str) -> list[dict]:
        path = self._path("runs", run_id, ".notes.jsonl")
        records = self._read(path)
        try:
            for record in records:
                note = MemoryNote.model_validate(record)
                if note.run_id != run_id:
                    raise ValueError("Memory note run identity mismatch")
        except ValueError as exc:
            raise self._corruption(path, "invalid_memory_note") from exc
        return records

    def memory_events(self, run_id: str) -> list[dict]:
        return self._read(self._path("runs", run_id, ".memory.jsonl"))

    def log_memory(self, run_id: str, record: dict) -> None:
        self._append(
            self._path("runs", run_id, ".memory.jsonl"), self.redactor.redact(record)
        )

    def log_tool(self, run_id: str, record: dict[str, Any]) -> None:
        self._append(
            self._path("runs", run_id, ".tools.jsonl"), self.redactor.redact(record)
        )

    def job_state(self, job_id: str) -> dict | None:
        records = self._read(self._path("jobs", job_id, ".jsonl"))
        return records[-1] if records else None

    def save_job(self, job_id: str, record: dict) -> None:
        self._append(self._path("jobs", job_id, ".jsonl"), record)
        self._index("jobs", job_id, record)

    def delivery_events(self, run_id: str) -> list[dict]:
        return self._read(self._path("runs", run_id, ".deliveries.jsonl"))

    def log_delivery(self, run_id: str, record: dict) -> None:
        self._append(
            self._path("runs", run_id, ".deliveries.jsonl"),
            self.redactor.redact(record),
        )

    def channel_post_events(self, key: str) -> list[dict]:
        return self._read(self._path("channel-posts", key, ".jsonl"))

    def log_channel_post(self, key: str, record: dict) -> None:
        """Append-only tool-initiated post receipts, separate from run delivery."""
        directory = self.root / "channel-posts"
        directory.mkdir(exist_ok=True, mode=0o700)
        self._append(
            self._path("channel-posts", key, ".jsonl"), self.redactor.redact(record)
        )

    def log_reasoning(self, run_id: str, record: dict) -> None:
        # Model reasoning is run evidence only: never transcript, never resent.
        self._append(
            self._path("runs", run_id, ".reasoning.jsonl"),
            self.redactor.redact(record),
        )

    def reasoning(self, run_id: str) -> list[dict]:
        return self._read(self._path("runs", run_id, ".reasoning.jsonl"))

    def calibration(self, model_id: str) -> dict | None:
        """Measured bytes-per-token for a model (rebuildable, advisory)."""
        path = self.root / "calibration.json"
        try:
            return json.loads(path.read_bytes()).get(model_id)
        except (OSError, ValueError, AttributeError):
            return None

    def record_calibration(
        self, model_id: str, request_bytes: int, prompt_tokens: int
    ) -> dict | None:
        if prompt_tokens <= 0 or request_bytes <= 0:
            return None
        ratio = min(8.0, max(1.5, request_bytes / prompt_tokens))
        path = self.root / "calibration.json"
        with self.lock:
            try:
                data = json.loads(path.read_bytes())
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            entry = data.get(model_id) or {"bytes_per_token": ratio, "samples": 0}
            samples = min(int(entry.get("samples", 0)) + 1, 1000)
            weight = max(0.05, 1 / samples)
            entry = {
                "bytes_per_token": round(
                    entry["bytes_per_token"] * (1 - weight) + ratio * weight, 4
                ),
                "samples": samples,
            }
            data[model_id] = entry
            temp = path.with_suffix(".json.tmp")
            try:
                temp.write_text(json.dumps(data))
                temp.replace(path)
            except OSError:
                return None
        return entry

    def log_diagnostic(self, run_id: str, record: dict) -> None:
        self._append(
            self._path("runs", run_id, ".diagnostics.jsonl"),
            self.redactor.redact(record),
        )

    def list_records(self, kind: str) -> list[dict]:
        if kind not in {"sessions", "runs", "jobs"}:
            raise ValueError("unknown index")
        with self.lock, closing(sqlite3.connect(self.root / "app.db")) as db, db:
            rows = db.execute(f"SELECT payload FROM {kind} ORDER BY rowid").fetchall()
        return [json.loads(row[0]) for row in rows]
