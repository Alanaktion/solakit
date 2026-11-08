"""Regressions for defects found in the 2026-11 code audit."""

import asyncio
import json
import os
import signal
import sys

import httpx
import pytest

from solakit.core.loop import Execution
from solakit.core.model import ModelClient, ModelUnavailable
from solakit.core.schemas import Origin, Run, Session
from solakit.core.store import Store
from solakit.services import process
from solakit.tools import Registry, ToolContext, tool
from solakit.ui import auth


def model_list(config):
    return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})


def sse(config, content, finish):
    chunks = [
        {
            "model": config.model.model_id,
            "choices": [{"index": 0, "delta": {"role": "assistant"}}],
        },
        *(
            [
                {
                    "model": config.model.model_id,
                    "choices": [{"index": 0, "delta": {"content": content}}],
                }
            ]
            if content
            else []
        ),
        {
            "model": config.model.model_id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
        },
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(
        200, content=body.encode(), headers={"content-type": "text/event-stream"}
    )


@pytest.mark.parametrize(
    ("content", "finish", "accepted"),
    [("", "stop", False), ("  \n", "stop", False), ("", "length", True)],
)
def test_streamed_empty_reply_is_not_a_completed_answer(
    config, content, finish, accepted
):
    def handler(request):
        return sse(config, content, finish)

    model = ModelClient(config.model, transport=httpx.MockTransport(handler))

    async def exercise():
        try:
            if accepted:
                data = await model.complete([{"role": "user", "content": "x"}], [])
                assert data["choices"][0]["finish_reason"] == "length"
            else:
                with pytest.raises(ModelUnavailable, match="Empty completion"):
                    await model.complete([{"role": "user", "content": "x"}], [])
        finally:
            await model.close()

    asyncio.run(exercise())


def test_completed_tool_result_survives_cancellation_request(config):
    store = Store(config.paths.data)
    store.create_session(Session(id="s"))
    entered = None

    @tool(name="finish", profiles=["*"])
    async def finish(ctx: ToolContext) -> str:
        """Finish accepted work even when the run is cancelled meanwhile."""
        entered.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            asyncio.current_task().uncancel()
        return "accepted work finished"

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        call = {
            "id": "call1",
            "type": "function",
            "function": {"name": "finish", "arguments": "{}"},
        }
        return httpx.Response(
            200,
            json={
                "model": config.model.model_id,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [call],
                        }
                    }
                ],
            },
        )

    registry = Registry(config)
    registry.register(finish)
    config.profiles["interactive"].tools = ["finish"]
    model = ModelClient(config.model, transport=httpx.MockTransport(handler))
    execution = Execution(config, store, registry, model)

    async def exercise():
        nonlocal entered
        entered = asyncio.Event()
        submission = asyncio.create_task(execution.submit("s", "go"))
        await entered.wait()
        run_id = next(iter(execution.pending))
        assert await execution.cancel(run_id)
        run = await submission
        assert run.status == "cancelled"
        tool_messages = [m for m in store.transcript("s") if m["role"] == "tool"]
        assert len(tool_messages) == 1
        assert "accepted work finished" in tool_messages[0]["content"]
        await execution.close()

    asyncio.run(exercise())


def test_escaped_descendant_cannot_hold_shell_output_open(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "DRAIN_GRACE_S", 0.5)
    script = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; "
        "time.sleep(60)'], start_new_session=True)\n"
        "print(child.pid, flush=True)\n"
    )

    async def exercise():
        async with asyncio.timeout(20):
            return await process.execute(
                [sys.executable, "-c", script], tmp_path, 1, 10000
            )

    result = asyncio.run(exercise())
    try:
        assert result["timed_out"]
        assert not result["stdout_complete"]
        assert result["stdout"].strip().isdigit()
    finally:
        if result["stdout"].strip().isdigit():
            try:
                os.kill(int(result["stdout"].strip()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_unavailable_memory_status_does_not_starve_pending_runs(config):
    store = Store(config.paths.data)
    store.create_session(Session(id="s"))
    stuck = Run(
        session="s",
        profile="interactive",
        origin=Origin(),
        status="completed",
        memory_status="status_unavailable",
    )
    fresh = Run(
        session="s",
        profile="interactive",
        origin=Origin(),
        status="completed",
        memory_status="accepted",
    )
    store.save_run(stuck)
    store.save_run(fresh)
    assert [r["id"] for r in store.pending_memory()] == [fresh.id, stuck.id]


def test_session_renewal_persistence_is_throttled(tmp_path, monkeypatch):
    clock = [100.0]
    sessions = auth.UISessions(clock=lambda: clock[0])
    sessions.load(tmp_path / "ui-sessions.json", "token")
    identity, _ = sessions.login("token", "token", "peer")
    writes = []
    original = sessions.save
    monkeypatch.setattr(sessions, "save", lambda: (writes.append(1), original()))
    for _ in range(20):
        clock[0] += 1
        assert sessions.valid(identity)
    assert writes == []
    clock[0] += getattr(auth, "RENEWAL_PERSIST_S", 60)
    assert sessions.valid(identity)
    assert len(writes) == 1
    restarted = auth.UISessions(clock=lambda: clock[0])
    restarted.load(tmp_path / "ui-sessions.json", "token")
    assert restarted.valid(identity)
