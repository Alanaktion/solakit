"""Operational SQLite state and durable session services; caller owns the data lock."""

import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import JsonValue, TypeAdapter

from solakit.records import (
    Artifact,
    CompactionBoundary,
    Delegation,
    Delivery,
    DeliveryState,
    DurableEvent,
    ErrorCode,
    Failure,
    ImageGeneration,
    Job,
    LetterPublication,
    MemoryCommit,
    MemoryNote,
    MemoryRecall,
    MemoryState,
    Message,
    ModelRequest,
    ModelStep,
    Occurrence,
    Origin,
    Record,
    RecordID,
    RecoveryNote,
    Run,
    RunState,
    SchedulerControl,
    Session,
    SessionMode,
    Timestamp,
    ToolCall,
    ToolReceipt,
    new_id,
    utc_now,
)
from solakit.storage.evidence import (
    RECORDS,
    CorruptionError,
    Entry,
    EvidenceLog,
    Reference,
    StorageError,
    canonical,
    digest,
    ensure_directory,
    event_type,
    publish,
    sync_directory,
)

if TYPE_CHECKING:
    from solakit.config import EffectiveProfile


type StoredRecord = (
    Session
    | Run
    | Message
    | ModelStep
    | ModelRequest
    | ToolCall
    | ToolReceipt
    | Delegation
    | Delivery
    | MemoryNote
    | MemoryRecall
    | MemoryCommit
    | CompactionBoundary
    | RecoveryNote
    | Job
    | Occurrence
    | SchedulerControl
    | ImageGeneration
    | LetterPublication
)

SCHEMA = """
CREATE TABLE entities (
 kind TEXT NOT NULL, id TEXT NOT NULL, session_id TEXT, run_id TEXT,
 sequence INTEGER NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL,
 entry_id TEXT NOT NULL UNIQUE, PRIMARY KEY(kind,id)
);
CREATE INDEX entity_pages ON entities(kind,session_id,sequence,id);
CREATE INDEX entity_runs ON entities(kind,run_id,sequence);
CREATE INDEX session_pages ON entities(kind,created_at,id);
CREATE TABLE sessions (id TEXT PRIMARY KEY, sequence INTEGER NOT NULL);
CREATE TABLE admissions (request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
 run_id TEXT NOT NULL UNIQUE);
CREATE TABLE outbox (ordinal INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
 body BLOB NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, reference TEXT);
CREATE INDEX outbox_pending ON outbox(ordinal) WHERE delivered=0;
CREATE INDEX event_pages ON outbox(
 json_extract(body,'$.event.session_id'),json_extract(body,'$.event.sequence'));
CREATE INDEX tool_receipts ON entities(kind,json_extract(body,'$.tool_call_id'));
CREATE INDEX model_attempts ON entities(kind,run_id,json_extract(body,'$.index'));
CREATE TABLE imports (source TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
PRAGMA user_version=1;
"""
ID_ADAPTER = TypeAdapter(RecordID)
TIME_ADAPTER = TypeAdapter(Timestamp)

AUTH_SCHEMA = """
CREATE TABLE login_sessions (
 token_hash TEXT PRIMARY KEY, csrf_hash TEXT NOT NULL, expires_at REAL NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE login_throttle (id INTEGER PRIMARY KEY CHECK(id=1),
 failures INTEGER NOT NULL, window_start REAL NOT NULL);
PRAGMA user_version=2;
"""

ARCHIVED_SESSIONS = (
    "SELECT id FROM entities WHERE kind='session' AND json_extract(body,'$.archived')=1"
    " AND id NOT IN (SELECT json_extract(body,'$.event.session_id') FROM outbox "
    "WHERE delivered=0 AND json_extract(body,'$.event.session_id') IS NOT NULL)"
)
ARCHIVED_ENTRIES = (
    "SELECT id FROM outbox WHERE delivered=1 "
    "AND coalesce(json_extract(body,'$.event.session_id'),"
    "CASE WHEN json_extract(body,'$.kind')='legacy' THEN "
    "json_extract(body,'$.details.session_id') END) IN (" + ARCHIVED_SESSIONS + ")"
)
TRANSFER_RUN = "session-transfer:run:"
TRANSFER_SOURCE = "session_transfer"
LOCAL_RUN = (
    f"run_id NOT IN (SELECT substr(source,{len(TRANSFER_RUN) + 1}) FROM imports "
    f"WHERE source GLOB '{TRANSFER_RUN}*')"
)


def valid_id(value: str) -> str:
    return ID_ADAPTER.validate_python(value)


def page_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("page limit must be between 1 and 100")


def no_fault(stage: str) -> None:
    pass


