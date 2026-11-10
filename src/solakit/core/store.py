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
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from solakit.core.schemas import (
    Message,
    Run,
    Session,
    SessionArchived,
    now,
)
from solakit.core.secrets import Redactor
from solakit.core.store_base import (  # noqa: F401 (re-exported)
    IDENTIFIER,
    INDEX_VERSION,
    StorageCorruption,
    atomic_write,
    safe_id,
    sync_directory,
)
from solakit.core.store_index import IndexMixin
from solakit.core.store_offsets import OffsetMixin, file_signature
from solakit.core.store_sidecars import SidecarMixin


class Store(IndexMixin, SidecarMixin, OffsetMixin):
    def __init__(
        self, root: Path, redactor: Redactor | None = None, exclusive: bool = False
    ):
        self.root = root.expanduser().resolve()
        self.redactor = redactor or Redactor()
        self.lock = threading.RLock()
        self._fts_dirty_sessions: set[str] = set()
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
        self._db: sqlite3.Connection | None = None
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

    @contextmanager
    def _database(self):
        """Serialize access to the reusable derived-index connection."""
        with self.lock:
            if self._db is None:
                self._db = sqlite3.connect(
                    self.root / "app.db", check_same_thread=False
                )
            with self._db:
                yield self._db

    def close(self) -> None:
        with self.lock:
            if self._db is not None:
                self._db.close()
                self._db = None
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
            before_signature = file_signature(path) if before else None
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
                self._append_offset(session_id, path, before_signature, message.run_id)
                self._insert_search_message(
                    self._message_search_row(session_id, "active", line_no, record)
                )
            except (sqlite3.Error, OSError) as exc:
                self._search_write_failed(session_id, exc)

    def check_transcript(self, session_id: str) -> None:
        """Validate rotation state and active-file presence without reading history."""
        with self.lock:
            self.session(session_id)
            self.context_started_at(session_id)
            self.check_compaction(session_id)
            path = self._path("sessions", session_id, ".jsonl")
            manifest = self._path("sessions", session_id, ".compaction.json")
            manual = self._path("sessions", session_id, ".archive.json")
            if not path.exists() and (manifest.exists() or manual.exists()):
                raise self._corruption(path, "missing_active_transcript")

    def transcript(self, session_id: str) -> list[dict]:
        with self.lock:
            self.check_transcript(session_id)
            path = self._path("sessions", session_id, ".jsonl")
            records = self._read(path)
            try:
                for record in records:
                    Message.model_validate(record)
            except ValidationError as exc:
                raise self._corruption(path, "invalid_message") from exc
            return records

    def archives(self, session_id: str) -> list[str]:
        self.session(session_id)
        candidates = (self.root / "sessions").glob(
            f"{safe_id(session_id)}.jsonl.[0-9]*"
        )
        prefix = f"{session_id}.jsonl."
        paths = [p for p in candidates if p.name.removeprefix(prefix).isdigit()]
        return [p.name for p in sorted(paths, key=lambda p: int(p.suffix[1:]))]

    def archive(self, session_id: str, number: int) -> list[dict]:
        """Read an existing immutable transcript archive."""
        safe_id(session_id)
        name = f"{session_id}.jsonl.{number}"
        with self.lock:
            if number < 1 or name not in self.archives(session_id):
                raise FileNotFoundError(name)
            return self._read(self.root / "sessions" / name)

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
            with self._database() as db:
                db.execute(
                    "INSERT OR REPLACE INTO runs VALUES (?, ?)",
                    (run.id, json.dumps(record)),
                )
                # Recovery may finish ancient runs today. Admission time alone
                # counts as activity; activity cache damage cannot fail a save.
                try:
                    self._activity(
                        db, run.session, self._activity_timestamp(run.created_at)
                    )
                except sqlite3.Error:
                    self._issue(
                        self.root / "app.db", "session_activity_index_unavailable"
                    )

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
        with self._database() as db:
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
        record["messages"] = (
            self.run_messages(record["session"], run_id) if include_messages else []
        )
        record["memory_events"] = self.memory_events(run_id)
        record["memory_notes"] = self.memory_notes(run_id)
        record["tools"] = self._read(self._path("runs", run_id, ".tools.jsonl"))
        record["reasoning"] = self.reasoning(run_id)
        record["plan"] = plan_state(self.plan_events(run_id))
        record["injection_texts"] = self.injections(run_id)
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
            result: list[dict] = []
            remainder = b""
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


def plan_state(events: list[dict]) -> list[dict]:
    """Fold append-only plan events into the current checklist."""
    items: list[dict] = []
    for event in events:
        if event.get("op") == "set":
            items = [
                {"id": index + 1, "text": text, "status": "todo", "reason": None}
                for index, text in enumerate(event.get("items", []))
            ]
        elif event.get("op") == "update":
            for item in items:
                if item["id"] == event.get("id"):
                    item["status"] = event.get("status", item["status"])
                    item["reason"] = event.get("reason")
    return items
