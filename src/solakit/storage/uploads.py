"""Bounded, topic-scoped staging for attachments awaiting admission."""

import time
from pathlib import Path

from solakit.records import new_id
from solakit.storage.evidence import CorruptionError, ensure_directory, publish
from solakit.storage.store import valid_id

UPLOAD_TTL = 30 * 60


class Uploads:
    def __init__(self, root: Path, read_limit: int, *, per_topic: int = 4, total: int = 16) -> None:
        self.root = root
        self.read_limit = read_limit
        self.per_topic = per_topic
        self.total = total

    def clean(self) -> list[Path]:
        if self.root.is_symlink():
            raise CorruptionError("upload directory cannot be a symlink")
        ensure_directory(self.root)
        drafts = []
        cutoff = time.time() - UPLOAD_TTL
        for path in self.root.iterdir():
            if path.is_symlink() or not path.is_file():
                raise CorruptionError("invalid upload staging file")
            if path.name.startswith(".") or path.stat().st_mtime <= cutoff:
                path.unlink()
            else:
                drafts.append(path)
        return drafts

    def stage(self, topic: str, data: bytes) -> str:
        valid_id(topic)
        drafts = self.clean()
        if (
            len(drafts) >= self.total
            or sum(draft.name.startswith(topic + "-") for draft in drafts) >= self.per_topic
        ):
            raise OverflowError("Upload storage is full")
        identity = new_id()
        publish(self.root / f"{topic}-{identity}", data)
        return identity

    def read(self, topic: str, identity: str) -> bytes:
        valid_id(topic)
        valid_id(identity)
        path = self.root / f"{topic}-{identity}"
        if self.root.is_symlink() or path.is_symlink():
            raise CorruptionError("invalid upload staging path")
        try:
            if path.stat().st_mtime <= time.time() - UPLOAD_TTL:
                raise KeyError(identity)
            with path.open("rb") as stream:
                raw = stream.read(self.read_limit + 1)
        except FileNotFoundError:
            raise KeyError(identity) from None
        if len(raw) > self.read_limit:
            raise CorruptionError("staged upload exceeds size limit")
        return raw

    def discard(self, topic: str, identity: str) -> None:
        (self.root / f"{topic}-{identity}").unlink(missing_ok=True)
