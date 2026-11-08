"""Single pinned oMLX client. No provider abstraction or fallback."""

import asyncio
import logging
import re
import time

import httpx

from solakit.core.config import ModelConfig, validate_api_token
from solakit.core.io import durable
from solakit.core.media import MediaError
from solakit.core.secrets import Redactor

LOGGER = logging.getLogger(__name__)


def safe_url(value: str) -> str:
    try:
        return str(
            httpx.URL(value).copy_with(
                username="", password="", query=None, fragment=None
            )
        )
    except (ValueError, httpx.InvalidURL):
        return "<invalid endpoint>"


class ModelUnavailable(Exception):
    """Pinned endpoint is unreachable, invalid, or serving another model."""


class ModelClient:
    def __init__(
        self, config: ModelConfig, transport=None, *, api_key: str | None = None
    ):
        self.config = config
        key = (
            validate_api_token(api_key)
            if api_key is not None
            else config.resolve_api_key()
        )
        self.redactor = Redactor([key] if key else [])
        self.auth_configured = bool(key)
        self.available = False
        self.media = None
        self.client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/",
            timeout=config.timeout_s,
            transport=transport,
            trust_env=config.trust_env,
            headers={"Authorization": f"Bearer {key}"} if key else {},
        )

    def _begin(self, operation: str, path: str, timeout: float) -> float:
        endpoint = safe_url(str(self.client.base_url.join(path)))
        LOGGER.debug(
            "%s",
            self.redactor.redact(
                f"Model {operation}: endpoint={endpoint} pinned={self.config.model_id} "
                f"timeout_s={timeout} auth_configured={self.auth_configured} "
                f"trust_env={self.config.trust_env}"
            ),
        )
        return time.monotonic()

    def _failure(self, operation: str, path: str, exc: Exception) -> None:
        endpoint = safe_url(str(self.client.base_url.join(path)))
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            hint = (
                "check [model].api_key and backend token configuration"
                if status in {401, 403}
                else "check the backend URL/path and server logs"
            )
            reason = f"HTTP {status}; {hint}"
        elif isinstance(exc, (httpx.TimeoutException, TimeoutError)):
            reason = (
                "request timed out; check backend load/connectivity and model.timeout_s"
            )
        elif isinstance(exc, httpx.RequestError):
            reason = (
                "connection failed; check oMLX is listening at this host/port "
                "and model.trust_env"
            )
        else:
            reason = (
                "backend response rejected; check the pinned model ID "
                "and API response shape"
            )
        LOGGER.warning(
            "%s",
            self.redactor.redact(
                f"Model {operation} failed: endpoint={endpoint} "
                f"error={type(exc).__name__}; {reason}"
            ),
        )
        # Never log headers or model payloads. Sanitize URLs and the configured key
        # even if the transport's exception text embeds either of them.
        detail = re.sub(
            r"https?://[^\s<>\"']+", lambda match: safe_url(match[0]), str(exc)
        )
        LOGGER.debug(
            "%s", self.redactor.redact(f"Model {operation} diagnostic: {detail}")
        )

    async def preflight(self) -> None:
        self.available = False
        started = self._begin("preflight GET", "models", min(5, self.config.timeout_s))
        try:
            response = await self.client.get(
                "models", timeout=min(5, self.config.timeout_s)
            )
            response.raise_for_status()
            served = [x["id"] for x in response.json()["data"]]
            if self.config.model_id not in served:
                LOGGER.debug(
                    "%s", self.redactor.redact(f"Served model IDs: {served[:20]}")
                )
                raise ModelUnavailable("Pinned model is not served")
            self.available = True
            LOGGER.debug(
                "Model preflight succeeded: elapsed_ms=%.1f",
                (time.monotonic() - started) * 1000,
            )
        except ModelUnavailable as exc:
            self._failure("preflight GET", "models", exc)
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            self._failure("preflight GET", "models", exc)
            raise ModelUnavailable("Model endpoint unavailable") from exc

    async def complete(
        self, messages: list[dict], tools: list[dict], *, on_text=None
    ) -> dict:
        started = self._begin(
            "completion POST", "chat/completions", self.config.timeout_s
        )
        try:
            if any(message.get("images") for message in messages):
                if self.media is None:
                    raise ModelUnavailable("Image input storage is not configured")
                try:
                    messages = await durable(self.media.project, messages)
                except MediaError as exc:
                    raise ModelUnavailable(str(exc)) from exc
            request = {
                "model": self.config.model_id,
                "messages": messages,
                "max_tokens": self.config.max_tokens,
            }
            if tools:
                request["tools"] = tools
            else:
                request["tool_choice"] = "none"
            from solakit.core.model_stream import complete_stream

            # Always stream the transport so closing it on cancellation signals
            # disconnect to the generator, even when no UI preview is requested.
            async with asyncio.timeout(self.config.timeout_s):
                data = await complete_stream(self.client, request, on_text)
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
                        or not call["id"]
                        or call["id"] in ids
                        or call.get("type") != "function"
                        or not isinstance(call.get("function"), dict)
                        or not isinstance(call["function"].get("name"), str)
                        or not call["function"]["name"]
                        or not isinstance(call["function"].get("arguments"), str)
                    ):
                        raise ModelUnavailable("Invalid tool call envelope")
                    ids.add(call["id"])
            if not calls and message.get("content") is None:
                raise ModelUnavailable("Empty completion")
            self.available = True
            LOGGER.debug(
                "Model completion succeeded: elapsed_ms=%.1f",
                (time.monotonic() - started) * 1000,
            )
            return data
        except ModelUnavailable as exc:
            self.available = False
            self._failure("completion POST", "chat/completions", exc)
            raise
        except (
            httpx.HTTPError,
            TimeoutError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            AttributeError,
        ) as exc:
            self.available = False
            self._failure("completion POST", "chat/completions", exc)
            raise ModelUnavailable("Model completion failed") from exc

    async def close(self) -> None:
        await self.client.aclose()
