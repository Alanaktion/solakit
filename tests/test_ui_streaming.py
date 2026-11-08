"""Streaming transport, durable ordering and sanitized Markdown presentation."""

import asyncio
import json
from html.parser import HTMLParser

import httpx
import pytest
from fastapi.testclient import TestClient

from solakit.core.model import ModelClient
from solakit.core.schemas import Session
from solakit.core.secrets import Redactor
from solakit.main import create_app
from solakit.tools import ToolContext, ToolResult, tool
from solakit.ui.markdown import markdown


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, release=None, entered=None):
        self.chunks, self.release, self.entered = chunks, release, entered
        self.closed = False

    async def aclose(self):
        self.closed = True

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            yield chunk
            if index == 0 and self.release is not None:
                if self.entered is not None:
                    self.entered.set()
                await self.release.wait()


def frame(config, delta=None, finish=None, *, usage=None, model=None):
    return (
        "data: "
        + json.dumps(
            {
                "model": model or config.model.model_id,
                "choices": []
                if usage
                else [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
                "usage": usage,
            },
            ensure_ascii=False,
        )
        + "\n\n"
    ).encode()


def backend(config, chunks, **options):
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(200, stream=Stream(chunks, **options))

    return ModelClient(config.model, transport=httpx.MockTransport(handle))


def test_markdown_sanitizes_active_content_and_renders_structure():
    source = """## Moon

**bold** and *soft* and `code`

- one
- two

```python
print("<script>")
```

| name | value |
| --- | --- |
| a | b |

[good](https://example.org) [bad](javascript:alert(1))
<img src=x onerror=alert(1)><script>alert(1)</script>
![tracking](https://example.org/track.png)
"""
    output = str(markdown(source))
    assert "<h2>Moon</h2>" in output
    assert "<strong>bold</strong>" in output
    assert "<pre><code>" in output and "<table>" in output

    class Tags(HTMLParser):
        def handle_starttag(self, tag, attrs):
            assert tag not in {"script", "img", "iframe", "style", "svg"}
            assert not any(
                key.startswith("on") or key == "style" for key, value in attrs
            )
            for key, value in attrs:
                if key == "href":
                    assert not value.startswith(("javascript:", "data:"))

    Tags().feed(output)
    assert "nofollow noopener noreferrer" in output
    assert source.startswith("## Moon")  # The renderer accepts, never mutates source.


def test_preview_masks_secret_fragments_before_publication():
    redact = Redactor(["private-token-value"])
    assert redact.preview("hello private-to") == "hello "
    assert redact.preview("hello private-token-value") == "hello [redacted]"
    assert redact.preview("ordinary text") == "ordinary text"
    assert Redactor(["aaaa"]).preview("aaaa") == "[redacted]"


def test_stream_assembles_utf8_calls_usage_and_keeps_tool_order(config):
    async def exercise():
        invoked = []
        calls = [
            frame(
                config,
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "echo", "arguments": '{"text":'},
                        }
                    ],
                },
            ),
            frame(
                config,
                {"tool_calls": [{"index": 0, "function": {"arguments": '"月"}'}}]},
                "tool_calls",
            ),
            frame(config, usage={"prompt_tokens": 4, "completion_tokens": 2}),
            b"data: [DONE]\n\n",
        ]
        counter = 0

        def handle(request):
            nonlocal counter
            if request.url.path.endswith("/models"):
                return httpx.Response(
                    200, json={"data": [{"id": config.model.model_id}]}
                )
            counter += 1
            chunks = (
                calls
                if counter == 1
                else [
                    frame(config, {"content": "**finished 月**"}),
                    frame(config, finish="stop"),
                    frame(config, usage={"prompt_tokens": 5, "completion_tokens": 3}),
                    b"data: [DONE]\n\n",
                ]
            )
            # Byte-sized fragments exercise boundaries inside UTF-8 and SSE fields.
            return httpx.Response(
                200, stream=Stream([bytes([b]) for b in b"".join(chunks)])
            )

        model = ModelClient(config.model, transport=httpx.MockTransport(handle))
        app = create_app(config=config, model=model)
        async with app.router.lifespan_context(app):

            @tool(name="echo", profiles=["interactive"])
            async def echo(ctx: ToolContext, text: str) -> ToolResult:
                history = app.state.store.transcript(ctx.session.id)
                assert history[-1]["tool_calls"][0]["id"] == "call-1"
                invoked.append(text)
                return ToolResult(content=text)

            app.state.registry.register(echo)
            config.profiles["interactive"].tools = ["echo"]
            app.state.registry.validate()
            app.state.store.create_session(Session(id="stream"))
            run = await app.state.execution.submit("stream", "hello", stream=True)
            assert run.status == "completed" and run.tokens == {
                "prompt_tokens": 9,
                "completion_tokens": 5,
            }
            assert invoked == ["月"]
            history = app.state.store.transcript("stream")
            assert [m["role"] for m in history] == [
                "user",
                "assistant",
                "tool",
                "assistant",
            ]
            assert history[-1]["content"] == "**finished 月**"

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["wrong", "interrupted", "length", "bad_tool"])
def test_stream_failures_never_dispatch_partial_calls(config, mode):
    chunks = [
        frame(
            config,
            {
                "content": "partial",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "c",
                        "function": {"name": "missing", "arguments": "{}"},
                    }
                ],
            },
            model="wrong" if mode == "wrong" else None,
        )
    ]
    if mode != "interrupted":
        chunks += [
            frame(config, finish="length" if mode == "length" else "tool_calls"),
            b"data: [DONE]\n\n",
        ]
    if mode == "bad_tool":
        chunks = [
            frame(
                config,
                {"tool_calls": [{"index": 0, "id": "c", "function": {"name": 42}}]},
            ),
            b"data: [DONE]\n\n",
        ]
    app = create_app(config=config, model=backend(config, chunks))
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer operator-test-token"}
        client.post("/api/sessions", json={"id": "stream"}, headers=headers)
        response = client.post(
            "/api/sessions/stream/messages?stream=true",
            json={"message": "hello"},
            headers=headers,
        )
        assert response.status_code == 200
        record = response.json()
        assert not record["tools"]
        if mode == "length":
            assert record["status"] == "budget_exceeded"
            assert record["messages"][-1]["content"] == "partial"
        else:
            assert record["status"] == "model_unavailable"
            assert record["messages"] == []


