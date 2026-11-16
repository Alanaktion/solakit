"""Idempotent rehearsal import; preserve copied raw JSONL and unknown fields."""

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from solakit.evidence import Entry, checked_json, digest, publish
from solakit.store import Store


def import_fixture(store: Store, path: Path) -> int:
    data = path.read_bytes()
    fingerprint = digest(data)
    source = f"{path.resolve()}:{fingerprint}"
    if store.db.execute("SELECT 1 FROM imports WHERE source=?", (source,)).fetchone():
        return 0
    raw_path = store.root / "imports" / fingerprint
    if not raw_path.exists():
        publish(raw_path, data)
    elif raw_path.read_bytes() != data:
        raise ValueError("raw import hash conflict")
    entries = []
    for line, raw in enumerate(data.splitlines(), 1):
        record = checked_json(raw)
        if not isinstance(record, dict):
            raise ValueError("legacy fixture lines must be JSON objects")
        identity = uuid5(NAMESPACE_URL, f"sola-legacy:{source}:{line}").hex
        entries.append(
            Entry(
                id=identity,
                kind="legacy",
                record=record,
                details={
                    "source_name": path.name,
                    "source_path": str(path.resolve()),
                    "sha256": fingerprint,
                    "line": line,
                },
            )
        )
    store._flush()
    try:
        with store.db:
            store.db.execute("INSERT INTO imports VALUES (?,?)", (source, fingerprint))
            for entry in entries:
                store.db.execute(
                    "INSERT INTO outbox(id,body) VALUES (?,?)", (entry.id, entry.encoded())
                )
        store.fault("state_commit")
        store._flush()
        store._index()
        store.fault("acknowledge")
    except BaseException:
        store.failed = True
        raise
    return len(entries)
