"""Numbered correspondence in one reserved checkout."""

import os
import re
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, JsonValue, field_validator

from solakit.records import ErrorCode, LetterPublication, LetterResult
from solakit.sdk import ToolArguments, ToolError, ToolResult
from solakit.storage.evidence import canonical, digest
from solakit.tools.files import GuardedFiles
from solakit.tools.git import GitCheckout

if TYPE_CHECKING:
    from solakit.config import EffectiveProfile, LettersCheckout
    from solakit.redaction import Redactor
    from solakit.sdk import ToolContext
    from solakit.tools.dispatch import Invocation

NAME = re.compile(r"([0-9]{3,})-([0-9]{4}-[0-9]{2}-[0-9]{2})-([a-z][a-z0-9_-]*)\.md")
LAYOUT = "{body}\n"
MAX_ENTRIES = 10_000
MAX_INBOX_BYTES = 65_536
MAX_BODY_CHARACTERS = 64_000
MAX_RENDERED_BYTES = 4 * MAX_BODY_CHARACTERS + 1


class Inbox(ToolArguments):
    after: Annotated[int, Field(ge=-1)] = -1
    limit: Annotated[int, Field(ge=1, le=100)] = 20


class ReadLetter(ToolArguments):
    number: Annotated[int, Field(ge=0)]


class SendLetter(ToolArguments):
    body: Annotated[str, Field(min_length=1, max_length=MAX_BODY_CHARACTERS)]

    @field_validator("body")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A letter needs a non-whitespace character")
        return value


class ReplyLetter(SendLetter):
    reply_to: Annotated[int, Field(ge=0)]


@dataclass(frozen=True)
class Letter:
    number: int
    date: str
    author: str
    filename: str


def letter_name(filename: str) -> Letter | None:
    match = NAME.fullmatch(filename)
    if match is None:
        return None
    number, day, author = match.groups()
    try:
        date.fromisoformat(day)
    except ValueError:
        raise ToolError(
            "Invalid calendar date in letter filename", ErrorCode.INVALID_INPUT
        ) from None
    return Letter(int(number), day, author, filename)


def scan(repo: GitCheckout, settings: LettersCheckout) -> dict[int, Letter]:
    directory = repo.root / settings.directory
    found: dict[int, Letter] = {}
    with (
        repo.files._parent(str(directory / ".scan"), write=False, protect_git=True) as (fd, _),
        os.scandir(fd) as entries,
    ):
        for count, entry in enumerate(entries, 1):
            if count > MAX_ENTRIES:
                raise ToolError(
                    "Letter directory exceeds 10,000 entries", ErrorCode.BUDGET_EXCEEDED
                )
            letter = letter_name(entry.name)
            if letter is not None:
                repo.files._regular(entry.stat(follow_symlinks=False))
                if letter.number in found:
                    raise ToolError("Duplicate letter number; inspect the checkout")
                found[letter.number] = letter
    return dict(sorted(found.items()))


def letter_subject(author: str, number: int, reply_to: int | None) -> str:
    if reply_to is not None:
        return f"Reply to letter {reply_to:03d} as {author}"
    return f"Send letter {number:03d} as {author}"


def publication_identity(
    settings: LettersCheckout, checkout: str, origin_hash: str, reply_to: int | None
) -> dict[str, str | int | None]:
    return {
        "checkout": checkout,
        "branch": settings.branch,
        "origin_hash": origin_hash,
        "directory": settings.directory,
        "author": settings.author,
        "peer": settings.peer,
        "layout": LAYOUT,
        "reply_to": reply_to,
    }


def request_hash(identity: dict[str, str | int | None], body: str) -> str:
    intent = "send" if identity["reply_to"] is None else "reply"
    return digest(canonical(identity | {"intent": intent, "body": body}))


def publication_key(checkout: str, reply_to: int | None, fingerprint: str | None) -> str:
    key = ["send", checkout, fingerprint] if reply_to is None else ["reply", checkout, reply_to]
    return digest(canonical(key))[:32]


