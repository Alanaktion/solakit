"""Single-operator sessions with durable, sliding inactivity expiry."""

import hashlib
import json
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from solakit.core.store import atomic_write

COOKIE = "solakit_session"
LIFETIME = 5 * 24 * 60 * 60


class UISessions:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.path: Path | None = None
        self.token_hash = ""
        self.clock = clock
        self.sessions: dict[str, float] = {}
        self.failures: dict[str, tuple[int, float, float]] = {}

    def load(self, path: Path, token: str):
        self.path = path
        self.token_hash = hashlib.sha256(token.encode()).hexdigest()
        self.clear()
        if path.exists():
            data = json.loads(path.read_text())
            if data["token_hash"] == self.token_hash:
                self.sessions = {
                    key: expiry
                    for key, expiry in data["sessions"].items()
                    if expiry > self.clock()
                }
        self.save()

    def save(self):
        if self.path is not None:
            atomic_write(
                self.path,
                json.dumps(
                    {
                        "token_hash": self.token_hash,
                        "sessions": self.sessions,
                    }
                ).encode(),
            )

    @staticmethod
    def key(identity: str) -> str:
        return hashlib.sha256(identity.encode()).hexdigest()

    def clear(self):
        self.sessions.clear()
        self.failures.clear()

    def valid(self, identity: str | None) -> bool:
        if not identity:
            return False
        expiry = self.sessions.get(self.key(identity))
        if expiry is None:
            return False
        if expiry <= self.clock():
            self.sessions.pop(self.key(identity), None)
            self.save()
            return False
        self.sessions[self.key(identity)] = self.clock() + LIFETIME
        self.save()
        return True

    def authorized(
        self, authorization: str | None, cookie: str | None, token: str
    ) -> bool:
        bearer = authorization is not None and secrets.compare_digest(
            authorization.encode(), f"Bearer {token}".encode()
        )
        return bearer or self.valid(cookie)

    def login(self, candidate: str, token: str, peer: str) -> tuple[str | None, int]:
        at = self.clock()
        self.sessions = {
            key: expiry for key, expiry in self.sessions.items() if expiry > at
        }
        self.failures = {
            key: value
            for key, value in self.failures.items()
            if max(value[1] + 120, value[2]) > at
        }
        count, since, locked_until = self.failures.get(peer, (0, at, 0))
        if locked_until > at:
            return None, 429
        if at - since >= 60:
            count, since = 0, at
        if secrets.compare_digest(candidate.encode(), token.encode()):
            self.failures.pop(peer, None)
            identity = secrets.token_urlsafe(32)
            self.sessions[self.key(identity)] = at + LIFETIME
            self.save()
            return identity, 200
        count += 1
        self.failures[peer] = (count, since, at + 60 if count >= 5 else 0)
        return None, 401

    def revoke(self, identity: str | None):
        if identity:
            self.sessions.pop(self.key(identity), None)
            self.save()
