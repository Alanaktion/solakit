import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from test_api import login
from test_compaction import compact_profile, material, seed, summary_reply
from test_run_loop import call, harness, reply, wire
from test_vision import OBSERVATION, submit, topic, upload, vision  # noqa: F401

from solakit.config import ConfigError, EffectiveProfile, MemorySettings
from solakit.engine.compaction import active_boundary, select
from solakit.engine.run_loop import records
from solakit.records import (
    ErrorCode,
    Failure,
    Message,
    ModelStep,
    Run,
    RunState,
    SessionMode,
    ToolCall,
    ToolReceipt,
    new_id,
)
from solakit.runtime import ApplicationContext
from solakit.storage.session_transfer import export_session, import_session

WRITE = '{"path":"note.txt","content":"once"}'


def scripted(
    *steps: dict[str, Any] | None,
) -> Callable[[dict[str, Any]], Awaitable[bytes]]:
    """None is a provider failure; compaction requests are answered separately."""
    queue = list(steps)

    async def respond(body: dict[str, Any]) -> bytes:
        if source := material(body):
            return summary_reply(source)
        step = queue.pop(0)
        return b"data: not-json\n\n" if step is None else wire(step)

    return respond


async def finish(context: ApplicationContext, run: Run) -> Run:
    assert context.runs
    return await context.runs.wait(run.id, timeout=5)


async def ask(context: ApplicationContext, session_id: str, content: str) -> Run:
    assert context.runs
    run = context.runs.submit(
        session_id, profile=context.profile, content=content, request_id=new_id()
    )
    return await finish(context, run)


def messages(context: ApplicationContext, session_id: str) -> list[tuple[str, str]]:
    assert context.store
    return [(m.role, m.content) for _, m in context.store.messages(session_id, limit=100)]


@pytest.mark.parametrize("mode", [SessionMode.SHARED, SessionMode.EPHEMERAL])
def test_retry_before_any_tool_call(profile: EffectiveProfile, mode: SessionMode) -> None:
    async def scenario() -> None:
        selected = profile.model_copy(update={"mode": mode})
        context, requests = harness(selected, scripted(None, reply("Hello.")))
        await context.start()
        assert context.store and context.runs
        try:
            session = context.store.create_session(mode=mode)
            failed = await ask(context, session.id, "Say hello.")
            assert failed.state == RunState.FAILED
            assert context.runs.retryable(session.id) == failed.id
            first = context.runs.retry(failed.id)
            # Concurrent clicks and a resubmission after a lost response share the successor.
            assert context.runs.retry(failed.id).id == first.id
            done = await finish(context, first)
            assert done.state == RunState.COMPLETED, done.reason
            assert context.runs.retry(failed.id).id == first.id
            assert context.runs.retryable(session.id) is None
            assert messages(context, session.id) == [
                ("user", "Say hello."),
                ("assistant", "Hello."),
            ]
            assert [m for m in requests[-1]["messages"] if m["role"] == "user" and "name" not in m][
                -1
            ]["content"] == "Say hello."
        finally:
            await context.stop()

    asyncio.run(scenario())


