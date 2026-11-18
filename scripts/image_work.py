"""Inspect image work or reconcile a verified ComfyUI ID; never submits a workflow."""

import argparse
import asyncio
from pathlib import Path

from solakit.config import DEFAULT_CONFIG, load_config, resolve_profile
from solakit.images import ImageService
from solakit.runtime import DataDirectoryLock
from solakit.store import Store


async def run(args: argparse.Namespace) -> None:
    profile = resolve_profile(load_config(args.config), base=args.config.resolve().parent)
    if not (profile.data_dir / "state.db").is_file():
        raise ValueError("existing operational state is required")
    owner = DataDirectoryLock(profile.data_dir)
    store = Store(profile.data_dir)
    images = ImageService(store, profile.images)
    owner.acquire()
    try:
        await store.start()
        if args.submission_id:
            if not args.image_id or not profile.images.enabled:
                raise ValueError(
                    "reconciliation requires --image-id and the enabled original endpoint"
                )
            await images.adapter.start()
            print((await images.reconcile(args.image_id, args.submission_id)).model_dump_json())
        else:
            for row in store.db.execute(
                "SELECT id FROM entities WHERE kind='image' ORDER BY created_at"
            ):
                if args.image_id is None or args.image_id == row[0]:
                    print(images.get(row[0]).model_dump_json())
    finally:
        await images.adapter.stop()
        await store.stop()
        owner.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--image-id")
    parser.add_argument(
        "--submission-id", help="Explicit verified history ID; no POST or resubmission"
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception:
        parser.exit(
            1,
            "Image reconciliation failed; inspect configuration, history and scope. "
            "No workflow was submitted.\n",
        )


if __name__ == "__main__":
    main()
