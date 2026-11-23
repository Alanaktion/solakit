"""Idempotent rehearsal import; preserve copied raw JSONL and unknown fields."""

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from solakit.evidence import (
    Entry,
    canonical,
    checked_json,
    digest,
    ensure_directory,
    publish,
    sync_directory,
)
from solakit.store import Store


def retain_tree(store: Store, source: Path) -> dict[str, object]:
    """Retain a copied legacy tree without loading code or activating configuration."""
    from solakit.backup import _copy, file_metadata

    if source.is_symlink() or not source.is_dir():
        raise ValueError("retention source must be a copied directory, without symlinks")
    source = source.resolve()
    root = store.root.resolve()
    if source.is_relative_to(root) or root.is_relative_to(source):
        raise ValueError("retention source and runtime data directories must be disjoint")
    sources = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("retention sources must contain only directories and regular files")
        if path.is_file():
            sources.append(path)
    metadata = {str(path.relative_to(source)): file_metadata(path) for path in sources}
    manifest = canonical({"source_root": str(source), "files": metadata, "activation": "none"})
    destination = store.root / "imports" / ("retained-" + digest(manifest))
    ensure_directory(destination)
    for path in sources:
        relative = str(path.relative_to(source))
        target = destination / "files" / relative
        if target.exists():
            actual = file_metadata(target)
        else:
            actual = _copy(path, target)
        if actual != metadata[relative] or target.is_symlink():
            raise ValueError("retention copy changed or conflicts; inspect before continuing")
    marker = destination / "manifest.json"
    for directory in sorted(destination.rglob("*"), reverse=True):
        if directory.is_dir():
            sync_directory(directory)
    if not marker.exists():
        publish(marker, manifest)
    elif marker.read_bytes() != manifest:
        raise ValueError("retention manifest conflict")
    return {
        "files": len(sources),
        "manifest": str(marker.relative_to(store.root)),
        "sha256": digest(manifest),
        "activation": "none",
    }


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