def test_sse_arrives_before_completion_masks_partial_secrets_and_survives_disconnect(
    config,
):
    async def exercise():
        release, entered, preview_seen = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        chunks = [
            frame(config, {"content": "**hello** operator-test-to"}),
            frame(config, {"content": "ken"}),
            frame(config, finish="stop"),
            b"data: [DONE]\n\n",
        ]
        app = create_app(
            config=config,
            model=backend(config, chunks, release=release, entered=entered),
        )
        auth = {"Authorization": "Bearer operator-test-token"}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test", headers=auth
            ) as client:
                await client.post("/api/sessions", json={"id": "s"})
                response = await client.post(
                    "/sessions/s/messages",
                    data={"message": "hello"},
                    headers={"Accept": "application/json"},
                )
                assert response.status_code == 202
                run_id = response.json()["run_id"]
                await asyncio.wait_for(entered.wait(), 2)
                assert app.state.store.transcript("s") == []

                async def receive():
                    await asyncio.Event().wait()

                frames = []

                async def send(message):
                    if message["type"] == "http.response.body":
                        body = message.get("body", b"").decode()
                        frames.append(body)
                        if "Streaming preview" in body:
                            assert "operator-test-to" not in body
                            assert "<strong>hello</strong>" in body
                            preview_seen.set()

                scope = {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.3"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/sessions/s/events",
                    "raw_path": b"/sessions/s/events",
                    "query_string": b"",
                    "root_path": "",
                    "headers": [(b"authorization", b"Bearer operator-test-token")],
                    "server": ("test", 80),
                    "client": ("test", 1),
                }
                observer = asyncio.create_task(app(scope, receive, send))
                await asyncio.wait_for(preview_seen.wait(), 2)
                # Browser disconnect must not cancel accepted execution.
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
                pending = app.state.execution.pending[run_id]
                release.set()
                run = await asyncio.wait_for(asyncio.shield(pending.future), 2)
                assert run.status == "completed"
                assert (
                    app.state.store.transcript("s")[-1]["content"]
                    == "**hello** operator-test-token"
                )
                tail = await client.get("/sessions/s/events")
                assert "text/event-stream" in tail.headers["content-type"]
                assert "operator-test-token" not in tail.text
                assert "[redacted]" in tail.text and '"pending": false' in tail.text
                page = await client.get("/sessions/s")
                assert "<strong>hello</strong>" in page.text
                assert (await client.get("/sessions/missing/events")).status_code == 404
                assert (
                    await client.get(
                        "/sessions/s/events", headers={"Authorization": ""}
                    )
                ).status_code == 401

    asyncio.run(exercise())


