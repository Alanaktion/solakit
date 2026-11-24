import asyncio
import copy
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest

from solakit.adapters.model import ModelError, assemble_stream, normalize
from solakit.config import EffectiveProfile
from solakit.engine.prompts import PromptInput, assemble
from solakit.records import ModelRequest, ModelStep, new_id, utc_now
from solakit.runtime import compose
from solakit.storage.evidence import canonical

PIN = "fixture-qwen"


def response(*, tools: bool = False) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "café 🐈",
        "reasoning_content": "private reasoning",
    }
    if tools:
        message["content"] = None
        message["tool_calls"] = [
            {
                "id": "call-0",
                "type": "function",
                "function": {"name": "probe.echo", "arguments": '{"text":"café 🐈"}'},
            }
        ]
    return {
        "id": "completion-0",
        "model": PIN,
        "choices": [
            {"index": 0, "finish_reason": "tool_calls" if tools else "stop", "message": message}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def events(data: dict[str, Any]) -> bytes:
    message = data["choices"][0]["message"]
    deltas: list[dict[str, Any]] = [
        {"role": "assistant"},
        {"reasoning_content": "private "},
        {"reasoning_content": "reasoning"},
    ]
    if message.get("tool_calls"):
        deltas.extend(
            [
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-0",
                            "type": "function",
                            "function": {"name": "probe.", "arguments": '{"text":"ca'},
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-0",
                            "function": {"name": "echo", "arguments": 'fé 🐈"}'},
                        }
                    ]
                },
            ]
        )
    else:
        deltas.extend({"content": text} for text in ("ca", "fé ", "🐈"))
    values = [
        {
            "id": data["id"],
            "model": PIN,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        for delta in deltas
    ]
    values.append(
        {
            "model": PIN,
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": data["choices"][0]["finish_reason"]}
            ],
        }
    )
    values.append({"model": PIN, "choices": [], "usage": data["usage"]})
    return (
        b": heartbeat\r\n\r\n"
        + b"".join(b"data: " + canonical(value) + b"\r\n\r\n" for value in values)
        + b"data: [DONE]\r\n\r\n"
    )


class BytesStream(httpx2.AsyncByteStream):
    def __init__(self, body: bytes, *, block: bool = False) -> None:
        self.body, self.block = body, block
        self.entered = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.entered.set()
        if self.block:
            await asyncio.sleep(60)
        for byte in self.body:
            yield bytes([byte])

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("tools", [False, True])
def test_stream_matches_nonstream_including_split_utf8_and_arguments(tools: bool) -> None:
    async def scenario() -> None:
        data = response(tools=tools)
        wire = events(data)
        raw = bytearray()
        result = await assemble_stream(BytesStream(wire).__aiter__(), PIN, raw)
        assert normalize(result, PIN) == normalize(data, PIN)
        assert bytes(raw) == wire

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"model": "wrong"},
        {"choices": []},
        {"id": 123},
        {"usage": []},
        {"usage": {"prompt_tokens": -1}},
        {"usage": {"total_tokens": True}},
    ],
)
def test_bad_response_envelope(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        normalize(response() | change, PIN)


@pytest.mark.parametrize(
    "change",
    [
        {"finish_reason": "length"},
        {"finish_reason": "content_filter"},
        {"finish_reason": "tool_calls"},
        {"message": {"role": "assistant", "content": ""}},
        {"message": {"role": "assistant", "reasoning_content": "reasoning only"}},
        {"message": {"role": "user", "content": "wrong role"}},
        {"message": {"role": "assistant", "content": ["wrong type"]}},
    ],
)
def test_incomplete_choice(change: dict[str, Any]) -> None:
    data = response()
    data["choices"][0].update(change)
    with pytest.raises(ValueError):
        normalize(data, PIN)


@pytest.mark.parametrize("arguments", ["{", "[]", '{"x":NaN}', '{"x":1,"x":2}'])
def test_bad_arguments_never_produce_calls(arguments: str) -> None:
    data = response(tools=True)
    data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
    with pytest.raises(ValueError):
        normalize(data, PIN)


def test_duplicate_call_ids_and_missing_id() -> None:
    data = response(tools=True)
    calls = data["choices"][0]["message"]["tool_calls"]
    calls.append(copy.deepcopy(calls[0]))
    with pytest.raises(ModelError, match="Duplicate"):
        normalize(data, PIN)
    calls.pop()
    calls[0].pop("id")
    with pytest.raises(ValueError):
        normalize(data, PIN)


def test_missing_usage_identity_and_literal_think_examples() -> None:
    data = response()
    data.pop("usage")
    data.pop("model")  # /models verifies the pin; returned identity is checked when available.
    for text in ("<think>literal example</think>", "```xml\n<think>example</think>\n```"):
        data["choices"][0]["message"]["content"] = text
        normalized = normalize(data, PIN)
        assert normalized["content"] == text
        assert normalized["reasoning"] == "private reasoning"
        assert normalized["usage"] is None


@pytest.mark.parametrize(
    "wire",
    [
        events(response(tools=True)).replace(b"data: [DONE]\r\n\r\n", b""),
        b"data: [DONE]\n\n",
        b'data: {"model":"wrong","choices":[]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":-1}]}}]}\n\n',
        b"data: {broken}\n\n",
    ],
)
def test_broken_stream_never_completes(wire: bytes) -> None:
    async def scenario() -> None:
        with pytest.raises(ValueError):
            await assemble_stream(BytesStream(wire).__aiter__(), PIN, bytearray())

    asyncio.run(scenario())


def test_only_exact_empty_omlx_heartbeat_is_ignored() -> None:
    heartbeat: dict[str, Any] = {
        "id": "heartbeat",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "keepalive",
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}
        ],
    }

    async def scenario() -> None:
        wire = b"data: " + canonical(heartbeat) + b"\n\n" + events(response())
        result = await assemble_stream(BytesStream(wire).__aiter__(), PIN, bytearray())
        assert normalize(result, PIN)["content"] == "café 🐈"
        heartbeat["choices"][0]["delta"]["content"] = "unexpected text"
        with pytest.raises(ModelError, match="pin"):
            await assemble_stream(
                BytesStream(b"data: " + canonical(heartbeat) + b"\n\n").__aiter__(),
                PIN,
                bytearray(),
            )

    asyncio.run(scenario())


