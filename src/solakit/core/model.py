"""Single pinned oMLX client. No provider abstraction or fallback."""

import httpx

from solakit.core.config import ModelConfig


class ModelUnavailable(Exception):
    """Pinned endpoint is unreachable, invalid, or serving another model."""


class ModelClient:
    def __init__(self, config: ModelConfig, transport=None):
        self.config = config
        self.available = False
        self.client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/",
            timeout=config.timeout_s,
            transport=transport,
            trust_env=config.trust_env,
        )

    async def preflight(self) -> None:
        self.available = False
        try:
            response = await self.client.get(
                "models", timeout=min(5, self.config.timeout_s)
            )
            response.raise_for_status()
            if self.config.model_id not in [x["id"] for x in response.json()["data"]]:
                raise ModelUnavailable("Pinned model is not served")
            self.available = True
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ModelUnavailable("Model endpoint unavailable") from exc

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        try:
            request = {
                "model": self.config.model_id,
                "messages": messages,
                "stream": False,
            }
            if tools:
                request["tools"] = tools
            response = await self.client.post("chat/completions", json=request)
            response.raise_for_status()
            data = response.json()
            if data.get("model") != self.config.model_id:
                raise ModelUnavailable("Completion model does not match pinned ID")
            message = data["choices"][0]["message"]
            if message.get("role") != "assistant":
                raise ModelUnavailable("Invalid completion message")
            if not isinstance(message.get("content"), (str, type(None))):
                raise ModelUnavailable("Invalid completion content")
            calls = message.get("tool_calls")
            if calls is not None:
                if not isinstance(calls, list):
                    raise ModelUnavailable("Invalid tool call envelope")
                ids = set()
                for call in calls:
                    if (
                        not isinstance(call, dict)
                        or not isinstance(call.get("id"), str)
                        or call["id"] in ids
                        or call.get("type") != "function"
                        or not isinstance(call.get("function"), dict)
                        or not isinstance(call["function"].get("name"), str)
                        or not isinstance(call["function"].get("arguments"), str)
                    ):
                        raise ModelUnavailable("Invalid tool call envelope")
                    ids.add(call["id"])
            if not calls and message.get("content") is None:
                raise ModelUnavailable("Empty completion")
            self.available = True
            return data
        except ModelUnavailable:
            self.available = False
            raise
        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            AttributeError,
        ) as exc:
            self.available = False
            raise ModelUnavailable("Model completion failed") from exc

    async def close(self) -> None:
        await self.client.aclose()
