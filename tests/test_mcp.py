import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from test_tools import changed, opened

from solakit.records import ErrorCode

URL = "https://mcp.example/mcp"
TOKEN_ENV = "TEST_SOLA_MCP_TOKEN"
TOKEN = "Bearer synthetic-mcp-credential"
SERVERS = {
    "wiki": {"url": URL, "headers": {"X-Team": "a"}, "headers_env": {"Authorization": TOKEN_ENV}}
}


def server(result: Any, *, sse: bool) -> tuple[httpx2.MockTransport, list[Any]]:
    seen: list[Any] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["Authorization"] == TOKEN
        assert request.headers["X-Team"] == "a"
        body = json.loads(request.content)
        seen.append(body)
        if body["method"] == "initialize":
            return httpx2.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"protocolVersion": "2025-06-18"},
                },
                headers={"Mcp-Session-Id": "s1"},
            )
        assert request.headers["Mcp-Session-Id"] == "s1"
        assert request.headers["MCP-Protocol-Version"] == "2025-06-18"
        if "id" not in body:
            return httpx2.Response(202)
        reply = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if not sse:
            return httpx2.Response(200, json=reply)
        ping = {"jsonrpc": "2.0", "id": 99, "method": "ping"}
        stream = f"event: message\ndata: {json.dumps(ping)}\n\ndata: {json.dumps(reply)}\n\n"
        return httpx2.Response(200, text=stream, headers={"Content-Type": "text/event-stream"})

    return httpx2.MockTransport(handle), seen


@pytest.mark.parametrize("sse", [False, True])
def test_list_and_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sse: bool) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    with opened(tmp_path, mcp_servers=SERVERS, grants=["mcp.*"]) as h:
        assert h.session.eligible() == {"mcp.list", "mcp.call"}
        assert TOKEN_ENV not in json.dumps(h.profile.explain())
        tools = {"tools": [{"name": "search", "inputSchema": {"type": "object"}}]}
        h.dispatcher.mcp.transport, seen = server(tools, sse=sse)
        receipt = h.dispatch("mcp.list", server="wiki")
        assert receipt.status == "succeeded" and receipt.effect == "none"
        assert isinstance(receipt.result, str) and json.loads(receipt.result) == tools
        assert [m["method"] for m in seen] == [
            "initialize",
            "notifications/initialized",
            "tools/list",
        ]

        reply = {"content": [{"type": "text", "text": f"hit {TOKEN}"}]}
        h.dispatcher.mcp.transport, seen = server(reply, sse=sse)
        receipt = h.dispatch("mcp.call", server="wiki", tool="search", arguments={"q": "x"})
        assert receipt.status == "succeeded" and receipt.effect == "confirmed"
        assert seen[-1]["params"] == {"name": "search", "arguments": {"q": "x"}}
        assert isinstance(receipt.result, str) and TOKEN not in receipt.result

        h.dispatcher.mcp.transport, _ = server(
            {"isError": True, "content": [{"type": "text", "text": "no such page"}]}, sse=sse
        )
        receipt = h.dispatch("mcp.call", server="wiki", tool="search")
        assert receipt.status == "failed" and receipt.failure
        assert "no such page" in receipt.failure.message
        assert all(TOKEN.encode() not in path.read_bytes() for path in h.store.log.paths())


@pytest.mark.parametrize("case", ["disabled", "unknown", "missing", "changed", "unauthorized"])
def test_denied_or_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    if case != "missing":
        monkeypatch.setenv(TOKEN_ENV, TOKEN)
    with opened(
        tmp_path, mcp_servers=None if case == "disabled" else SERVERS, grants=["mcp.*"]
    ) as h:
        if case == "changed":
            h.dispatcher.current = lambda admitted: changed(
                h.profile, mcp_servers={"wiki": {"url": "https://other.example/mcp"}}
            )

        def handle(request: httpx2.Request) -> httpx2.Response:
            if case != "unauthorized":
                pytest.fail("unexpected request")
            return httpx2.Response(401)

        h.dispatcher.mcp.transport = httpx2.MockTransport(handle)
        receipt = h.dispatch("mcp.list", server="other" if case == "unknown" else "wiki")
        assert receipt.failure is not None and receipt.effect == "none"
        expected = {
            "disabled": ErrorCode.TOOL_DENIED,
            "changed": ErrorCode.TOOL_DENIED,
            "unknown": ErrorCode.INVALID_INPUT,
            "missing": ErrorCode.CONFIGURATION,
            "unauthorized": ErrorCode.CONFIGURATION,
        }[case]
        assert receipt.failure.code == expected
