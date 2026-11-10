"""Declared dependencies for UI route modules and their internal API client."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any

import httpx
from fastapi import FastAPI, Request
from starlette.responses import Response


class RejectCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request) -> bool:
        return False


def internal_client(app: FastAPI) -> httpx.AsyncClient:
    # Authentication is request-local. In particular a Set-Cookie response from
    # the inner middleware must never authenticate a later, unrelated request.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://solakit.internal",
        timeout=None,
        trust_env=False,
        cookies=CookieJar(policy=RejectCookies()),
    )


@dataclass(frozen=True)
class UIContext:
    logged_in: Callable[[Request], bool]
    login_redirect: Callable[[Request], Response]
    render: Callable[..., Response]
    form: Callable[..., Awaitable[dict]]
    api: Callable[..., Awaitable[Any]]
    all_runs: Callable[..., Awaitable[list[dict]]]
    health: Callable[[Request], Awaitable[dict]]
    schema_views: Callable[[list[dict]], list[dict]]
    session_page: Callable[..., Awaitable[Response]]
    conversation: Callable[..., Awaitable[tuple[dict, list[dict]]]]
    chat_context: Callable[..., Awaitable[dict]]
