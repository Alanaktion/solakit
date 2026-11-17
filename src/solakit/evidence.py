"""Append-only evidence and verified byte references on a local filesystem."""

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from solakit.records import (
    CompactionBoundary,
    Delivery,
    DurableEvent,
    Job,
    MemoryCommit,
    MemoryNote,
    MemoryRecall,
    Message,
    ModelRequest,
    ModelStep,
    Occurrence,
    Record,
    RecordID,
    RecoveryNote,
    Run,
    RunState,
    SchedulerControl,
    SchemaVersion,
    Session,
    ToolCall,
    ToolReceipt,
    Value,
    new_id,
)

RECORDS: dict[str, type[Record]] = {
    "session": Session,
    "run": Run,
    "message": Message,
    "model": ModelStep,
    "request": ModelRequest,
    "tool": ToolCall,
    "receipt": ToolReceipt,
    "delivery": Delivery,
    "memory": MemoryCommit,
    "memory_note": MemoryNote,
    "memory_recall": MemoryRecall,
    "compaction": CompactionBoundary,
    "recovery": RecoveryNote,
    "job": Job,
    "occurrence": Occurrence,
    "scheduler": SchedulerControl,
}
SEGMENT = re.compile(r"^[0-9]{8}\.jsonl$")


class StorageError(RuntimeError):
    pass