def test_interleaved_calls_are_sorted_by_index() -> None:
    fragments = [
        {"index": 1, "id": "second", "function": {"name": "b", "arguments": '{"x":'}},
        {"index": 0, "id": "first", "function": {"name": "a", "arguments": "{}"}},
        {"index": 1, "function": {"arguments": "2}"}},
    ]

    async def scenario() -> None:
        wire = b"".join(
            b"data: "
            + canonical(
                {
                    "model": PIN,
                    "choices": [
                        {"index": 0, "delta": {"tool_calls": [fragment]}, "finish_reason": None}
                    ],
                }
            )
            + b"\n\n"
            for fragment in fragments
        )
        wire += (
            b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}'
            b"\n\ndata: [DONE]\n\n"
        )
        result = await assemble_stream(BytesStream(wire).__aiter__(), PIN, bytearray())
        calls = normalize(result, PIN)["calls"]
        assert [call.provider_tool_call_id for call in calls] == ["first", "second"]
        assert calls[1].arguments == {"x": 2}

    asyncio.run(scenario())


def test_incomplete_response_keeps_normalized_reasoning_and_usage() -> None:
    for finish in ("length", "stop"):
        data = response()
        data["choices"][0]["message"]["content"] = ""
        data["choices"][0]["finish_reason"] = finish
        with pytest.raises(ModelError) as caught:
            normalize(data, PIN)
        assert caught.value.values["reasoning"] == "private reasoning"
        assert caught.value.values["usage"]["total_tokens"] == 15
        assert caught.value.values["finish_reason"] == finish


