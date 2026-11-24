"""Disposable Phase 11 browser fixture. No live model, memory or operator data."""

import argparse
import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import uvicorn
from fastapi import FastAPI

from solakit.api import create_app
from solakit.config import EffectiveProfile, OperatorConfig, resolve_profile
from solakit.records import (
    Artifact,
    CompactionBoundary,
    ErrorCode,
    Failure,
    MemoryCommit,
    MemoryNote,
    MemoryState,
    Message,
    ModelStep,
    Origin,
    RunState,
    ToolCall,
    ToolReceipt,
    new_id,
)
from solakit.runtime import compose
from solakit.sdk import Tool, ToolArguments, ToolContext, ToolResult
from solakit.storage.evidence import canonical
from solakit.tools.builtins import TOOLS
from solakit.tools.registry import Registry

PASSWORD = "phase11-disposable-fixture"
HOSTILE = '<img src=x onerror="window.PWNED=true"> [bad](javascript:alert(1)) **Safe bold**'


class Stream(httpx2.AsyncByteStream):
    def __init__(self, deltas: list[dict[str, Any]], finish: str) -> None:
        self.deltas, self.finish = deltas, finish

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for delta in self.deltas:
            await asyncio.sleep(0.2)
            yield (
                b"data: "
                + canonical({"model": "fixture-qwen", "choices": [{"index": 0, "delta": delta}]})
                + b"\n\n"
            )
        yield (
            b"data: "
            + canonical({"choices": [{"index": 0, "delta": {}, "finish_reason": self.finish}]})
            + b"\n\ndata: [DONE]\n\n"
        )


