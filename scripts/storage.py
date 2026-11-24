"""Developer evidence maintenance; never starts server/model/memory clients."""

import argparse
import asyncio
import json
from pathlib import Path

from solakit.runtime import DataDirectoryLock
from solakit.storage.backup import backup, restore
from solakit.storage.legacy import import_fixture, retain_tree
from solakit.storage.search import rebuild
from solakit.storage.store import Store


async def run(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "restore":
        restore(args.source.resolve(), args.destination.resolve())
        return {"status": "restored", "destination": str(args.destination)}
    root = args.data_dir.resolve()
    if not root.is_dir():
        raise ValueError("data directory must already exist")
    owner = DataDirectoryLock(root)
    owner.acquire()
    try:
        if args.command == "rebuild":
            return {"status": "rebuilt", "entries": rebuild(root)}
        store = Store(root)
        try:
            await store.start()
            if args.command == "backup":
                backup(store, args.destination.resolve())
                return {"status": "backed_up", "destination": str(args.destination)}
            if args.command == "retain-tree":
                return {"status": "retained", **retain_tree(store, args.source)}
            return {"status": "imported", "entries": import_fixture(store, args.source)}
        finally:
            await store.stop()
    finally:
        owner.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("rebuild", "backup", "import-fixture", "retain-tree"):
        command = commands.add_parser(name)
        command.add_argument("--data-dir", required=True, type=Path)
        if name == "backup":
            command.add_argument("destination", type=Path)
        elif name in ("import-fixture", "retain-tree"):
            command.add_argument("source", type=Path)
    command = commands.add_parser("restore")
    command.add_argument("source", type=Path)
    command.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), indent=2))


if __name__ == "__main__":
    main()
