"""Disposable Phase 11 browser fixture. No live model, memory or operator data."""

import argparse
import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, TypedDict

import httpx2
import uvicorn
from fastapi import FastAPI
from PIL import Image

from solakit.api import create_app
from solakit.config import EffectiveProfile, OperatorConfig, resolve_profile
from solakit.records import (
    Artifact,
    CompactionBoundary,
    ErrorCode,
    Failure,
    ImageGeneration,
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
from solakit.tools.images import validate_png
from solakit.tools.registry import Registry

PASSWORD = "phase11-disposable-fixture"
HOSTILE = '<img src=x onerror="window.PWNED=true"> [bad](javascript:alert(1)) **Safe bold**'
CONVERTED = "# Fixture document\n\nExtracted Markdown.\n"


class RunLineage(TypedDict):
    origin: Origin
    session_id: str
    run_id: str


class FeedBody(httpx2.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        path = Path(__file__).resolve().parents[1] / "tests/fixtures/feeds/rss.xml"
        yield path.read_bytes()


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
                "feeds": {"enabled": True},
                "memory": {"enabled": True, "roots": ["viking://user/default/memories"]},
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
    assert context.model and context.tools and context.store and context.feeds
    images, memory = context.images, context.memory
    assert images and memory
    context.tools.registry = Registry(
        (
            Tool("fixture.pause", "Wait four seconds.", ToolArguments, pause),
            *(tool for tool in TOOLS if tool.name in ("delegate.run", "feeds.propose")),
        )
    )
    grants = frozenset(context.tools.registry.tools)
    context.profile = profile.model_copy(update={"granted_tools": grants, "tools": grants})
    default_profile = context.tools.current

    def current_profile(admitted: EffectiveProfile) -> EffectiveProfile:
        if admitted.name == "no-tools":
            return default_profile(admitted.model_copy(update={"name": "default"})).model_copy(
                update={"name": admitted.name, "granted_tools": frozenset(), "tools": frozenset()}
            )
        return default_profile(admitted)

    context.tools.current = current_profile
    flaked: set[str] = set()
    memory_commit_id = ""
    late_image: ImageGeneration | None = None
    if args.image_fixture:
        png = args.image_fixture.read_bytes()
    else:
        output = BytesIO()
        image = Image.new("RGB", (320, 192), "teal")
        image.paste("gold", (12, 12, 308, 38))
        image.save(output, format="PNG")
        png = output.getvalue()
    width = int.from_bytes(png[16:20])
    height = int.from_bytes(png[20:24])
    validate_png(png, width, height)

    def memory_response(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/health":
            return httpx2.Response(200, json={"status": "ok", "version": "0.5.5"})
        assert request.url.path.startswith("/api/v1/tasks/")
        return httpx2.Response(
            200,
            json={
                "status": "ok",
                "result": {
                    "task_type": "session_commit",
                    "resource_id": "sola-" + memory_commit_id
                    if request.url.path.endswith("/fixture-task")
                    else "other-session",
                    "status": "completed",
                },
            },
        )

    memory.adapter.transport = httpx2.MockTransport(memory_response)
    memory.current = lambda admitted: admitted

    async def fixture_resolver(host: str, port: int) -> list[str]:
        return ["93.184.215.14"]

    def feed_response(request: httpx2.Request) -> httpx2.Response:
        if request.headers["Host"] == "fixture-feed.example":
            return httpx2.Response(200, stream=FeedBody())
        return httpx2.Response(503)

    context.feeds.resolver = fixture_resolver
    context.feeds.transport = httpx2.MockTransport(feed_response)

    async def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return httpx2.Response(
                200,
                json={"data": [{"id": "fixture-qwen"}, {"id": "MarkItDown", "owned_by": "omlx"}]},
            )
        body = json.loads(request.content)
        if body["model"] == "MarkItDown":
            return httpx2.Response(
                200,
                json={
                    "model": "MarkItDown",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": CONVERTED},
                        }
                    ],
                },
            )
        latest = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")
        if isinstance(latest, list):
            latest = next(part["text"] for part in latest if part.get("type") == "text")
        if latest.startswith("fail"):
            return httpx2.Response(503)
        # "tool flaky" fails once after its tool result, so Retry can finish the turn.
        if "flaky" in latest and body["messages"][-1]["role"] == "tool" and latest not in flaked:
            flaked.add(latest)
            return httpx2.Response(503)
        delegation = latest == "Delegate the browser fixture."
        proposal = latest.startswith("Suggest the fixture feed ")
        tool = (latest.startswith("tool") or delegation or proposal) and body["messages"][-1][
            "role"
        ] != "tool"
        deltas: list[dict[str, Any]]
        if tool:
            function = (
                {"name": "delegate.run", "arguments": '{"task":"Report the fixture label."}'}
                if delegation
                else {"name": "fixture.pause", "arguments": "{}"}
            )
            if proposal:
                function = {
                    "name": "feeds.propose",
                    "arguments": json.dumps(
                        {
                            "url": "https://fixture-feed.example/rss/" + latest.rsplit(" ", 1)[-1],
                            "reason": "Fixture subscription proposal",
                            "discovered_from": "Fixture discovery topic",
                        }
                    ),
                }
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
        elif proposal:
            deltas = [{"content": "Fixture feed proposed."}]
        elif latest.startswith('{"message":"What is in the fixture excerpt?'):
            excerpt = json.loads(latest)["untrusted_document_attachments"][0]["text"]
            assert (
                json.loads(excerpt)["untrusted_external_feed_excerpt"]["title"] == "A first story"
            )
            deltas = [{"content": "I read the external feed excerpt."}]
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

    @app.post("/api/v1/fixture/complete-image", include_in_schema=False)
    async def complete_image() -> dict[str, str]:
        nonlocal late_image
        store = context.store
        assert late_image and store
        if late_image.artifact is None:
            artifact = Artifact.model_validate(
                store.artifact(late_image.id, png) | {"media_type": "image/png"}
            )
            late_image = images.save(late_image, state="downloaded", artifact=artifact, error=None)
        return {"artifact_id": late_image.id}

    @asynccontextmanager
    async def fixture_lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal late_image, memory_commit_id
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
                    admission=profile.model_copy(update={"memory_mode": "optional"}).model_dump(
                        mode="json"
                    )
                    if i == 499
                    else None,
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
                    memory_commit_id = commit.id
                    store.transition_memory(commit.id, MemoryState.RUNNING)
                    store.transition_memory(commit.id, MemoryState.AMBIGUOUS)
                if i == 499:
                    summary = Message(
                        origin=run.origin,
                        session_id=topic.id,
                        run_id=run.id,
                        role="assistant",
                        content=(
                            "The earlier fixture turns established a working plan.\n\n"
                            "- **Decided** to keep the existing topic.\n"
                            "- **Next** review the remaining work."
                        ),
                    )
                    store.append(summary)
                    store.append(
                        CompactionBoundary(
                            origin=run.origin,
                            session_id=topic.id,
                            run_id=run.id,
                            source_message_ids=tuple(source_ids[-3:]),
                            through_message_id=source_ids[-1],
                            summary_message_id=summary.id,
                        )
                    )
                store.append(message)
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

            image_topic = store.create_session(title="Saved image fixture")
            uploaded = Artifact.model_validate(
                store.artifact(new_id(), png) | {"media_type": "image/png"}
            )
            image_run = store.admit(
                image_topic.id,
                request_id=new_id(),
                content="Saved image for browser verification",
                profile="default",
                image=uploaded,
            )
            store.transition_run(image_run.id, RunState.CANCELLED)

            result_topic = store.create_session(title="Tool results fixture")
            result_run = store.admit(
                result_topic.id,
                request_id=new_id(),
                content="Inspect fixture work",
                profile="default",
            )
            store.transition_run(result_run.id, RunState.RUNNING)
            lineage: RunLineage = {
                "origin": result_run.origin,
                "session_id": result_topic.id,
                "run_id": result_run.id,
            }
            step = ModelStep(**lineage, index=1, model="fixture-qwen")
            store.append(step)

            def record_call(name: str) -> ToolCall:
                call = ToolCall(
                    **lineage,
                    name=name,
                    model_step_id=step.id,
                    provider_tool_call_id=new_id(),
                    arguments={"fixture": name},
                )
                store.append(call)
                return call

            search = record_call("files.search")
            store.append(
                ToolReceipt(
                    **lineage,
                    tool_call_id=search.id,
                    status="succeeded",
                    result=json.dumps({"matches": ["one", "two", "three"], "truncated": True}),
                )
            )
            long_call = record_call("files.write")
            long_text = b"First chunk\n" + b"a" * (65536 - 12) + b"Second chunk\n"
            long_artifact = Artifact.model_validate(store.artifact(new_id(), long_text))
            store.append(
                ToolReceipt(
                    **lineage,
                    tool_call_id=long_call.id,
                    status="succeeded",
                    result=json.dumps({"path": "fixture/notes.md"}),
                    artifact=long_artifact,
                )
            )
            failures: list[tuple[Literal["failed", "denied", "cancelled"], ErrorCode, str]] = [
                ("failed", ErrorCode.TOOL_FAILED, "Could not read fixture output"),
                ("denied", ErrorCode.TOOL_DENIED, "Permission denied"),
                ("cancelled", ErrorCode.CANCELLED, "Tool execution cancelled"),
            ]
            for status, code, reason in failures:
                call = record_call(f"fixture.{status}")
                store.append(
                    ToolReceipt(
                        **lineage,
                        tool_call_id=call.id,
                        status=status,
                        failure=Failure(code=code, message=reason),
                    )
                )
            record_call("fixture.missing")

            def generation() -> ImageGeneration:
                call = record_call("images.generate")
                body = Artifact.model_validate(store.artifact(new_id(), b"{}"))
                return ImageGeneration(
                    **lineage,
                    tool_call_id=call.id,
                    endpoint="http://fixture.invalid",
                    workflow_sha256=body.sha256,
                    parameters={"width": width, "height": height},
                    fingerprint=body.sha256,
                    body=body,
                    requested_id=new_id(),
                )

            generated = generation()
            generated_artifact = Artifact.model_validate(
                store.artifact(generated.id, png) | {"media_type": "image/png"}
            )
            images.save(generated, state="downloaded", artifact=generated_artifact)
            store.append(
                ToolReceipt(
                    **lineage,
                    tool_call_id=generated.tool_call_id,
                    status="succeeded",
                    effect="confirmed",
                    artifact=generated_artifact,
                )
            )
            late_image = images.save(generation(), state="uncertain", error="Still checking")
            store.append(
                ToolReceipt(
                    **lineage,
                    tool_call_id=late_image.tool_call_id,
                    status="uncertain",
                    effect="uncertain",
                    failure=Failure(code=ErrorCode.EFFECT_UNCERTAIN, message="Still checking"),
                )
            )
            store.transition_run(result_run.id, RunState.COMPLETED)
            metadata = {
                "origin": origin,
                "topic": topic.id,
                "other": other.id,
                "turns": 500,
                "agent_name": profile.agent_name,
                "memory_commit_id": memory_commit_id,
                "image_topic": image_topic.id,
                "image_width": width,
                "image_height": height,
                "result_topic": result_topic.id,
                "result_run": result_run.id,
                "generated_image": generated.id,
                "late_image": late_image.id,
            }
            (args.output / "fixture.json").write_text(json.dumps(metadata, indent=2))
            print(f"Browser fixture ready at {origin}; password: {PASSWORD}", flush=True)
            yield

    app.router.lifespan_context = fixture_lifespan
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