async def pause(context: ToolContext, args: ToolArguments) -> ToolResult:
    await asyncio.sleep(4)
    return ToolResult(data={"text": HOSTILE, "detail": "bounded output\n" * 1000})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--agent-name", default="Sola")
    parser.add_argument(
        "--image-fixture", type=Path, help="Optional saved PNG; never contacts an image provider"
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ["SOLA_CHAT_FIXTURE_PASSWORD"] = PASSWORD
    origin = f"http://127.0.0.1:{args.port}"
    profile = resolve_profile(
        OperatorConfig.model_validate(
            {
                "data_dir": str(args.output / "data"),
                "agent_name": args.agent_name,
                "delegation_worker": "default",
                "model": {
                    "pin": "fixture-qwen",
                    "generation_timeout_seconds": 30,
                    "context_tokens": 1048576,
                    "image_inputs": True,
                },
                "defaults": {"workspace_roots": [str(args.output)], "memory_mode": "off"},
                "background": {"timezone": "UTC", "quiet_start": 0, "quiet_end": 0},
                "http": {
                    "port": args.port,
                    "allowed_origins": [origin],
                    "password_env": "SOLA_CHAT_FIXTURE_PASSWORD",
                },
            }
        )
    )
    context = compose(profile)
    assert context.model and context.tools and context.store
    context.tools.registry = Registry(
        (
            Tool("fixture.pause", "Wait four seconds.", ToolArguments, pause),
            next(tool for tool in TOOLS if tool.name == "delegate.run"),
        )
    )
    grants = frozenset({"fixture.pause", "delegate.run"})
    context.profile = profile.model_copy(update={"granted_tools": grants, "tools": grants})
    default_profile = context.tools.current

    def current_profile(admitted: EffectiveProfile) -> EffectiveProfile:
        if admitted.name == "no-tools":
            return default_profile(admitted.model_copy(update={"name": "default"})).model_copy(
                update={"name": admitted.name, "granted_tools": frozenset(), "tools": frozenset()}
            )
        return default_profile(admitted)

    context.tools.current = current_profile

    async def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "fixture-qwen"}]})
        body = json.loads(request.content)
        latest = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")
        if isinstance(latest, list):
            latest = next(part["text"] for part in latest if part.get("type") == "text")
        if latest.startswith("fail"):
            return httpx2.Response(503)
        delegation = latest == "Delegate the browser fixture."
        tool = (latest.startswith("tool") or delegation) and body["messages"][-1]["role"] != "tool"
        deltas: list[dict[str, Any]]
        if tool:
            function = (
                {"name": "delegate.run", "arguments": '{"task":"Report the fixture label."}'}
                if delegation
                else {"name": "fixture.pause", "arguments": "{}"}
            )
            deltas = [
                {
                    "content": "Delegating the fixture."
                    if delegation
                    else "Pausing briefly before I answer."
                },
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": new_id(),
                            "type": "function",
                            "function": function,
                        }
                    ]
                },
            ]
        elif delegation:
            deltas = [{"content": "The worker reported a blue fixture."}]
        elif latest == "Report the fixture label.":
            deltas = [{"content": "Fixture label: blue."}]
        else:
            content = (
                "NO_ACTION"
                if "no action" in latest.lower()
                else f"Sola heard: {latest[:160]}\n\n{HOSTILE}"
            )
            deltas = [{"content": content[i : i + 12]} for i in range(0, len(content), 12)]
        return httpx2.Response(
            200,
            stream=Stream(deltas, "tool_calls" if tool else "stop"),
            headers={"content-type": "text/event-stream"},
        )

    context.model.transport = httpx2.MockTransport(respond)
    app = create_app(context, profile_names=("no-tools",))
    lifecycle = app.router.lifespan_context

    @asynccontextmanager
    async def fixture_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with lifecycle(app):
            store = context.store
            assert store
            topic = store.create_session(title="A long conversation · 500 turns")
            source_ids: list[str] = []
            for i in range(500):
                run = store.admit(
                    topic.id,
                    request_id=new_id(),
                    content=f"Fixture turn {i + 1}",
                    profile="default",
                )
                store.transition_run(run.id, RunState.RUNNING)
                message = Message(
                    origin=Origin.INTERACTIVE,
                    session_id=topic.id,
                    run_id=run.id,
                    role="assistant",
                    content=f"Settled answer {i + 1}. A quiet place to keep thinking.\n\n{HOSTILE}",
                )
                if i == 499:
                    step = ModelStep(
                        origin=run.origin,
                        session_id=topic.id,
                        run_id=run.id,
                        index=1,
                        model="fixture-qwen",
                    )
                    store.append(step)
                    call = ToolCall(
                        origin=run.origin,
                        session_id=topic.id,
                        run_id=run.id,
                        model_step_id=step.id,
                        provider_tool_call_id=new_id(),
                        name="fixture.pause",
                        arguments={},
                    )
                    store.append(call)
                    store.append(
                        ToolReceipt(
                            origin=run.origin,
                            session_id=topic.id,
                            run_id=run.id,
                            tool_call_id=call.id,
                            status="uncertain",
                            effect="uncertain",
                            result=HOSTILE,
                            failure=Failure(
                                code=ErrorCode.EFFECT_UNCERTAIN,
                                message="Fixture effect is uncertain.",
                            ),
                        )
                    )
                    note = MemoryNote(
                        origin=run.origin,
                        session_id=topic.id,
                        run_id=run.id,
                        tool_call_id=call.id,
                        content="Synthetic staged note.",
                    )
                    store.append(note)
                    commit = MemoryCommit(
                        origin=run.origin, session_id=topic.id, run_id=run.id, note_ids=(note.id,)
                    )
                    store.append(commit)
                    store.transition_memory(commit.id, MemoryState.RUNNING)
                    store.transition_memory(commit.id, MemoryState.AMBIGUOUS)
                store.append(message)
                if i == 499:
                    store.append(
                        CompactionBoundary(
                            origin=run.origin,
                            session_id=topic.id,
                            run_id=run.id,
                            source_message_ids=tuple(source_ids[-3:]),
                            through_message_id=source_ids[-1],
                            summary_message_id=message.id,
                        )
                    )
                source_ids.append(message.id)
                store.transition_run(run.id, RunState.COMPLETED)
            other = store.create_session(title="A fresh topic")
            interrupted = store.admit(
                other.id, request_id=new_id(), content="An interrupted fixture", profile="default"
            )
            store.transition_run(
                interrupted.id,
                RunState.INTERRUPTED,
                reason=Failure(
                    code=ErrorCode.PROCESS_INTERRUPTED,
                    message="Process stopped; effects require inspection.",
                ),
            )
            metadata = {
                "origin": origin,
                "topic": topic.id,
                "other": other.id,
                "turns": 500,
                "agent_name": profile.agent_name,
            }
            if args.image_fixture:
                from solakit.tools.images import validate_png

                raw = args.image_fixture.read_bytes()
                width, height = int.from_bytes(raw[16:20]), int.from_bytes(raw[20:24])
                validate_png(raw, width, height)
                image_topic = store.create_session(title="Saved image fixture")
                image_run = store.admit(
                    image_topic.id,
                    request_id=new_id(),
                    content="Saved synthetic test image",
                    profile="default",
                )
                artifact = Artifact.model_validate(
                    store.artifact(new_id(), raw) | {"media_type": "image/png"}
                )
                store.append(
                    Message(
                        origin=Origin.INTERACTIVE,
                        session_id=image_topic.id,
                        run_id=image_run.id,
                        role="user",
                        content="Saved image for browser verification",
                        image=artifact,
                    )
                )
                store.transition_run(image_run.id, RunState.CANCELLED)
                metadata.update(image_topic=image_topic.id, image_width=width, image_height=height)
            (args.output / "fixture.json").write_text(json.dumps(metadata, indent=2))
            print(f"Browser fixture ready at {origin}; password: {PASSWORD}", flush=True)
            yield

    app.router.lifespan_context = fixture_lifespan
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