@pytest.mark.parametrize("stream", [False, True])
def test_adapter_records_exact_bytes_before_network_and_private_evidence(
    profile: EffectiveProfile,
    stream: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        context = compose(profile)
        assert context.model is not None and context.store is not None
        store = context.store
        seen = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/models"):
                return httpx2.Response(200, json={"data": [{"id": PIN}, {"id": "other"}]})
            recorded = [entry for _, entry in store.log.scan() if entry.kind == "request"]
            assert len(recorded) == 1
            body = recorded[0].record["body"]
            assert isinstance(body, dict)
            assert (profile.data_dir / body["path"]).read_bytes() == request.content
            assert request.headers["content-type"] == "application/json"
            seen.append(request.content)
            data = response(tools=True)
            return (
                httpx2.Response(200, stream=BytesStream(events(data)))
                if stream
                else (httpx2.Response(200, json=data))
            )

        context.model.transport = httpx2.MockTransport(handler)
        await context.start()
        try:
            session = store.create_session()
            run = store.admit(
                session.id, request_id=new_id(), content="PRIVATE input", profile="default"
            )
            message = store.messages(session.id)[0][1]
            frozen = PromptInput(at=utc_now(), input=message, stream=stream)
            prompt = assemble(profile, frozen)
            result = await context.model.complete(prompt, run, 1)
            assert result.step.failure is None and result.calls[0].arguments == {"text": "café 🐈"}
            assert result.step.usage == response()["usage"]
            assert seen == [prompt.body]
            assert result.step.response is not None
            assert (profile.data_dir / result.step.response.path).is_file()
            for path in (profile.data_dir / "artifacts").iterdir():
                assert path.stat().st_mode & 0o777 == 0o600
                assert b"PRIVATE-transport-key" not in path.read_bytes()
            assert "PRIVATE input" not in caplog.text
            assert "PRIVATE-transport-key" not in caplog.text
        finally:
            await context.stop()
        await context.start()
        assert len(store.run_records(run.id)) == 4
        await context.stop()

    monkeypatch.setenv("OPENAI_API_KEY", "PRIVATE-transport-key")
    caplog.set_level(logging.DEBUG)
    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["http", "wrong_pin", "broken", "length", "malformed"])
def test_failures_record_one_attempt_without_retries(profile: EffectiveProfile, fault: str) -> None:
    async def scenario() -> None:
        context = compose(profile)
        assert context.model is not None and context.store is not None
        sent = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/models"):
                return httpx2.Response(
                    200, json={"data": [{"id": "wrong" if fault == "wrong_pin" else PIN}]}
                )
            sent.append(request.content)
            if fault == "http":
                return httpx2.Response(500, json={"error": {"message": "PRIVATE error"}})
            data = response(tools=True)
            if fault == "length":
                data["choices"][0]["finish_reason"] = "length"
            wire = events(data).removesuffix(b"data: [DONE]\r\n\r\n")
            if fault == "malformed":
                wire = b"data: {bad}\n\n"
            return httpx2.Response(200, stream=BytesStream(wire))

        context.model.transport = httpx2.MockTransport(handler)
        await context.start()
        try:
            session = context.store.create_session()
            run = context.store.admit(
                session.id, request_id=new_id(), content="test", profile="default"
            )
            message = context.store.messages(session.id)[0][1]
            result = await context.model.complete(
                assemble(profile, PromptInput(at=utc_now(), input=message)), run, 1
            )
            assert result.step.failure is not None and not result.calls
            assert len(sent) == (0 if fault == "wrong_pin" else 1)
            records = [record for _, record in context.store.run_records(run.id)]
            assert len([r for r in records if isinstance(r, ModelRequest)]) == 1
            assert len([r for r in records if isinstance(r, ModelStep)]) == 1
            assert "PRIVATE error" not in result.step.failure.message
        finally:
            await context.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_cancellation_timeout_close_response_and_release_permit(
    profile: EffectiveProfile, cancel: bool
) -> None:
    async def scenario() -> None:
        short = profile.model_copy(update={"model_generation_timeout_seconds": 1})
        context = compose(short)
        assert context.model is not None and context.store is not None
        body = BytesStream(b"", block=True)

        def handler(request: httpx2.Request) -> httpx2.Response:
            return (
                httpx2.Response(200, json={"data": [{"id": PIN}]})
                if request.url.path.endswith("/models")
                else httpx2.Response(200, stream=body)
            )

        context.model.transport = httpx2.MockTransport(handler)
        await context.start()
        try:
            session = context.store.create_session()
            run = context.store.admit(
                session.id, request_id=new_id(), content="test", profile="default"
            )
            message = context.store.messages(session.id)[0][1]
            prompt = assemble(short, PromptInput(at=utc_now(), input=message))
            task = asyncio.create_task(context.model.complete(prompt, run, 1))
            await body.entered.wait()
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await task
                assert result.step.failure is not None and result.step.failure.code == "timeout"
            assert body.closed and not context.inference.locked()
            steps = [r for _, r in context.store.run_records(run.id) if isinstance(r, ModelStep)]
            assert len(steps) == 1 and steps[0].failure is not None
            assert steps[0].failure.code == ("cancelled" if cancel else "timeout")
        finally:
            await context.stop()

    asyncio.run(scenario())


