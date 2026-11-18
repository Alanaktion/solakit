import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_run_loop import call, harness, reply, wire

from solakit.compaction import INSTRUCTIONS, active_boundary, manifest_for, select
from solakit.config import EffectiveProfile
from solakit.evidence import CorruptionError, canonical, digest
from solakit.memory import MemoryService
from solakit.model import ModelError, ModelResult
from solakit.prompts import PreparedPrompt
from solakit.records import (
    ErrorCode,
    Failure,
    Message,
    ModelRequest,
    ModelStep,
    Run,
    RunState,
    SessionMode,
    ToolCall,
    ToolProposal,
    ToolReceipt,
    new_id,
)
from solakit.run_loop import records
from solakit.sdk import Tool, ToolArguments, ToolContext, ToolResult
from solakit.search import search
from solakit.store import Store


def compact_profile(profile: EffectiveProfile) -> EffectiveProfile:
    return profile.model_copy(
        update={
            "model_context_tokens": 18000,
            "budgets": profile.budgets.model_copy(update={"output_tokens": 1024}),
            "memory_mode": "off",
        }
    )


def seed(store: Store, session_id: str, *, count: int = 6, uncertain: bool = False) -> list[Run]:
    result = []
    for i in range(count):
        run = store.admit(
            session_id,
            request_id=new_id(),
            profile="default",
            content=f"Fact {i}: deadline corrected to 2026-12-05. Commitment: finish report. "
            + "background detail " * 65,
        )
        run = store.transition_run(run.id, RunState.RUNNING)
        proposals = (ToolProposal(provider_tool_call_id="file", name="files.read", arguments="{}"),)
        step = ModelStep(
            origin=run.origin,
            run_id=run.id,
            session_id=session_id,
            index=1,
            model="fixture-qwen",
            content="Acknowledged. " + "known detail " * 50,
            tool_calls=proposals if i == 0 else (),
        )
        store.append(step)
        if i == 0:
            call = ToolCall(
                origin=run.origin,
                run_id=run.id,
                session_id=session_id,
                model_step_id=step.id,
                provider_tool_call_id="file",
                name="files.read",
                arguments={},
            )
            store.append(call)
            store.append(
                ToolReceipt(
                    origin=run.origin,
                    run_id=run.id,
                    session_id=session_id,
                    tool_call_id=call.id,
                    status="uncertain" if uncertain else "succeeded",
                    effect="uncertain" if uncertain else "none",
                    result="Original tool result.",
                    failure=Failure(code=ErrorCode.EFFECT_UNCERTAIN, message="Unknown")
                    if uncertain
                    else None,
                )
            )
        store.append(
            Message(
                origin=run.origin,
                run_id=run.id,
                session_id=session_id,
                role="assistant",
                content=step.content,
            )
        )
        result.append(store.transition_run(run.id, RunState.COMPLETED))
    return result


def material(body: dict[str, Any]) -> dict[str, Any] | None:
    if not any(m["content"] == INSTRUCTIONS for m in body["messages"]):
        return None
    value = next(json.loads(m["content"]) for m in body["messages"] if m["role"] == "user")
    assert isinstance(value, dict)
    return value


def summary_reply(source: dict[str, Any], *, text: str | None = None) -> bytes:
    return wire(
        reply(
            canonical(
                {
                    "through_run_id": source["through_run_id"],
                    "source_sha256": source["source_sha256"],
                    "summary": text
                    if text is not None
                    else (
                        "Deadline corrected to 2026-12-05; finish report remains open. Source "
                        + source["turns"][0]["source_id"]
                    ),
                }
            ).decode()
        )
    )


