"""Bounded Markdown extraction through Cloudflare Browser Run."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from ipaddress import ip_address
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit

import httpx2
from pydantic import Field, HttpUrl, field_validator

from solakit.records import ErrorCode
from solakit.redaction import Redactor
from solakit.sdk import ToolArguments, ToolError
from solakit.storage.evidence import checked_json

if TYPE_CHECKING:
    from solakit.config import WebFetchSettings

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
MAX_RESPONSE_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 30


class WebFetch(ToolArguments):
    url: Annotated[str, Field(max_length=2048, pattern=r"(?i)^https?://")]

    @field_validator("url")
    @classmethod
    def page_url(cls, value: str) -> str:
        error = "Expected a public HTTP(S) URL without credentials"
        if "\\" in value or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise ValueError(error)
        authority = urlsplit(value).netloc
        if not authority or "@" in authority:
            raise ValueError(error)
        # Match browser URL parsing, including alternative spellings of IPv4 addresses.
        url = HttpUrl(value)
        host = (url.host or "").strip("[]").rstrip(".")
        try:
            address = ip_address(host)
        except ValueError:
            if "." not in host or host.endswith((".localhost", ".local")):
                raise ValueError(error) from None
        else:
            if not address.is_global or address.is_multicast:
                raise ValueError(error)
        if url.port == 0:
            raise ValueError(error)
        return str(url)


class CloudflareMarkdown:
    def __init__(self, redactor: Redactor) -> None:
        self.redactor = redactor
        self.transport: httpx2.AsyncBaseTransport | None = None

    async def fetch(self, args: WebFetch, authorize: Callable[[], WebFetchSettings]) -> str:
        try:
            async with (
                asyncio.timeout(TIMEOUT_SECONDS),
                httpx2.AsyncClient(
                    timeout=TIMEOUT_SECONDS,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                ) as client,
            ):
                settings = authorize()
                token = os.environ.get(settings.api_token_env, "")
                if not token.strip():
                    raise ToolError("Cloudflare API token is missing", ErrorCode.CONFIGURATION)
                self.redactor.register(token)
                if self.redactor.redact(args.url) != args.url:
                    raise ToolError("Page URL contains a credential", ErrorCode.INVALID_INPUT)
                async with client.stream(
                    "POST",
                    f"{CLOUDFLARE_API}/accounts/{settings.account_id}/browser-run/markdown",
                    json={"url": args.url},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    if response.status_code == 429:
                        raise ToolError("Cloudflare Browser Run rate limit reached")
                    if response.status_code != 200:
                        raise ToolError(
                            f"Cloudflare Browser Run returned HTTP {response.status_code}"
                        )
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise ToolError("Cloudflare Browser Run returned an unexpected encoding")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ToolError(
                                "Cloudflare Browser Run response exceeds its byte limit"
                            )
                        raw.extend(chunk)
        except httpx2.TimeoutException, TimeoutError:
            raise ToolError("Cloudflare Browser Run request timed out", ErrorCode.TIMEOUT) from None
        except httpx2.HTTPError:
            raise ToolError("Cloudflare Browser Run request failed") from None
        return _markdown(bytes(raw))


def _markdown(raw: bytes) -> str:
    error = "Malformed Cloudflare Browser Run response"
    try:
        payload = checked_json(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("success"), bool):
            raise ToolError(error)
        if not payload["success"]:
            raise ToolError("Cloudflare Browser Run could not extract Markdown")
        markdown = payload.get("result")
        if not isinstance(markdown, str):
            raise ToolError(error)
        markdown.encode("utf-8")
    except ValueError, RecursionError:
        raise ToolError(error) from None
    return markdown
