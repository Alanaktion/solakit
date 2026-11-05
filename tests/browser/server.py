"""Isolated browser-QA server: scratch data, mock model, no external integrations."""

import asyncio
import io
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from PIL import Image

import solakit.main as main_module
from solakit.core.config import (
    Channel,
    Config,
    Job,
    MemoryConfig,
    PathsConfig,
    SecretRef,
)
from solakit.core.media import Media
from solakit.core.memory import MemoryClient
from solakit.core.model import ModelClient
from solakit.core.schemas import Message, Origin, Session
from solakit.main import create_app

root = Path(tempfile.mkdtemp(prefix="solakit-browser-"))
(root / "SOUL.md").write_text("Browser QA persona.\n")
(root / "fragments/profiles").mkdir(parents=True)
(root / "fragments/profiles/interactive.md").write_text("")
(root / "fragments/recall-header.md").write_text("Scratch memory data:")
os.environ["SOLA_BROWSER_TOKEN"] = "scratch-browser-token"
config = Config(
    enabled_tools=["runtime"],
    memory=MemoryConfig(
        enabled=True,
        base_url="http://scratch-memory.invalid",
        roots=["viking://user/scratch/memories"],
    ),
    operator_token=SecretRef(env="SOLA_BROWSER_TOKEN"),
    paths=PathsConfig(
        data=root / "data", soul=root / "SOUL.md", fragments=root / "fragments"
    ),
    jobs=[
        Job(
            id="browser-job",
            cron="0 0 1 1 *",
            timezone="America/Denver",
            profile="interactive",
            task="Browser job",
            bind_session="ephemeral",
            enabled=True,
        ),
        Job(
            id="disabled-job",
            cron="0 0 1 1 *",
            profile="interactive",
            task="Never run",
            bind_session="ephemeral",
            enabled=False,
        ),
    ],
    channels=[
        Channel(
            id="disabled-channel",
            type="api",
            channel_id="scratch",
            session="scratch",
            enabled=False,
        )
    ],
)

config.profiles["interactive"].tools = ["runtime.status"]


class BrowserStream(httpx.AsyncByteStream):
    def __init__(self, events, *, delay=0, fail=False):
        self.events, self.delay, self.fail = events, delay, fail

    async def __aiter__(self):
        for event in self.events:
            yield ("data: " + json.dumps(event) + "\n\n").encode()
            await asyncio.sleep(self.delay)
        if self.fail:
            raise httpx.ReadError("Deliberate scratch stream interruption")
        yield b"data: [DONE]\n\n"


def chunk(delta, finish=None):
    return {
        "model": config.model.model_id,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def streamed(events, *, delay=0, fail=False):
    return httpx.Response(
        200,
        stream=BrowserStream(events, delay=delay, fail=fail),
        headers={"content-type": "text/event-stream"},
    )


async def respond(request):
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})
    body = json.loads(request.content)
    task = next(
        (
            m.get("content", "")
            for m in reversed(body["messages"])
            if m["role"] == "user"
        ),
        "",
    )
    if "hold-browser" in task:
        await asyncio.sleep(30)
    if "tool-browser" in task and body["messages"][-1]["role"] == "user":
        function = {"name": "runtime.status", "arguments": "{}"}
        if body.get("stream"):
            return streamed(
                [
                    chunk(
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "browser-tool",
                                    "type": "function",
                                    "function": {
                                        "name": "runtime.status",
                                        "arguments": "{",
                                    },
                                }
                            ],
                        }
                    ),
                    chunk(
                        {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]},
                        "tool_calls",
                    ),
                ],
                delay=0.3,
            )
        return httpx.Response(
            200,
            json={
                "model": config.model.model_id,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "browser-tool",
                                    "type": "function",
                                    "function": function,
                                }
                            ],
                        }
                    }
                ],
            },
        )
    text = "Browser reply.\n\n```text\n" + "long-code-" * 24 + "\n```"
    if body.get("stream"):
        if "stream-browser" in task or "break-browser" in task:
            pieces = [
                "## Streaming heading\n\n**par",
                "tial** [local link](/tools)\n\n",
                "| Column | Value |\n| --- | --- |\n| "
                + "widecell" * 40
                + " | data |\n\n",
                "```text\n" + "long-code-" * 100 + "\n```\n\n",
                ("A scrolling paragraph for browser verification.\n\n" * 30),
                "Stream finished.",
            ]
            events = [chunk({"role": "assistant", "content": p}) for p in pieces]
            if "break-browser" in task:
                return streamed(events[:1], delay=0.8, fail=True)
            events.append(chunk({}, "stop"))
            return streamed(events, delay=0.8)
        return streamed(
            [chunk({"role": "assistant", "content": text}), chunk({}, "stop")]
        )
    return httpx.Response(
        200,
        json={
            "model": config.model.model_id,
            "choices": [{"message": {"role": "assistant", "content": text}}],
        },
    )


