"""OpenViking summaries and crash-recoverable transcript rotation."""

import hashlib
import json
import os
from pathlib import Path

from solakit.core.io import durable
from solakit.core.memory import MemoryClient, MemoryUnavailable
from solakit.core.schemas import Message, Origin, Session
from solakit.core.store import (
    StorageCorruption,
    Store,
    atomic_write,
    safe_id,
    sync_directory,
)


class ContextLimit(Exception):
    """The context cannot safely fit and no completed summary can replace it."""


def request_bytes(
    model: str, messages: list[dict], tools: list[dict], max_tokens: int
) -> int:
    """Text/metadata bytes plus visual reservation; never measured token usage."""
    from solakit.core.media import IMAGE_CONTEXT_BYTES

    image_budget = (
        sum(len(message.get("images", [])) for message in messages)
        * IMAGE_CONTEXT_BYTES
    )
    return image_budget + len(
        json.dumps(
            {
                "model": model,
                "messages": messages,
                "tools": tools,
                "stream": False,
                "max_tokens": max_tokens,
                **({"tool_choice": "none"} if not tools else {}),
            },
            ensure_ascii=False,
        ).encode("utf-8")
    )


def manifest_path(store: Store, session_id: str, kind: str = "compaction") -> Path:
    if kind not in {"compaction", "archive"}:
        raise ValueError("Unknown rotation kind")
    return store.root / "sessions" / f"{safe_id(session_id)}.{kind}.json"


def finish_rotation(store: Store, session_id: str, manifest: dict) -> None:
    """Idempotent recovery under the store lock, before any new append is allowed."""
    with store.lock:
        active = store.root / "sessions" / f"{safe_id(session_id)}.jsonl"
        archive_name = manifest["archive"]
        if (
            not archive_name.startswith(f"{session_id}.jsonl.")
            or not archive_name.removeprefix(f"{session_id}.jsonl.").isdigit()
        ):
            raise StorageCorruption("Invalid compaction archive name")
        archive = active.parent / archive_name
        kind = manifest.get("kind", "compaction")
        journal = manifest_path(store, session_id, kind)
        if kind == "archive":
            payload = b""
        else:
            boundary = Message.model_validate(manifest["boundary"])
            payload = boundary.model_dump_json().encode() + b"\n"
        if not archive.exists():
            if (
                not active.exists()
                or hashlib.sha256(active.read_bytes()).hexdigest()
                != manifest["source_sha256"]
            ):
                raise StorageCorruption("Compaction source changed before archival")
            os.replace(active, archive)
            sync_directory(active.parent)
        if (
            hashlib.sha256(archive.read_bytes()).hexdigest()
            != manifest["source_sha256"]
        ):
            raise StorageCorruption("Compaction archive hash mismatch")
        if active.exists():
            if active.read_bytes() != payload:
                raise StorageCorruption(
                    "Unexpected active transcript during compaction recovery"
                )
        else:
            atomic_write(active, payload)
        manifest = {**manifest, "stage": "committed"}
        atomic_write(journal, json.dumps(manifest).encode())
        if (store.root / "app.db").exists():
            store.reindex_messages(session_id)


def recover_rotations(store: Store) -> None:
    for kind in ("compaction", "archive"):
        for path in (store.root / "sessions").glob(f"*.{kind}.json"):
            manifest = json.loads(path.read_bytes())
            if manifest.get("kind", "compaction") != kind:
                raise StorageCorruption("Rotation manifest kind mismatch")
            if manifest["stage"] == "prepared":
                finish_rotation(
                    store, path.name.removesuffix(f".{kind}.json"), manifest
                )


def prepare_rotation(store: Store, session_id: str, manifest: dict) -> None:
    """Persist intent and rotate in one durable operation, with no cancellation gap."""
    with store.lock:
        atomic_write(manifest_path(store, session_id), json.dumps(manifest).encode())
        finish_rotation(store, session_id, manifest)


class Compactor:
    def __init__(self, store: Store, memory: MemoryClient | None):
        self.store, self.memory = store, memory

    async def compact(self, session: Session) -> dict:
        if self.memory is None or session.memory_scope != "shared":
            raise ContextLimit(
                "Context exceeds budget; shared OpenViking is required for compaction"
            )
        history = await durable(self.store.transcript, session.id)
        if not history:
            raise ContextLimit("Prompt/task exceeds budget without compactable history")
        active = self.store.root / "sessions" / f"{safe_id(session.id)}.jsonl"
        health = await durable(self.store.storage_health)
        if any(i["file"] == f"sessions/{session.id}.jsonl" for i in health["issues"]):
            raise ContextLimit("Transcript damage requires recovery before compaction")
        payload = await durable(active.read_bytes)
        digest = hashlib.sha256(payload).hexdigest()
        operation = f"compact-{digest[:40]}"
        path = manifest_path(self.store, session.id)
        await durable(
            atomic_write,
            path,
            json.dumps(
                {
                    "stage": "summarizing",
                    "operation": operation,
                    "source_sha256": digest,
                }
            ).encode(),
        )
        try:
            summary, uri = await self.memory.scoped(session).summary(
                self.store, operation, history
            )
        except MemoryUnavailable as exc:
            raise ContextLimit(str(exc)) from exc
        names = await durable(self.store.archives, session.id)
        archive_number = (
            max((int(name.rsplit(".", 1)[1]) for name in names), default=0) + 1
        )
        archive = f"{session.id}.jsonl.{archive_number}"
        boundary = Message(
            role="assistant",
            content=summary,
            kind="compaction",
            summary_uri=uri,
            archive=archive,
            run_id=operation,
            origin=Origin(channel="compaction"),
        )
        manifest = {
            "stage": "prepared",
            "operation": operation,
            "source_sha256": digest,
            "archive": archive,
            "boundary": boundary.model_dump(mode="json"),
        }
        await durable(prepare_rotation, self.store, session.id, manifest)
        return {**manifest, "stage": "committed"}