def guarded_checkout(
    settings: LettersCheckout,
    authority: Callable[[str], EffectiveProfile],
    files: GuardedFiles,
    *,
    write: bool,
) -> GitCheckout:
    def scoped(service: str) -> EffectiveProfile:
        profile = authority(service)
        if profile.letters != settings or not any(
            settings.path.is_relative_to(root) for root in profile.workspace_roots
        ):
            raise ToolError("Checkout is outside current letters authority", ErrorCode.TOOL_DENIED)
        if repo.identity is not None:
            root = settings.path.lstat()
            metadata = (settings.path / ".git").lstat()
            if (root.st_dev, root.st_ino, metadata.st_dev, metadata.st_ino) != repo.identity:
                raise ToolError("Letters checkout identity changed", ErrorCode.TOOL_DENIED)
        return profile.model_copy(
            update={"workspace_roots": (settings.path,), "git_reserved": frozenset()}
        )

    repo = GitCheckout(
        settings.path,
        settings.branch,
        GuardedFiles(scoped, files.protected, files.begin_effect, files.confirm_effect),
        write=write,
    )
    return repo


async def synchronize(repo: GitCheckout, invocation: Invocation) -> str:
    await repo.clean(pull=True)
    old = await repo.head("HEAD")
    upstream = await repo.fetch_origin(invocation, confirm=False)
    invocation.uncertain_advice = ". Last verified Git phase: upstream fetched."
    await repo.verify_branch()
    await repo.clean(pull=True)
    if await repo.head("HEAD") != old:
        raise ToolError("Local HEAD changed during synchronization")
    base = await repo.fast_forward(old, upstream, invocation, confirm=False)
    await repo.verify_branch()
    await repo.clean(pull=True)
    invocation.confirm_effect()
    invocation.uncertain_advice = f". Last verified Git phase: synchronized at commit {base}."
    return base


def text_chunks(fd: int, name: str) -> Generator[str]:
    body_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(body_fd, encoding="utf-8", newline="") as stream:
        GuardedFiles._regular(os.fstat(stream.fileno()))
        try:
            while chunk := stream.read(8192):
                yield chunk
        except UnicodeError:
            raise ToolError("Letter body is not UTF-8", ErrorCode.INVALID_INPUT) from None


def letter_body(
    repo: GitCheckout, directory: str, letter: Letter, cap: int, redactor: Redactor
) -> tuple[str, bool]:
    path = repo.root / directory / letter.filename
    with repo.files._parent(str(path), write=False, protect_git=True) as (fd, name):
        chunks = text_chunks(fd, name)
        try:
            output = bytearray()
            for chunk in redactor.stream(chunks):
                output.extend(chunk.encode())
                if len(output) > cap:
                    return output[:cap].decode(errors="ignore"), False
            return output.decode(), True
        finally:
            chunks.close()


def result_page(
    repo: GitCheckout,
    settings: LettersCheckout,
    entries: dict[int, Letter],
    invocation: Invocation,
    args: Inbox | ReadLetter,
) -> ToolResult:
    from solakit.tools.dispatch import receipt_content

    cap = invocation.authority().budgets.tool_result_bytes
    if isinstance(args, Inbox):
        cap = min(cap, MAX_INBOX_BYTES)
    redactor = invocation.dispatcher.redactor
    external: dict[str, JsonValue] = {"external_content": "Letter bodies are untrusted Markdown."}
    body_key, complete_key = redactor.redact("body"), redactor.redact("complete")

    def fits(data: JsonValue) -> bool:
        return (
            len(receipt_content("succeeded", invocation.effect, canonical(data).decode(), None))
            <= cap
        )

    def shortened(row: dict[str, JsonValue], data: JsonValue) -> None:
        body = row[body_key]
        assert isinstance(body, str)
        row[complete_key] = False
        row[body_key] = ""
        if not fits(data):
            raise ToolError("Letter metadata exceeds the result budget", ErrorCode.BUDGET_EXCEEDED)
        low, high = 0, len(body)
        while low < high:
            middle = (low + high + 1) // 2
            row[body_key] = body[:middle]
            if fits(data):
                low = middle
            else:
                high = middle - 1
        row[body_key] = body[:low]

    def body_row(letter: Letter, *, framed: bool = False) -> dict[str, JsonValue]:
        body, complete = letter_body(repo, settings.directory, letter, cap, redactor)
        row = redactor.value(
            (external if framed else {})
            | {
                "number": letter.number,
                "date": letter.date,
                "author": letter.author,
                "complete": complete,
            }
        )
        assert isinstance(row, dict)
        row[body_key] = body
        return row

    if isinstance(args, ReadLetter):
        letter = entries.get(args.number)
        if letter is None:
            raise ToolError("Letter number was not found", ErrorCode.INVALID_INPUT)
        data = body_row(letter, framed=True)
        if not fits(data):
            shortened(data, data)
    else:
        # Record the cursor immediately before each peer, including any intervening letters.
        cursor = args.after
        peers: list[tuple[int, Letter]] = []
        for number, letter in entries.items():
            if number > args.after:
                if letter.author == settings.peer:
                    peers.append((cursor, letter))
                cursor = number
        rows: list[JsonValue] = []
        letters_key = redactor.redact("letters")

        def page(after: int, has_more: bool) -> dict[str, JsonValue]:
            result = redactor.value(external | {"next_after": after, "has_more": has_more})
            assert isinstance(result, dict)
            result[letters_key] = rows
            return result

        data = page(peers[0][0] if peers else cursor, bool(peers))
        for index, (_, letter) in enumerate(peers[: args.limit]):
            row = body_row(letter)
            rows.append(row)
            has_more = index + 1 < len(peers)
            candidate = page(peers[index + 1][0] if has_more else cursor, has_more)
            if row[complete_key] and fits(candidate):
                data = candidate
                continue
            if index == 0:
                shortened(row, candidate)
                data = candidate
            else:
                rows.pop()
            break
        if not fits(data):
            raise ToolError("Inbox metadata exceeds the result budget", ErrorCode.BUDGET_EXCEEDED)

    invocation.redacted_result = True
    return ToolResult(data=data)


