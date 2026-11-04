"""Append-only evidence, atomic metadata, and a disposable SQLite index.

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
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from solakit.core.schemas import Message, Run, RunStatus, Session, now
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
        with self.lock:
            if not path.exists():
                return []
            data = path.read_bytes()
            records, offset = [], 0
            for line in data.splitlines(keepends=True):
                terminated = line.endswith(b"\n")
                fragment = line[:-1] if terminated else line
                try:
                    records.append(self._decode_record(fragment))
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
        with self.lock, sqlite3.connect(self.root / "app.db") as db:
            self._issues.clear()
            for kind in ("sessions", "runs", "jobs"):
                db.execute(
                    f"CREATE TABLE IF NOT EXISTS {kind} "
                    "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                )
                db.execute(f"DELETE FROM {kind}")
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
                    self.transcript(session.id)
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

    def append_message(self, session_id: str, message: Message) -> None:
        self.session(session_id)
        self._append(
            self._path("sessions", session_id, ".jsonl"),
            message.model_dump(mode="json"),
        )

    def transcript(self, session_id: str) -> list[dict]:
        with self.lock:
            self.session(session_id)
            path = self._path("sessions", session_id, ".jsonl")
            manifest = self._path("sessions", session_id, ".compaction.json")
            if not path.exists() and manifest.exists():
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