class Store:
    def __init__(self, root: Path, *, fault: Callable[[str], None] = no_fault) -> None:
        self.root = root
        self.log = EvidenceLog(root)
        self.fault = fault
        self._connection: sqlite3.Connection | None = None
        self.failed = False
        self.search_status = "unavailable"
        self.search_error: str | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._connection is None or self.failed:
            raise StorageError("store unavailable; close and recover before continuing")
        return self._connection

    async def start(self) -> None:
        if self._connection is not None:
            if self.failed:
                raise StorageError("close and recover the failed store before restarting")
            return
        self.failed = False
        for name in ("evidence", "recovery", "artifacts", "imports"):
            directory = self.root / name
            if directory.is_symlink():
                raise CorruptionError("managed directory cannot be a symlink")
            ensure_directory(directory)
        path = self.root / "state.db"
        if path.is_symlink():
            raise CorruptionError("state database cannot be a symlink")
        if not path.exists() and any(self.log.paths()):
            raise CorruptionError("operational state missing; restore a coherent backup")
        path.touch(mode=0o600, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.create_function("casefold", 1, str.casefold, deterministic=True)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise CorruptionError("unversioned operational database")
                self.db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
                sync_directory(self.root)
            elif version not in (1, 2):
                raise CorruptionError("unsupported state schema version")
            if version in (0, 1):
                self.db.executescript("BEGIN IMMEDIATE;\n" + AUTH_SCHEMA + "\nCOMMIT;")
            if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise CorruptionError("state integrity check failed")
            # shortcut: check archived payloads on reads/restore; rebuild offline for eager checks.
            archived = tuple(
                (
                    Reference.model_validate_json(row[0]),
                    row[1],
                    row[2] or 0,
                )
                for row in self.db.execute(
                    "SELECT reference,json_extract(body,'$.event.session_id'),"
                    "json_extract(body,'$.event.sequence') FROM outbox WHERE id IN ("
                    + ARCHIVED_ENTRIES
                    + ")"
                )
            )
            self.log.recover(archived=archived)
            self._reconcile(active_only=True)
            self._flush()
            from solakit.engine.compaction import recover

            recover(self)
            self._recover_work()
            self._index()
        except BaseException:
            self.failed = True
            raise

    async def stop(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _reconcile(self, *, active_only: bool = False) -> None:
        known = {row[0] for row in self.db.execute("SELECT id FROM outbox")}
        if set(self.log.references) - known:
            raise CorruptionError("evidence absent from operational outbox; restore state")
        latest: dict[tuple[str, str], Entry] = {}
        sequences: dict[str, int] = {}
        first_sequence: dict[tuple[str, str], int] = {}
        admissions: dict[str, tuple[str, str]] = {}
        rows = self.db.execute(
            "SELECT * FROM outbox "
            + ("WHERE id NOT IN (" + ARCHIVED_ENTRIES + ") " if active_only else "")
            + "ORDER BY ordinal"
        )
        for row in rows:
            ref = self.log.references.get(row["id"])
            if ref is None:
                if row["delivered"]:
                    raise CorruptionError("acknowledged evidence missing")
                entry = Entry.model_validate_json(row["body"])
            else:
                entry = self.log.read(ref)
                if entry.encoded() != row["body"]:
                    raise CorruptionError("outbox/evidence payload mismatch")
                if row["delivered"] and row["reference"] != ref.model_dump_json():
                    raise CorruptionError("state evidence offset mismatch")
            if entry.kind in RECORDS:
                key = (entry.kind, str(entry.record["id"]))
                latest[key] = entry
                if key not in first_sequence and entry.kind == "run":
                    if bool(entry.details.get(TRANSFER_SOURCE)) != self.imported_run(key[1]):
                        raise CorruptionError("run import marker differs from evidence")
                first_sequence.setdefault(key, entry.event.sequence if entry.event else 0)
                if (
                    entry.event
                    and entry.event.type == "run.admitted"
                    and not entry.details.get(TRANSFER_SOURCE)
                ):
                    admissions[str(entry.record["request_id"])] = (
                        digest(canonical(entry.details)),
                        key[1],
                    )
            if entry.event:
                event = entry.event
                if event.sequence != sequences.get(event.session_id, 0) + 1:
                    raise CorruptionError("operational event sequence gap")
                sequences[event.session_id] = event.sequence
        entities = self.db.execute(
            "SELECT * FROM entities"
            + (
                " WHERE kind IN ('session','job') OR session_id IS NULL OR session_id NOT IN ("
                + ARCHIVED_SESSIONS
                + ")"
                if active_only
                else ""
            )
        ).fetchall()
        if len(entities) != len(latest):
            raise CorruptionError("operational record missing")
        for row in entities:
            latest_entry = latest.get((row["kind"], row["id"]))
            if (
                latest_entry is None
                or latest_entry.id != row["entry_id"]
                or canonical(latest_entry.record).decode() != row["body"]
            ):
                raise CorruptionError("operational snapshot differs from evidence")
            record = RECORDS[row["kind"]].model_validate_json(canonical(latest_entry.record))
            session_id = (
                record.id if isinstance(record, Session) else latest_entry.record.get("session_id")
            )
            run_id = record.id if isinstance(record, Run) else latest_entry.record.get("run_id")
            if (
                row["session_id"] != session_id
                or row["run_id"] != run_id
                or row["sequence"] != first_sequence[row["kind"], row["id"]]
                or row["created_at"] != record.created_at.isoformat()
            ):
                raise CorruptionError("operational routing/cursor mismatch")
        actual_admissions = {
            row["request_id"]: (row["fingerprint"], row["run_id"])
            for row in self.db.execute(
                "SELECT * FROM admissions"
                + (
                    " WHERE run_id NOT IN (SELECT id FROM entities WHERE kind='run' "
                    "AND session_id IN (" + ARCHIVED_SESSIONS + "))"
                    if active_only
                    else ""
                )
            )
        }
        if actual_admissions != admissions:
            raise CorruptionError("admission ledger differs from evidence")
        session_rows = self.db.execute("SELECT * FROM sessions").fetchall()
        if {row["id"] for row in session_rows} != {
            identity for kind, identity in latest if kind == "session"
        }:
            raise CorruptionError("session routing row missing")
        archived_sessions = (
            {session[0] for session in self.db.execute(ARCHIVED_SESSIONS)} if active_only else set()
        )
        for row in session_rows:
            if row["id"] in archived_sessions:
                continue
            if row["sequence"] != sequences.get(row["id"], 0):
                raise CorruptionError("session high-water mismatch")

    def _flush(self) -> None:
        try:
            for row in self.db.execute(
                "SELECT * FROM outbox WHERE delivered=0 ORDER BY ordinal"
            ).fetchall():
                entry = Entry.model_validate_json(row["body"])
                ref = self.log.append(entry, self.fault)
                with self.db:
                    self.db.execute(
                        "UPDATE outbox SET delivered=1, reference=? WHERE id=?",
                        (ref.model_dump_json(), entry.id),
                    )
                self.fault("outbox_complete")
        except BaseException:
            self.failed = True
            raise

    def _index(self) -> None:
        from solakit.storage.search import update_index

        try:
            update_index(self.log)
            self.search_status = "available"
            self.search_error = None
        except Exception as error:
            self.search_status = "degraded"
            self.search_error = str(error)

    def _entry(self, record: StoredRecord, details: dict[str, object]) -> Entry:
        kind = next(name for name, cls in RECORDS.items() if isinstance(record, cls))
        session_id = (
            record.id if isinstance(record, Session) else getattr(record, "session_id", None)
        )
        event = None
        sequence = 0
        entry_id = new_id()
        if not isinstance(record, (Session, Job, Occurrence, SchedulerControl, LetterPublication)):
            sequence = self.db.execute(
                "SELECT sequence FROM sessions WHERE id=?", (session_id,)
            ).fetchone()[0]
            sequence += 1
            event = DurableEvent.model_validate(
                {
                    "event_id": entry_id,
                    "session_id": session_id,
                    "run_id": record.id if isinstance(record, Run) else record.run_id,
                    "sequence": sequence,
                    "type": event_type(kind, record),
                    "payload": {"kind": kind, "record_id": record.id},
                }
            )
        return Entry(
            id=entry_id,
            kind=kind,  # type: ignore[arg-type]
            record=record.model_dump(mode="json"),
            event=event,
            details=details,
        )

    def _save_entry(self, entry: Entry) -> None:
        identity = entry.record["id"]
        event = entry.event
        session_id = identity if entry.kind == "session" else entry.record.get("session_id")
        sequence = event.sequence if event else 0
        if (
            event
            and not self.db.execute(
                "UPDATE sessions SET sequence=? WHERE id=? AND sequence=?",
                (sequence, session_id, sequence - 1),
            ).rowcount
        ):
            raise CorruptionError("invalid next session sequence")
        record = RECORDS[entry.kind].model_validate_json(canonical(entry.record))
        self.db.execute(
            "INSERT INTO entities VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET "
            "body=excluded.body,entry_id=excluded.entry_id",
            (
                entry.kind,
                identity,
                session_id,
                event.run_id if event else entry.record.get("run_id"),
                sequence,
                record.created_at.isoformat(),
                canonical(entry.record).decode(),
                entry.id,
            ),
        )
        self.db.execute("INSERT INTO outbox(id,body) VALUES (?,?)", (entry.id, entry.encoded()))

    def _commit(
        self,
        records: tuple[StoredRecord, ...],
        *,
        details: dict[str, object] | None = None,
        prepare: Callable[[], None] = lambda: None,
    ) -> None:
        self._commit_entries((self._entry(record, details or {}) for record in records), prepare)

    def _commit_entries(self, entries: Iterable[Entry], prepare: Callable[[], None]) -> None:
        self._flush()
        try:
            with self.db:
                prepare()
                for entry in entries:
                    self._save_entry(entry)
            self.fault("state_commit")
            self._flush()
            self._index()
            self.fault("acknowledge")
        except BaseException:
            self.failed = True
            raise

    def _load[T: Record](self, kind: str, identity: str, cls: type[T]) -> T:
        valid_id(identity)
        row = self.db.execute(
            "SELECT * FROM entities WHERE kind=? AND id=?", (kind, identity)
        ).fetchone()
        if row is None:
            raise KeyError(identity)
        return self._read_row(row, cls)

    def _read_row[T: Record](self, row: sqlite3.Row, cls: type[T]) -> T:
        ref = self.log.references.get(row["entry_id"])
        if ref is None:
            raise StorageError("record evidence pending")
        entry = self.log.read(ref)
        body = canonical(entry.record)
        if (
            entry.kind != row["kind"]
            or entry.record["id"] != row["id"]
            or body.decode() != row["body"]
        ):
            raise CorruptionError("record projection differs from evidence")
        return cls.model_validate_json(body)

    def session(self, identity: str) -> Session:
        return self._load("session", identity, Session)

    def run(self, identity: str) -> Run:
        return self._load("run", identity, Run)

    def retried_by(self, identity: str) -> Run | None:
        row = self.db.execute(
            "SELECT id FROM entities WHERE kind='run' AND session_id=? "
            "AND json_extract(body,'$.retry_of')=?",
            (self.run(identity).session_id, identity),
        ).fetchone()
        return self.run(row[0]) if row else None

    def retry_chain(self, identity: str) -> list[Run]:
        """Every attempt up to this one, original first."""
        chain = [self.run(identity)]
        while previous := chain[-1].retry_of:
            chain.append(self.run(previous))
        return chain[::-1]

    def superseded(self, identity: str) -> bool:
        """A later retry of this attempt has started."""
        while following := self.retried_by(identity):
            if following.started_at is not None:
                return True
            identity = following.id
        return False

    def imported_run(self, identity: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM imports WHERE source=?", (TRANSFER_RUN + identity,)
            ).fetchone()
            is not None
        )

    def require_local_run(self, identity: str) -> None:
        if self.imported_run(identity):
            raise ValueError("Imported run evidence is historical")

    def job(self, identity: str) -> Job:
        return self._load("job", identity, Job)

    def occurrence(self, identity: str) -> Occurrence:
        return self._load("occurrence", identity, Occurrence)

    def letter_publication(self, identity: str) -> LetterPublication:
        return self._load("letter_publication", identity, LetterPublication)

    def letter_checkout_name(self, path: Path) -> str:
        from solakit.config import same_path

        for row in self.db.execute(
            "SELECT DISTINCT json_extract(body,'$.checkout') FROM entities "
            "WHERE kind='letter_publication'"
        ):
            if same_path(path, Path(row[0])):
                return str(row[0])
        return str(path)

    def letter_number_reserved(self, checkout: str, number: int) -> bool:
        return any(
            self.letter_publication(row[0]).number == number
            for row in self.db.execute(
                "SELECT id FROM entities WHERE kind='letter_publication' "
                "AND json_extract(body,'$.checkout')=? "
                "AND json_extract(body,'$.phase')!='confirmed' "
                "AND json_extract(body,'$.number') IS NOT NULL",
                (checkout,),
            )
        )

    def save_letter_publication(self, record: LetterPublication) -> None:
        try:
            previous = self.letter_publication(record.id)
        except KeyError:
            if record.tool_call_id is not None:
                call = self._load("tool", record.tool_call_id, ToolCall)
                expected = "letters.send" if record.reply_to is None else "letters.reply"
                if (
                    call.name != expected
                    or call.origin != record.origin
                    or record.phase != "prepared"
                ):
                    raise ValueError("Letter publication differs from its tool intent") from None
            elif record.origin != Origin.SYSTEM or record.legacy_evidence is None:
                raise ValueError("Letter publication requires tool or legacy evidence") from None
        else:
            phases = ("prepared", "created", "committed", "confirmed")
            mutable = {"phase", "commit", "result"}
            if (
                previous.phase == "prepared"
                and previous.origin == Origin.SYSTEM
                and previous.legacy_evidence is not None
                and record.phase == "confirmed"
            ):
                mutable.update(
                    field
                    for field in ("number", "path", "file_hash", "base", "request_hash")
                    if getattr(previous, field) is None
                )
            if (
                phases.index(record.phase) <= phases.index(previous.phase)
                or (previous.commit is not None and record.commit != previous.commit)
                or record.model_dump(exclude=mutable) != previous.model_dump(exclude=mutable)
            ):
                raise ValueError("Letter publication cannot replace its original allocation")
        self._commit((record,))

    def jobs(self, *, include_deleted: bool = False) -> list[Job]:
        return [
            self.job(row[0])
            for row in self.db.execute(
                "SELECT id FROM entities WHERE kind='job' "
                "AND (? OR NOT json_extract(body,'$.deleted')) ORDER BY created_at,id",
                (include_deleted,),
            ).fetchall()
        ]

    def admission_details(self, run_id: str) -> dict[str, object]:
        self.run(run_id)
        # Run transitions keep the original session sequence. It indexes the admission event.
        position = self.db.execute(
            "SELECT session_id,sequence FROM entities WHERE kind='run' AND id=?", (run_id,)
        ).fetchone()
        row = self.db.execute(
            "SELECT reference FROM outbox WHERE delivered=1 "
            "AND json_extract(body,'$.event.session_id')=? "
            "AND json_extract(body,'$.event.sequence')=? "
            "AND json_extract(body,'$.event.run_id')=? "
            "AND json_extract(body,'$.event.type')='run.admitted'",
            (*position, run_id),
        ).fetchone()
        if row is None:
            raise StorageError("durable admission unavailable")
        return self.log.read(Reference.model_validate_json(row[0])).details

    def admission_profile(self, run_id: str) -> dict[str, object]:
        profile = self.admission_details(run_id).get("admission")
        if not isinstance(profile, dict) or not profile:
            raise ValueError("tool dispatch requires a durable effective profile at admission")
        return profile

    def admitted_profile(self, run_id: str) -> EffectiveProfile:
        from solakit.config import EffectiveProfile

        self.require_local_run(run_id)
        return EffectiveProfile.model_validate_json(canonical(self.admission_profile(run_id)))

    def tool_attempts(self, run_id: str) -> int:
        self.run(run_id)
        return int(
            self.db.execute(
                "SELECT count(*) FROM entities WHERE kind='tool' AND run_id=?", (run_id,)
            ).fetchone()[0]
        )

    def model_index(self, run_id: str) -> int:
        return int(
            self.db.execute(
                "SELECT coalesce(max(json_extract(body,'$.index')),0) FROM entities "
                "WHERE kind='request' AND run_id=?",
                (run_id,),
            ).fetchone()[0]
        )

    def delegation(self, call_id: str) -> Delegation | None:
        try:
            return self._load("delegation", call_id, Delegation)
        except KeyError:
            return None

    def child_attempts(self, run_id: str, kind: str) -> int:
        return int(
            self.db.execute(
                "SELECT count(*) FROM entities AS link JOIN entities AS attempt "
                "ON attempt.run_id=json_extract(link.body,'$.child_run_id') "
                "WHERE link.kind='delegation' AND link.run_id=? AND attempt.kind=?",
                (run_id, kind),
            ).fetchone()[0]
        )

    def delegation_result(
        self, link: Delegation, *, include_report: bool = True
    ) -> dict[str, JsonValue]:
        child = self.run(link.child_run_id)
        row = self.db.execute(
            "SELECT kind,id FROM entities WHERE run_id=? AND kind<>'run' "
            "ORDER BY sequence DESC LIMIT 1",
            (child.id,),
        ).fetchone()
        evidence: list[JsonValue] = [child.id]
        if row:
            evidence.append(row["id"])
        result: dict[str, JsonValue] = {
            "session_id": child.session_id,
            "run_id": child.id,
            "status": child.state.value,
            "evidence_ids": evidence,
        }
        if (
            include_report
            and child.state == RunState.COMPLETED
            and row
            and row["kind"] == "message"
        ):
            report = self._load("message", row["id"], Message)
            if report.role == "assistant":
                result["report"] = report.content[:4000]
        return result

    def queued_runs(self) -> list[Run]:
        """The admitted outbox ordinal is the durable order, across sessions."""
        return [
            self.run(row[0])
            for row in self.db.execute(
                "SELECT entity.id FROM entities AS entity JOIN outbox "
                "ON outbox.id=entity.entry_id WHERE entity.kind='run' "
                "AND json_extract(entity.body,'$.state')='queued' ORDER BY outbox.ordinal"
            ).fetchall()
        ]

    def request(self, request_id: str) -> Run | None:
        valid_id(request_id)
        row = self.db.execute(
            "SELECT run_id FROM admissions WHERE request_id=?", (request_id,)
        ).fetchone()
        return self.run(row[0]) if row else None

    def create_session(
        self,
        *,
        mode: SessionMode = SessionMode.SHARED,
        title: str = "",
        origin: Origin = Origin.INTERACTIVE,
    ) -> Session:
        if origin == Origin.DELEGATED:
            raise ValueError("delegated sessions require a parent tool admission")
        session = Session(origin=origin, mode=mode, title=title)

        def prepare() -> None:
            self.db.execute("INSERT INTO sessions VALUES (?,0)", (session.id,))

        self._commit((session,), prepare=prepare)
        return session

    def update_session(
        self,
        identity: str,
        *,
        title: str | None = None,
        archived: bool | None = None,
        pinned: bool | None = None,
    ) -> Session:
        old = self.session(identity)
        changes = {
            key: value
            for key, value in {"title": title, "archived": archived, "pinned": pinned}.items()
            if value is not None
        }
        session = Session.model_validate(old.model_dump() | changes)
        if session != old:
            self._commit((session,))
        return session

    def archive_sessions(self, identities: list[str]) -> int:
        if not 1 <= len(identities) <= 100:
            raise ValueError("archive requires between 1 and 100 session IDs")
        sessions = [self.session(identity) for identity in dict.fromkeys(identities)]
        changed = tuple(
            session.model_copy(update={"archived": True})
            for session in sessions
            if not session.archived
        )
        if changed:
            self._commit(changed)
        return len(changed)

    def list_sessions(
        self,
        *,
        after: tuple[str, str] | None = None,
        limit: int = 50,
        archived: bool = False,
        include_ephemeral: bool = False,
        q: str = "",
        pinned: bool | None = None,
    ) -> list[Session]:
        page_limit(limit)
        if after is not None:
            valid_id(after[1])
            after = (TIME_ADAPTER.validate_json(canonical(after[0])).isoformat(), after[1])
        # Metadata lives in state.db; JSON fields are indexed by the page's stable creation key.
        rows = self.db.execute(
            "SELECT id FROM entities WHERE kind='session' AND (created_at,id)>(?,?) "
            "AND json_extract(body,'$.archived')=? "
            "AND (? OR json_extract(body,'$.mode')!='ephemeral') "
            "AND (? IS NULL OR json_extract(body,'$.pinned')=?) "
            "AND instr(casefold(json_extract(body,'$.title')),?)>0 ORDER BY created_at,id LIMIT ?",
            (
                *(after or ("", "")),
                archived,
                include_ephemeral,
                pinned,
                pinned,
                q.strip().casefold(),
                limit,
            ),
        ).fetchall()
        return [self.session(row[0]) for row in rows]

    def session_catalog(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
        archived: bool | None,
        q: str,
        profile: str | None,
        mode: SessionMode | None,
        created_from: datetime | None,
        created_before: datetime | None,
        activity_from: datetime | None,
        activity_before: datetime | None,
    ) -> tuple[list[tuple[Session, datetime]], bool]:
        page_limit(limit)
        conditions = [
            "s.kind='session'",
            "instr(casefold(json_extract(s.body,'$.title')),:title)>0",
            "(:archived IS NULL OR json_extract(s.body,'$.archived')=:archived)",
            "(:mode IS NULL OR json_extract(s.body,'$.mode')=:mode)",
        ]
        params: dict[str, str | int | None] = {
            "title": q.strip().casefold(),
            "archived": archived,
            "mode": mode,
            "limit": limit + 1,
        }
        if after is not None:
            conditions.append("(s.created_at,s.id)<(:after_at,:after_id)")
            params["after_at"], params["after_id"] = after
        if profile is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM entities r WHERE r.kind='run' AND r.session_id=s.id "
                "AND json_extract(r.body,'$.profile')=:profile)"
            )
            params["profile"] = profile
        for name, condition, value in (
            ("created_from", "s.created_at>=:created_from", created_from),
            ("created_before", "s.created_at<:created_before", created_before),
            ("activity_from", "last_activity_at>=:activity_from", activity_from),
            ("activity_before", "last_activity_at<:activity_before", activity_before),
        ):
            if value is not None:
                conditions.append(condition)
                params[name] = value.isoformat()
        rows = self.db.execute(
            "SELECT s.*, COALESCE((SELECT MAX(r.created_at) FROM entities r "
            "WHERE r.kind='run' AND r.session_id=s.id), s.created_at) AS last_activity_at "
            f"FROM entities s WHERE {' AND '.join(conditions)} "
            "ORDER BY s.created_at DESC,s.id DESC LIMIT :limit",
            params,
        ).fetchall()
        return [
            (self._read_row(row, Session), datetime.fromisoformat(row["last_activity_at"]))
            for row in rows[:limit]
        ], len(rows) > limit

    def session_catalog_profiles(self) -> set[str]:
        return {
            row[0]
            for row in self.db.execute(
                "SELECT DISTINCT json_extract(body,'$.profile') FROM entities WHERE kind='run'"
            )
            if row[0]
        }

    def admit(
        self,
        session_id: str | None,
        *,
        request_id: str,
        content: str,
        profile: str,
        admission: dict[str, object] | None = None,
        origin: Origin = Origin.INTERACTIVE,
        job_id: str | None = None,
        occurrence_id: str | None = None,
        retry_of: str | None = None,
        claim: tuple[Job, Occurrence, Job] | None = None,
        image: Artifact | None = None,
        image_source: tuple[str, str] | None = None,
        upload_id: str | None = None,
        upload: bytes | None = None,
        parent_tool_call_id: str | None = None,
    ) -> Run:
        from solakit.engine.lineage import current_run, deny_autonomous_successor

        deny_autonomous_successor()
        valid_id(request_id)
        parent_call = None
        if parent_tool_call_id is not None:
            parent_call = self._load("tool", parent_tool_call_id, ToolCall)
            parent = self.run(parent_call.run_id)
            caller = current_run.get()
            if (
                session_id is not None
                or origin != Origin.DELEGATED
                or parent_call.name != "delegate.run"
                or parent.origin != Origin.INTERACTIVE
                or parent.parent_tool_call_id is not None
                or parent.state != RunState.RUNNING
                or caller is None
                or caller.id != parent.id
                or self.session(parent.session_id).parent_session_id is not None
                or request_id != parent_call.id
            ):
                raise ValueError("delegation requires its running interactive parent call")
            session = Session(
                id=digest(canonical(["delegate-topic", request_id]))[:32],
                origin=Origin.DELEGATED,
                mode=SessionMode.ISOLATED,
                title="Delegated research",
                parent_session_id=parent.session_id,
            )
            session_id = session.id
        else:
            if session_id is None or origin == Origin.DELEGATED:
                raise ValueError("admission requires a session")
            session = self.session(session_id)
            if session.parent_session_id is not None or session.origin == Origin.DELEGATED:
                raise ValueError("delegated topics require delegation")
        details: dict[str, object] = {
            "session_id": session_id,
            "content": content,
            "profile": profile,
            "admission": admission or {},
            "origin": origin,
            "job_id": job_id,
            "occurrence_id": occurrence_id,
            "retry_of": retry_of,
        }
        if parent_call is not None:
            details["parent_tool_call_id"] = parent_call.id
        existing = self.db.execute(
            "SELECT * FROM admissions WHERE request_id=?", (request_id,)
        ).fetchone()
        if upload_id is not None:
            details["upload_id"] = upload_id
        if image is not None:
            details["image"] = image.model_dump(mode="json")
        if image_source is not None and (
            existing is None or "image_source" in self.admission_details(existing["run_id"])
        ):
            details["image_source"] = image_source
        fingerprint = digest(canonical(details))
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise ValueError("idempotency key reused with different admission")
            return self.run(existing["run_id"])
        if origin in (Origin.SCHEDULED, Origin.PROACTIVE):
            if claim is None:
                raise ValueError("scheduled admission requires an occurrence claim")
            job, occurrence, advanced = claim
            if (
                (job_id, occurrence_id, origin, profile)
                != (job.id, occurrence.id, job.origin, job.profile)
                or occurrence.job_id != job.id
                or occurrence.generation != job.generation
                or occurrence.definition != job.model_dump(mode="json")
                or (advanced.id, advanced.version, advanced.generation)
                != (job.id, job.version, job.generation)
                or self.job(job.id) != job
                or job.deleted
                or (job.session_id is not None and job.session_id != session_id)
                or job.mode != session.mode
                or (occurrence.trigger == "scheduled" and (not job.enabled or job.consumed))
                or self.db.execute(
                    "SELECT 1 FROM entities WHERE kind='occurrence' AND id=?", (occurrence.id,)
                ).fetchone()
            ):
                raise ValueError("invalid or already claimed occurrence")
        elif claim is not None:
            raise ValueError("only scheduled admission may claim occurrences")
        if session.archived:
            raise ValueError("archived sessions cannot admit work")
        if (
            session.mode == SessionMode.EPHEMERAL
            and retry_of is None
            and self.db.execute(
                "SELECT 1 FROM entities WHERE kind='run' AND session_id=?", (session_id,)
            ).fetchone()
        ):
            raise ValueError("ephemeral sessions admit one run")
        if retry_of is not None and self.run(retry_of).session_id != session_id:
            raise ValueError("retry must belong to the same session")
        run = Run(
            session_id=session_id,
            request_id=request_id,
            profile=profile,
            origin=origin,
            job_id=job_id,
            occurrence_id=occurrence_id,
            retry_of=retry_of,
            parent_tool_call_id=parent_tool_call_id,
        )
        if upload_id is not None:
            if upload is None or image is not None:
                raise ValueError("Admission requires one staged image")
            image = Artifact.model_validate(
                self.artifact(upload_id, upload) | {"media_type": "image/png"}
            )
        message = Message(
            origin=origin,
            session_id=session_id,
            run_id=run.id,
            role="user",
            content=content,
            image=image,
            image_route="vision" if image and admission and admission.get("vision") else None,
        )

        def prepare() -> None:
            if parent_call is not None:
                self.db.execute("INSERT INTO sessions VALUES (?,0)", (session.id,))
            self.db.execute(
                "INSERT INTO admissions VALUES (?,?,?)", (request_id, fingerprint, run.id)
            )

        claimed: tuple[StoredRecord, ...] = ()
        if claim is not None:
            job, occurrence, advanced = claim
            occurrence = Occurrence.model_validate(
                occurrence.model_dump() | {"run_id": run.id, "state": "claimed"}
            )
            claimed = (occurrence, advanced)
        if parent_call is not None:
            claimed = (
                session,
                Delegation(
                    id=parent_call.id,
                    origin=parent_call.origin,
                    session_id=parent_call.session_id,
                    run_id=parent_call.run_id,
                    child_session_id=session.id,
                    child_run_id=run.id,
                ),
            )
        # A retry answers the original message, so it records none of its own.
        records = (run,) if retry_of is not None else (run, message)
        self._commit((*claimed, *records), details=details, prepare=prepare)
        return run

    def job_topic_archive(self, run: Run) -> tuple[Session, ...]:
        if run.occurrence_id is not None:
            topic = self.session(run.session_id)
            if topic.mode == SessionMode.EPHEMERAL and not topic.archived:
                return (topic.model_copy(update={"archived": True}),)
        return ()

    def transition_run(
        self, identity: str, state: RunState, *, reason: Failure | None = None
    ) -> Run:
        self.require_local_run(identity)
        run = self.run(identity).transition(state, at=utc_now(), reason=reason)
        records: tuple[StoredRecord, ...] = (run,)
        if run.occurrence_id is not None and state not in (RunState.QUEUED, RunState.RUNNING):
            occurrence = self.occurrence(run.occurrence_id)
            occurrence = Occurrence.model_validate(
                occurrence.model_dump()
                | {
                    "state": "completed"
                    if state == RunState.COMPLETED
                    else ("uncertain" if state == RunState.INTERRUPTED else "failed"),
                    "decision": occurrence.decision if state == RunState.COMPLETED else "failed",
                    "reason": reason,
                }
            )
            records += (occurrence, *self.job_topic_archive(run))
        self._commit(records)
        return run

    def append(
        self,
        record: Message
        | ModelStep
        | ModelRequest
        | ToolCall
        | ToolReceipt
        | Delivery
        | MemoryNote
        | MemoryRecall
        | MemoryCommit
        | CompactionBoundary
        | RecoveryNote,
    ) -> None:
        kind = next(name for name, cls in RECORDS.items() if isinstance(record, cls))
        self.require_local_run(record.run_id)
        run = self.run(record.run_id)
        if run.session_id != record.session_id or run.origin != record.origin:
            raise ValueError("record lineage differs from its run")
        if self.db.execute(
            "SELECT 1 FROM entities WHERE kind=? AND id=?", (kind, record.id)
        ).fetchone():
            raise ValueError("record already exists; use lifecycle transition methods")
        if isinstance(record, ToolCall):
            parent = self._load("model", record.model_step_id, ModelStep)
            self._same_run(parent, record)
            if self.db.execute(
                "SELECT 1 FROM entities WHERE kind='tool' AND run_id=? "
                "AND json_extract(body,'$.model_step_id')=? "
                "AND json_extract(body,'$.provider_tool_call_id')=?",
                (record.run_id, record.model_step_id, record.provider_tool_call_id),
            ).fetchone():
                raise ValueError("provider tool call already dispatched")
        if isinstance(record, RecoveryNote):
            parent = self._load("model", record.model_step_id, ModelStep)
            if parent.run_id != record.run_id:
                raise ValueError("recovery crosses runs")
        if isinstance(record, (ToolReceipt, Message)) and record.tool_call_id is not None:
            tool = self._load("tool", record.tool_call_id, ToolCall)
            self._same_run(tool, record)
            if (
                isinstance(record, ToolReceipt)
                and self.db.execute(
                    "SELECT 1 FROM entities WHERE kind='receipt' "
                    "AND json_extract(body,'$.tool_call_id')=?",
                    (record.tool_call_id,),
                ).fetchone()
            ):
                raise ValueError("tool already has a receipt")
        if (
            isinstance(record, (ModelStep, ModelRequest))
            and self.db.execute(
                "SELECT 1 FROM entities WHERE kind=? AND run_id=? "
                "AND json_extract(body,'$.index')=?",
                (kind, run.id, record.index),
            ).fetchone()
        ):
            raise ValueError("model attempt index already recorded")
        if isinstance(record, ModelStep) and record.request_id is not None:
            request = self._load("request", record.request_id, ModelRequest)
            if (request.run_id, request.session_id, request.index, request.model) != (
                record.run_id,
                record.session_id,
                record.index,
                record.model,
            ):
                raise ValueError("model response differs from its request")
            if (record.purpose == "vision") != (request.purpose == "vision"):
                raise ValueError("vision response differs from its request")
        if isinstance(record, Delivery):
            if record.content is None:
                self._same_run(self._load("message", record.message_id, Message), record)
            if record.state != DeliveryState.PENDING:
                raise ValueError("delivery must start pending")
        if isinstance(record, MemoryNote):
            tool = self._load("tool", record.tool_call_id, ToolCall)
            if tool.run_id != record.run_id or run.state != RunState.RUNNING:
                raise ValueError("staged note requires its running tool")
            if self.db.execute(
                "SELECT 1 FROM entities WHERE kind='memory_note' "
                "AND json_extract(body,'$.tool_call_id')=?",
                (record.tool_call_id,),
            ).fetchone():
                raise ValueError("tool already staged a note")
        if isinstance(record, MemoryCommit):
            if record.state != MemoryState.PENDING:
                raise ValueError("memory work must start pending")
            if self.db.execute(
                "SELECT 1 FROM entities WHERE kind='memory' AND run_id=?",
                (record.run_id,),
            ).fetchone():
                raise ValueError("run already has a memory commit")
            if record.payload and record.payload_sha256 != digest(canonical(record.payload)):
                raise ValueError("memory payload hash mismatch")
        if isinstance(record, CompactionBoundary):
            if record.manifest is not None:
                from solakit.engine.compaction import active_boundary, verify

                manifest, _ = verify(self, record)
                active = active_boundary(self, record.session_id)
                if record.state != "prepared" or manifest.prior_boundary_id != (
                    active.id if active else None
                ):
                    raise ValueError("compaction must prepare against the active boundary")
                self._commit((record,))
                return
            if record.state != "committed":
                raise ValueError("prepared compaction requires a manifest")
            for identity in (
                *record.source_message_ids,
                record.through_message_id,
                record.summary_message_id,
            ):
                message = self._load("message", identity, Message)
                if message.session_id != record.session_id:
                    raise ValueError("compaction crosses sessions")
            positions = [
                self.db.execute(
                    "SELECT sequence FROM entities WHERE kind='message' AND id=?", (identity,)
                ).fetchone()[0]
                for identity in record.source_message_ids
            ]
            if (
                positions != sorted(set(positions))
                or record.through_message_id != record.source_message_ids[-1]
            ):
                raise ValueError("compaction sources must be ordered through the boundary")
            summary = self._load("message", record.summary_message_id, Message)
            self._same_run(summary, record)
            summary_position = self.db.execute(
                "SELECT sequence FROM entities WHERE kind='message' AND id=?", (summary.id,)
            ).fetchone()[0]
            if summary.role != "assistant" or summary_position <= positions[-1]:
                raise ValueError("compaction summary must follow its original sources")
        self._commit((record,))

    @staticmethod
    def _same_run(
        parent: ModelStep | ToolCall | Message,
        record: ToolCall | ToolReceipt | Message | Delivery | CompactionBoundary,
    ) -> None:
        if parent.run_id != record.run_id or parent.session_id != record.session_id:
            raise ValueError("reference crosses run/session")

    def transition_delivery(
        self, identity: str, state: DeliveryState, *, reason: Failure | None = None
    ) -> Delivery:
        record = self._load("delivery", identity, Delivery)
        self.require_local_run(record.run_id)
        record = record.transition(state, reason=reason)
        self._commit((record,))
        return record

    def transition_memory(
        self, identity: str, state: MemoryState, *, reason: Failure | None = None, **changes: object
    ) -> MemoryCommit:
        record = self._load("memory", identity, MemoryCommit)
        self.require_local_run(record.run_id)
        record = record.transition(state, reason=reason)
        record = MemoryCommit.model_validate(record.model_dump() | changes)
        self._commit((record,))
        return record

    def messages(
        self, session_id: str, *, after: int = 0, limit: int = 50, before: int | None = None
    ) -> list[tuple[int, Message]]:
        return cast(
            list[tuple[int, Message]], self._page("message", session_id, after, limit, before)
        )

    def runs(
        self, session_id: str, *, after: int = 0, limit: int = 50, before: int | None = None
    ) -> list[tuple[int, Run]]:
        return cast(list[tuple[int, Run]], self._page("run", session_id, after, limit, before))

    def _page(
        self, kind: str, session_id: str, after: int, limit: int, before: int | None = None
    ) -> list[tuple[int, Record]]:
        self.session(session_id)
        page_limit(limit)
        if type(after) is not int or after < 0:
            raise ValueError("cursor must be a nonnegative sequence")
        if before is not None and (type(before) is not int or before < 0 or after):
            raise ValueError("use either after or a nonnegative before cursor")
        direction = "DESC" if before is not None else "ASC"
        rows = self.db.execute(
            "SELECT * FROM entities WHERE kind=? AND session_id=? AND sequence>? "
            "AND (? IS NULL OR ?=0 OR sequence<?) "
            "AND (kind!='message' OR json_extract(body,'$.origin') "
            "NOT IN ('scheduled','proactive') "
            "OR (json_extract(body,'$.role')='assistant' AND EXISTS ("
            "SELECT 1 FROM entities AS delivery WHERE delivery.kind='delivery' "
            "AND json_extract(delivery.body,'$.message_id')=entities.id "
            "AND json_extract(delivery.body,'$.state')='delivered'))) "
            f"ORDER BY sequence {direction},id LIMIT ?",
            (kind, session_id, after, before, before, before, limit),
        )
        return [(row["sequence"], self._read_row(row, RECORDS[kind])) for row in rows]

    def run_records(
        self, run_id: str, *, after: int = 0, limit: int = 50, before: int | None = None
    ) -> list[tuple[int, Record]]:
        self.run(run_id)
        page_limit(limit)
        if type(after) is not int or after < 0:
            raise ValueError("cursor must be a nonnegative sequence")
        if before is not None and (type(before) is not int or before < 0 or after):
            raise ValueError("use either after or a nonnegative before cursor")
        direction = "DESC" if before is not None else "ASC"
        rows = self.db.execute(
            "SELECT * FROM entities WHERE run_id=? AND sequence>? "
            "AND (? IS NULL OR ?=0 OR sequence<?) "
            f"ORDER BY sequence {direction},id LIMIT ?",
            (run_id, after, before, before, before, limit),
        )
        return [(row["sequence"], self._read_row(row, RECORDS[row["kind"]])) for row in rows]

    def events(self, session_id: str, *, after: int = 0, limit: int = 50) -> list[DurableEvent]:
        self.session(session_id)
        page_limit(limit)
        if type(after) is not int or after < 0:
            raise ValueError("cursor must be a nonnegative sequence")
        rows = self.db.execute(
            "SELECT reference FROM outbox WHERE delivered=1 "
            "AND json_extract(body,'$.event.session_id')=? "
            "AND json_extract(body,'$.event.sequence')>? "
            "ORDER BY json_extract(body,'$.event.sequence') LIMIT ?",
            (session_id, after, limit),
        ).fetchall()
        events = [self.log.read(Reference.model_validate_json(row[0])).event for row in rows]
        return [event for event in events if event is not None]

    def snapshot(self, session_id: str) -> tuple[Session, int]:
        with self.db:
            self.db.execute("BEGIN")
            session = self.session(session_id)
            sequence = self.db.execute(
                "SELECT sequence FROM sessions WHERE id=?", (session_id,)
            ).fetchone()[0]
        return session, sequence

    def artifact(self, identity: str, data: bytes) -> dict[str, object]:
        valid_id(identity)
        _ = self.db
        path = self.root / "artifacts" / identity
        if path.is_symlink():
            raise CorruptionError("artifact cannot be a symlink")
        if path.exists():
            if path.read_bytes() != data:
                raise CorruptionError("artifact identity reused with different bytes")
        else:
            publish(path, data)
        return {"path": f"artifacts/{identity}", "sha256": digest(data), "length": len(data)}

    def _recover_work(self) -> None:
        reason = Failure(
            code=ErrorCode.PROCESS_INTERRUPTED,
            message="Process stopped before outcome was recorded",
        )
        for row in self.db.execute(
            f"SELECT kind,id FROM entities WHERE {LOCAL_RUN} AND ("
            "(kind='run' AND json_extract(body,'$.state')='running') OR "
            "(kind='run' AND json_extract(body,'$.state')='queued' "
            "AND json_extract(body,'$.parent_tool_call_id') IS NOT NULL) OR "
            "(kind='delivery' AND json_extract(body,'$.state')='sending') OR "
            "(kind='memory' AND json_extract(body,'$.state')='running'))"
        ).fetchall():
            if row["kind"] == "run":
                self.transition_run(row["id"], RunState.INTERRUPTED, reason=reason)
            elif row["kind"] == "delivery":
                record = self._load("delivery", row["id"], Delivery)
                if record.state == DeliveryState.SENDING:
                    self.transition_delivery(record.id, DeliveryState.UNCERTAIN, reason=reason)
            elif row["kind"] == "memory":
                memory = self._load("memory", row["id"], MemoryCommit)
                if memory.state == MemoryState.RUNNING:
                    self.transition_memory(memory.id, MemoryState.AMBIGUOUS, reason=reason)

        for row in self.db.execute(
            f"SELECT body FROM entities AS tool WHERE kind='tool' AND {LOCAL_RUN} AND NOT EXISTS ("
            "SELECT 1 FROM entities AS receipt WHERE receipt.kind='receipt' "
            "AND json_extract(receipt.body,'$.tool_call_id')=tool.id)"
        ).fetchall():
            call = ToolCall.model_validate_json(row[0])
            if call.name == "delegate.run":
                link = self.delegation(call.id)
                result = self.delegation_result(link, include_report=False) if link else None
                raw = canonical(result) if result else b""
                self.append(
                    ToolReceipt(
                        origin=call.origin,
                        session_id=call.session_id,
                        run_id=call.run_id,
                        tool_call_id=call.id,
                        status="failed",
                        result=result,
                        result_bytes=len(raw),
                        result_sha256=digest(raw) if raw else None,
                        failure=reason,
                    )
                )
                continue
            self.append(
                ToolReceipt(
                    origin=call.origin,
                    session_id=call.session_id,
                    run_id=call.run_id,
                    tool_call_id=call.id,
                    status="uncertain",
                    effect="uncertain",
                    failure=Failure(
                        code=ErrorCode.EFFECT_UNCERTAIN,
                        message="Process stopped before a tool receipt; inspect before retry",
                    ),
                )
            )
