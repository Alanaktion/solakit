"""Isolated browser-QA server: scratch data, mock model, no external integrations."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx
import uvicorn

import solakit.main as main_module
from solakit.core.config import (
    Channel,
    Config,
    Job,
    MemoryConfig,
    PathsConfig,
    SecretRef,
)
from solakit.core.memory import MemoryClient
from solakit.core.model import ModelClient
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


async def respond(request):
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})
    body = json.loads(request.content)
    task = body["messages"][-1].get("content", "")
    if "hold-browser" in task:
        await asyncio.sleep(30)
    if "tool-browser" in task and body["messages"][-1]["role"] == "user":
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
                                    "function": {
                                        "name": "runtime.status",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
            },
        )
    text = "Browser reply.\n\n```text\n" + "long-code-" * 24 + "\n```"
    if body.get("stream"):
        events = [
            {
                "model": config.model.model_id,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": text},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": config.model.model_id,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            },
        ]
        content = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
        return httpx.Response(
            200,
            text=content + "data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
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
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=18382, log_level="warning")
