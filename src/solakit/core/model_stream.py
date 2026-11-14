"""Bounded OpenAI SSE assembly. Tool arguments stay inert until full validation."""

import json

from solakit.core.model import ModelUnavailable, ReasoningExhausted

MAX_STREAM_BYTES = 2 * 1024 * 1024
THINK_OPEN, THINK_CLOSE = "<think>", "</think>"


def split_think(raw: str) -> tuple[str, str]:
    """Split a leading ``<think>…</think>`` from content: (reasoning, visible).

    Servers that do not separate reasoning emit it inline. Only a block at the
    very start counts; an unclosed block is all reasoning so far."""
    stripped = raw.lstrip()
    if not stripped.startswith(THINK_OPEN):
        if THINK_OPEN.startswith(stripped) and stripped:
            return "", ""  # A partial "<thi" prefix: undecided, show nothing yet.
        return "", raw
    end = stripped.find(THINK_CLOSE)
    if end < 0:
        return stripped[len(THINK_OPEN) :], ""
    visible = stripped[end + len(THINK_CLOSE) :]
    return stripped[len(THINK_OPEN) : end], visible.lstrip("\n")


def normalize_message(message: dict) -> dict:
    """Move server or inline reasoning out of content into ``reasoning``."""
    reasoning = message.pop("reasoning_content", None) or message.pop("reasoning", None)
    content = message.get("content")
    inline = ""
    if isinstance(content, str):
        inline, visible = split_think(content)
        if inline or content.lstrip().startswith(THINK_OPEN):
            message["content"] = (
                visible if visible or not message.get("tool_calls") else None
            )
    combined = "\n".join(part for part in (reasoning, inline) if part)
    if combined:
        message["reasoning"] = combined
    return message


def empty_keepalive(data):
    """Recognize only oMLX's legacy empty heartbeat, never model output."""
    return (
        isinstance(data, dict)
        and isinstance(data.get("id"), str)
        and data
        == {
            "id": data.get("id"),
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "keepalive",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None,
                }
            ],
        }
    )


TYPED = ("model", "messages", "max_tokens", "tools", "tool_choice", "stream_options")


async def complete_stream(  # noqa: C901
    client,
    request,
    on_text,
    *,
    max_reasoning_chars=None,
    observation=None,
    extra_headers=None,
):
    """Run one chat completion through ``AsyncOpenAI`` and assemble the result.

    The SDK performs transport and SSE parsing; this adds solakit's bounds,
    reasoning/think separation and tool-call validation."""
    request = dict(request)
    if request.setdefault("stream", True):
        request["stream_options"] = {"include_usage": True}
    kwargs = {key: request[key] for key in TYPED if key in request}
    # Sampling/thinking keys (e.g. chat_template_kwargs) pass through verbatim.
    kwargs["extra_body"] = {
        key: value for key, value in request.items() if key not in TYPED
    }
    content, calls, reasoning = "", {}, ""
    shown = ""
    usage, finish = None, None
    total = 0
    create = client.chat.completions.with_streaming_response.create
    async with create(
        # Always parse as SSE; extra_body carries the wire "stream" value, and
        # a JSON content type is handled below.
        stream=True,
        extra_headers=extra_headers,
        **kwargs,
    ) as response:
        # Some compatible endpoints return a complete JSON response even when
        # streaming is requested. Keep the same completion validation upstream.
        if response.headers.get("content-type", "").split(";")[0] == "application/json":
            body = bytearray()
            async for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_STREAM_BYTES:
                    raise ModelUnavailable("Model response exceeds size limit")
            data = json.loads(body)
            if observation is not None:
                observation["response"] = data
            for choice in data.get("choices") or []:
                if isinstance(choice.get("message"), dict):
                    normalize_message(choice["message"])
            return data
        async for chunk in await response.parse():
            data = chunk.model_dump(exclude_unset=True)
            total += len(json.dumps(data))
            if total > MAX_STREAM_BYTES:
                raise ModelUnavailable("Model stream exceeds size limit")
            if observation is not None:
                observation["last_event"] = data
            if empty_keepalive(data):
                continue
            if data.get("model") != request["model"]:
                raise ModelUnavailable("Completion model does not match pinned ID")
            if data.get("usage") is not None:
                usage = data["usage"]
            choices = data["choices"]
            if not choices:
                continue  # Usage-only tail.
            if len(choices) != 1 or choices[0].get("index", 0) != 0 or finish:
                raise ModelUnavailable("Invalid model stream choice")
            choice = choices[0]
            delta = choice["delta"]
            if delta.get("role") not in {None, "assistant"}:
                raise ModelUnavailable("Invalid streamed role")
            for key in ("reasoning_content", "reasoning"):
                thought = delta.get(key)
                if thought is not None:
                    if not isinstance(thought, str):
                        raise ModelUnavailable("Invalid streamed reasoning")
                    reasoning += thought
            text = delta.get("content")
            if text is not None:
                if not isinstance(text, str):
                    raise ModelUnavailable("Invalid streamed content")
                content += text
                visible = split_think(content)[1]
                if on_text is not None and visible != shown:
                    shown = visible
                    await on_text(visible)
            if observation is not None:
                observation.update(
                    content=content,
                    reasoning=reasoning,
                    tool_calls=list(calls.values()),
                    usage=usage,
                    finish_reason=choice.get("finish_reason"),
                )
            if (
                max_reasoning_chars is not None
                and not calls
                and not split_think(content)[1].strip()
                and len(reasoning) + len(split_think(content)[0]) > max_reasoning_chars
            ):
                raise ReasoningExhausted("Reasoning exceeded max_reasoning_tokens")
            for fragment in delta.get("tool_calls") or []:
                index = fragment["index"]
                if type(index) is not int or not 0 <= index < 64:
                    raise ModelUnavailable("Invalid streamed tool index")
                call = calls.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if fragment.get("type") not in {None, "function"}:
                    raise ModelUnavailable("Invalid streamed tool type")
                if fragment.get("id") is not None:
                    call["id"] += fragment["id"]
                for key in ("name", "arguments"):
                    value = (fragment.get("function") or {}).get(key)
                    if value is not None:
                        call["function"][key] += value
            finish = choice.get("finish_reason")
            if observation is not None:
                observation["finish_reason"] = finish
                observation["usage"] = usage
    if finish not in {"stop", "tool_calls", "length"}:
        raise ModelUnavailable("Model stream interrupted before completion")
    if sorted(calls) != list(range(len(calls))):
        raise ModelUnavailable("Non-contiguous streamed tool calls")
    message = {
        "role": "assistant",
        "content": content if content or not calls else None,
    }
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    if reasoning:
        message["reasoning_content"] = reasoning
    normalize_message(message)
    return {
        "model": request["model"],
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": usage,
    }
