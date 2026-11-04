"""Single-operator UI sessions, invalidated on restart with no persistent state."""

import secrets
import time
from collections.abc import Callable

COOKIE = "solakit_session"
LIFETIME = 7 * 24 * 60 * 60


class UISessions:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.sessions: dict[str, float] = {}
        self.failures: dict[str, tuple[int, float, float]] = {}

    def clear(self):
        self.sessions.clear()
        self.failures.clear()

    def valid(self, identity: str | None) -> bool:
        if not identity:
            return False
        expiry = self.sessions.get(identity)
        if expiry is None:
            return False
        if expiry <= self.clock():
            self.sessions.pop(identity, None)
            return False
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
            self.sessions[identity] = at + LIFETIME
            return identity, 200
        count += 1
        self.failures[peer] = (count, since, at + 60 if count >= 5 else 0)
        return None, 401

    def revoke(self, identity: str | None):
        if identity:
            self.sessions.pop(identity, None)