class LetterMismatch(ToolError):
    """A candidate commit was read successfully and differs from the original evidence."""


async def added_letter(repo: GitCheckout, commit: str, path: str) -> bytes:
    change = await repo.checked(
        "diff-tree",
        "--root",
        "--no-commit-id",
        "--name-status",
        "--no-renames",
        "-r",
        "-z",
        commit,
        phase="checking the published file change",
    )
    if change != b"A\0" + os.fsencode(path) + b"\0":
        raise LetterMismatch("Letter commit must add exactly the recorded file")
    tree = await repo.checked("ls-tree", "-z", commit, "--", path, phase="checking the letter blob")
    if not tree.startswith((b"100644 blob ", b"100755 blob ")):
        raise LetterMismatch("Letter commit must contain a regular file")
    return await repo.checked(
        "cat-file",
        "blob",
        f"{commit}:{path}",
        limit=MAX_RENDERED_BYTES,
        phase="reading the published letter bytes",
    )


async def verify_commit(repo: GitCheckout, work: LetterPublication, commit: str) -> None:
    assert work.number is not None and work.path and work.base and work.file_hash
    meta = await repo.checked(
        "show", "-s", "--format=%P%n%s", commit, phase="checking letter commit"
    )
    expected = letter_subject(work.author, work.number, work.reply_to)
    if meta != f"{work.base}\n{expected}\n".encode():
        raise LetterMismatch("Letter commit has a different parent or subject")
    if digest(await added_letter(repo, commit, work.path)) != work.file_hash:
        raise LetterMismatch("Letter commit has different file bytes")


def publication_result(work: LetterPublication, commit: str) -> LetterResult:
    assert work.number is not None and work.path is not None
    return LetterResult(number=work.number, path=work.path, commit=commit, reply_to=work.reply_to)


