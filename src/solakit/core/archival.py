"""Operator-only transcript rotation, using compaction's immutable archive format."""

import hashlib
import json

from solakit.core.compaction import finish_rotation, manifest_path
from solakit.core.schemas import now
from solakit.core.store import StorageCorruption, Store, atomic_write, safe_id


def archive_session(store: Store, session_id: str) -> dict:
    """Caller holds execution admission lock and has rejected pending session runs."""
    with store.lock:
        store.session(session_id)
        messages = store.transcript(session_id)
        source = store.root / "sessions" / f"{safe_id(session_id)}.jsonl"
        if any(
            issue["file"] == f"sessions/{session_id}.jsonl"
            for issue in store.storage_health()["issues"]
        ):
            raise StorageCorruption(
                "Transcript damage requires recovery before archival"
            )
        if not messages:
            return {"session": session_id, "archived": False, "archive": None}
        number = (
            max(
                (int(name.rsplit(".", 1)[1]) for name in store.archives(session_id)),
                default=0,
            )
            + 1
        )
        manifest = {
            "kind": "archive",
            "stage": "prepared",
            "archive": f"{session_id}.jsonl.{number}",
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "created_at": now().isoformat(),
            "message_count": len(messages),
        }
        atomic_write(
            manifest_path(store, session_id, "archive"), json.dumps(manifest).encode()
        )
        finish_rotation(store, session_id, manifest)
        return {
            "session": session_id,
            "archived": True,
            "archive": manifest["archive"],
            "number": number,
            "message_count": len(messages),
            "context_started_at": manifest["created_at"],
        }
