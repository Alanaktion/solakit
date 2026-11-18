"""Manually submit complete shared-session conversation history to OpenViking."""

import argparse
import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import JsonValue

from solakit.config import DEFAULT_CONFIG, EffectiveProfile, load_config, resolve_profile
from solakit.evidence import canonical, checked_json, digest, ensure_directory, publish
from solakit.memory import OpenViking, scope
from solakit.records import MemoryCommit, Message, Origin, SessionMode
from solakit.redaction import Redactor
from solakit.runtime import DataDirectoryLock
from solakit.store import Store


def commit(
    session_id: str, run_id: str, index: int, payload: list[dict[str, JsonValue]]
) -> MemoryCommit:
    checksum = digest(canonical(payload))
    return MemoryCommit(
        id=uuid5(NAMESPACE_URL, f"sola-memory-history-v1:{session_id}:{index}:{checksum}").hex,
        origin=Origin.SYSTEM,
        session_id=session_id,
        run_id=run_id,
        payload=tuple(payload),
        payload_sha256=checksum,
    )


def batches(store: Store) -> Iterator[MemoryCommit]:
    for row in store.db.execute("SELECT id FROM entities WHERE kind='session' ORDER BY id"):
        session = store.session(row[0])
        if session.mode != SessionMode.SHARED:
            continue
        summaries = {
            row[0]
            for row in store.db.execute(
                "SELECT json_extract(body,'$.summary_message_id') FROM entities "
                "WHERE kind='compaction' AND session_id=?",
                (session.id,),
            )
        }
        payload: list[dict[str, JsonValue]] = []
        run_id = ""
        index = 0

        for message_row in store.db.execute(
            "SELECT id FROM entities WHERE kind='message' AND session_id=? ORDER BY sequence,id",
            (session.id,),
        ):
            message = store._load("message", message_row[0], Message)
            if message.role not in ("user", "assistant") or message.id in summaries:
                continue
            # 8,000 characters fit even when every character needs six JSON bytes.
            for start in range(0, max(1, len(message.content)), 8000):
                part: dict[str, JsonValue] = {
                    "role": message.role,
                    "content": message.content[start : start + 8000],
                    "source_message_ids": [message.id],
                }
                if payload and (
                    len(payload) == 100 or len(canonical({"messages": [*payload, part]})) > 65536
                ):
                    yield commit(session.id, run_id, index, payload)
                    payload = []
                    index += 1
                run_id = message.run_id
                payload.append(part)
        if payload:
            yield commit(session.id, run_id, index, payload)


async def import_history(
    profile: EffectiveProfile, *, dry_run: bool = False, adapter: OpenViking | None = None
) -> dict[str, object]:
    if not (profile.data_dir / "state.db").is_file():
        raise ValueError("existing migrated operational state is required")
    adapter = adapter or OpenViking(profile.memory, Redactor())
    adapter.check(profile)
    owner = DataDirectoryLock(profile.data_dir)
    owner.acquire()
    store = Store(profile.data_dir)
    try:
        await store.start()
        plan = []
        parts = 0
        for batch in batches(store):
            plan.append(batch.id)
            parts += len(batch.payload)
        manifest = {
            "version": 1,
            "base_url": profile.memory.base_url.rstrip("/"),
            **scope(profile.memory),
            "batch_ids": plan,
            "message_parts": parts,
        }
        result: dict[str, object] = {
            "dry_run": dry_run,
            "batches": len(plan),
            "message_parts": parts,
            "accepted": 0,
            "already_accepted": 0,
            "scope": scope(profile.memory),
        }
        directory = profile.data_dir / "imports/openviking-history"
        manifest_path = directory / "manifest.json"
        if manifest_path.exists() and checked_json(manifest_path.read_bytes()) != manifest:
            raise ValueError("history or destination changed since this one-time import began")
        if dry_run or not plan:
            return result
        ensure_directory(directory)
        if not manifest_path.exists():
            publish(manifest_path, canonical(manifest))
        await adapter.start()
        await adapter.verify()
        accepted_count = already_accepted = 0
        for batch in batches(store):
            started = directory / f"{batch.id}.started.json"
            accepted = directory / f"{batch.id}.accepted.json"
            if accepted.exists():
                receipt = checked_json(accepted.read_bytes())
                if not isinstance(receipt, dict) or not isinstance(receipt.get("task_id"), str):
                    raise ValueError("invalid history import acceptance receipt")
                already_accepted += 1
                continue
            if started.exists():
                raise ValueError(
                    f"uncertain submission: inspect OpenViking session sola-{batch.id}; "
                    "the batch will not be replayed"
                )
            publish(
                started,
                canonical(
                    {
                        "session_id": batch.session_id,
                        "provider_session_id": "sola-" + batch.id,
                        "payload_sha256": batch.payload_sha256,
                    }
                ),
            )
            task_id = await adapter.submit(profile, batch, lambda: profile)
            publish(accepted, canonical({"task_id": task_id}))
            accepted_count += 1
        return result | {"accepted": accepted_count, "already_accepted": already_accepted}
    finally:
        try:
            await adapter.stop()
        finally:
            try:
                await store.stop()
            finally:
                owner.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true", help="Count batches without remote calls")
    args = parser.parse_args()
    try:
        profile = resolve_profile(load_config(args.config), base=args.config.resolve().parent)
        result = asyncio.run(import_history(profile, dry_run=args.dry_run))
    except Exception as error:
        parser.exit(1, f"History import stopped ({type(error).__name__}): {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
