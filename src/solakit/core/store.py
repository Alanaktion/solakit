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
import os
import re
import sqlite3
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from solakit.core.schemas import Message, Run, RunStatus, Session, now
from solakit.core.search import (
    FTS_INSERT,
    FTS_SCHEMA,
    SearchUnavailable,
    search_index,
    timestamp,
)
from solakit.core.secrets import Redactor

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
            self.rebuild()
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
        with self.lock, sqlite3.connect(self.root / "app.db") as db:
            db.execute(
                f"INSERT OR REPLACE INTO {kind} VALUES (?, ?)",
                (record_id, json.dumps(payload)),
            )

    def rebuild(self) -> None:
        """Refresh derived tables without deleting authoritative managed tables."""
        with self.lock, sqlite3.connect(self.root / "app.db") as db:
            self._issues.clear()
            for kind in ("sessions", "runs", "jobs"):
                db.execute(
                    f"CREATE TABLE IF NOT EXISTS {kind} "
                    "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                )
                db.execute(f"DELETE FROM {kind}")
            fts = True
            try:
                db.execute("DROP TABLE IF EXISTS messages_fts")
                db.execute(FTS_SCHEMA)
            except sqlite3.Error:
                self._issue(self.root / "app.db", "fts_index_unavailable")
                fts = False
            for path in sorted((self.root / "sessions").glob("*.meta.json")):
                try:
                    session = self.session(path.name.removesuffix(".meta.json"))
                except StorageCorruption:
                    continue
                db.execute(
                    "INSERT INTO sessions VALUES (?, ?)",
                    (session.id, session.model_dump_json()),
                )
                try:
                    rows = self._message_search_rows(session.id)
                    if fts:
                        try:
                            db.executemany(FTS_INSERT, rows)
                        except sqlite3.Error:
                            self._issue(self.root / "app.db", "fts_index_unavailable")
                            fts = False
                except StorageCorruption:
                    pass  # Report damage while keeping other sessions usable.
            for path in sorted((self.root / "runs").glob("*.events.jsonl")):
                try:
                    records = self._read(path)
                    if not records:
                        continue
                    run = Run.model_validate(records[-1])
                except StorageCorruption:
                    continue
                except ValidationError:
                    self._issue(path, "invalid_run")
                    continue
                db.execute(
                    "INSERT INTO runs VALUES (?, ?)", (run.id, run.model_dump_json())
                )

            for path in sorted((self.root / "jobs").glob("*.jsonl")):
                try:
                    records = self._read(path)
                except StorageCorruption:
                    continue
                if records:
                    db.execute(
                        "INSERT INTO jobs VALUES (?, ?)",
                        (path.stem, json.dumps(records[-1])),
                    )

    def recover(self) -> None:
        for record in self.list_records("runs"):
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
            self.session(session_id)
            # Refuse writes after a failed rotation.
            self.context_started_at(session_id)
            self.check_compaction(session_id)
            path = self._path("sessions", session_id, ".jsonl")
            record = message.model_dump(mode="json")
            self._append(path, record)
            try:
                with path.open("rb") as stream:
                    line_no = sum(1 for _ in stream)
                with sqlite3.connect(self.root / "app.db") as db:
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

    def _message_search_rows(self, session_id):
        self.transcript(session_id)  # Validate active evidence and rotation state.
        rows = []
        for name in [*self.archives(session_id), f"{session_id}.jsonl"]:
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
                with sqlite3.connect(self.root / "app.db") as db:
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
        with self.lock, sqlite3.connect(self.root / "app.db") as db:
            rows = db.execute(
                "SELECT payload FROM runs WHERE "
                "json_extract(payload, '$.memory_status') IN "
                "('accepted','pending','running','cancelling','status_unavailable') "
                "AND json_extract(payload, '$.status') NOT IN ('queued','running') "
                "ORDER BY rowid LIMIT 100"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def run(self, run_id: str) -> dict:
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
        prompt = self._path("runs", run_id, ".prompt.txt")
        record["prompt"] = (
            prompt.read_bytes().decode("utf-8") if prompt.exists() else None
        )
        record["messages"] = [
            m for m in self.all_messages(record["session"]) if m["run_id"] == run_id
        ]
        record["memory_events"] = self.memory_events(run_id)
        record["tools"] = self._read(self._path("runs", run_id, ".tools.jsonl"))
        record["deliveries"] = self.delivery_events(run_id)
        record["diagnostics"] = self._read(
            self._path("runs", run_id, ".diagnostics.jsonl")
        )
        return record

    def write_prompt(self, run_id: str, prompt: str) -> str:
        payload = prompt.encode("utf-8")
        # Never redact prompt bytes: this is the exact model input for audit.
        with self._path("runs", run_id, ".prompt.txt").open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(self.root / "runs")
        return hashlib.sha256(payload).hexdigest()

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

    def log_diagnostic(self, run_id: str, record: dict) -> None:
        self._append(
            self._path("runs", run_id, ".diagnostics.jsonl"),
            self.redactor.redact(record),
        )

    def list_records(self, kind: str) -> list[dict]:
        if kind not in {"sessions", "runs", "jobs"}:
            raise ValueError("unknown index")
        with self.lock, sqlite3.connect(self.root / "app.db") as db:
            rows = db.execute(f"SELECT payload FROM {kind} ORDER BY rowid").fetchall()
        return [json.loads(row[0]) for row in rows]
