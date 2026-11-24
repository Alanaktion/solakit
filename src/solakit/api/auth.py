"""Single-operator logins; raw credentials and tokens never enter evidence."""

import hashlib
import hmac
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from solakit.config import HTTPSettings
from solakit.storage.store import Store

COOKIE = "sola_session"
CSRF_COOKIE = "sola_csrf"


class RequestError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def hashed(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Login:
    token_hash: str
    expires_at: float
    cookie: bool


class AuthService:
    def __init__(
        self, store: Store, settings: HTTPSettings, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.store, self.settings, self.clock = store, settings, clock

    def login(self, password: str) -> tuple[str, str, float]:
        now = self.clock()
        row = self.store.db.execute("SELECT * FROM login_throttle WHERE id=1").fetchone()
        failures = row["failures"] if row and now < row["window_start"] + 60 else 0
        if failures >= 5:
            raise RequestError(429, "login_throttled", "Retry after the login window")
        expected = os.getenv(self.settings.password_env, "")
        if not expected:
            raise RequestError(503, "login_unconfigured", "Operator login is not configured")
        if not hmac.compare_digest(hashed(password), hashed(expected)):
            with self.store.db:
                self.store.db.execute(
                    "INSERT INTO login_throttle VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "failures=excluded.failures,window_start=excluded.window_start",
                    (failures + 1, row["window_start"] if failures else now),
                )
            raise RequestError(401, "unauthorized", "Invalid operator credential")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = now + self.settings.session_seconds
        with self.store.db:
            self.store.db.execute("DELETE FROM login_throttle")
            self.store.db.execute(
                "DELETE FROM login_sessions WHERE expires_at<=? OR revoked=1", (now,)
            )
            self.store.db.execute(
                "INSERT INTO login_sessions VALUES (?,?,?,0)",
                (hashed(token), hashed(csrf), expires),
            )
        return token, csrf, expires

    def authenticate(self, cookie: str | None, authorization: str | None) -> Login:
        if cookie is not None and authorization is not None:
            raise RequestError(400, "ambiguous_auth", "Use cookie or bearer authentication")
        token = cookie
        if authorization is not None:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token or " " in token:
                raise RequestError(401, "unauthorized", "Valid authentication is required")
        if not token or len(token) > 256:
            raise RequestError(401, "unauthorized", "Valid authentication is required")
        login = Login(hashed(token), 0, cookie is not None)
        return self.validate(login)

    def validate(self, login: Login) -> Login:
        row = self.store.db.execute(
            "SELECT * FROM login_sessions WHERE token_hash=?", (login.token_hash,)
        ).fetchone()
        if row is None or row["revoked"] or row["expires_at"] <= self.clock():
            raise RequestError(401, "unauthorized", "Valid authentication is required")
        return Login(login.token_hash, row["expires_at"], login.cookie)

    def csrf(self, login: Login, value: str | None) -> None:
        row = self.store.db.execute(
            "SELECT csrf_hash FROM login_sessions WHERE token_hash=?", (login.token_hash,)
        ).fetchone()
        if not value or len(value) > 256 or not hmac.compare_digest(row[0], hashed(value)):
            raise RequestError(403, "csrf_failed", "Valid CSRF header is required")

    def logout(self, login: Login, *, all_sessions: bool = False) -> None:
        with self.store.db:
            if all_sessions:
                self.store.db.execute("UPDATE login_sessions SET revoked=1")
            else:
                self.store.db.execute(
                    "UPDATE login_sessions SET revoked=1 WHERE token_hash=?", (login.token_hash,)
                )
