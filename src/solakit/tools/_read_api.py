"""Private shared transport seam for read-only API tools."""

import json

import httpx

from solakit.tools import ToolContext, ToolError, ToolResult
from solakit.tools._remote import RemoteTools


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


async def invoke(ctx: ToolContext, operation: str, **arguments) -> ToolResult:
    try:
        async with client() as http:
            result = await getattr(RemoteTools(http, ctx), operation)(**arguments)
        return ToolResult(content=json.dumps(result, ensure_ascii=False))
    except (httpx.HTTPError, ValueError, OSError) as exc:
        if isinstance(exc, ValueError):
            raise ToolError(str(exc)) from None
        raise ToolError("Remote service unavailable") from None