def memory_response(request):
    endpoint = request.url.path
    result = {}
    if endpoint.endswith("search/find"):
        result = {"memories": []}
    elif endpoint.endswith("/commit"):
        remote = endpoint.split("/")[-2]
        result = {
            "status": "accepted",
            "task_id": "scratch-task",
            "archive_uri": f"viking://user/scratch/sessions/{remote}/history/archive_001",
        }
    elif "/tasks/" in endpoint:
        result = {"status": "completed"}
    elif "/archives/" in endpoint:
        result = {
            "status": "completed",
            "archive_id": "archive_001",
            "overview": "Browser scratch conversation summary.",
        }
    return httpx.Response(200, json={"status": "ok", "result": result})


# Only this isolated QA process substitutes the memory transport. No request can
# reach a live memory server, including startup, summary or commit operations.
main_module.MemoryClient = lambda cfg, key=None: MemoryClient(
    cfg, key, transport=httpx.MockTransport(memory_response)
)

app = create_app(
    config=config,
    model=ModelClient(config.model, transport=httpx.MockTransport(respond)),
)


observer_drops = set()


class DisconnectObserver:
    """Cut a real SSE response mid-body without cancelling accepted execution."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        target = scope.get("path", "")

        async def transmit(message):
            if (
                target in observer_drops
                and message["type"] == "http.response.body"
                and message.get("body")
            ):
                observer_drops.remove(target)
                raise ConnectionResetError("Deliberate browser-QA observer disconnect")
            await send(message)

        await self.app(scope, receive, transmit)


app.add_middleware(DisconnectObserver)


@app.post("/qa/disconnect/{identity}")
def disconnect_scratch_observer(identity: str):
    observer_drops.add(f"/sessions/{identity}/events")
    return {"disconnecting": True}


@app.post("/qa/expire")
def expire_scratch_sessions():
    # QA-only route in this isolated fixture, never mounted by the application.
    for identity in app.state.ui_sessions.sessions:
        app.state.ui_sessions.sessions[identity] = 0
    return {"expired": True}


@app.post("/qa/vision")
async def scratch_vision():
    # Scratch-only fixture, never mounted by the production application.
    session = await asyncio.to_thread(
        app.state.store.create_session, Session(memory_scope="ephemeral")
    )
    source = io.BytesIO()
    Image.new("RGB", (1024, 512), "red").save(source, format="PNG")
    ref = await asyncio.to_thread(
        Media(app.state.store.root).save, source.getvalue(), "fixture.png"
    )
    run = await app.state.execution.submit(session.id, "", images=[ref])
    return {"session": session.id, "image": ref.sha256, "status": run.status}


@app.post("/qa/session-lifecycle")
async def scratch_session_lifecycle():
    prefix = "imported-" + uuid4().hex[:10]
    ids = [f"{prefix}-{n:02}" for n in range(55)]

    def seed():
        for n, identity in enumerate(ids):
            app.state.store.create_session(
                Session(id=identity, title=f"Imported job {n}")
            )
            app.state.store.append_message(
                identity,
                Message(
                    role="user",
                    content="Lifecycle searchable proof",
                    ts=datetime(2020, 1, 1, tzinfo=UTC),
                    run_id="legacy-" + identity,
                    origin=Origin(channel="job"),
                ),
            )

    await asyncio.to_thread(seed)
    return {"prefix": prefix, "ids": ids}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=18382, log_level="warning")
