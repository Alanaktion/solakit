"""Bounded OpenAI SSE assembly. Tool arguments stay inert until full validation."""

import json

from solakit.core.model import ModelUnavailable

MAX_STREAM_BYTES = 2 * 1024 * 1024


async def events(response):
    """Parse data events across arbitrary network/UTF-8 boundaries."""
    buffer = b""
    data = []
    size, received = 0, 0
    async for chunk in response.aiter_bytes():
        received += len(chunk)
        if received > MAX_STREAM_BYTES:
            raise ModelUnavailable("Model stream exceeds size limit")
        buffer += chunk
        if len(buffer) + size > MAX_STREAM_BYTES:
            raise ModelUnavailable("Model stream event exceeds size limit")
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line:
                if data:
                    yield b"\n".join(data).decode("utf-8")
                data, size = [], 0
            elif line.startswith(b"data:"):
                value = line[5:].removeprefix(b" ")
                data.append(value)
                size += len(value)
    # A missing event delimiter/[DONE] is an interrupted response, not success.
    if buffer or data:
        raise ModelUnavailable("Model stream ended inside an event")


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


async def complete_stream(client, request, on_text):
    request = {**request, "stream": True, "stream_options": {"include_usage": True}}
    content, calls = "", {}
    usage, finish = None, None
    total, done = 0, False
    async with client.stream("POST", "chat/completions", json=request) as response:
        response.raise_for_status()
        async for event in events(response):
            total += len(event.encode("utf-8"))
            if total > MAX_STREAM_BYTES:
                raise ModelUnavailable("Model stream exceeds size limit")
            if event == "[DONE]":
                done = True
                break
            data = json.loads(event)
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
            text = delta.get("content")
            if text is not None:
                if not isinstance(text, str):
                    raise ModelUnavailable("Invalid streamed content")
                content += text
                await on_text(content)
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
    if not done or finish not in {"stop", "tool_calls", "length"}:
        raise ModelUnavailable("Model stream interrupted before completion")
    if sorted(calls) != list(range(len(calls))):
        raise ModelUnavailable("Non-contiguous streamed tool calls")
    message = {
        "role": "assistant",
        "content": content if content or not calls else None,
    }
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    return {
        "model": request["model"],
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": usage,
    }