def test_retry_after_write_skips_repeat_across_a_second_failure(
    profile: EffectiveProfile, tmp_path: Path
) -> None:
    async def scenario() -> bytes:
        write = reply("", calls=[call("files.write", WRITE)])
        context, requests = harness(
            profile,
            scripted(
                write,
                None,
                reply("", calls=[call("files.write", WRITE)]),
                None,
                reply("", calls=[call("files.write", WRITE)]),
                reply("Saved."),
                reply("Next."),
            ),
        )
        await context.start()
        assert context.store and context.runs
        store, runs = context.store, context.runs
        try:
            session = store.create_session()
            first = await ask(context, session.id, "Write note.txt.")
            assert first.state == RunState.FAILED
            (tmp_path / "note.txt").write_text("edited since")
            second = await finish(context, runs.retry(first.id))
            assert second.state == RunState.FAILED
            assert runs.retryable(session.id) == second.id
            third = await finish(context, runs.retry(second.id))
            assert third.state == RunState.COMPLETED, third.reason
            assert (tmp_path / "note.txt").read_text() == "edited since"
            original = next(r for r in records(store, first.id) if isinstance(r, ToolReceipt))
            for attempt in (second, third):
                skipped = next(r for r in records(store, attempt.id) if isinstance(r, ToolReceipt))
                assert skipped.effect == "none" and isinstance(skipped.result, dict)
                assert skipped.result["receipt_id"] == original.id
            seen = requests[-2]["messages"]
            assert [m["content"] for m in seen if m["role"] == "user" and "name" not in m] == [
                "Write note.txt."
            ]
            assert any(m["role"] == "tool" for m in seen)
            assert not any("Previous run outcome" in str(m["content"]) for m in seen)
            assert messages(context, session.id)[-1] == ("assistant", "Saved.")
            assert [role for role, _ in messages(context, session.id)].count("user") == 1
            assert (await ask(context, session.id, "Thanks.")).state == RunState.COMPLETED
            history = requests[-1]["messages"]
            assert sum(m["content"] == "Write note.txt." for m in history) == 1
            assert not any("Previous run outcome" in str(m["content"]) for m in history)
            return export_session(store, session.id)
        finally:
            await context.stop()

    archive = asyncio.run(scenario())

    async def imported() -> None:
        context, _ = harness(profile.model_copy(update={"data_dir": tmp_path / "copy"}), [])
        await context.start()
        assert context.store and context.runs
        try:
            root = import_session(context.store, archive, new_id())
            copied = [run for _, run in context.store.runs(root)]
            assert [run.retry_of for run in copied] == [None, copied[0].id, copied[1].id, None]
            assert context.runs.retryable(root) is None
            with pytest.raises(ValueError):
                context.runs.retry(copied[1].id)
        finally:
            await context.stop()

    asyncio.run(imported())


@pytest.mark.parametrize("block", ["newer", "archived", "uncertain", "missing", "revoked"])
def test_retry_rejections(profile: EffectiveProfile, block: str) -> None:
    async def scenario() -> None:
        context, _ = harness(profile, scripted(None, reply("Newer.")))
        await context.start()
        assert context.store and context.runs
        store, runs = context.store, context.runs
        try:
            session = store.create_session()
            failed = await ask(context, session.id, "Hello.")
            if block == "newer":
                await ask(context, session.id, "Never mind.")
            elif block == "archived":
                store.update_session(session.id, archived=True)
            elif block == "revoked":
                current = runs.dispatcher.current
                runs.dispatcher.current = lambda admitted: current(admitted).model_copy(
                    update={"model_pin": "other"}
                )
            else:
                # A hand-built failed attempt holding an unsettled tool intent.
                failed = store.admit(
                    session.id,
                    request_id=new_id(),
                    content="Again.",
                    profile=failed.profile,
                    admission=store.admission_profile(failed.id),
                )
                failed = store.transition_run(failed.id, RunState.RUNNING)
                step = ModelStep(
                    origin=failed.origin,
                    session_id=session.id,
                    run_id=failed.id,
                    index=1,
                    model="fixture-qwen",
                    content="",
                )
                store.append(step)
                intent = ToolCall(
                    origin=failed.origin,
                    session_id=session.id,
                    run_id=failed.id,
                    model_step_id=step.id,
                    provider_tool_call_id="a",
                    name="files.write",
                    arguments={},
                )
                store.append(intent)
                if block == "uncertain":
                    store.append(
                        ToolReceipt(
                            origin=failed.origin,
                            session_id=session.id,
                            run_id=failed.id,
                            tool_call_id=intent.id,
                            status="uncertain",
                            effect="uncertain",
                            failure=Failure(code=ErrorCode.EFFECT_UNCERTAIN, message="Unknown"),
                        )
                    )
                failed = store.transition_run(failed.id, RunState.FAILED)
            assert runs.retryable(session.id) is None
            with pytest.raises((ValueError, ConfigError)):
                runs.retry(failed.id)
        finally:
            await context.stop()

    asyncio.run(scenario())


