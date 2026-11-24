"""Corpus candidate using the production run service and disposable SDK tools."""

import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any

import httpx2

from evals.cases import Case, digest
from evals.sandbox import Sandbox
from evals.scoring import score
from solakit.config import EffectiveProfile, OperatorConfig, resolve_profile
from solakit.engine.run_loop import records
from solakit.records import (
    ErrorCode,
    Message,
    ModelRequest,
    ModelStep,
    RecoveryNote,
    RunState,
    ToolCall,
    ToolReceipt,
    new_id,
    utc_now,
)
from solakit.runtime import compose
from solakit.sdk import Tool, ToolArguments, ToolContext, ToolError, ToolResult
from solakit.storage.evidence import canonical
from solakit.tools.registry import Registry


class Read(ToolArguments):
    path: str


class Write(Read):
    content: str


class Publish(ToolArguments):
    text: str


class Recall(ToolArguments):
    query: str


def registry(sandbox: Sandbox) -> Registry:
    async def read(context: ToolContext, args: Read) -> ToolResult:
        return ToolResult(data=context.files.read_text(args.path))

    async def write(context: ToolContext, args: Write) -> ToolResult:
        return ToolResult(data=context.files.write_text(args.path, args.content, overwrite=True))

    async def publish(context: ToolContext, args: Publish) -> ToolResult:
        context.begin_effect(())
        sandbox.effects.append({"text": args.text})
        context.confirm_effect()
        return ToolResult(data={"fake_destination": "notes", "sequence": len(sandbox.effects)})

    async def recall(context: ToolContext, args: Recall) -> ToolResult:
        if sandbox.case.memory_outage:
            raise ToolError("Fixture memory unavailable", ErrorCode.MEMORY_UNAVAILABLE)
        return ToolResult(data=[])

    async def status(context: ToolContext, args: ToolArguments) -> ToolResult:
        return ToolResult(data={"runtime": "evaluation", "disposable": True})

    available: tuple[Tool[Any], ...] = (
        Tool("read_file", "Evaluation read", Read, read),
        Tool("write_file", "Evaluation write", Write, write, True),
        Tool("publish_note", "Fake publication", Publish, publish, True),
        Tool("recall", "Fixture recall", Recall, recall),
        Tool("runtime_info", "Evaluation status", ToolArguments, status),
    )
    names = {tool["function"]["name"] for tool in sandbox.case.tools}
    result = Registry(tool for tool in available if tool.name in names)
    # Preserve the corpus schemas exactly; the SDK models still validate actual dispatch.
    result.schemas = {tool["function"]["name"]: tool for tool in sandbox.case.tools}
    return result


