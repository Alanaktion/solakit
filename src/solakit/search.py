"""Disposable FTS5 projection, usable without state, server, model, or memory."""

import hashlib
import os
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from solakit.evidence import (
    CorruptionError,
    Entry,
    EvidenceLog,
    Reference,
    StorageError,
    canonical,
    digest,
    parse_entry,
    sync_directory,
)
from solakit.records import new_id

SCHEMA = """
CREATE TABLE sources(segment TEXT PRIMARY KEY, size INTEGER NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE refs(entry_id TEXT PRIMARY KEY, reference TEXT NOT NULL);
CREATE VIRTUAL TABLE documents USING fts5(entry_id UNINDEXED, record_id UNINDEXED,
 session_id UNINDEXED, run_id UNINDEXED, kind UNINDEXED, text, tokenize='unicode61');
PRAGMA user_version=1;
"""


class SearchUnavailable(StorageError):
    pass


def _open(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly and not path.is_file():
        raise SearchUnavailable("search unavailable; rebuild the evidence index")
    if not readonly:
        path.touch(mode=0o600, exist_ok=True)
    db = sqlite3.connect(path.as_uri() + "?mode=ro" if readonly else str(path), uri=readonly)
    db.row_factory = sqlite3.Row
    return db


def _check(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA user_version").fetchone()[0] != 1:
        raise SearchUnavailable("unsupported search projection; rebuild required")


def _document(entry: Entry) -> tuple[str, str, str, str, str]:
    if entry.kind == "legacy":
        return entry.id, "", "", entry.kind, canonical(entry.record).decode()
    identity = str(entry.record["id"])
    session = identity if entry.kind == "session" else str(entry.record["session_id"])
    run = identity if entry.kind == "run" else str(entry.record.get("run_id", ""))
    text = entry.record.get("content")
    if not isinstance(text, str):
        text = canonical(entry.record).decode()
    return identity, session, run, entry.kind, text


def _insert(db: sqlite3.Connection, ref: Reference, entry: Entry) -> None:
    db.execute("INSERT INTO refs VALUES (?,?)", (entry.id, ref.model_dump_json()))
    db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?)", (entry.id, *_document(entry)))


def _fingerprint(path: Path, size: int | None = None) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        remaining = size if size is not None else path.stat().st_size
        while remaining:
            data = stream.read(min(1024 * 1024, remaining))
            if not data:
                raise CorruptionError("indexed evidence shortened")
            hasher.update(data)
            remaining -= len(data)
    return hasher.hexdigest()


def update_index(log: EvidenceLog) -> None:
    """Append only new complete lines; recovery supplies validated source hashes."""
    path = log.root / "search.db"
    fresh = not path.exists()
    db = _open(path)
    try:
        if fresh:
            db.executescript(SCHEMA)
        _check(db)
        checkpoints = {row["segment"]: row for row in db.execute("SELECT * FROM sources")}
        if set(checkpoints) - set(log.sizes):
            raise CorruptionError("indexed evidence segment missing")
        with db:
            for segment, size in log.sizes.items():
                old = checkpoints.get(segment)
                offset = old["size"] if old else 0
                source = log.directory / segment
                if offset > size:
                    raise CorruptionError("indexed evidence shortened")
                if old and not log.index_checked and _fingerprint(source, offset) != old["sha256"]:
                    raise CorruptionError("indexed evidence fingerprint changed")
                if offset == size:
                    continue
                with source.open("rb") as stream:
                    stream.seek(offset)
                    while stream.tell() < size:
                        position = stream.tell()
                        data = stream.readline(size - position)
                        if not data.endswith(b"\n"):
                            # Sealed trailing bytes are fingerprinted but are not records.
                            log._tail(source, position, data, repair=False)
                            break
                        entry = parse_entry(data)
                        ref = Reference(
                            segment=segment,
                            offset=position,
                            length=len(data),
                            sha256=digest(data),
                            entry_id=entry.id,
                        )
                        _insert(db, ref, entry)
                db.execute(
                    "INSERT OR REPLACE INTO sources VALUES (?,?,?)",
                    (
                        segment,
                        size,
                        log.hashers[segment].hexdigest(),
                    ),
                )
        log.index_checked = True
    finally:
        db.close()


def rebuild(root: Path) -> int:
    """Caller holds ownership. Build independently of state.db, then replace."""
    log = EvidenceLog(root)
    temporary = root / f".search-{new_id()}.db"
    db = _open(temporary)
    count = 0
    try:
        db.executescript(SCHEMA)
        with db:
            for ref, entry in log.scan():
                _insert(db, ref, entry)
                count += 1
            for path in log.paths():
                db.execute(
                    "INSERT INTO sources VALUES (?,?,?)",
                    (
                        path.name,
                        path.stat().st_size,
                        _fingerprint(path),
                    ),
                )
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise CorruptionError("rebuilt search integrity check failed")
        db.execute("INSERT INTO documents(documents) VALUES ('integrity-check')")
        db.commit()
        db.close()
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, root / "search.db")
        sync_directory(root)
        return count
    finally:
        db.close()
        temporary.unlink(missing_ok=True)


def search(
    root: Path, query: str, *, limit: int = 20, session_id: str | None = None
) -> list[dict[str, object]]:
    if not query.strip() or len(query.encode("utf-8")) > 4096:
        raise ValueError("query must contain 1–4096 UTF-8 bytes")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("search limit must be between 1 and 100")
    if session_id is not None:
        from solakit.store import valid_id

        valid_id(session_id)
    log = EvidenceLog(root)
    try:
        db = _open(root / "search.db", readonly=True)
        try:
            _check(db)
            paths = log.paths()
            checkpoints = {row["segment"]: row for row in db.execute("SELECT * FROM sources")}
            names = {path.name for path in paths}
            if set(checkpoints) - names:
                raise CorruptionError("indexed evidence segment missing")
            if names - set(checkpoints):
                raise SearchUnavailable("search stale; rebuild required")
            for path in paths:
                log.validate_seal(path)
                checkpoint = checkpoints[path.name]
                size = path.stat().st_size
                if size > checkpoint["size"]:
                    raise SearchUnavailable("search stale; rebuild required")
                if size != checkpoint["size"] or _fingerprint(path) != checkpoint["sha256"]:
                    raise CorruptionError("search source fingerprint mismatch")
            phrase = '"' + query.replace('"', '""') + '"'
            rows = db.execute(
                "SELECT entry_id,record_id,session_id,run_id,kind,text,"
                "snippet(documents,5,'[',']','…',24) AS snippet "
                "FROM documents WHERE documents MATCH ? AND (? IS NULL OR session_id=?) "
                "ORDER BY rank,entry_id LIMIT ?",
                (phrase, session_id, session_id, limit),
            ).fetchall()
            results = []
            for row in rows:
                ref_row = db.execute(
                    "SELECT reference FROM refs WHERE entry_id=?", (row["entry_id"],)
                ).fetchone()
                if ref_row is None:
                    raise CorruptionError("search reference missing")
                ref = Reference.model_validate_json(ref_row[0])
                entry = log.read(ref)
                projected = tuple(
                    row[key] for key in ("record_id", "session_id", "run_id", "kind", "text")
                )
                if entry.id != row["entry_id"] or projected != _document(entry):
                    raise CorruptionError("search result differs from evidence")
                result = dict(row)
                del result["text"]
                results.append(result | {"evidence": ref.model_dump(mode="json")})
            return results
        finally:
            db.close()
    except (sqlite3.DatabaseError, ValidationError) as error:
        raise CorruptionError("search projection corrupt; rebuild required") from error
