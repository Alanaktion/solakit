"""Inspect memory work, or explicitly attach a verified provider task after uncertainty."""

import argparse
import asyncio
import json

from solakit.config import DEFAULT_CONFIG, load_config, resolve_profile
from solakit.memory import MemoryService, OpenViking
from solakit.records import MemoryCommit
from solakit.redaction import Redactor
from solakit.runtime import DataDirectoryLock
from solakit.store import Store


async def reconcile(service: MemoryService, identity: str, task_id: str) -> MemoryCommit:
    return await service.reconcile(identity, task_id)


async def run(args: argparse.Namespace) -> None:
    profile = resolve_profile(load_config(args.config), base=args.config.resolve().parent)
    if not (profile.data_dir / "state.db").is_file():
        raise ValueError("existing operational state is required")
    owner = DataDirectoryLock(profile.data_dir)
    owner.acquire()
    store = Store(profile.data_dir)
    service = MemoryService(store, OpenViking(profile.memory, Redactor()), lambda admitted: profile)
    try:
        await store.start()
        if args.provider_task_id:
            if not args.commit_id:
                raise ValueError("--commit-id is required for reconciliation")
            await service.adapter.start()
            work = await reconcile(service, args.commit_id, args.provider_task_id)
            print(work.model_dump_json())
        else:
            for row in store.db.execute(
                "SELECT id FROM entities WHERE kind='memory' ORDER BY created_at"
            ):
                work = store._load("memory", row[0], MemoryCommit)
                if args.commit_id is None or work.id == args.commit_id:
                    print(work.model_dump_json())
    finally:
        await service.adapter.stop()
        await store.stop()
        owner.release()


def main() -> None:
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--commit-id")
    parser.add_argument("--provider-task-id", help="Explicitly reconcile; never resubmits content")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as error:
        parser.exit(
            1,
            json.dumps(
                {
                    "error": type(error).__name__,
                    "message": "Inspect configuration, scope and task; no submission was replayed",
                }
            )
            + "\n",
        )


if __name__ == "__main__":
    main()
