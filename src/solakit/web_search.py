"""Bounded, read-only access to the Brave Web Search API."""

import asyncio
import os
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Annotated
from urllib.parse import urlsplit

import httpx2
from pydantic import Field, JsonValue, field_validator

from solakit.evidence import checked_json
from solakit.records import ErrorCode
from solakit.redaction import Redactor
from solakit.sdk import ToolArguments, ToolError

BRAVE_WEB_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_URL_LENGTH = 2048
TIMEOUT_SECONDS = 10


class WebSearch(ToolArguments):
    query: Annotated[str, Field(min_length=1, max_length=600, pattern=r"\S")]
    limit: Annotated[int, Field(ge=1, le=10)] = 5

    @field_validator("query")
    @classmethod
    def query_words(cls, value: str) -> str:
        if not 1 <= len(value.split()) <= 75:
            raise ValueError("Search query must have 1–75 words")
        return value.strip()


class _Excerpt(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(text: str) -> str:
    parser = _Excerpt()
    parser.feed(text)
    parser.close()
    return " ".join("".join(parser.parts).split())


def _hit(row: object, redactor: Redactor) -> dict[str, JsonValue] | None:
    if not isinstance(row, dict):
        return None
    title, url, snippet = row.get("title"), row.get("url"), row.get("description")
    if not isinstance(title, str) or not isinstance(url, str):
        return None
    if snippet is not None and not isinstance(snippet, str):
        return None
    if (
        len(url) > MAX_URL_LENGTH
        or any(char.isspace() or ord(char) <= 32 or ord(char) == 127 for char in url)
        or "\\" in url
        or redactor.redact(url) != url
    ):
        return None
    try:
        link = urlsplit(url)
        if (
            link.scheme not in ("http", "https")
            or not link.hostname
            or link.username is not None
            or link.port == 0
        ):
            return None
        url.encode()
        title = redactor.redact(_plain_text(title))[:160]
        snippet = redactor.redact(_plain_text(snippet or ""))[:400]
        title.encode()
        snippet.encode()
    except ValueError:
        return None
    return {"title": title, "url": url, "snippet": snippet} if title else None


def _results(raw: bytes, limit: int, redactor: Redactor) -> list[JsonValue]:
    error = "Malformed Brave Search response"
    try:
        payload = checked_json(raw)
    except ValueError, RecursionError:
        raise ToolError(error) from None
    if not isinstance(payload, dict) or payload.get("type") != "search":
        raise ToolError(error)
    web = payload.get("web")
    if web is None:
        return []
    if not isinstance(web, dict) or not isinstance(web.get("results"), list):
        raise ToolError(error)
    results: list[JsonValue] = []
    for row in web["results"]:
        hit = _hit(row, redactor)
        if hit is not None:
            results.append(hit)
            if len(results) == limit:
                break
    return results


class BraveSearch:
    def __init__(self, redactor: Redactor) -> None:
        self.redactor = redactor
        self.transport: httpx2.AsyncBaseTransport | None = None

    async def search(self, args: WebSearch, authorize: Callable[[], str | None]) -> list[JsonValue]:
        try:
            async with (
                asyncio.timeout(TIMEOUT_SECONDS),
                httpx2.AsyncClient(
                    timeout=TIMEOUT_SECONDS,
                    verify=True,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                ) as client,
            ):
                env = authorize()
                credential = os.environ.get(env, "") if env else ""
                if not credential.strip():
                    raise ToolError("Brave Search credential is missing", ErrorCode.CONFIGURATION)
                self.redactor.register(credential)
                async with client.stream(
                    "GET",
                    BRAVE_WEB_SEARCH_URL,
                    params={"q": args.query, "count": args.limit, "result_filter": "web"},
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "X-Subscription-Token": credential,
                    },
                ) as response:
                    if response.status_code == 429:
                        raise ToolError("Brave Search rate limit reached")
                    if response.status_code != 200:
                        raise ToolError(
                            f"Brave Search provider returned HTTP {response.status_code}"
                        )
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise ToolError("Brave Search returned an unexpected content encoding")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ToolError("Brave Search response exceeds its byte limit")
                        raw.extend(chunk)
        except httpx2.TimeoutException, TimeoutError:
            raise ToolError("Brave Search request timed out", ErrorCode.TIMEOUT) from None
        except httpx2.HTTPError:
            raise ToolError("Brave Search provider request failed") from None
        return _results(bytes(raw), args.limit, self.redactor)
