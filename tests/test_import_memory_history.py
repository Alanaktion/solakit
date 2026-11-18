import asyncio
import json
from typing import Any

import httpx2
import pytest

from scripts.import_memory_history import batches, import_history
from scripts.migration import write
from solakit.config import EffectiveProfile, MemorySettings
from solakit.evidence import canonical
from solakit.memory import MemoryError, OpenViking
from solakit.records import (
    CompactionBoundary,
    Message,
    Origin,
    Run,
    RunState,
    Session,
    SessionMode,
    new_id,
    utc_now,
)
from solakit.redaction import Redactor
from solakit.runtime import DataDirectoryLock, OwnershipError
from solakit.store import Store


@pytest.fixture
def memory_profile(profile: EffectiveProfile) -> EffectiveProfile:
    return profile.model_copy(
        update={
            "memory": MemorySettings(
                enabled=True, account="test", roots=("viking://user/default/memories",)
            )
        }
    )


async def history(profile: EffectiveProfile) -> list[Message]:
    store = Store(profile.data_dir)
    await store.start()
    originals = []
    try:
        for mode in SessionMode:
            session = Session(origin=Origin.SYSTEM, mode=mode, archived=True)
            now = utc_now()
            run = Run(
                origin=Origin.SYSTEM,
                session_id=session.id,
                request_id=new_id(),
                profile="legacy",
                state=RunState.COMPLETED,
                created_at=now,
                started_at=now,
                ended_at=now,
            )
            messages = [
                Message(
                    origin=run.origin,
                    session_id=session.id,
                    run_id=run.id,
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"message {index} λ\n",
                )
                for index in range(205 if mode == SessionMode.SHARED else 1)
            ]
            messages[0] = messages[0].model_copy(update={"content": '\\"\nλ😀' * 20000})
            summary = Message(
                origin=run.origin,
                session_id=session.id,
                run_id=run.id,
                role="assistant",
                content="COMPACTION SUMMARY MUST NOT BE SENT",
            )
            boundary = CompactionBoundary(
                origin=run.origin,
                session_id=session.id,
                run_id=run.id,
                through_message_id=messages[-1].id,
                source_message_ids=tuple(m.id for m in messages),
                summary_message_id=summary.id,
            )
            write(store, [session, run, *messages, summary, boundary])
            if mode == SessionMode.SHARED:
                originals = messages
        return originals
    finally:
        await store.stop()


def adapter(
    profile: EffectiveProfile, requests: list[httpx2.Request], *, lose_response: int = 0
) -> OpenViking:
    commits = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal commits
        requests.append(request)
        if request.url.path == "/health":
            return httpx2.Response(200, json={"status": "ok", "version": "0.5.5"})
        result: dict[str, Any] = {}
        if request.url.path == "/api/v1/sessions":
            session = json.loads(request.content)["session_id"]
            result = {
                "session_id": session,
                "uri": f"viking://user/default/sessions/{session}",
                "user": {"account_id": "test", "user_id": "default"},
            }
        if request.url.path.endswith("/commit"):
            commits += 1
            if commits == lose_response:
                raise httpx2.ReadTimeout("lost acceptance")
            result = {"task_id": "task-" + request.url.path.split("/")[-2]}
        return httpx2.Response(200, json={"status": "ok", "result": result})

    remote = OpenViking(profile.memory, Redactor())
    remote.transport = httpx2.MockTransport(respond)
    return remote


