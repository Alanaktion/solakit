"""Inspect memory work, or explicitly attach a verified provider task after uncertainty."""

import argparse
import asyncio
import json
from urllib.parse import quote

from solakit.config import DEFAULT_CONFIG, load_config, resolve_profile
from solakit.memory import MemoryService, OpenViking
from solakit.records import MemoryCommit, MemoryState, utc_now
from solakit.redaction import Redactor
from solakit.runtime import DataDirectoryLock
from solakit.store import Store


async def reconcile(service: MemoryService, identity: str, task_id: str) -> MemoryCommit:
    work = service.store._load("memory", identity, MemoryCommit)
    if work.state not in (MemoryState.AMBIGUOUS, MemoryState.FAILED):
        raise ValueError("only ambiguous or failed work needs manual reconciliation")
    profile = service.profile(work.run_id)
    service.adapter.check(profile)
    await service.adapter.verify()
    result = await service.adapter.request("GET", "/api/v1/tasks/" + quote(task_id, safe=""))
    if (
        not isinstance(result, dict)
        or result.get("task_type") != "session_commit"
        or result.get("resource_id") != "sola-" + work.id
        or result.get("status") not in ("pending", "running", "completed", "failed", "cancelled")
    ):
        raise ValueError("provider task does not identify this commit's session")
    return service.store.transition_memory(
        identity,
        MemoryState.ACCEPTED,
        provider_commit_id=task_id,
        attempts=0,
        next_check=utc_now(),
    )


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
