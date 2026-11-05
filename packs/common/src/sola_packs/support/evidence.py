"""Append-only operation receipts; ambiguous sends/creates never auto-replay."""

import fcntl
import json
import os
import stat
from datetime import UTC, datetime

from sola_packs.support import fingerprint

from solakit.core.store import sync_directory
from solakit.tools import ToolError


class Lease:
    def __init__(self, ctx, namespace, identity):
        self.directory = ctx.store.root / "pack-operations" / namespace
        self.path = self.directory / (fingerprint(identity) + ".jsonl")
        self.fd = None

    def __enter__(self):
        base = self.directory.parent
        base.mkdir(mode=0o700, exist_ok=True)
        sync_directory(base.parent)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        sync_directory(base)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(self.fd).st_mode):
                raise ToolError("Operation evidence must be a regular file")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            sync_directory(self.directory)
        except BaseException as exc:
            os.close(self.fd)
            self.fd = None
            if isinstance(exc, BlockingIOError):
                raise ToolError(
                    "Operation is already in progress; check its state before retrying"
                ) from exc
            raise
        return self

    def __exit__(self, *args):
        os.close(self.fd)
        self.fd = None

    def events(self):
        """Read bounded, complete receipts while the resource lock is held."""
        size = os.fstat(self.fd).st_size
        if size > 1_000_000:
            raise ToolError(
                "Operation evidence exceeds its limit; inspect before retrying"
            )
        os.lseek(self.fd, 0, os.SEEK_SET)
        payload = os.read(self.fd, size)
        try:
            if payload and not payload.endswith(b"\n"):
                raise ValueError("torn operation evidence")
            events = [json.loads(line) for line in payload.splitlines()]
            if any(
                not isinstance(event, dict)
                or not isinstance(event.get("stage"), str)
                or not isinstance(event.get("request_hash"), str)
                for event in events
            ):
                raise ValueError("invalid operation evidence")
            return events
        except (ValueError, TypeError) as exc:
            raise ToolError(
                "Operation evidence requires recovery; no retry was performed"
            ) from exc


class Operation(Lease):
    def __init__(self, ctx, namespace, identity, request):
        super().__init__(ctx, namespace, identity)
        self.request_hash = fingerprint(request)
        self.current = None

    def __enter__(self):
        super().__enter__()
        try:
            for event in self.events():
                if event["request_hash"] != self.request_hash:
                    raise ToolError(
                        "Operation identity already used for a different request"
                    )
                self.current = event
        except BaseException as exc:
            super().__exit__()
            if isinstance(exc, (ValueError, KeyError, TypeError)):
                raise ToolError(
                    "Operation evidence requires recovery; no retry was performed"
                ) from exc
            raise
        return self

    def record(self, stage, **details):
        event = {
            "request_hash": self.request_hash,
            "stage": stage,
            "at": datetime.now(UTC).isoformat(),
            **details,
        }
        payload = json.dumps(event, ensure_ascii=False).encode() + b"\n"
        os.lseek(self.fd, 0, os.SEEK_END)
        with os.fdopen(self.fd, "ab", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        self.current = event
        return event
