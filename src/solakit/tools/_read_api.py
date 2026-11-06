"""Private shared transport seam for read-only API tools."""

import json

import httpx

from solakit.tools import ToolContext, ToolError, ToolResult
from solakit.tools._remote import RemoteTools


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


def encode_result(result: dict, cap: int) -> str:
    """Fit structured results before registry truncation, recording every omission."""
    while True:
        encoded = json.dumps(result, ensure_ascii=False)
        if len(encoded) <= cap:
            return encoded
        choices = []

        def visit(value):
            if isinstance(value, dict):
                for key, item in list(value.items()):
                    if isinstance(item, str) and len(item) > 64:
                        choices.append((len(json.dumps(item)), value, key, item))
                    elif isinstance(item, list) and item:
                        choices.append((len(json.dumps(item)), value, key, item))
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(result)
        if not choices:
            raise ToolError("Output cap too small")
        _, parent, key, item = max(choices, key=lambda choice: choice[0])
        parent[key] = item[: len(item) // 2]
        parent[key + "_truncated"] = True


async def invoke(ctx: ToolContext, operation: str, **arguments) -> ToolResult:
    try:
        async with client() as http:
            result = await getattr(RemoteTools(http, ctx), operation)(**arguments)
        cap = ctx.config.profiles[ctx.profile].tool_output_max_chars
        if operation in {"notifications", "pr_status", "latest_release"}:
            cap = min(cap, 12000)
        return ToolResult(content=encode_result(result, cap))
    except httpx.InvalidURL:
        raise ToolError(
            "Remote service unavailable: invalid URL or proxy configuration"
        ) from None
    except (httpx.HTTPError, ValueError, OSError) as exc:
        if isinstance(exc, ValueError):
            raise ToolError(str(exc)) from None
        raise ToolError("Remote service unavailable") from None
