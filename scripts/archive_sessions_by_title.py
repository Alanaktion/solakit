#!/usr/bin/env python3
"""Lifecycle-archive sessions whose titles contain reviewed phrases."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from solakit.core.store import Store


def archive(root: Path, phrases: list[str]) -> dict:
    needles = [phrase.casefold() for phrase in phrases]
    store = Store(root, exclusive=True)
    try:
        matched = [
            record
            for record in store.list_records("sessions")
            if any(needle in record.get("title", "").casefold() for needle in needles)
        ]
        active = [record for record in matched if not record.get("archived_at")]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = root / "backups" / f"session-lifecycle-{stamp}"
        if backup.exists():
            raise FileExistsError(backup)
        (backup / "sessions").mkdir(parents=True)
        shutil.copy2(root / "app.db", backup / "app.db")
        for record in matched:
            name = f"{record['id']}.meta.json"
            shutil.copy2(root / "sessions" / name, backup / "sessions" / name)

        for record in active:
            store.set_session_archived(record["id"], True)

        counts = {}
        for phrase, needle in zip(phrases, needles, strict=True):
            family = [
                record
                for record in matched
                if needle in record.get("title", "").casefold()
            ]
            counts[phrase] = {
                "matched": len(family),
                "newly_archived": sum(
                    not record.get("archived_at") for record in family
                ),
            }
        return {
            "backup": str(backup),
            "matched": len(matched),
            "newly_archived": len(active),
            "already_archived": len(matched) - len(active),
            "families": counts,
            "storage_health": store.storage_health(),
        }
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("phrase", nargs="+")
    args = parser.parse_args()
    result = archive(args.data.expanduser().resolve(), args.phrase)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