class CorruptionError(StorageError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def checked_json(data: bytes) -> object:
    return json.loads(data, object_pairs_hook=_unique)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_directory(path: Path) -> None:
    if path.is_dir():
        return
    ensure_directory(path.parent)
    path.mkdir(mode=0o700, exist_ok=True)
    sync_directory(path.parent)


def publish(path: Path, data: bytes) -> None:
    """Publish a new immutable sidecar, never replacing an existing name."""
    temporary = path.with_name(f".{path.name}.{new_id()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def event_type(kind: str, record: Record) -> str:
    if isinstance(record, Run):
        if record.state == RunState.QUEUED:
            return "run.admitted"
        return "run.started" if record.state == RunState.RUNNING else "run.terminal"
    return {
        "message": "message.appended",
        "model": "model.recorded",
        "request": "model.requested",
        "tool": "tool.intent",
        "receipt": "tool.receipt",
        "memory": "memory.changed",
        "memory_note": "memory.changed",
        "memory_recall": "memory.changed",
        "delivery": "delivery.changed",
        "compaction": "compaction.recorded",
        "recovery": "recovery.noted",
    }[kind]


class Entry(Value):
    schema_version: SchemaVersion = 1
    id: RecordID = Field(default_factory=new_id)
    kind: Literal[
        "session",
        "run",
        "message",
        "model",
        "request",
        "tool",
        "receipt",
        "delivery",
        "memory",
        "memory_note",
        "memory_recall",
        "compaction",
        "recovery",
        "legacy",
        "job",
        "occurrence",
        "scheduler",
    ]
    record: dict[str, object]
    event: DurableEvent | None = None
    details: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_record(self) -> Entry:
        if (self.kind not in ("session", "legacy", "job", "occurrence", "scheduler")) != (
            self.event is not None
        ):
            raise ValueError("execution entries require an event; session/legacy entries have none")
        if self.kind in RECORDS:
            record = RECORDS[self.kind].model_validate_json(canonical(self.record))
            if self.event is not None:
                if self.event.type != event_type(self.kind, record):
                    raise ValueError("event type differs from record")
                if self.event.event_id != self.id:
                    raise ValueError("entry and event identities must agree")
                if getattr(record, "session_id", None) != self.event.session_id:
                    raise ValueError("event session mismatch")
                run_id = record.id if isinstance(record, Run) else getattr(record, "run_id", None)
                if run_id != self.event.run_id:
                    raise ValueError("event run mismatch")
                if self.event.payload != {"kind": self.kind, "record_id": record.id}:
                    raise ValueError("event payload mismatch")
        canonical(self.model_dump(mode="json"))
        return self

    def encoded(self) -> bytes:
        return canonical(self.model_dump(mode="json")) + b"\n"


def parse_entry(data: bytes) -> Entry:
    try:
        checked_json(data)
        return Entry.model_validate_json(data)
    except (ValueError, TypeError, UnicodeError) as error:
        raise CorruptionError("invalid complete evidence entry") from error


class Reference(Value):
    segment: Annotated[str, Field(pattern=r"^[0-9]{8}\.jsonl$")]
    offset: Annotated[int, Field(ge=0)]
    length: Annotated[int, Field(gt=0)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    entry_id: RecordID


class EvidenceLog:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.directory = root / "evidence"
        self.references: dict[str, Reference] = {}
        self.active = "00000001.jsonl"
        self.hashers: dict[str, Any] = {}
        self.sizes: dict[str, int] = {}
        self.index_checked = False

    def paths(self) -> list[Path]:
        if not self.directory.is_dir():
            raise CorruptionError("evidence directory unavailable")
        paths = sorted(self.directory.iterdir())
        if any(
            not SEGMENT.fullmatch(path.name) or path.is_symlink() or not path.is_file()
            for path in paths
        ):
            raise CorruptionError("unexpected evidence segment")
        if [int(path.stem) for path in paths] != list(range(1, len(paths) + 1)):
            raise CorruptionError("evidence segment gap")
        return paths

    def _tail(self, path: Path, offset: int, tail: bytes, *, repair: bool) -> None:
        note = self.root / "recovery" / f"{path.stem}.json"
        expected = {
            "schema_version": 1,
            "segment": path.name,
            "offset": offset,
            "length": len(tail),
            "sha256": digest(tail),
            "reason": "incomplete final line",
        }
        if note.exists():
            try:
                actual = checked_json(note.read_bytes())
            except (ValueError, OSError) as error:
                raise CorruptionError("invalid sealed-tail metadata") from error
            if actual != expected:
                raise CorruptionError("sealed evidence tail changed")
        elif repair:
            publish(note, canonical(expected))
        else:
            raise CorruptionError("unsealed incomplete evidence tail; recovery required")

    def scan(self, *, repair: bool = False) -> Iterator[tuple[Reference, Entry]]:
        seen: dict[str, str] = {}
        sequences: dict[str, int] = {}
        paths = self.paths()
        for path in paths:
            offset = 0
            torn = False
            with path.open("rb") as stream:
                for data in stream:
                    if not data.endswith(b"\n"):
                        if (
                            path != paths[-1]
                            and not (self.root / "recovery" / f"{path.stem}.json").exists()
                        ):
                            raise CorruptionError("incomplete interior segment")
                        self._tail(path, offset, data, repair=repair)
                        torn = True
                        break
                    entry = parse_entry(data)
                    fingerprint = digest(data)
                    prior = seen.get(entry.id)
                    if prior is not None:
                        if prior != fingerprint:
                            raise CorruptionError("evidence ID has conflicting payloads")
                        raise CorruptionError("duplicate physical evidence entry")
                    seen[entry.id] = fingerprint
                    if entry.event is not None:
                        session = entry.event.session_id
                        if entry.event.sequence != sequences.get(session, 0) + 1:
                            raise CorruptionError("evidence event sequence gap")
                        sequences[session] = entry.event.sequence
                    ref = Reference(
                        segment=path.name,
                        offset=offset,
                        length=len(data),
                        sha256=fingerprint,
                        entry_id=entry.id,
                    )
                    yield ref, entry
                    offset += len(data)
            if not torn and (self.root / "recovery" / f"{path.stem}.json").exists():
                raise CorruptionError("sealed tail missing")
        self.active = (
            f"{len(paths) + (1 if paths and torn else 0):08d}.jsonl" if paths else "00000001.jsonl"
        )

    def recover(self) -> None:
        self.references = {ref.entry_id: ref for ref, _ in self.scan(repair=True)}
        # Sync existing segments too: a prior writer may have died before its fsync.
        self.hashers.clear()
        self.sizes.clear()
        self.index_checked = False
        for path in self.paths():
            with path.open("rb") as stream:
                hasher = hashlib.sha256()
                size = 0
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
                    size += len(chunk)
                self.hashers[path.name] = hasher
                self.sizes[path.name] = size
                os.fsync(stream.fileno())
        sync_directory(self.directory)

    def validate_seal(self, path: Path) -> None:
        note = self.root / "recovery" / f"{path.stem}.json"
        try:
            with path.open("rb") as stream:
                if note.exists():
                    metadata = checked_json(note.read_bytes())
                    if not isinstance(metadata, dict) or type(metadata.get("offset")) is not int:
                        raise ValueError("invalid sealed-tail offset")
                    offset = metadata["offset"]
                    if offset < 0:
                        raise ValueError("negative sealed-tail offset")
                    if offset:
                        stream.seek(offset - 1)
                        if stream.read(1) != b"\n":
                            raise ValueError("sealed tail has no line boundary")
                    stream.seek(offset)
                    tail = stream.read()
                    if not tail or b"\n" in tail:
                        raise ValueError("sealed tail missing or contains complete lines")
                    self._tail(path, offset, tail, repair=False)
                else:
                    stream.seek(0, os.SEEK_END)
                    if stream.tell():
                        stream.seek(-1, os.SEEK_END)
                        if stream.read(1) != b"\n":
                            raise CorruptionError(
                                "unsealed incomplete evidence tail; recovery required"
                            )
        except (OSError, ValueError) as error:
            raise CorruptionError("invalid sealed-tail metadata") from error

    def read(self, ref: Reference) -> Entry:
        path = self.directory / ref.segment
        self.validate_seal(path)
        try:
            with path.open("rb") as stream:
                stream.seek(ref.offset)
                data = stream.read(ref.length)
        except OSError as error:
            raise CorruptionError("evidence reference unavailable") from error
        if digest(data) != ref.sha256 or not data.endswith(b"\n"):
            raise CorruptionError("stale or corrupt evidence offset")
        entry = parse_entry(data)
        if entry.id != ref.entry_id:
            raise CorruptionError("evidence identity mismatch")
        return entry

    def append(self, entry: Entry, fault: Callable[[str], None]) -> Reference:
        data = entry.encoded()
        prior = self.references.get(entry.id)
        if prior is not None:
            if self.read(prior).encoded() != data:
                raise CorruptionError("outbox payload conflicts with evidence")
            return prior
        path = self.directory / self.active
        fresh = not path.exists()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as stream:
            offset = stream.tell()
            stream.write(data)
            stream.flush()
            fault("append")
            os.fsync(stream.fileno())
            if fresh:
                sync_directory(self.directory)
            fault("fsync")
        ref = Reference(
            segment=self.active,
            offset=offset,
            length=len(data),
            sha256=digest(data),
            entry_id=entry.id,
        )
        self.references[entry.id] = ref
        self.hashers.setdefault(self.active, hashlib.sha256()).update(data)
        self.sizes[self.active] = offset + len(data)
        return ref