def test_stream_timeout_and_size_are_bounded(config, monkeypatch):
    from solakit.core import model_stream
    from solakit.core.model import ModelUnavailable

    async def exercise():
        async def preview(text):
            pass

        release = asyncio.Event()
        config.model.timeout_s = 0.02
        model = backend(config, [frame(config, {"content": "hello"})], release=release)
        with pytest.raises(ModelUnavailable):
            await model.complete(
                [{"role": "user", "content": "hello"}], [], on_text=preview
            )
        await model.close()
        monkeypatch.setattr(model_stream, "MAX_STREAM_BYTES", 16)
        model = backend(config, [b":" + b"x" * 20 + b"\n\n"])
        with pytest.raises(ModelUnavailable):
            await model.complete(
                [{"role": "user", "content": "hello"}], [], on_text=preview
            )
        await model.close()

    asyncio.run(exercise())


def test_disconnect_during_durable_admission_still_queues_run(
    config, mock_model, monkeypatch
):
    import threading

    async def exercise():
        app = create_app(config=config, model=mock_model)
        async with app.router.lifespan_context(app):
            app.state.store.create_session(Session(id="s"))
            original = app.state.store.save_run
            entered, release = threading.Event(), threading.Event()

            def held_save(run):
                if run.status == "queued":
                    entered.set()
                    assert release.wait(3)
                return original(run)

            monkeypatch.setattr(app.state.store, "save_run", held_save)
            submission = asyncio.create_task(app.state.execution.enqueue("s", "hello"))
            assert await asyncio.to_thread(entered.wait, 2)
            submission.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await submission
            await asyncio.wait_for(app.state.execution.queues["s"].join(), 2)
            runs = app.state.store.list_records("runs")
            assert len(runs) == 1 and runs[0]["status"] == "completed"
            assert app.state.store.transcript("s")[-1]["content"] == "Hello back"

    asyncio.run(exercise())


@pytest.mark.parametrize("live", [False, True])
def test_cancelling_live_generation_closes_transport_without_saving_preview(
    config, live
):
    async def exercise():
        entered = asyncio.Event()
        stream = Stream(
            [frame(config, {"content": "unsaved partial"})],
            release=asyncio.Event(),
            entered=entered,
        )

        def handle(request):
            if request.url.path.endswith("/models"):
                return httpx.Response(
                    200, json={"data": [{"id": config.model.model_id}]}
                )
            return httpx.Response(200, stream=stream)

        model = ModelClient(config.model, transport=httpx.MockTransport(handle))
        app = create_app(config=config, model=model)
        async with app.router.lifespan_context(app):
            app.state.store.create_session(Session(id="s"))
            pending = await app.state.execution.enqueue("s", "hello", stream=live)
            await asyncio.wait_for(entered.wait(), 2)
            assert await app.state.execution.cancel(pending.run.id)
            assert (await pending.future).status == "cancelled"
            assert stream.closed
            assert app.state.store.transcript("s") == []

    asyncio.run(exercise())


def test_active_stream_stays_visible_during_rotation_attribution_race():
    from solakit.ui.view import turns

    # Rotation is durable before the run's compaction attribution is saved. A
    # snapshot of older run metadata must not terminate live observation.
    history = turns(
        {
            "id": "s",
            "default_profile": "interactive",
            "messages": [
                {
                    "kind": "compaction",
                    "content": "summary",
                    "ts": "2026-11-05T12:00:01+00:00",
                }
            ],
        },
        [
            {
                "id": "r",
                "session": "s",
                "status": "running",
                "created_at": "2026-11-05T12:00:00+00:00",
            }
        ],
        {},
    )
    assert len(history) == 2
    assert history[0]["boundary"] and history[1]["pending"]


