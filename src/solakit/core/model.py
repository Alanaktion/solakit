"""Single pinned oMLX client. No provider abstraction or fallback."""

import asyncio
import hashlib
import json
import logging
import re
import time

import httpx

from solakit.core.config import ModelConfig, validate_api_token
from solakit.core.io import durable
from solakit.core.media import Media, MediaError
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

    evidence: dict | None = None


class EmptyCompletion(ModelUnavailable):
    """Healthy endpoint stopped without visible text or tool calls."""


class ReasoningExhausted(ModelUnavailable):
    """Reasoning reached its cap before any visible answer or tool call.

    The endpoint is healthy; the caller may retry once with thinking off."""


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
        self.media: Media | None = None
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

    async def complete(  # noqa: C901
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        on_text=None,
        params: dict | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
        max_reasoning_chars: int | None = None,
        diagnostic_redactor: Redactor | None = None,
        transport_stream: bool = True,
    ) -> dict:
        started = self._begin(
            "completion POST", "chat/completions", self.config.timeout_s
        )
        data, observation, request = None, {}, {}
        diagnostic_redactor = diagnostic_redactor or self.redactor
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
                "max_tokens": max_tokens or self.config.max_tokens,
            }
            request["stream"] = transport_stream
            if transport_stream:
                request["stream_options"] = {"include_usage": True}
            # Configured sampling/thinking keys; validation excludes reserved keys.
            request.update(params or {})
            if tools:
                request["tools"] = tools
                if tool_choice is not None:
                    request["tool_choice"] = tool_choice
            else:
                request["tool_choice"] = "none"
            from solakit.core.model_stream import complete_stream

            # The HTTP body is always read incrementally and cancellation closes
            # it, including JSON-mode recovery from a server's broken SSE output.
            async with asyncio.timeout(self.config.timeout_s):
                data = await complete_stream(
                    self.client,
                    request,
                    on_text,
                    max_reasoning_chars=max_reasoning_chars,
                    observation=observation,
                )
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
            if (
                self.config.tool_call_fallback
                and tools
                and not calls
                and data["choices"][0].get("finish_reason") != "length"
                and message.get("content")
            ):
                from solakit.core.toolcall_fallback import parse

                offered = {tool["function"]["name"] for tool in tools}
                recovered = parse(message["content"], offered)
                if recovered:
                    calls = message["tool_calls"] = recovered
                    message["tool_call_source"] = "content_fallback"
                    data["choices"][0]["finish_reason"] = "tool_calls"
            if (
                not calls
                and not (message.get("content") or "").strip()
                and message.get("reasoning")
                and data["choices"][0].get("finish_reason") != "length"
            ):
                # Healthy endpoint, but only reasoning came back: retryable.
                raise ReasoningExhausted("Completion contained only reasoning")
            # A stream with neither text nor calls assembles to "". Only a
            # token-limited response may be empty; it is kept as partial evidence.
            if (
                not calls
                and not (message.get("content") or "").strip()
                and data["choices"][0].get("finish_reason") != "length"
            ):
                raise EmptyCompletion("Empty completion")
            self.available = True
            LOGGER.debug(
                "Model completion succeeded: elapsed_ms=%.1f",
                (time.monotonic() - started) * 1000,
            )
            return data
        except ModelUnavailable as exc:
            exc.evidence = self.rejection_evidence(
                request,
                data if data is not None else observation,
                str(exc),
                diagnostic_redactor,
            )
            self.available = isinstance(exc, (ReasoningExhausted, EmptyCompletion))
            if not self.available:
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
            failure = ModelUnavailable("Model completion failed")
            failure.evidence = self.rejection_evidence(
                request,
                data if data is not None else observation,
                type(exc).__name__,
                diagnostic_redactor,
            )
            raise failure from exc

    def rejection_evidence(
        self, request: dict, response, error: str, redactor: Redactor
    ) -> dict:
        """Bound private diagnostic output after masking, without request text."""
        redactor = Redactor([*self.redactor.values, *redactor.values])
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True).encode()
        payload = json.dumps(redactor.redact(response), ensure_ascii=False)
        limit = 32768
        return {
            "validation_error": redactor.redact(error),
            "request_sha256": hashlib.sha256(encoded).hexdigest(),
            "request_params": redactor.redact(
                {
                    key: value
                    for key, value in request.items()
                    if key not in ("messages", "tools")
                }
            ),
            "message_count": len(request.get("messages", [])),
            "tool_names": [t["function"]["name"] for t in request.get("tools", [])],
            "response_json": payload[:limit],
            "response_chars": len(payload),
            "response_truncated": len(payload) > limit,
        }

    async def close(self) -> None:
        await self.client.aclose()
