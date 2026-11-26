"""Bounded MCP client over the Streamable HTTP transport."""

import asyncio
import json
from collections.abc import Callable
from typing import Annotated

import httpx2
from pydantic import Field, JsonValue

from solakit.records import ErrorCode
from solakit.sdk import ToolArguments, ToolError
from solakit.storage.evidence import checked_json

PROTOCOL_VERSION = "2025-06-18"
MAX_RESPONSE_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 30

ServerName = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$", max_length=128)]


class McpList(ToolArguments):
    server: ServerName
    cursor: Annotated[str, Field(max_length=4096)] | None = None


class McpCall(ToolArguments):
    server: ServerName
    tool: Annotated[str, Field(min_length=1, max_length=256)]
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


def _message(raw: bytes, request_id: int) -> dict[str, JsonValue] | None:
    try:
        payload = checked_json(raw)
    except ValueError, RecursionError:
        raise ToolError("Malformed MCP server response") from None
    # A JSON body may be a batch; SSE events may carry unrelated server requests.
    for item in payload if isinstance(payload, list) else [payload]:
        if isinstance(item, dict) and item.get("id") == request_id and "method" not in item:
            return item
    return None


class McpClient:
    def __init__(self) -> None:
        self.transport: httpx2.AsyncBaseTransport | None = None

    async def request(
        self,
        url: str,
        headers: dict[str, str],
        method: str,
        params: dict[str, JsonValue],
        before: Callable[[], None] = lambda: None,
    ) -> dict[str, JsonValue]:
        """One fresh session per request: initialize, notify, then the method itself."""
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
                base = headers | {
                    "Accept": "application/json, text/event-stream",
                    "Accept-Encoding": "identity",
                    "Content-Type": "application/json",
                }
                init, session = await self._post(
                    client,
                    url,
                    base,
                    1,
                    "initialize",
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "solakit", "version": "0.1.0"},
                    },
                )
                version = init.get("protocolVersion")
                base["MCP-Protocol-Version"] = (
                    version if isinstance(version, str) else PROTOCOL_VERSION
                )
                if session:
                    base["Mcp-Session-Id"] = session
                notified = await client.post(
                    url,
                    headers=base,
                    content=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                )
                if notified.status_code >= 300:
                    raise ToolError(f"MCP server returned HTTP {notified.status_code}")
                before()
                result, _ = await self._post(client, url, base, 2, method, params)
                # shortcut: sessions are left for the server to expire; send DELETE if one objects.
                return result
        except httpx2.TimeoutException, TimeoutError:
            raise ToolError("MCP server request timed out", ErrorCode.TIMEOUT) from None
        except httpx2.HTTPError:
            raise ToolError("MCP server request failed") from None

    async def _post(
        self,
        client: httpx2.AsyncClient,
        url: str,
        headers: dict[str, str],
        request_id: int,
        method: str,
        params: dict[str, JsonValue],
    ) -> tuple[dict[str, JsonValue], str | None]:
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        async with client.stream(
            "POST", url, headers=headers, content=json.dumps(body)
        ) as response:
            if response.status_code in (401, 403):
                raise ToolError(
                    f"MCP server refused authorization (HTTP {response.status_code})",
                    ErrorCode.CONFIGURATION,
                )
            if response.status_code != 200:
                raise ToolError(f"MCP server returned HTTP {response.status_code}")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ToolError("MCP server returned an unexpected content encoding")
            stream = response.headers.get("Content-Type", "").startswith("text/event-stream")
            message = None
            size = 0
            data: list[str] = []
            buffer = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ToolError("MCP server response exceeds its byte limit")
                buffer.extend(chunk)
                if not stream:
                    continue
                # SSE: an event is the joined data lines before a blank line.
                *lines, rest = buffer.split(b"\n")
                buffer = bytearray(rest)
                for line in (raw.rstrip(b"\r").decode("utf-8", "replace") for raw in lines):
                    if line.startswith("data:"):
                        data.append(line[5:].removeprefix(" "))
                    elif not line and data:
                        message = _message("\n".join(data).encode(), request_id)
                        data = []
                        if message is not None:
                            break
                if message is not None:
                    break
            if not stream:
                message = _message(bytes(buffer), request_id)
            elif message is None and data:
                message = _message("\n".join(data).encode(), request_id)
            session = response.headers.get("Mcp-Session-Id")
        if message is None:
            raise ToolError("MCP server sent no response")
        if isinstance(error := message.get("error"), dict):
            detail = error.get("message")
            raise ToolError(
                "MCP server error: " + (detail if isinstance(detail, str) else "unknown")
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise ToolError("Malformed MCP server response")
        return result, session