def test_legacy_empty_keepalive_never_becomes_output_or_completion(config):
    from solakit.core.model import ModelUnavailable

    heartbeat = {
        "id": "chatcmpl-probe",
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

    async def exercise():
        previews = []

        async def preview(text):
            previews.append(text)

        raw = ("data: " + json.dumps(heartbeat) + "\n\n").encode()
        model = backend(
            config,
            [raw, frame(config, {"content": "Ready"}, "stop"), b"data: [DONE]\n\n"],
        )
        try:
            result = await model.complete([], [], on_text=preview)
            assert result["choices"][0]["message"]["content"] == "Ready"
            assert previews == ["Ready"]
        finally:
            await model.close()
        model = backend(config, [raw, b"data: [DONE]\n\n"])
        try:
            with pytest.raises(ModelUnavailable):
                await model.complete([], [], on_text=preview)
        finally:
            await model.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("mutation", ["text", "tools", "usage", "finish", "model"])
def test_keepalive_with_payload_still_fails_pinned_validation(config, mutation):
    from solakit.core.model import ModelUnavailable

    event = {
        "id": "chatcmpl-probe",
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
    if mutation == "text":
        event["choices"][0]["delta"]["content"] = "Untrusted"
    elif mutation == "tools":
        event["choices"][0]["delta"]["tool_calls"] = []
    elif mutation == "usage":
        event["usage"] = {"prompt_tokens": 1, "completion_tokens": 1}
    elif mutation == "finish":
        event["choices"][0]["finish_reason"] = "stop"
    else:
        event["model"] = "another-model"

    async def exercise():
        async def preview(text):
            pytest.fail("Invalid keepalive was published")

        raw = ("data: " + json.dumps(event) + "\n\n").encode()
        model = backend(
            config,
            [raw, frame(config, {"content": "Ready"}, "stop"), b"data: [DONE]\n\n"],
        )
        try:
            with pytest.raises(ModelUnavailable):
                await model.complete([], [], on_text=preview)
        finally:
            await model.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("live", [False, True])
def test_stop_retains_output_through_last_tool_call(config, live):
    async def exercise():
        entered = asyncio.Event()
        interrupted = Stream(
            [frame(config, {"content": "unfinished next response"})],
            release=asyncio.Event(),
            entered=entered,
        )
        requests = 0

        def handle(request):
            nonlocal requests
            if request.url.path.endswith("/models"):
                return httpx.Response(
                    200, json={"data": [{"id": config.model.model_id}]}
                )
            assert json.loads(request.content)["stream"] is True
            requests += 1
            if requests == 1:
                return httpx.Response(
                    200,
                    stream=Stream(
                        [
                            frame(
                                config,
                                {
                                    "content": "Checking now",
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "last-call",
                                            "type": "function",
                                            "function": {
                                                "name": "echo",
                                                "arguments": "{}",
                                            },
                                        }
                                    ],
                                },
                                "tool_calls",
                            ),
                            b"data: [DONE]\n\n",
                        ]
                    ),
                )
            return httpx.Response(200, stream=interrupted)

        model = ModelClient(config.model, transport=httpx.MockTransport(handle))
        app = create_app(config=config, model=model)
        async with app.router.lifespan_context(app):

            @tool(name="echo", profiles=["interactive"])
            async def echo(ctx: ToolContext) -> ToolResult:
                return ToolResult(content="retained tool output")

            app.state.registry.register(echo)
            config.profiles["interactive"].tools = ["echo"]
            app.state.registry.validate()
            app.state.store.create_session(Session(id="s"))
            pending = await app.state.execution.enqueue("s", "hello", stream=live)
            await asyncio.wait_for(entered.wait(), 2)
            before = app.state.store.transcript("s")
            assert [m["role"] for m in before] == ["user", "assistant", "tool"]
            assert before[1]["content"] == "Checking now"
            assert before[1]["tool_calls"][0]["id"] == "last-call"
            assert "retained tool output" in before[2]["content"]
            assert await asyncio.wait_for(app.state.execution.cancel(pending.run.id), 2)
            assert interrupted.closed
            assert (await pending.future).status == "cancelled"
            assert app.state.store.transcript("s") == before
            assert requests == 2

    asyncio.run(exercise())
