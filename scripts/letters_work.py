"""Inspect correspondence work, reconcile a published commit, or seed v1 replies."""

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from solakit.config import DEFAULT_CONFIG, LettersCheckout, load_config, resolve_profile
from solakit.records import LetterPublication, Origin
from solakit.runtime import DataDirectoryLock
from solakit.sdk import ToolError
from solakit.storage.evidence import digest
from solakit.storage.store import Store
from solakit.tools.files import GuardedFiles
from solakit.tools.git import GitCheckout
from solakit.tools.letters import (
    LAYOUT,
    MAX_RENDERED_BYTES,
    LetterMismatch,
    added_letter,
    guarded_checkout,
    letter_name,
    letter_subject,
    publication_identity,
    publication_key,
    publication_result,
    request_hash,
    verify_commit,
)

COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
REPLY = re.compile(r"Reply to letter ([0-9]+) as ([a-z][a-z0-9_-]*)")


async def reconcile(
    store: Store, repo: GitCheckout, settings: LettersCheckout, origin_hash: str, identity: str
) -> LetterPublication:
    work = store.letter_publication(identity)
    checkout = store.letter_checkout_name(settings.path)
    expected = publication_identity(settings, checkout, origin_hash, work.reply_to)
    if any(getattr(work, key) != value for key, value in expected.items()):
        raise ToolError("Configured checkout does not match the recorded publication")
    if work.phase == "confirmed":
        return work
    if not (work.path and work.file_hash and work.base and work.number is not None):
        raise ToolError("Original allocation is incomplete. Inspect v1 evidence and reseed.")
    path = str(repo.root / work.path)

    def verify_local_file() -> None:
        if digest(repo.files.read_bytes(path, limit=MAX_RENDERED_BYTES)) != work.file_hash:
            raise ToolError("The local letter bytes differ from the prepared record")

    verify_local_file()
    upstream = await repo.fetch_origin()
    if work.commit:
        commit = work.commit
        await verify_commit(repo, work, commit)
    else:
        subject = letter_subject(work.author, work.number, work.reply_to)
        commits = await repo.checked(
            "log",
            "--all",
            "--reflog",
            "--format=%H",
            "--fixed-strings",
            f"--grep={subject}",
            "HEAD",
            upstream,
            f"^{work.base}",
            phase="finding the interrupted letter commit",
        )
        matches = []
        for candidate in dict.fromkeys(commits.decode("ascii").splitlines()):
            try:
                await verify_commit(repo, work, candidate)
            except LetterMismatch:
                continue
            matches.append(candidate)
        if len(matches) != 1:
            raise ToolError("Could not identify a unique commit matching the prepared letter")
        commit = matches[0]
    await repo.checked(
        "merge-base", "--is-ancestor", commit, upstream, phase="verifying remote publication"
    )
    if digest(await repo.origin_url(publishing=True)) != origin_hash:
        raise ToolError("Origin changed during reconciliation")
    verify_local_file()
    confirmed = work.model_copy(
        update={"phase": "confirmed", "commit": commit, "result": publication_result(work, commit)}
    )
    store.save_letter_publication(confirmed)
    return confirmed


def v1_hash(value: object) -> str:
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())


@dataclass(frozen=True)
class LegacyReceipt:
    evidence: str
    fingerprint: str
    filename: str | None
    completed: bool
    result: dict[str, object]


def legacy_receipts(operations: Path, original: Path, directory: str) -> dict[int, LegacyReceipt]:
    found = {}
    for path in sorted((operations / "letters").glob("*.jsonl")):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            GuardedFiles._regular(os.fstat(stream.fileno()))
            raw = stream.read(1_000_001)
        if not raw:
            continue
        if len(raw) > 1_000_000 or not raw.endswith(b"\n"):
            raise ToolError("Legacy receipt is incomplete or exceeds its size limit")
        events = [json.loads(line) for line in raw.splitlines()]
        targets = set()
        filenames = set()
        hashes = set()
        for event in events:
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("stage"), str)
                or not isinstance(event.get("request_hash"), str)
            ):
                raise ToolError("Invalid legacy publication event")
            result = event.get("result", {})
            if not isinstance(result, dict):
                raise ToolError("Invalid legacy receipt result")
            for source in (event, result):
                if "reply_to" in source:
                    target = source["reply_to"]
                    if type(target) is not int or target < 0:
                        raise ToolError("Invalid target in legacy reply evidence")
                    targets.add(target)
                if "filename" in source:
                    filename = source["filename"]
                    if not isinstance(filename, str) or "/" in filename:
                        raise ToolError("Invalid filename in legacy reply evidence")
                    filenames.add(filename)
            hashes.add(event["request_hash"])
        if len(targets) != 1:
            raise ToolError("Legacy reply evidence needs one identifiable target")
        target = targets.pop()
        if path.stem != v1_hash([str((original / directory).resolve()), target]):
            continue
        if len(hashes) != 1 or len(filenames) > 1:
            raise ToolError("Conflicting legacy reply evidence")
        fingerprint = hashes.pop()
        if not isinstance(fingerprint, str) or re.fullmatch("[0-9a-f]{64}", fingerprint) is None:
            raise ToolError("Invalid legacy request hash")
        found[target] = LegacyReceipt(
            digest(raw),
            fingerprint,
            next(iter(filenames), None),
            events[-1]["stage"] == "published",
            events[-1].get("result", {}),
        )
    return found