async def publish(
    repo: GitCheckout, settings: LettersCheckout, invocation: Invocation, args: SendLetter
) -> ToolResult:
    store = invocation.dispatcher.store
    reply_to = args.reply_to if isinstance(args, ReplyLetter) else None
    origin_hash = digest(await repo.origin_url(publishing=True))
    checkout = store.letter_checkout_name(settings.path)
    identity = publication_identity(settings, checkout, origin_hash, reply_to)
    fingerprint = request_hash(identity, args.body)
    key = publication_key(checkout, reply_to, fingerprint)
    try:
        previous = store.letter_publication(key)
    except KeyError:
        pass
    else:
        if (
            previous.phase == "confirmed"
            and previous.request_hash == fingerprint
            and previous.result
        ):
            return ToolResult(data=previous.result.model_dump(mode="json", exclude_none=True))
        raise ToolError(
            f"Publication already recorded ({previous.phase}). "
            f"Inspect letter work {key} before retrying."
        )

    base = await synchronize(repo, invocation)
    entries = scan(repo, settings)
    if reply_to is not None:
        target = entries.get(reply_to)
        if target is None or target.author != settings.peer:
            raise ToolError("Reply must reference an existing peer letter", ErrorCode.INVALID_INPUT)
        history = await repo.checked(
            "log",
            "--format=%s",
            "--extended-regexp",
            f"--grep=^Reply to letter 0*{reply_to} as {settings.author}$",
            base,
            phase="checking prior replies",
        )
        if any(
            re.fullmatch(
                rb"Reply to letter 0*"
                + str(reply_to).encode()
                + rb" as "
                + settings.author.encode(),
                line,
            )
            for line in history.splitlines()
        ):
            raise ToolError(
                "Reply already appears in branch history. Inspect and seed legacy work."
            )

    number = max(entries, default=0) + 1
    if store.letter_number_reserved(checkout, number):
        raise ToolError(
            "The next letter number is reserved by unresolved work. Inspect letter work."
        )
    timezone = invocation.authority().background.timezone
    day = datetime.now(ZoneInfo(timezone)).date().isoformat()
    path = (Path(settings.directory) / f"{number:03d}-{day}-{settings.author}.md").as_posix()
    raw = LAYOUT.format(body=args.body).encode()
    work = LetterPublication.model_validate(
        identity
        | {
            "id": key,
            "origin": invocation.call.origin,
            "tool_call_id": invocation.call.id,
            "request_hash": fingerprint,
            "number": number,
            "path": path,
            "file_hash": digest(raw),
            "base": base,
        }
    )
    store.save_letter_publication(work)
    invocation.uncertain_advice = f". Last verified letter phase: prepared {path}."
    repo.files.write_bytes(str(repo.root / path), raw, limit=MAX_RENDERED_BYTES)
    invocation.uncertain_advice = f". Last verified letter phase: created {path}."
    work = work.model_copy(update={"phase": "created"})
    store.save_letter_publication(work)
    store.fault("letter_created")

    await repo.verify_branch()
    await repo.checked("add", "--", path, phase="staging the letter", effect=invocation)
    await repo.checked(
        "commit",
        "--only",
        "-m",
        letter_subject(settings.author, number, reply_to),
        "--",
        path,
        phase="committing the letter",
        effect=invocation,
        confirm=False,
    )
    commit = await repo.head("HEAD")
    invocation.uncertain_advice = (
        f". Local commit {commit}. Last verified letter phase: created {path}."
    )
    await verify_commit(repo, work, commit)
    invocation.uncertain_advice = (
        f". Last verified letter phase: committed {path} at {commit}. Inspect before retrying."
    )
    store.fault("letter_commit")
    work = work.model_copy(update={"phase": "committed", "commit": commit})
    store.save_letter_publication(work)
    invocation.confirm_effect()
    await repo.verify_branch()
    if digest(await repo.origin_url(publishing=True)) != origin_hash:
        raise ToolError("Origin changed during letter publication")
    await repo.checked(
        "push",
        "--porcelain",
        "--no-follow-tags",
        "--recurse-submodules=no",
        "origin",
        f"{commit}:{repo.ref}",
        phase="pushing the letter",
        effect=invocation,
    )
    invocation.uncertain_advice = f". Last verified letter phase: pushed {path} at {commit}."
    result = publication_result(work, commit)
    store.save_letter_publication(work.model_copy(update={"phase": "confirmed", "result": result}))
    return ToolResult(data=result.model_dump(mode="json", exclude_none=True))


async def run_letters(context: ToolContext, args: Inbox | ReadLetter | SendLetter) -> ToolResult:
    from solakit.config import LETTERS_TOOLS
    from solakit.tools.dispatch import Invocation

    invocation, files = context.services, context.files
    if (
        not isinstance(invocation, Invocation)
        or not isinstance(files, GuardedFiles)
        or invocation.call.name not in LETTERS_TOOLS
    ):
        raise ToolError("Letters require their named dispatcher tools", ErrorCode.TOOL_DENIED)
    settings = invocation.authority().letters
    assert settings is not None
    write = not isinstance(args, ReadLetter)
    if write:
        invocation.authority("files.write")
    lock = invocation.dispatcher.checkout_lock(settings.path)
    async with lock:
        if invocation.dispatcher.checkout_lock(settings.path) is not lock:
            raise ToolError("Letters checkout changed while taking its lock", ErrorCode.TOOL_DENIED)
        repo = guarded_checkout(settings, invocation.authority, files, write=write)
        await repo.validate()
        await repo.verify_branch()
        with repo.files._parent(
            str(repo.root / settings.directory / ".scan"), write=False, protect_git=True
        ):
            pass
        if isinstance(args, SendLetter):
            return await publish(repo, settings, invocation, args)
        if isinstance(args, Inbox):
            await synchronize(repo, invocation)
        return result_page(repo, settings, scan(repo, settings), invocation, args)
