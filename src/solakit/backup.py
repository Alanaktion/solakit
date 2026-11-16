"""Coherent offline checkpoints and verified restore into a new data directory."""

import os
import shutil
import sqlite3
from pathlib import Path

from solakit.evidence import (
    CorruptionError,
    canonical,
    checked_json,
    digest,
    publish,
    sync_directory,
)
from solakit.store import Store


def _copy(source: Path, target: Path) -> dict[str, object]:
    if source.is_symlink() or not source.is_file():
        raise CorruptionError("backup source must be a regular file")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with source.open("rb") as reader, target.open("xb") as writer:
        os.chmod(target, 0o600)
        shutil.copyfileobj(reader, writer)
        writer.flush()
        os.fsync(writer.fileno())
    return {"length": target.stat().st_size, "sha256": digest(target.read_bytes())}


def backup(store: Store, destination: Path) -> None:
    store._flush()
    if destination.resolve().is_relative_to(store.root.resolve()):
        raise ValueError("backup destination must be outside the data directory")
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    state = destination / "state.db"
    state.touch(mode=0o600)
    db = sqlite3.connect(state)
    try:
        store.db.backup(db)
        db.execute("PRAGMA journal_mode=DELETE")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise CorruptionError("backup state integrity check failed")
    finally:
        db.close()
    with state.open("rb") as stream:
        os.fsync(stream.fileno())
    files: dict[str, object] = {
        "state.db": {"length": state.stat().st_size, "sha256": digest(state.read_bytes())},
    }
    for name in ("evidence", "recovery", "artifacts", "imports"):
        directory = store.root / name
        if directory.is_symlink():
            raise CorruptionError("symlink in backup source")
        for source in sorted(directory.rglob("*")):
            if source.is_symlink():
                raise CorruptionError("symlink in backup source")
            if source.is_file():
                relative = source.relative_to(store.root)
                metadata = _copy(source, destination / relative)
                if name == "evidence" and metadata != {
                    "length": store.log.sizes.get(source.name),
                    "sha256": store.log.hashers[source.name].hexdigest(),
                }:
                    raise CorruptionError("evidence changed outside the writer")
                files[str(relative)] = metadata
    for path in sorted(destination.rglob("*"), reverse=True):
        if path.is_dir():
            sync_directory(path)
    manifest = {
        "schema_version": 1,
        "files": files,
        "evidence_high_water": store.log.sizes,
        "session_high_water": {
            row[0]: row[1] for row in store.db.execute("SELECT * FROM sessions")
        },
    }
    publish(destination / "manifest.json", canonical(manifest))
    sync_directory(destination.parent)


def restore(source: Path, destination: Path) -> None:
    if destination.exists():
        raise ValueError("restore requires a new destination directory")
    source = source.resolve()
    try:
        manifest = checked_json((source / "manifest.json").read_bytes())
        if (
            not isinstance(manifest, dict)
            or type(manifest.get("schema_version")) is not int
            or manifest.get("schema_version") != 1
        ):
            raise ValueError("unsupported backup manifest")
        files = manifest["files"]
        if not isinstance(files, dict) or "state.db" not in files:
            raise ValueError("backup state missing")
        for relative, metadata in files.items():
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or str(path) != relative:
                raise ValueError("unsafe backup path")
            if relative != "state.db" and (
                len(path.parts) < 2
                or path.parts[0] not in ("evidence", "recovery", "artifacts", "imports")
            ):
                raise ValueError("unexpected backup path")
            target = source / path
            if any(part.is_symlink() for part in (target, *target.parents)):
                raise ValueError("symlink in backup")
            data = target.read_bytes()
            if metadata != {"length": len(data), "sha256": digest(data)}:
                raise ValueError("backup checksum mismatch")
        water = manifest["evidence_high_water"]
        if not isinstance(water, dict) or water != {
            Path(name).name: metadata["length"]
            for name, metadata in files.items()
            if name.startswith("evidence/")
        }:
            raise ValueError("backup evidence high-water mismatch")
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise CorruptionError("backup validation failed") from error
    temporary = destination.with_name(f".{destination.name}.restore")
    temporary.mkdir(mode=0o700, exist_ok=False)
    # Failed restore staging remains inspectable, never overwriting a destination.
    for relative, metadata in files.items():
        if _copy(source / relative, temporary / relative) != metadata:
            raise CorruptionError("backup changed during restore")
    for name in ("evidence", "recovery", "artifacts", "imports"):
        (temporary / name).mkdir(mode=0o700, exist_ok=True)
    check = sqlite3.connect(f"{(temporary / 'state.db').as_uri()}?mode=ro", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise CorruptionError("restored state integrity check failed")
        if check.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise CorruptionError("unsupported backup state schema")
        # Reuse reconciliation read-only, without running startup recovery or indexing.
        check.row_factory = sqlite3.Row
        verifier = Store(temporary)
        verifier._connection = check
        verifier.log.references = {ref.entry_id: ref for ref, _ in verifier.log.scan()}
        verifier._reconcile()
        watermarks = {row[0]: row[1] for row in check.execute("SELECT * FROM sessions")}
        if watermarks != manifest["session_high_water"]:
            raise CorruptionError("restored state high-water mismatch")
    finally:
        check.close()
    for path in sorted(temporary.rglob("*"), reverse=True):
        if path.is_dir():
            sync_directory(path)
    sync_directory(temporary)
    # Cooperating operators serialize restore; the destination must remain absent.
    if destination.exists():
        raise ValueError("restore destination appeared")
    temporary.rename(destination)
    sync_directory(destination.parent)