async def legacy_reply(
    repo: GitCheckout,
    settings: LettersCheckout,
    checkout: str,
    origin_hash: str,
    commit: str,
    target: int,
    original: Path,
    receipt: LegacyReceipt | None,
) -> LetterPublication | None:
    base = (
        (await repo.checked("show", "-s", "--format=%P", commit, phase="checking v1 letter parent"))
        .decode("ascii")
        .strip()
    )
    if not COMMIT.fullmatch(base):
        raise ToolError("v1 reply must have a single parent")
    change = await repo.checked(
        "diff-tree",
        "--no-commit-id",
        "--name-status",
        "--no-renames",
        "-r",
        "-z",
        commit,
        phase="checking v1 letter path",
    )
    entries = change.split(b"\0")
    if len(entries) != 3 or entries[0] != b"A":
        raise ToolError("v1 reply must add a single letter")
    path = Path(os.fsdecode(entries[1]))
    letter = letter_name(path.name)
    if (
        path.parent != Path(settings.directory)
        or letter is None
        or letter.author != settings.author
    ):
        raise ToolError("v1 reply is outside the configured letter layout")
    treeish = base if settings.directory == "." else f"{base}:{settings.directory}"
    tree = await repo.checked("ls-tree", "-z", treeish, phase="checking the v1 reply target")
    matches = []
    for entry in tree.split(b"\0")[:-1]:
        meta, _, name = entry.partition(b"\t")
        candidate = letter_name(os.fsdecode(name))
        if candidate is None:
            continue
        if candidate.number == letter.number:
            raise ToolError("v1 reply reused an allocated letter number")
        if candidate.number == target:
            matches.append((meta, candidate))
    if (
        len(matches) != 1
        or not matches[0][0].startswith((b"100644 blob ", b"100755 blob "))
        or matches[0][1].author != settings.peer
    ):
        raise ToolError("v1 reply does not reference a unique peer letter in its parent")
    raw = await added_letter(repo, commit, path.as_posix())
    if not raw.endswith(b"\n"):
        raise ToolError("v1 reply does not match the body-plus-LF layout")
    body = raw[:-1].decode("utf-8")
    identity = publication_identity(settings, checkout, origin_hash, target)
    fingerprint = None
    if receipt is not None:
        original_hash = v1_hash(
            {
                "number": target,
                "body": body,
                "author": settings.author,
                "peer": settings.peer,
                "layout": LAYOUT,
                "repository": str(original),
            }
        )
        exact = receipt.fingerprint == original_hash and receipt.filename == letter.filename
        if receipt.completed:
            exact = exact and all(
                receipt.result.get(key) == value
                for key, value in {
                    "number": letter.number,
                    "reply_to": target,
                    "filename": letter.filename,
                    "publication": f"Published commit {commit}",
                }.items()
            )
        elif not exact:
            return None
        if exact:
            fingerprint = request_hash(identity, body)
    work = LetterPublication.model_validate(
        identity
        | {
            "id": publication_key(checkout, target, None),
            "origin": Origin.SYSTEM,
            "legacy_evidence": f"git:{commit}" if receipt is None else receipt.evidence,
            "number": letter.number,
            "path": path.as_posix(),
            "file_hash": digest(raw),
            "base": base,
            "request_hash": fingerprint,
            "phase": "confirmed",
            "commit": commit,
        }
    )
    return work.model_copy(update={"result": publication_result(work, commit)})