def test_multiple_compactions_preserve_evidence_tail_search_and_memory(
    profile: EffectiveProfile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async def respond(body: dict[str, Any]) -> bytes:
            source = material(body)
            return summary_reply(source) if source else wire(reply("A visible reply."))

        context, requests = harness(compact_profile(profile), respond)
        await context.start()
        assert context.store and context.runs and context.memory
        store = context.store
        try:
            session = store.create_session()
            original_runs = seed(store, session.id)
            original_bytes = {p: p.read_bytes() for p in store.log.paths()}
            receipts = [
                r
                for run in original_runs
                for r in records(store, run.id)
                if isinstance(r, ToolReceipt)
            ]
            boundaries = []
            for _ in range(3):
                run = context.runs.submit(
                    session.id,
                    profile=context.profile,
                    content="Continue our conversation.",
                    request_id=new_id(),
                )
                outcome = await context.runs.wait(run.id, timeout=5)
                assert outcome.state == RunState.COMPLETED, outcome.reason
                boundary = active_boundary(store, session.id)
                assert boundary
                boundaries.append(boundary.id)
                selection = select(store, store.run(run.id), 65536)
                assert selection.summary and selection.boundary
                final = requests[-1]
                assert (
                    sum(m["content"] == selection.summary.content for m in final["messages"]) == 1
                )
                assert selection.provenance
                assert selection.provenance["selected_tail_run_ids"] == [
                    r.id for r in selection.runs
                ]
                assert len(selection.runs) >= 2
                seed(store, session.id, count=4)
            assert len(set(boundaries)) == 3
            assert any(
                source.get("previous_summary") for body in requests if (source := material(body))
            )
            for path, before in original_bytes.items():
                assert path.read_bytes().startswith(before)
            assert [store._load("receipt", r.id, ToolReceipt) for r in receipts] == receipts
            assert search(store.root, "deadline corrected", session_id=session.id)
            for request in [r for r in records(store, run.id) if isinstance(r, ModelRequest)]:
                manifest = json.loads((store.root / request.manifest.path).read_bytes())
                if manifest.get("purpose") != "compaction":
                    assert manifest["compaction"]["omitted_source_count"] > 0
                    assert "safety_reserved" in manifest["accounting"]
            # Extraction sees only admitted conversation, never the assistant summary.
            memory_profile = context.profile.model_copy(
                update={
                    "memory_mode": "optional",
                    "memory": context.profile.memory.model_copy(update={"enabled": True}),
                }
            )
            memory = MemoryService(store, context.memory.adapter, lambda _: memory_profile)
            monkeypatch.setattr(memory, "profile", lambda _: memory_profile)
            memory.enqueue(store.run(run.id))
            row = store.db.execute(
                "SELECT body FROM entities WHERE kind='memory' AND run_id=?",
                (run.id,),
            ).fetchone()
            assert selection.summary
            assert row and selection.summary.content not in {
                m["content"] for m in json.loads(row[0])["payload"]
            }
        finally:
            await context.stop()
        await store.start()
        try:
            active = active_boundary(store, session.id)
            assert active and active.id == boundaries[-1]
        finally:
            await store.stop()

    asyncio.run(scenario())


def test_concurrent_arrivals_do_not_enter_compaction(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def respond(body: dict[str, Any]) -> bytes:
            source = material(body)
            if source:
                entered.set()
                await release.wait()
                return summary_reply(source)
            return wire(reply("Visible reply."))

        context, requests = harness(compact_profile(profile), respond)
        await context.start()
        assert context.store and context.runs
        try:
            session = context.store.create_session()
            seed(context.store, session.id)
            first = context.runs.submit(
                session.id,
                profile=context.profile,
                content="Current input.",
                request_id=new_id(),
            )
            await asyncio.wait_for(entered.wait(), 3)
            assert context.inference.locked()
            second = context.runs.submit(
                session.id,
                profile=context.profile,
                content="Future private input.",
                request_id=new_id(),
            )
            assert context.store.run(second.id).state == RunState.QUEUED
            assert "Future private input" not in canonical(requests).decode()
            release.set()
            assert (await context.runs.wait(first.id, timeout=5)).state == RunState.COMPLETED
            assert (await context.runs.wait(second.id, timeout=5)).state == RunState.COMPLETED
            assert "Future private input" not in canonical(requests[:-1]).decode()
            assert requests[-1]["messages"][-1]["content"] == "Future private input."
            assert not context.inference.locked()
        finally:
            release.set()
            await context.stop()

    asyncio.run(scenario())


def test_summary_adapter_exception_is_an_explicit_context_limit(
    profile: EffectiveProfile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        context, requests = harness(compact_profile(profile), [])
        await context.start()
        assert context.store and context.runs and context.model

        async def unavailable(prompt: PreparedPrompt, run: Run, index: int) -> ModelResult:
            raise ModelError("Model unavailable", ErrorCode.MODEL_UNAVAILABLE)

        monkeypatch.setattr(context.model, "complete", unavailable)
        try:
            session = context.store.create_session()
            seed(context.store, session.id, count=10)
            run = context.runs.submit(
                session.id,
                profile=context.profile,
                content="Continue.",
                request_id=new_id(),
            )
            outcome = await context.runs.wait(run.id, timeout=5)
            assert outcome.reason and outcome.reason.code == ErrorCode.CONTEXT_LIMIT
            assert not requests and active_boundary(context.store, session.id) is None
        finally:
            await context.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("revoked_during_summary", [False, True])
def test_compaction_respects_total_and_current_model_budget(
    profile: EffectiveProfile,
    revoked_during_summary: bool,
) -> None:
    async def scenario() -> None:
        async def respond(body: dict[str, Any]) -> bytes:
            source = material(body)
            assert source
            context.profile = context.profile.model_copy(
                update={
                    "budgets": context.profile.budgets.model_copy(update={"model_calls": 1}),
                }
            )
            return summary_reply(source)

        selected = compact_profile(profile)
        if not revoked_during_summary:
            selected = selected.model_copy(
                update={
                    "budgets": selected.budgets.model_copy(update={"model_calls": 1}),
                }
            )
        context, requests = harness(selected, respond)
        await context.start()
        assert context.store and context.runs
        try:
            session = context.store.create_session()
            seed(context.store, session.id, count=10)
            run = context.runs.submit(
                session.id,
                profile=context.profile,
                content="Continue.",
                request_id=new_id(),
            )
            outcome = await context.runs.wait(run.id, timeout=5)
            assert outcome.state == RunState.FAILED
            assert len(requests) == (1 if revoked_during_summary else 0)
            assert outcome.reason and outcome.reason.code in (
                ErrorCode.CONTEXT_LIMIT,
                ErrorCode.BUDGET_EXCEEDED,
            )
        finally:
            await context.stop()

    asyncio.run(scenario())


def test_oversized_tool_result_keeps_receipt_and_fails_explicitly(
    profile: EffectiveProfile,
) -> None:
    async def scenario() -> None:
        async def big(context: ToolContext, args: ToolArguments) -> ToolResult:
            return ToolResult(data="x" * 25000)

        context, requests = harness(
            compact_profile(profile),
            [reply("", calls=[call("big")])],
            (Tool("big", "Large read result", ToolArguments, big),),
        )
        await context.start()
        assert context.store and context.runs
        try:
            session = context.store.create_session()
            run = context.runs.submit(
                session.id,
                profile=context.profile,
                content="Use big.",
                request_id=new_id(),
            )
            outcome = await context.runs.wait(run.id, timeout=3)
            assert outcome.reason and outcome.reason.code == ErrorCode.CONTEXT_LIMIT
            receipts = [r for r in records(context.store, run.id) if isinstance(r, ToolReceipt)]
            assert len(receipts) == 1 and receipts[0].result == "x" * 25000
            assert len(requests) == 1
        finally:
            await context.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "stage", ["compaction_prepared", "compaction_summary", "compaction_committed"]
)
def test_compaction_process_death_recovery(tmp_path: Path, stage: str) -> None:
    script = """
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, str(Path('tests').resolve()))
from test_compaction import compact_profile, seed, material, summary_reply
from test_run_loop import harness
from solakit.config import OperatorConfig, resolve_profile
from solakit.records import new_id
async def scenario():
    root = Path(sys.argv[1])
    profile = resolve_profile(OperatorConfig.model_validate({
        'data_dir': str(root / 'data'), 'model': {'pin': 'fixture-qwen'},
        'defaults': {'workspace_roots': [str(root)]},
    }))
    async def respond(body):
        return summary_reply(material(body))
    context, _ = harness(compact_profile(profile), respond)
    await context.start()
    session = context.store.create_session()
    seed(context.store, session.id)
    def crash(point):
        if point == sys.argv[2]:
            os._exit(75)
    context.store.fault = crash
    run = context.runs.submit(session.id, profile=context.profile,
                              content='Continue.', request_id=new_id())
    await context.runs.wait(run.id, timeout=10)
asyncio.run(scenario())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), stage],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 75, result.stderr

    async def scenario() -> None:
        store = Store(tmp_path / "data")
        for _ in range(2):
            await store.start()
            try:
                session = store.list_sessions()[0]
                active = active_boundary(store, session.id)
                assert active
                assert (
                    store.db.execute(
                        "SELECT count(*) FROM entities WHERE kind='compaction'"
                    ).fetchone()[0]
                    == 1
                )
            finally:
                await store.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("archived", [False, True])
def test_corrupt_summary_keeps_old_prepared_boundary_or_blocks_committed(
    profile: EffectiveProfile,
    committed: bool,
    archived: bool,
) -> None:
    async def scenario() -> None:
        async def respond(body: dict[str, Any]) -> bytes:
            source = material(body)
            return summary_reply(source) if source else wire(reply("Visible reply."))

        context, _ = harness(compact_profile(profile), respond)
        await context.start()
        assert context.store and context.runs
        store = context.store
        session = store.create_session()
        seed(store, session.id)
        if not committed:

            def crash(stage: str) -> None:
                if stage == "compaction_prepared":
                    raise RuntimeError("crash")

            store.fault = crash
        run = context.runs.submit(
            session.id,
            profile=context.profile,
            content="Continue.",
            request_id=new_id(),
        )
        await context.runs.wait(run.id, timeout=5)
        row = store.db.execute("SELECT body FROM entities WHERE kind='compaction'").fetchone()
        from solakit.records import CompactionBoundary

        boundary = CompactionBoundary.model_validate_json(row[0])
        summary = manifest_for(store, boundary).summary
        if archived:
            store.archive_sessions([session.id])
        await context.stop()
        (store.root / summary.path).write_bytes(b"corrupted")
        store.fault = lambda _: None
        try:
            if committed and not archived:
                with pytest.raises(CorruptionError):
                    await store.start()
            else:
                await store.start()
                assert bool(active_boundary(store, session.id)) == committed
                if committed:
                    store.update_session(session.id, archived=False)
                    await store.stop()
                    with pytest.raises(CorruptionError):
                        await store.start()
        finally:
            await store.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "stage",
    [
        "compaction_manifest",
        "compaction_prepared",
        "compaction_summary",
        "compaction_activate",
        "compaction_committed",
    ],
)
@pytest.mark.parametrize("archived", [False, True])
def test_compaction_crash_recovery_once(
    profile: EffectiveProfile, stage: str, archived: bool
) -> None:
    async def scenario() -> None:
        async def respond(body: dict[str, Any]) -> bytes:
            source = material(body)
            assert source
            return summary_reply(source)

        context, _ = harness(compact_profile(profile), respond)
        await context.start()
        assert context.store and context.runs
        store = context.store
        session = store.create_session()
        seed(store, session.id)

        def crash(point: str) -> None:
            if point == stage:
                raise RuntimeError("Injected crash")

        store.fault = crash
        run = context.runs.submit(
            session.id,
            profile=context.profile,
            content="Continue.",
            request_id=new_id(),
        )
        await context.runs.wait(run.id, timeout=5)
        if archived:
            store.archive_sessions([session.id])
        await context.stop()
        store.fault = lambda _: None
        for _ in range(2):
            await store.start()
            active = active_boundary(store, session.id)
            assert bool(active) == (stage != "compaction_manifest")
            if active:
                summary = manifest_for(store, active).summary
                assert digest((store.root / summary.path).read_bytes()) == summary.sha256
                assert (
                    store.db.execute(
                        "SELECT count(*) FROM entities WHERE kind='message' AND id=?",
                        (active.summary_message_id,),
                    ).fetchone()[0]
                    == 1
                )
            await store.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["empty", "invalid", "range", "oversized", "model"])
@pytest.mark.parametrize("overflow", [False, True])
def test_summary_failure_keeps_boundary_and_input(
    profile: EffectiveProfile, kind: str, overflow: bool
) -> None:
    async def scenario() -> None:
        async def respond(body: dict[str, Any]) -> bytes:
            source = material(body)
            if not source:
                return wire(reply("Visible reply."))
            if kind == "invalid":
                return wire(reply("not JSON"))
            if kind == "model":
                return wire(
                    reply(
                        "",
                        calls=[
                            {
                                "id": "forbidden",
                                "type": "function",
                                "function": {"name": "files.write", "arguments": "{}"},
                            }
                        ],
                    )
                )
            if kind == "range":
                source["through_run_id"] = new_id()
            return summary_reply(
                source, text="" if kind == "empty" else "x" * 5000 if kind == "oversized" else None
            )

        selected = compact_profile(profile)
        if not overflow:
            selected = selected.model_copy(update={"model_context_tokens": 24000})
        context, requests = harness(selected, respond)
        await context.start()
        assert context.store and context.runs
        try:
            session = context.store.create_session()
            seed(context.store, session.id, count=10 if overflow else 6)
            run = context.runs.submit(
                session.id,
                profile=context.profile,
                content="retained input",
                request_id=new_id(),
            )
            outcome = await context.runs.wait(run.id, timeout=5)
            if overflow:
                assert outcome.reason and outcome.reason.code == ErrorCode.CONTEXT_LIMIT
            else:
                assert outcome.state == RunState.COMPLETED
            assert active_boundary(context.store, session.id) is None
            assert len(requests) == (1 if overflow else 2)
            assert any(
                isinstance(r, Message) and r.content == "retained input"
                for r in records(context.store, run.id)
            )
        finally:
            await context.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["input", "schemas", "uncertain", "ephemeral"])
def test_uncompactable_context_fails_without_model_loop(
    profile: EffectiveProfile, kind: str
) -> None:
    async def scenario() -> None:
        selected = compact_profile(profile)
        if kind == "ephemeral":
            selected = selected.model_copy(update={"mode": SessionMode.EPHEMERAL})
        context, requests = harness(selected, [])
        if kind == "schemas":
            assert context.tools
            function = context.tools.registry.schemas["files.read"]["function"]
            assert isinstance(function, dict)
            function["description"] = "x" * 20000
        await context.start()
        assert context.store and context.runs
        try:
            session = context.store.create_session(mode=selected.mode)
            if kind in ("uncertain", "schemas"):
                seed(context.store, session.id, count=10, uncertain=kind == "uncertain")
            run = context.runs.submit(
                session.id,
                profile=context.profile,
                content="x" * 20000 if kind in ("input", "ephemeral") else "Continue.",
                request_id=new_id(),
            )
            outcome = await context.runs.wait(run.id, timeout=5)
            assert outcome.reason and outcome.reason.code == ErrorCode.CONTEXT_LIMIT
            assert not requests and active_boundary(context.store, session.id) is None
        finally:
            await context.stop()

    asyncio.run(scenario())
