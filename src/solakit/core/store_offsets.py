"""Disposable byte addresses for run messages; JSONL remains authoritative."""

import json
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from solakit.core.contracts import StoreServices
from solakit.core.schemas import Message

OFFSET_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_offsets (
    session_id TEXT NOT NULL, file TEXT NOT NULL, offset INTEGER NOT NULL,
    length INTEGER NOT NULL, run_id TEXT NOT NULL,
    PRIMARY KEY(session_id, file, offset)
);
CREATE INDEX IF NOT EXISTS message_offsets_run ON message_offsets(run_id, session_id);
CREATE TABLE IF NOT EXISTS message_files (
    session_id TEXT NOT NULL, file TEXT NOT NULL, signature TEXT NOT NULL,
    PRIMARY KEY(session_id, file)
);
"""


def file_signature(path: Path) -> str:
    stat = path.stat()
    return json.dumps(
        [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    )


class OffsetMixin(StoreServices):
    def _refresh_offsets(self, db: sqlite3.Connection, session_id: str) -> None:
        names = [*self.archives(session_id), f"{session_id}.jsonl"]
        existing = dict(
            db.execute(
                "SELECT file, signature FROM message_files WHERE session_id=?",
                (session_id,),
            )
        )
        for name in names:
            path = self.root / "sessions" / name
            if not path.exists():
                continue
            signature = file_signature(path)
            if existing.pop(name, None) == signature:
                continue
            # Reuse the evidence reader's recovery and corruption rules. Offsets
            # refer to original bytes, including any explicitly recovered lines.
            records = dict(self._read_lines(path))
            rows = []
            offset = 0
            with path.open("rb") as stream:
                for line_no, line in enumerate(
                    stream.read().splitlines(keepends=True), 1
                ):
                    record = records.get(line_no)
                    if record is not None:
                        try:
                            message = Message.model_validate(record)
                        except ValidationError as exc:
                            raise self._corruption(
                                path, "invalid_message", offset
                            ) from exc
                        rows.append(
                            (session_id, name, offset, len(line), message.run_id)
                        )
                    offset += len(line)
            db.execute(
                "DELETE FROM message_offsets WHERE session_id=? AND file=?",
                (session_id, name),
            )
            db.executemany("INSERT INTO message_offsets VALUES (?, ?, ?, ?, ?)", rows)
            db.execute(
                "INSERT OR REPLACE INTO message_files VALUES (?, ?, ?)",
                (session_id, name, signature),
            )
        for name in existing:
            db.execute(
                "DELETE FROM message_offsets WHERE session_id=? AND file=?",
                (session_id, name),
            )
            db.execute(
                "DELETE FROM message_files WHERE session_id=? AND file=?",
                (session_id, name),
            )

    def _append_offset(
        self, session_id: str, path: Path, previous_signature: str | None, run_id: str
    ) -> None:
        with self._database() as db:
            # Only advance a complete file index. A missing/stale cache is rebuilt
            # on demand; never mark earlier unindexed evidence as current.
            saved = db.execute(
                "SELECT signature FROM message_files WHERE session_id=? AND file=?",
                (session_id, path.name),
            ).fetchone()
            if saved is None:
                return
            stat = path.stat()
            if saved[0] != previous_signature:
                return
            offset = json.loads(saved[0])[2]
            db.execute(
                "INSERT OR REPLACE INTO message_offsets VALUES (?, ?, ?, ?, ?)",
                (session_id, path.name, offset, stat.st_size - offset, run_id),
            )
            db.execute(
                "UPDATE message_files SET signature=? WHERE session_id=? AND file=?",
                (file_signature(path), session_id, path.name),
            )

    def run_messages(self, session_id: str, run_id: str) -> list[dict]:
        with self.lock:
            self.check_transcript(session_id)
            try:
                with self._database() as db:
                    self._refresh_offsets(db, session_id)
                    rows = db.execute(
                        "SELECT file, offset, length FROM message_offsets "
                        "WHERE run_id=? AND session_id=?",
                        (run_id, session_id),
                    ).fetchall()
            except sqlite3.Error:
                # A disposable index cannot make otherwise readable evidence
                # unavailable. Degraded reads retain the original validated scan.
                self._issue(self.root / "app.db", "run_message_index_unavailable")
                return [
                    m for m in self.all_messages(session_id) if m["run_id"] == run_id
                ]
            rows.sort(
                key=lambda row: (
                    int(row[0].rsplit(".", 1)[1])
                    if row[0] != f"{session_id}.jsonl"
                    else float("inf"),
                    row[1],
                )
            )
            result = []
            for name, offset, length in rows:
                path = self.root / "sessions" / name
                with path.open("rb") as stream:
                    stream.seek(offset)
                    payload = stream.read(length)
                try:
                    record = self._decode_record(payload.removesuffix(b"\n"))
                    message = Message.model_validate(record)
                    if message.run_id != run_id:
                        raise ValueError("Run address mismatch")
                except (ValueError, UnicodeDecodeError) as exc:
                    raise self._corruption(path, "invalid_message", offset) from exc
                result.append(record)
            return result
