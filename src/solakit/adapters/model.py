"""Pinned oMLX boundary. SDK transport, inert tool calls, no implicit retries."""

import asyncio
import base64
import logging
import os
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

import httpx2
from openai import APIError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import Field, JsonValue

from solakit.config import EffectiveProfile
from solakit.engine.events import Previews
from solakit.engine.prompts import PreparedPrompt, inspect_prompt
from solakit.records import (
    DOCUMENT_TEXT_BYTES,
    Artifact,
    ErrorCode,
    Failure,
    ModelRequest,
    ModelStep,
    Origin,
    ProviderID,
    Run,
    ToolProposal,
    Value,
    new_id,
)
from solakit.redaction import Redactor
from solakit.storage.evidence import canonical, checked_json
from solakit.storage.store import Store

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DOCUMENT_CONVERTER = "MarkItDown"
DOCUMENT_EXTENSIONS = (".pdf", ".docx", ".pptx")
VISION_RESPONSE_BYTES = 64 * 1024
VISION_OBSERVATION_BYTES = 4096


class ModelError(ValueError):
    def __init__(
        self,
        message: str,
        code: ErrorCode = ErrorCode.INVALID_MODEL_RESPONSE,
        *,
        values: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.values = values or {}


class NormalizedCall(Value):
    provider_tool_call_id: ProviderID
    name: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.-]+$")
    arguments: dict[str, JsonValue]


class ModelResult(Value):
    step: ModelStep
    calls: tuple[NormalizedCall, ...] = ()


def _identity(data: dict[str, Any], pin: str) -> None:
    if data.get("model") is not None and data["model"] != pin:
        raise ModelError("Returned model differs from the pin", ErrorCode.MODEL_MISMATCH)


def normalize(data: dict[str, Any], pin: str) -> dict[str, Any]:
    """Only explicit reasoning fields are reasoning; quoted/code tags stay literal."""
    _identity(data, pin)
    provider_id = data.get("id")
    if provider_id is not None and (
        not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 256
    ):
        raise ModelError("Invalid response ID")
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ModelError("Expected one model choice")
    choice = choices[0]
    if (
        not isinstance(choice, dict)
        or type(choice.get("index", 0)) is not int
        or choice.get("index", 0) != 0
    ):
        raise ModelError("Invalid model choice")
    finish = choice.get("finish_reason")
    if finish is not None and not isinstance(finish, str):
        raise ModelError("Invalid finish reason")
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ModelError("Invalid assistant message")
    content = message.get("content")
    if content is None:
        content = ""
    reasoning = message.get("reasoning_content")
    if reasoning is None:
        reasoning = message.get("reasoning")
    if reasoning is None:
        reasoning = ""
    if not isinstance(content, str) or not isinstance(reasoning, str):
        raise ModelError("Invalid content or reasoning")
    usage = data.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise ModelError("Invalid usage")
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if key in usage and (type(usage[key]) is not int or usage[key] < 0):
                raise ModelError("Invalid token usage")
        canonical(usage)
    values: dict[str, Any] = {
        "content": content,
        "reasoning": reasoning,
        "finish_reason": finish,
        "provider_call_id": provider_id,
        "usage": usage,
    }
    if finish not in ("stop", "tool_calls"):
        raise ModelError("Model response is incomplete", values=values)
    raw_calls = message.get("tool_calls")
    if raw_calls is None:
        raw_calls = []
    if not isinstance(raw_calls, list) or len(raw_calls) > 64:
        raise ModelError("Invalid tool calls")
    proposals = []
    for raw in raw_calls:
        if not isinstance(raw, dict) or raw.get("type") != "function":
            raise ModelError("Invalid tool call type")
        function = raw.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("arguments"), str):
            raise ModelError("Invalid function arguments")
        proposals.append(
            ToolProposal.model_validate(
                {
                    "provider_tool_call_id": raw.get("id"),
                    "name": function.get("name"),
                    "arguments": function["arguments"],
                }
            )
        )
    if len({call.provider_tool_call_id for call in proposals}) != len(proposals):
        raise ModelError("Duplicate tool call IDs")
    if bool(proposals) != (finish == "tool_calls"):
        raise ModelError("Tool calls and finish reason disagree")
    values["tool_calls"] = tuple(proposals)
    calls = []
    try:
        for proposal in proposals:
            arguments = checked_json(proposal.arguments.encode())
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            canonical(arguments)
            calls.append(
                NormalizedCall(
                    provider_tool_call_id=proposal.provider_tool_call_id,
                    name=proposal.name,
                    arguments=arguments,
                )
            )
    except ValueError, TypeError, UnicodeError:
        raise ModelError("Malformed tool arguments; no calls executed", values=values) from None
    if not calls and not content.strip():
        raise ModelError("Model stopped without visible content or tool calls", values=values)
    return values | {"calls": tuple(calls)}