def test_failed_request_persistence_prevents_transmission(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        context = compose(profile)
        assert context.model is not None and context.store is not None
        sent = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            sent.append(request)
            raise AssertionError("network must not be called")

        context.model.transport = httpx2.MockTransport(handler)
        await context.start()
        try:
            session = context.store.create_session()
            run = context.store.admit(
                session.id, request_id=new_id(), content="test", profile="default"
            )
            message = context.store.messages(session.id)[0][1]

            def fail(stage: str) -> None:
                raise OSError("fixture disk full")

            other_endpoint = profile.model_copy(
                update={"model_base_url": "http://localhost:9999/v1"}
            )
            with pytest.raises(ModelError, match="configuration"):
                await context.model.complete(
                    assemble(other_endpoint, PromptInput(at=utc_now(), input=message)), run, 1
                )
            prompt = assemble(profile, PromptInput(at=utc_now(), input=message))
            with pytest.raises(ModelError, match="configuration"):
                await context.model.complete(prompt, run.model_copy(update={"id": new_id()}), 1)
            context.store.fault = fail
            with pytest.raises(OSError, match="disk full"):
                await context.model.complete(
                    assemble(profile, PromptInput(at=utc_now(), input=message)), run, 1
                )
            assert not sent and not context.inference.locked()
        finally:
            await context.stop()

    asyncio.run(scenario())


def test_waiting_cancellation_leaves_no_attempt_and_shared_client_is_reused(
    profile: EffectiveProfile,
) -> None:
    async def scenario() -> None:
        context = compose(profile)
        assert context.model is not None and context.store is not None
        first = BytesStream(events(response()), block=True)
        sent = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/models"):
                return httpx2.Response(200, json={"data": [{"id": PIN}]})
            sent.append(request)
            return httpx2.Response(
                200, stream=first if len(sent) == 1 else BytesStream(events(response()))
            )

        context.model.transport = httpx2.MockTransport(handler)
        await context.start()
        client = context.model.client
        try:
            session = context.store.create_session()
            run = context.store.admit(
                session.id, request_id=new_id(), content="test", profile="default"
            )
            message = context.store.messages(session.id)[0][1]
            prompt = assemble(profile, PromptInput(at=utc_now(), input=message))
            active = asyncio.create_task(context.model.complete(prompt, run, 1))
            await first.entered.wait()
            waiting = asyncio.create_task(context.model.complete(prompt, run, 2))
            await asyncio.sleep(0)
            assert len(sent) == 1
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
            result = await context.model.complete(prompt, run, 2)
            assert result.step.failure is None and context.model.client is client
            requests = [
                r for _, r in context.store.run_records(run.id) if isinstance(r, ModelRequest)
            ]
            assert [r.index for r in requests] == [1, 2]
        finally:
            await context.stop()
        assert client is not None and client.is_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize("slow", [False, True])
def test_failed_response_cleanup_is_recorded_as_failure(
    profile: EffectiveProfile, slow: bool
) -> None:
    class BrokenClose(BytesStream):
        async def aclose(self) -> None:
            if slow:
                await asyncio.sleep(60)
            raise RuntimeError("PRIVATE cleanup exception")

    async def scenario() -> None:
        short = profile.model_copy(update={"model_connect_timeout_seconds": 1})
        context = compose(short)
        assert context.model is not None and context.store is not None

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/models"):
                return httpx2.Response(200, json={"data": [{"id": PIN}]})
            return httpx2.Response(200, stream=BrokenClose(events(response())))

        context.model.transport = httpx2.MockTransport(handler)
        await context.start()
        try:
            session = context.store.create_session()
            run = context.store.admit(
                session.id, request_id=new_id(), content="test", profile="default"
            )
            message = context.store.messages(session.id)[0][1]
            with pytest.raises(ModelError, match="cleanup"):
                await context.model.complete(
                    assemble(short, PromptInput(at=utc_now(), input=message)), run, 1
                )
            steps = [r for _, r in context.store.run_records(run.id) if isinstance(r, ModelStep)]
            assert len(steps) == 1 and steps[0].failure is not None
            assert steps[0].failure.message == "Model response cleanup failed"
            assert not context.inference.locked()
        finally:
            await context.stop()

    asyncio.run(scenario())