def test_complete_history_bounded_batches_dry_run_and_rerun(
    memory_profile: EffectiveProfile,
) -> None:
    async def scenario() -> None:
        originals = await history(memory_profile)
        requests: list[httpx2.Request] = []
        remote = adapter(memory_profile, requests)
        result = await import_history(memory_profile, dry_run=True, adapter=remote)
        assert isinstance(result["batches"], int) and result["batches"] > 2
        assert not requests
        assert not (memory_profile.data_dir / "imports/openviking-history").exists()
        store = Store(memory_profile.data_dir)
        await store.start()
        try:
            prepared = list(batches(store))
            assert [b.id for b in prepared] == [b.id for b in batches(store)]
            assert all(len(b.payload) <= 100 for b in prepared)
            assert all(len(canonical({"messages": b.payload})) <= 65536 for b in prepared)
        finally:
            await store.stop()
        result = await import_history(memory_profile, adapter=remote)
        assert result["accepted"] == len(prepared)
        assert all(
            len(r.content) <= 65536 for r in requests if r.url.path.endswith("/messages/batch")
        )
        sent = [
            m
            for r in requests
            if r.url.path.endswith("/messages/batch")
            for m in json.loads(r.content)["messages"]
        ]
        restored: dict[str, str] = {}
        for message in sent:
            identity = message["source_message_ids"][0]
            restored[identity] = restored.get(identity, "") + message["content"]
        assert list(restored) == [m.id for m in originals]
        assert restored == {m.id: m.content for m in originals}
        requests.clear()
        result = await import_history(memory_profile, adapter=remote)
        assert result["accepted"] == 0 and result["already_accepted"] == len(prepared)
        assert all(r.method == "GET" for r in requests)
        directory = memory_profile.data_dir / "imports/openviking-history"
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())
        changed = memory_profile.model_copy(
            update={"memory": memory_profile.memory.model_copy(update={"account": "other"})}
        )
        with pytest.raises(ValueError, match="destination changed"):
            await import_history(changed, adapter=adapter(changed, requests))

    asyncio.run(scenario())


@pytest.mark.parametrize("lost_batch", [1, 2])
def test_uncertain_submission_is_never_replayed(
    memory_profile: EffectiveProfile, lost_batch: int
) -> None:
    async def scenario() -> None:
        await history(memory_profile)
        requests: list[httpx2.Request] = []
        with pytest.raises(MemoryError):
            await import_history(
                memory_profile, adapter=adapter(memory_profile, requests, lose_response=lost_batch)
            )
        assert any(r.url.path.endswith("/commit") for r in requests)
        requests.clear()
        with pytest.raises(ValueError, match="uncertain submission"):
            await import_history(memory_profile, adapter=adapter(memory_profile, requests))
        assert all(r.method == "GET" for r in requests)

    asyncio.run(scenario())


def test_preflight_outage_can_retry_without_uncertain_batch(
    memory_profile: EffectiveProfile,
) -> None:
    async def scenario() -> None:
        await history(memory_profile)
        remote = OpenViking(memory_profile.memory, Redactor())
        remote.transport = httpx2.MockTransport(lambda r: httpx2.Response(503))
        with pytest.raises(MemoryError):
            await import_history(memory_profile, adapter=remote)
        directory = memory_profile.data_dir / "imports/openviking-history"
        assert not list(directory.glob("*.started.json"))
        result = await import_history(memory_profile, adapter=adapter(memory_profile, []))
        assert result["accepted"] == result["batches"]
        store = Store(memory_profile.data_dir)
        await store.start()
        try:
            session = store.create_session()
            store.admit(session.id, request_id=new_id(), profile="default", content="new history")
        finally:
            await store.stop()
        requests: list[httpx2.Request] = []
        with pytest.raises(ValueError, match="history or destination changed"):
            await import_history(memory_profile, adapter=adapter(memory_profile, requests))
        assert not requests

    asyncio.run(scenario())


def test_import_requires_existing_state_enabled_memory_and_stopped_runtime(
    memory_profile: EffectiveProfile,
) -> None:
    async def scenario() -> None:
        requests: list[httpx2.Request] = []
        remote = adapter(memory_profile, requests)
        with pytest.raises(ValueError, match="existing migrated"):
            await import_history(memory_profile, adapter=remote)
        await history(memory_profile)
        disabled = memory_profile.model_copy(update={"memory_mode": "off"})
        with pytest.raises(MemoryError, match="disabled"):
            await import_history(disabled, adapter=remote)
        owner = DataDirectoryLock(memory_profile.data_dir)
        owner.acquire()
        try:
            with pytest.raises(OwnershipError):
                await import_history(memory_profile, adapter=remote)
        finally:
            owner.release()
        assert not requests

    asyncio.run(scenario())