async def _events(chunks: AsyncIterator[bytes], raw: bytearray) -> AsyncIterator[bytes]:
    """Frame SSE before UTF-8/JSON decoding, including split multibyte characters."""
    pending = b""
    lines: list[bytes] = []
    async for chunk in chunks:
        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
            raise ModelError("Model response exceeds size limit")
        raw.extend(chunk)
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            line = line.removesuffix(b"\r")
            if not line:
                if lines:
                    yield b"\n".join(lines)
                    lines.clear()
            elif line.startswith(b"data:"):
                lines.append(line[5:].removeprefix(b" "))
    raise ModelError("Model stream ended without [DONE]")


async def assemble_stream(
    chunks: AsyncIterator[bytes],
    pin: str,
    raw: bytearray,
    preview: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    content, reasoning = "", ""
    calls: dict[int, dict[str, Any]] = {}
    finish = None
    provider_id = None
    usage = None
    async for event in _events(chunks, raw):
        if event == b"[DONE]":
            if finish is None:
                raise ModelError("Model stream ended before finish reason")
            if sorted(calls) != list(range(len(calls))):
                raise ModelError("Noncontiguous streamed calls")
            return {
                "id": provider_id,
                "model": pin,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": finish,
                        "message": {
                            "role": "assistant",
                            "content": content,
                            "reasoning_content": reasoning,
                            "tool_calls": [calls[i] for i in sorted(calls)],
                        },
                    }
                ],
                "usage": usage,
            }
        data = checked_json(event)
        if not isinstance(data, dict) or "error" in data:
            raise ModelError("Invalid model stream event")
        # oMLX emits this exact empty heartbeat before the model starts generating.
        if isinstance(data.get("id"), str) and data == {
            "id": data["id"],
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "keepalive",
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}
            ],
        }:
            continue
        _identity(data, pin)
        if data.get("id") is not None:
            if provider_id is not None and provider_id != data["id"]:
                raise ModelError("Stream response ID changed")
            provider_id = data["id"]
        if data.get("usage") is not None:
            usage = data["usage"]
        choices = data.get("choices")
        if choices == [] and data.get("usage") is not None:
            continue
        if not isinstance(choices, list) or len(choices) != 1 or finish is not None:
            raise ModelError("Invalid stream choices or content after finish")
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("index") != 0:
            raise ModelError("Invalid streamed choice")
        delta = choice.get("delta")
        if not isinstance(delta, dict) or delta.get("role") not in (None, "assistant"):
            raise ModelError("Invalid streamed role")
        for key in ("content", "reasoning_content", "reasoning"):
            value = delta.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise ModelError("Invalid streamed text")
                if key == "content":
                    content += value
                    if preview is not None and value:
                        preview(content)
                elif key != "reasoning" or delta.get("reasoning_content") is None:
                    reasoning += value
        fragments = delta.get("tool_calls")
        if fragments is None:
            fragments = []
        if not isinstance(fragments, list):
            raise ModelError("Invalid streamed tool calls")
        for fragment in fragments:
            if not isinstance(fragment, dict):
                raise ModelError("Invalid tool fragment")
            index = fragment.get("index")
            if type(index) is not int or not 0 <= index < 64:
                raise ModelError("Invalid streamed tool index")
            call = calls.setdefault(
                index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
            if fragment.get("type") not in (None, "function"):
                raise ModelError("Invalid streamed tool type")
            if fragment.get("id") is not None:
                if not isinstance(fragment["id"], str):
                    raise ModelError("Invalid streamed tool ID")
                if call["id"] and call["id"] != fragment["id"]:
                    raise ModelError("Streamed tool ID changed")
                call["id"] = fragment["id"]
            function = fragment.get("function")
            if function is not None:
                if not isinstance(function, dict):
                    raise ModelError("Invalid streamed function")
                for key in ("name", "arguments"):
                    value = function.get(key)
                    if value is not None:
                        if not isinstance(value, str):
                            raise ModelError("Invalid streamed function fragment")
                        call["function"][key] += value
        finish = choice.get("finish_reason")
    raise ModelError("Model stream interrupted")


class ModelAdapter:
    default_key_env = "OPENAI_API_KEY"
    response_hooks: tuple[Callable[[httpx2.Response], Any], ...] = ()

    def __init__(
        self,
        profile: EffectiveProfile,
        store: Store,
        inference: asyncio.Semaphore,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.profile, self.store, self.inference = profile, store, inference
        self.redactor = redactor or Redactor()
        self.transport = transport
        self.client: AsyncOpenAI | None = None
        self.previews = Previews()

    async def start(self) -> None:
        if self.client is not None:
            return
        # SDK debug traces include request bodies; private artifacts own that evidence.
        for name in ("openai", "openai._base_client", "httpx2", "httpcore2"):
            logging.getLogger(name).setLevel(logging.WARNING)
        key = os.getenv(self.profile.model_api_key_env or self.default_key_env)
        if self.profile.model_api_key_env and not key:
            raise ModelError("Configured model credential is missing", ErrorCode.CONFIGURATION)
        self.redactor.register(key or "")
        self.client = AsyncOpenAI(
            base_url=self.profile.model_base_url.rstrip("/") + "/",
            api_key=key or "local",
            max_retries=0,
            timeout=httpx2.Timeout(
                self.profile.model_generation_timeout_seconds,
                connect=self.profile.model_connect_timeout_seconds,
            ),
            http_client=httpx2.AsyncClient(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                event_hooks={"response": list(self.response_hooks)},
            ),
        )

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None

    async def verify(self) -> dict[str, Any]:
        if self.client is None:
            raise ModelError("Model client is not started", ErrorCode.MODEL_UNAVAILABLE)
        async with asyncio.timeout(self.profile.model_connect_timeout_seconds):
            models = await self.client.get("models", cast_to=dict[str, Any])
        if not isinstance(models, dict):
            raise ModelError("Invalid /models response")
        data = models.get("data")
        if not isinstance(data, list) or not any(
            isinstance(model, dict) and model.get("id") == self.profile.model_pin for model in data
        ):
            raise ModelError("Pinned model absent from /models", ErrorCode.MODEL_MISMATCH)
        return {
            "pin": self.profile.model_pin,
            "models": models,
            "verified": "API model ID only; weight identity unverified",
        }

    async def document_inputs(self) -> bool:
        """The advertised virtual endpoint explicitly opts in to oMLX conversion."""
        try:
            models = (await self.verify())["models"]["data"]
        except APIError, TimeoutError, ModelError:
            return False
        return any(
            isinstance(model, dict)
            and model.get("id") == DOCUMENT_CONVERTER
            and model.get("owned_by") == "omlx"
            for model in models
        )

    async def convert_document(self, filename: str, raw: bytes) -> str:
        client = self.client
        if client is None or not await self.document_inputs():
            raise ModelError(
                "Document conversion is disabled on the model API", ErrorCode.CONFIGURATION
            )
        request = {
            "model": DOCUMENT_CONVERTER,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "file",
                            "file": {
                                "filename": filename,
                                "file_data": base64.b64encode(raw).decode(),
                            },
                        }
                    ],
                }
            ],
        }
        body = bytearray()
        response = None
        try:
            async with (
                asyncio.timeout(self.profile.model_generation_timeout_seconds),
                self.inference,
            ):
                response = await client.post(
                    "chat/completions", body=request, cast_to=httpx2.Response, stream=True
                )
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > DOCUMENT_TEXT_BYTES * 6 + 4096:
                        raise ModelError("Converted document exceeds the attachment limit")
                    body.extend(chunk)
        except APIStatusError as error:
            if error.status_code in (400, 413, 415, 422):
                raise ModelError("The model API could not read this document") from None
            raise ModelError(
                "Document conversion is unavailable", ErrorCode.MODEL_UNAVAILABLE
            ) from None
        except APIError, TimeoutError:
            raise ModelError(
                "Document conversion is unavailable", ErrorCode.MODEL_UNAVAILABLE
            ) from None
        finally:
            if response is not None:
                await response.aclose()
        try:
            data = checked_json(bytes(body))
            if not isinstance(data, dict) or data.get("model") != DOCUMENT_CONVERTER:
                raise ValueError("Unexpected converter response")
            result = normalize(data, DOCUMENT_CONVERTER)
            if result["calls"]:
                raise ValueError("Unexpected converter tool calls")
            return str(result["content"])
        except ValueError:
            raise ModelError("The model API did not return readable document text") from None

    async def complete(
        self,
        prompt: PreparedPrompt,
        run: Run,
        index: int,
        *,
        refresh_prompt: Callable[[PreparedPrompt], PreparedPrompt] | None = None,
    ) -> ModelResult:
        if self.client is None:
            raise ModelError("Model client is not started", ErrorCode.MODEL_UNAVAILABLE)
        async with self.inference:
            if refresh_prompt is not None:
                prompt = refresh_prompt(prompt)
            inspected = inspect_prompt(prompt)
            request = inspected["request"]
            manifest = inspected["manifest"]
            if (
                not isinstance(request, dict)
                or request.get("model") != self.profile.model_pin
                or type(request.get("stream")) is not bool
                or not isinstance(manifest, dict)
                or manifest.get("session_id") != run.session_id
                or manifest.get("run_id") != run.id
                or manifest.get("origin") != run.origin
                or manifest.get("endpoint")
                != self.profile.model_base_url.rstrip("/") + "/chat/completions"
                or manifest.get("timeouts")
                != {
                    "connect_seconds": self.profile.model_connect_timeout_seconds,
                    "generation_seconds": self.profile.model_generation_timeout_seconds,
                }
            ):
                raise ModelError(
                    "Request differs from pinned model configuration", ErrorCode.CONFIGURATION
                )
            purpose = manifest.get("purpose")
            vision = purpose == "vision"
            cap = VISION_RESPONSE_BYTES if vision else MAX_RESPONSE_BYTES
            recorded = ModelRequest(
                origin=run.origin,
                session_id=run.session_id,
                run_id=run.id,
                index=index,
                model=self.profile.model_pin,
                body=Artifact.model_validate(self.store.artifact(new_id(), prompt.body)),
                manifest=Artifact.model_validate(self.store.artifact(new_id(), prompt.manifest)),
                purpose="vision"
                if vision
                else "compaction"
                if purpose == "compaction"
                else "conversation",
            )
            self.store.append(recorded)
            raw = bytearray()
            response = None
            values: dict[str, Any] = {}
            calls: tuple[NormalizedCall, ...] = ()
            failure: Failure | None = Failure(
                code=ErrorCode.INVALID_MODEL_RESPONSE, message="Model request did not complete"
            )
            try:
                async with asyncio.timeout(self.profile.model_generation_timeout_seconds):
                    if not vision:
                        await self.verify()
                    # Raw content prevents SDK reserialization of the recorded bytes.
                    response = cast(
                        httpx2.Response,
                        await self.client.post(
                            "chat/completions",
                            cast_to=httpx2.Response,
                            content=prompt.body,
                            options={"headers": {"Accept-Encoding": "identity"}} if vision else {},
                            stream=True,
                        ),
                    )
                    if request["stream"]:

                        def preview(text: str) -> None:
                            if (
                                run.origin == Origin.INTERACTIVE
                                and manifest.get("purpose") != "compaction"
                            ):
                                self.previews.update(run.session_id, run.id, index, text)

                        data = await assemble_stream(
                            response.aiter_bytes(), self.profile.model_pin, raw, preview
                        )
                    else:
                        async for chunk in response.aiter_bytes():
                            if len(raw) + len(chunk) > cap:
                                raw.extend(chunk[: cap - len(raw)])
                                raise ModelError("Model response exceeds size limit")
                            raw.extend(chunk)
                        decoded = checked_json(bytes(raw))
                        if not isinstance(decoded, dict):
                            raise ModelError("Invalid JSON model response")
                        data = decoded
                    if vision and data.get("model") != self.profile.model_pin:
                        raise ModelError(
                            "Vision response did not identify the pin", ErrorCode.MODEL_MISMATCH
                        )
                    normalized = normalize(data, self.profile.model_pin)
                    calls = normalized.pop("calls")
                    if vision:
                        observation = self.redactor.redact(normalized["content"].strip())
                        if calls or len(observation.encode()) > VISION_OBSERVATION_BYTES:
                            raise ModelError(
                                "Vision response exceeds observation limits or contains tools"
                            )
                        normalized.update(content=observation, reasoning="")
                    values = normalized
                    failure = None
            except asyncio.CancelledError:
                failure = Failure(code=ErrorCode.CANCELLED, message="Model request cancelled")
                raise
            except TimeoutError, APITimeoutError:
                failure = Failure(code=ErrorCode.TIMEOUT, message="Model request timed out")
            except ModelError as error:
                values = {} if vision else error.values
                failure = Failure(code=error.code, message=str(error))
            except APIStatusError as error:
                raw.extend(error.response.content[:cap])
                failure = Failure(
                    code=ErrorCode.MODEL_UNAVAILABLE,
                    message=f"Model endpoint returned HTTP {error.status_code}",
                )
            except APIError, httpx2.HTTPError:
                failure = Failure(
                    code=ErrorCode.MODEL_UNAVAILABLE, message="Model transport failed"
                )
            except ValueError, TypeError, KeyError, IndexError, UnicodeError:
                failure = Failure(
                    code=ErrorCode.INVALID_MODEL_RESPONSE, message="Malformed model response"
                )
            finally:
                try:
                    if response is not None:
                        async with asyncio.timeout(self.profile.model_connect_timeout_seconds):
                            await response.aclose()
                except Exception:
                    if failure is None:
                        failure = Failure(
                            code=ErrorCode.MODEL_UNAVAILABLE,
                            message="Model response cleanup failed",
                        )
                    raise ModelError(
                        "Model response cleanup failed", ErrorCode.MODEL_UNAVAILABLE
                    ) from None
                finally:
                    step = ModelStep(
                        origin=run.origin,
                        session_id=run.session_id,
                        run_id=run.id,
                        index=index,
                        model=self.profile.model_pin,
                        request_id=recorded.id,
                        failure=failure,
                        response=Artifact.model_validate(self.store.artifact(new_id(), bytes(raw))),
                        purpose=recorded.purpose,
                        **values,
                    )
                    self.store.append(step)
                    self.previews.latest.pop(run.id, None)
            return ModelResult(step=step, calls=calls if failure is None else ())