async def seed(
    store: Store,
    repo: GitCheckout,
    settings: LettersCheckout,
    origin_hash: str,
    operations: Path,
    original: Path,
) -> list[LetterPublication]:
    receipts = legacy_receipts(operations, original, settings.directory)
    checkout = store.letter_checkout_name(settings.path)
    upstream = await repo.fetch_origin()
    history = await repo.checked(
        "log",
        "--format=%H%x09%s",
        "--grep=^Reply to letter ",
        upstream,
        phase="reading v1 reply history on the configured remote branch",
    )
    records = {}
    for line in history.decode("utf-8", errors="replace").splitlines():
        commit, _, subject = line.partition("\t")
        match = REPLY.fullmatch(subject)
        if match is None or match[2] != settings.author:
            continue
        target = int(match[1])
        if target in records:
            raise ToolError("Remote history has multiple replies for one target")
        records[target] = await legacy_reply(
            repo, settings, checkout, origin_hash, commit, target, original, receipts.get(target)
        )
    for target, receipt in receipts.items():
        if records.get(target) is None:
            letter = letter_name(receipt.filename) if receipt.filename else None
            records[target] = LetterPublication.model_validate(
                publication_identity(settings, checkout, origin_hash, target)
                | {
                    "id": publication_key(checkout, target, None),
                    "origin": Origin.SYSTEM,
                    "legacy_evidence": receipt.evidence,
                    "number": letter.number if letter else None,
                    "path": (
                        (Path(settings.directory) / letter.filename).as_posix() if letter else None
                    ),
                }
            )
    if digest(await repo.origin_url(publishing=True)) != origin_hash:
        raise ToolError("Origin changed during v1 verification")
    saved = []
    for record in records.values():
        assert record is not None
        try:
            previous = store.letter_publication(record.id)
        except KeyError:
            store.save_letter_publication(record)
        else:
            if (
                previous.phase == "prepared"
                and record.phase == "confirmed"
                and record.request_hash is not None
                and previous.origin == Origin.SYSTEM
                and previous.legacy_evidence == record.legacy_evidence
            ):
                store.save_letter_publication(
                    record.model_copy(update={"created_at": previous.created_at})
                )
            else:
                record = previous
        saved.append(record)
    return saved


async def run(args: argparse.Namespace) -> None:
    profile = resolve_profile(
        load_config(args.config), args.profile, base=args.config.resolve().parent
    )
    if not (profile.data_dir / "state.db").is_file():
        raise ValueError("Existing operational state is required")
    owner = DataDirectoryLock(profile.data_dir)
    store = Store(profile.data_dir)
    owner.acquire()
    try:
        await store.start()
        if not args.reconcile and not args.seed_v1:
            for row in store.db.execute(
                "SELECT id FROM entities WHERE kind='letter_publication' ORDER BY created_at"
            ):
                print(store.letter_publication(row[0]).model_dump_json())
            return
        settings = profile.letters
        if settings is None:
            raise ToolError("Configure the original letters checkout before verification")

        def unexpected_effect(*_: object) -> None:
            raise ToolError("Operator verification cannot publish")

        files = GuardedFiles(lambda _: profile, (), unexpected_effect, unexpected_effect)
        repo = guarded_checkout(settings, lambda _: profile, files, write=False)
        await repo.validate()
        await repo.verify_branch()
        origin_hash = digest(await repo.origin_url(publishing=True))
        if args.reconcile:
            print(
                (
                    await reconcile(store, repo, settings, origin_hash, args.reconcile)
                ).model_dump_json()
            )
        else:
            operations = (
                args.legacy_operations or profile.data_dir / "imports/legacy/pack-operations"
            )
            original = args.legacy_checkout or settings.path
            for work in await seed(
                store, repo, settings, origin_hash, operations, original.resolve()
            ):
                print(work.model_dump_json())
    finally:
        try:
            await store.stop()
        finally:
            owner.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--profile", default="default")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--reconcile", metavar="PUBLICATION_ID")
    operation.add_argument("--seed-v1", action="store_true")
    parser.add_argument("--legacy-checkout", type=Path, help="Original v1 path when seeding a copy")
    parser.add_argument(
        "--legacy-operations", type=Path, help="Original v1 pack-operations directory"
    )
    try:
        asyncio.run(run(parser.parse_args()))
    except (ToolError, OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Letter work verification failed. {error}\n")


if __name__ == "__main__":
    main()