def fixture_transport(case: Case, model: str) -> httpx2.MockTransport:
    responses = iter(copy.deepcopy(case.fake_responses))

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": model}]})
        data = next(responses)
        if data.get("interrupted"):
            wire = (
                b"data: "
                + canonical(
                    {
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": data["partial"]},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                + b"\n\n"
            )
        else:
            # Transport framing only: normalization, persistence and recovery are production code.
            choice = data["choices"][0]
            message = choice["message"]
            if message.get("tool_calls"):
                message["tool_calls"] = [
                    c | {"index": i} for i, c in enumerate(message["tool_calls"])
                ]
            wire = (
                b"data: "
                + canonical(
                    {
                        "model": data["model"],
                        "choices": [
                            {
                                "index": 0,
                                "delta": message,
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                + b"\n\n"
            )
            wire += (
                b"data: "
                + canonical(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": choice["finish_reason"],
                            }
                        ],
                        "usage": data.get("usage"),
                    }
                )
                + b"\n\ndata: [DONE]\n\n"
            )
        return httpx2.Response(200, content=wire, headers={"content-type": "text/event-stream"})

    return httpx2.MockTransport(respond)


async def run_runtime_trial(
    case: Case,
    trial: int,
    model: str,
    *,
    live: bool = False,
    base_url: str = "http://127.0.0.1:8000/v1",
    deferred: bool = False,
    output: Path | None = None,
) -> dict[str, Any]:
    with TemporaryDirectory(prefix="sola-runtime-eval-") as directory:
        root = output or Path(directory)
        if output is not None:
            root.mkdir(parents=True, exist_ok=False, mode=0o700)
        workspace = Path(directory) / "workspace"
        workspace.mkdir(mode=0o700)
        sandbox = Sandbox(workspace, case)
        fragment = root / "instructions.md"
        fragment.write_text("\n".join(m["content"] for m in case.messages if m["role"] == "system"))
        config = OperatorConfig.model_validate(
            {
                "data_dir": str(root / "data"),
                "model": {
                    "pin": model,
                    "base_url": base_url,
                    "generation_timeout_seconds": 60,
                    "api_key_env": "SOLA_EVAL_API_KEY" if os.getenv("SOLA_EVAL_API_KEY") else None,
                },
                "defaults": {
                    "workspace_roots": [str(workspace)],
                    "fragments": [str(fragment)],
                    "budgets": {"model_calls": case.max_steps, "output_tokens": 1024},
                },
            }
        )
        profile = resolve_profile(config)
        app = compose(profile)
        assert app.model and app.tools and app.runs and app.store
        app.tools.registry = registry(sandbox)
        names = frozenset(app.tools.registry.tools)
        app.profile = profile.model_copy(
            update={
                "granted_tools": names,
                "tools": names,
            }
        )
        active_run_id: str | None = None

        def current(admitted: EffectiveProfile) -> EffectiveProfile:
            assert app.store
            revoked = (
                frozenset(case.denied_tools)
                if active_run_id and app.store.tool_attempts(active_run_id)
                else frozenset()
            )
            return app.profile.model_copy(
                update={
                    "granted_tools": names - revoked,
                    "tools": names - revoked,
                }
            )

        app.tools.current = current
        if deferred:
            app.runs.deferred = names
        if not live or case.fixture_only:
            app.model.transport = fixture_transport(case, model)
        result: dict[str, Any] = {
            "version": 1,
            "harness": "runtime-v1",
            "case": case.model_dump(),
            "case_hash": digest(case.model_dump()),
            "trial": trial,
            "steps": [],
            "receipts": [],
            "final_message": None,
            "outcome": "failed",
            "started_at": utc_now().isoformat(),
            "fixture_only": case.fixture_only,
        }
        start = perf_counter()
        await app.start()
        try:
            session = app.store.create_session(title="Disposable corpus trial")
            admitted = app.runs.submit(
                session.id,
                profile=app.profile,
                request_id=new_id(),
                content=next(m["content"] for m in reversed(case.messages) if m["role"] == "user"),
            )
            active_run_id = admitted.id
            run = await app.runs.wait(admitted.id, timeout=case.max_steps * 65)
            source = records(app.store, run.id)
            result["runtime_state"] = run.state.value
            result["reason"] = run.reason.model_dump(mode="json") if run.reason else None
            result["evidence"] = str(root / "data") if output else "disposable"
            result["recovery"] = [
                r.model_dump(mode="json") for r in source if isinstance(r, RecoveryNote)
            ]
            requests = {r.id: r for r in source if isinstance(r, ModelRequest)}
            calls = {r.id: r for r in source if isinstance(r, ToolCall)}
            for step in (r for r in source if isinstance(r, ModelStep)):
                assert step.request_id and step.response
                request_record = requests[step.request_id]
                request = json.loads((app.store.root / request_record.body.path).read_bytes())
                raw = (app.store.root / step.response.path).read_bytes()
                result["steps"].append(
                    {
                        "request": request,
                        "request_hash": digest(request),
                        "prompt_hash": digest(request["messages"]),
                        "schema_hash": digest(request.get("tools", [])),
                        "response": {
                            "raw_sse": raw.decode(errors="replace"),
                            "normalized": step.model_dump(mode="json"),
                            "usage": step.usage,
                        },
                    }
                )
                result["final_message"] = {
                    "role": "assistant",
                    "content": step.content,
                    "reasoning_content": step.reasoning,
                }
            for record in source:
                if isinstance(record, ModelStep) and record.failure and record.tool_calls:
                    result["receipts"].extend(
                        {
                            "call_id": p.provider_tool_call_id,
                            "name": p.name,
                            "arguments": p.arguments,
                            "status": "invalid_arguments",
                            "executed": False,
                            "model_step_id": record.id,
                        }
                        for p in record.tool_calls
                    )
                if not isinstance(record, ToolReceipt):
                    continue
                receipt = record
                tool = calls[receipt.tool_call_id]
                status_name: str = receipt.status
                if receipt.status == "succeeded":
                    status_name = "ok"
                elif receipt.failure:
                    status_name = {
                        ErrorCode.INVALID_INPUT: "invalid_arguments",
                        ErrorCode.MEMORY_UNAVAILABLE: "unavailable",
                        ErrorCode.TOOL_DENIED: "denied" if tool.name in names else "missing_tool",
                    }.get(receipt.failure.code, receipt.status)
                result["receipts"].append(
                    {
                        "call_id": tool.provider_tool_call_id,
                        "name": tool.name,
                        "arguments": canonical(tool.arguments).decode(),
                        "status": status_name,
                        "receipt": receipt.model_dump(mode="json"),
                    }
                )
            steps = [r for r in source if isinstance(r, ModelStep)]
            if steps and steps[-1].failure is None and steps[-1].finish_reason == "stop":
                result["outcome"] = "stop"
            if run.state != RunState.COMPLETED and not any(
                r.role == "assistant" for r in source if isinstance(r, Message)
            ):
                result["outcome"] = run.reason.code.value if run.reason else run.state.value
            if (
                case.fixture_only
                and run.reason
                and run.reason.code == ErrorCode.INVALID_MODEL_RESPONSE
            ):
                result["outcome"] = "interrupted"
            result["artifacts"] = sandbox.artifacts()
            result["effects"] = sandbox.effects
        finally:
            await app.stop()
        result["latency_seconds"] = perf_counter() - start
        result["score"] = score(case, result)
        return result