def test_queued_retry_survives_restart(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        context, _ = harness(profile, scripted(None))
        await context.start()
        assert context.store and context.runs
        try:
            session = context.store.create_session()
            failed = await ask(context, session.id, "Hello.")
            assert context.runs.worker
            context.runs.worker.cancel()
            queued = context.runs.retry(failed.id)
        finally:
            await context.stop()
        restarted, requests = harness(profile, scripted(reply("Recovered.")))
        await restarted.start()
        try:
            done = await finish(restarted, queued)
            assert done.state == RunState.COMPLETED, done.reason
            assert any(m["content"] == "Hello." for m in requests[0]["messages"])
        finally:
            await restarted.stop()

    asyncio.run(scenario())


def test_retry_chain_memory_extraction(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        selected = profile.model_copy(
            update={
                "memory": MemorySettings(
                    enabled=True, account="test", roots=("viking://user/default/memories",)
                )
            }
        )
        context, _ = harness(
            selected,
            scripted(
                reply("", calls=[call("memory.remember", '{"content":"kept"}')]),
                None,
                reply("Noted."),
            ),
        )
        await context.start()
        assert context.store and context.runs and context.memory
        store = context.store
        try:
            assert context.memory.worker
            context.memory.worker.cancel()
            session = store.create_session()
            failed = await ask(context, session.id, "Remember this.")
            done = await finish(context, context.runs.retry(failed.id))
            assert done.state == RunState.COMPLETED, done.reason
            context.memory.enqueue(done)
            row = store.db.execute(
                "SELECT body FROM entities WHERE kind='memory' AND run_id=?", (done.id,)
            ).fetchone()
            payload = [m["content"] for m in json.loads(row[0])["payload"]]
            assert payload == ["Remember this.", "Noted.", "kept"]
            archive = export_session(store, session.id)
            import_session(store, archive, new_id())
        finally:
            await context.stop()

    asyncio.run(scenario())


def test_compaction_keeps_retry_chains_whole(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        context, _ = harness(
            compact_profile(profile),
            scripted(None, reply("Retried."), *[reply("Visible reply.")] * 4),
        )
        await context.start()
        assert context.store and context.runs
        store = context.store
        try:
            session = store.create_session()
            seed(store, session.id)
            failed = await ask(context, session.id, "Flaky question.")
            done = await finish(context, context.runs.retry(failed.id))
            assert done.state == RunState.COMPLETED, done.reason
            for _ in range(3):
                seed(store, session.id, count=2)
                later = await ask(context, session.id, "Continue.")
                assert later.state == RunState.COMPLETED, later.reason
                selection = select(store, later, 65536)
                tail = {run.id for run in selection.runs}
                assert (failed.id in tail) == (done.id in tail)
            assert active_boundary(store, session.id)
            assert done.id not in tail and failed.id not in tail
            import_session(store, export_session(store, session.id), new_id())
            assert any(
                isinstance(r, Message) and r.content == "Flaky question."
                for r in records(store, failed.id)
            )
        finally:
            await context.stop()
        await store.start()
        await store.stop()

    asyncio.run(scenario())


def test_retry_reuses_vision_analysis(vision: Any) -> None:  # noqa: F811
    client, context, state = vision
    login(client, bearer=True)
    session = topic(client)
    assert context.model
    original = context.model.transport
    assert original
    broken = {"on": True}
    handle = original.handler

    async def flaky(request: Any) -> Any:
        response = await handle(request)
        if broken["on"] and not request.url.path.endswith("/models"):
            broken["on"] = False
            return type(response)(200, content=b"data: not-json\n\n", headers=response.headers)
        return response

    original.handler = flaky
    run = submit(client, session, {"upload_id": upload(client, session)}).json()["run"]
    assert run["state"] == "failed", run
    page = client.get(f"/api/v1/sessions/{session}/runs?before=0").json()
    assert page["retryable"] == run["id"]
    retried = client.post(f"/api/v1/runs/{run['id']}/retry")
    assert retried.status_code in (200, 202), retried.text
    identity = retried.json()["run"]["id"]
    done = client.get(f"/api/v1/runs/{identity}?wait=5").json()
    assert done["state"] == "completed", done
    assert len(state["vision"]) == 1
    assert OBSERVATION in state["text"][-1]["messages"].__repr__()


def test_retry_applies_current_tool_grants(profile: EffectiveProfile, tmp_path: Path) -> None:
    async def scenario() -> None:
        context, _ = harness(
            profile,
            scripted(
                None,
                reply("", calls=[call("files.write", WRITE)]),
                reply("I cannot write that file."),
            ),
        )
        await context.start()
        assert context.store and context.runs
        runs = context.runs
        try:
            session = context.store.create_session()
            failed = await ask(context, session.id, "Hello.")
            current = runs.dispatcher.current
            runs.dispatcher.current = lambda admitted: current(admitted).model_copy(
                update={"tools": frozenset(), "granted_tools": frozenset()}
            )
            done = await finish(context, runs.retry(failed.id))
            receipt = next(r for r in records(context.store, done.id) if isinstance(r, ToolReceipt))
            assert receipt.status == "denied"
            assert not (tmp_path / "note.txt").exists()
        finally:
            await context.stop()

    asyncio.run(scenario())
