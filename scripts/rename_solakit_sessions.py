#!/usr/bin/env python3
"""Rename Solakit session evidence while preserving append-only run history."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from solakit.core.schemas import Run
from solakit.core.store import Store, atomic_write


def mappings(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        old, separator, new = value.partition("=")
        if not separator or not old or not new:
            raise ValueError(f"expected OLD=NEW, got {value!r}")
        if old in result or new in result.values():
            raise ValueError(f"duplicate mapping: {value!r}")
        result[old] = new
    return result


def copy_backup(root: Path, paths: set[Path]) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = root / "backups" / f"session-rename-{stamp}"
    if backup.exists():
        raise FileExistsError(backup)
    for source in sorted(paths):
        relative = source.relative_to(root)
        destination = backup / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return backup


def referenced_metadata(root: Path, rename: dict[str, str]) -> set[Path]:
    affected = set()
    for path in (root / "sessions").glob("*.meta.json"):
        record = json.loads(path.read_bytes())
        if record.get("id") in rename or record.get("parent_session") in rename:
            affected.add(path)
    return affected


def session_files(root: Path, rename: dict[str, str]) -> set[Path]:
    return {
        path
        for old in rename
        for path in (root / "sessions").glob(f"{old}.*")
        if path.is_file()
    }


def rename_sessions(root: Path, rename: dict[str, str]) -> dict:
    store = Store(root, exclusive=True)
    try:
        existing = {record["id"] for record in store.list_records("sessions")}
        missing = sorted(set(rename) - existing)
        collisions = sorted(set(rename.values()) & existing)
        if missing:
            raise ValueError(f"missing source sessions: {', '.join(missing)}")
        if collisions:
            raise ValueError(f"target sessions already exist: {', '.join(collisions)}")

        metadata = referenced_metadata(root, rename)
        evidence = session_files(root, rename)
        run_paths: set[Path] = set()
        runs: list[Run] = []
        for record in store.list_records("runs"):
            if record["session"] not in rename:
                continue
            run = Run.model_validate(record)
            runs.append(run)
            run_paths.update(
                path
                for path in (
                    root / "runs" / f"{run.id}.events.jsonl",
                    root / "runs" / f"{run.id}.json",
                )
                if path.exists()
            )

        backup = copy_backup(root, {root / "app.db", *metadata, *evidence, *run_paths})

        # Update mutable metadata atomically, including child-session bindings.
        for path in metadata:
            record = json.loads(path.read_bytes())
            old_id = record["id"]
            if old_id in rename:
                record["id"] = rename[old_id]
            parent = record.get("parent_session")
            if parent in rename:
                record["parent_session"] = rename[parent]
            destination = (
                root / "sessions" / f"{record['id']}.meta.json"
                if old_id in rename
                else path
            )
            atomic_write(destination, json.dumps(record).encode("utf-8"))
            if destination != path:
                path.unlink()

        # Transcript/archive bytes are evidence: rename files without rewriting.
        for old, new in rename.items():
            for path in sorted((root / "sessions").glob(f"{old}.*")):
                if path.name.endswith(".meta.json"):
                    continue
                destination = path.with_name(new + path.name.removeprefix(old))
                if destination.exists():
                    raise FileExistsError(destination)
                path.replace(destination)

        # Append a corrected state to each run; old run events remain verbatim.
        for run in runs:
            run.session = rename[run.session]
            store.save_run(run)

        store.rebuild()
        return {
            "backup": str(backup),
            "renamed_sessions": len(rename),
            "updated_child_metadata": len(metadata) - len(rename),
            "updated_runs": len(runs),
            "storage_health": store.storage_health(),
        }
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("rename", nargs="+", metavar="OLD=NEW")
    args = parser.parse_args()
    result = rename_sessions(args.data.expanduser().resolve(), mappings(args.rename))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
