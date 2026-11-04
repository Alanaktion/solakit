"""Durable append-only evidence with a disposable SQLite index.

A run's JSONL event stream is authoritative. The .json file is a replaceable
inspection snapshot, never the only copy. A torn final JSONL write is preserved
and ignored during reads; later writes begin on a fresh line.
"""

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from solakit.core.schemas import Message, Run, RunStatus, Session, now
from solakit.core.secrets import Redactor

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def safe_id(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError("invalid record identifier")
    return value


class Store:
    def __init__(
        self, root: Path, redactor: Redactor | None = None, exclusive: bool = False
    ):
        self.root = root.expanduser().resolve()
        self.redactor = redactor or Redactor()
        self.lock = threading.RLock()
        for directory in (self.root, self.root / "sessions", self.root / "runs"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ownership = None
        if exclusive:
            self.ownership = (self.root / ".runtime.lock").open("a")
            try:
                fcntl.flock(self.ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.ownership.close()
                raise RuntimeError("Another runtime owns this data directory") from None
        self.rebuild()
        self.recover()

    def close(self) -> None:
        if self.ownership is not None:
            self.ownership.close()
            self.ownership = None

    def _path(self, kind: str, record_id: str, suffix: str) -> Path:
        return self.root / kind / f"{safe_id(record_id)}{suffix}"

    def _append(self, path: Path, record: dict) -> None:
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self.lock, path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell():
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    stream.write(b"\n")
            stream.write(payload.encode("utf-8") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _read(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        records = []
        for line in path.read_bytes().splitlines():
            try:
                records.append(json.loads(line))
            except (ValueError, UnicodeDecodeError):
                # Preserve partial/corrupt evidence; never rewrite history.
                continue
        return records

    def _index(self, kind: str, record_id: str, payload: dict) -> None:
        with self.lock, sqlite3.connect(self.root / "app.db") as db:
            db.execute(
                f"INSERT OR REPLACE INTO {kind} VALUES (?, ?)",
                (record_id, json.dumps(payload)),
            )

    def rebuild(self) -> None:
        with self.lock, sqlite3.connect(self.root / "app.db") as db:
            for kind in ("sessions", "runs"):
                db.execute(
                    f"CREATE TABLE IF NOT EXISTS {kind} "
                    "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                )
                db.execute(f"DELETE FROM {kind}")
            for path in sorted((self.root / "sessions").glob("*.meta.json")):
                session = Session.model_validate_json(path.read_bytes())
                db.execute(
                    "INSERT INTO sessions VALUES (?, ?)",
                    (session.id, session.model_dump_json()),
                )
            for path in sorted((self.root / "runs").glob("*.events.jsonl")):
                records = self._read(path)
                if records:
                    run = Run.model_validate(records[-1])
                    db.execute(
                        "INSERT INTO runs VALUES (?, ?)",
                        (run.id, run.model_dump_json()),
                    )

    def recover(self) -> None:
        for record in self.list_records("runs"):
            run = Run.model_validate(record)
            if run.status in (RunStatus.queued, RunStatus.running):
                run.status = RunStatus.interrupted
                run.finished_at = now()
                run.error = "Process stopped before run finalization"
                self.save_run(run)

    def create_session(self, session: Session) -> Session:
        path = self._path("sessions", session.id, ".meta.json")
        with self.lock:
            if path.exists():
                return self.session(session.id)
            # Metadata is immutable, created once; index can always be rebuilt.
            with path.open("x", encoding="utf-8") as stream:
                stream.write(session.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            self._index("sessions", session.id, session.model_dump(mode="json"))
        return session

    def session(self, session_id: str) -> Session:
        return Session.model_validate_json(
            self._path("sessions", session_id, ".meta.json").read_bytes()
        )

    def append_message(self, session_id: str, message: Message) -> None:
        self.session(session_id)
        self._append(
            self._path("sessions", session_id, ".jsonl"),
            message.model_dump(mode="json"),
        )

    def transcript(self, session_id: str) -> list[dict]:
        self.session(session_id)
        return self._read(self._path("sessions", session_id, ".jsonl"))

    def save_run(self, run: Run) -> None:
        with self.lock:
            record = self.redactor.redact(run.model_dump(mode="json"))
            self._append(self._path("runs", run.id, ".events.jsonl"), record)
            path = self._path("runs", run.id, ".json")
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record), encoding="utf-8")
            temporary.replace(path)
            self._index("runs", run.id, record)

    def run(self, run_id: str) -> dict:
        events = self._read(self._path("runs", run_id, ".events.jsonl"))
        if not events:
            raise FileNotFoundError(run_id)
        record = events[-1].copy()
        prompt = self._path("runs", run_id, ".prompt.txt")
        record["prompt"] = (
            prompt.read_bytes().decode("utf-8") if prompt.exists() else None
        )
        record["memory_events"] = self.memory_events(run_id)
        record["tools"] = self._read(self._path("runs", run_id, ".tools.jsonl"))
        return record

    def write_prompt(self, run_id: str, prompt: str) -> str:
        payload = prompt.encode("utf-8")
        # Never redact prompt bytes: this is the exact model input for audit.
        with self._path("runs", run_id, ".prompt.txt").open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
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

    def list_records(self, kind: str) -> list[dict]:
        if kind not in {"sessions", "runs"}:
            raise ValueError("unknown index")
        with self.lock, sqlite3.connect(self.root / "app.db") as db:
            rows = db.execute(f"SELECT payload FROM {kind} ORDER BY rowid").fetchall()
        return [json.loads(row[0]) for row in rows]
