import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue
from test_api import BASE, PASSWORD, login
from test_run_loop import call, harness, reply, wire

from solakit.api import create_app
from solakit.config import EffectiveProfile
from solakit.engine.run_loop import records
from solakit.records import Delegation, Job, Origin, Run, RunState, SessionMode, ToolReceipt, new_id
from solakit.runtime import ApplicationContext
from solakit.storage.evidence import StorageError
from solakit.storage.session_transfer import export_session, import_session
from solakit.storage.store import Store

BRIEF = "From survey.txt, report the species and count."
REQUEST = "Delegate the survey."
DELEGATE = reply("", calls=[call("delegate.run", json.dumps({"task": BRIEF}))])


def setup(
    profile: EffectiveProfile,
    responses: list[dict[str, Any]] | Callable[[dict[str, Any]], Awaitable[bytes]],
) -> tuple[ApplicationContext, list[dict[str, Any]]]:
    selected = profile.model_copy(update={"delegation_worker": "research"})
    context, prompts = harness(selected, responses)
    assert context.tools and context.store and context.runs
    context.runs.max_active = 2
    grants = frozenset(
        {
            "delegate.run",
            "delegate.continue",
            "files.read",
            "files.list",
            "files.write",
            "jobs.create",
        }
    )
    context.profile = context.profile.model_copy(update={"tools": grants, "granted_tools": grants})
    parent = context.profile
    worker = parent.model_copy(
        update={
            "name": "research",
            "origin": Origin.DELEGATED,
            "mode": SessionMode.ISOLATED,
            "memory_mode": "off",
            "tools": parent.tools - {"files.list"},
        }
    )

    def current(admitted: EffectiveProfile) -> EffectiveProfile:
        selected = worker if admitted.name == worker.name else context.profile
        return selected.model_copy(update={"origin": admitted.origin, "mode": admitted.mode})

    context.tools.current = current
    return context, prompts


def start_parent(context: ApplicationContext, content: str = REQUEST) -> Run:
    assert context.runs and context.store
    return context.runs.submit(
        context.store.create_session().id,
        profile=context.profile,
        content=content,
        request_id=new_id(),
    )


def get_receipts(store: Store, run_id: str) -> list[ToolReceipt]:
    return [record for record in records(store, run_id) if isinstance(record, ToolReceipt)]


def get_link(store: Store, run_id: str) -> Delegation:
    return next(record for record in records(store, run_id) if isinstance(record, Delegation))


@pytest.mark.parametrize("case", ["continued", "foreign", "busy", "archived", "revoked"])
def test_continue_existing_worker_after_restart(profile: EffectiveProfile, case: str) -> None:
    async def scenario() -> None:
        first, _ = setup(
            profile,
            [DELEGATE, reply("The survey found waxwings."), reply("Worker report received.")],
        )
        assert first.store and first.runs
        await first.start()
        try:
            parent = start_parent(first)
            assert (await first.runs.wait(parent.id, timeout=3)).state == RunState.COMPLETED
            original = get_link(first.store, parent.id)
        finally:
            await first.stop()

        question = "Which bird did you identify earlier?"
        arguments = json.dumps({"session_id": original.child_session_id, "task": question})
        replies = [reply("Acknowledged."), reply("", calls=[call("delegate.continue", arguments)])]
        if case == "continued":
            replies.append(reply("Waxwings."))
        replies.append(reply("Follow-up attempt settled."))
        app, prompts = setup(profile, replies)
        assert app.store and app.runs and app.tools
        store, runs, dispatcher = app.store, app.runs, app.tools
        resolve = dispatcher.current

        def current(admitted: EffectiveProfile) -> EffectiveProfile:
            selected = resolve(admitted)
            if admitted.name != "research":
                return selected
            updated = selected.tools - {"files.read"} if case == "revoked" else selected.tools
            return selected.model_copy(update={"tools": updated | {"files.list"}})

        dispatcher.current = current
        await app.start()
        try:

            def submit(topic: str, content: str) -> Run:
                return runs.submit(topic, profile=app.profile, content=content, request_id=new_id())

            other_turn = submit(parent.session_id, "An unrelated parent-only detail.")
            assert (await runs.wait(other_turn.id, timeout=3)).state == RunState.COMPLETED
            if case == "archived":
                store.update_session(original.child_session_id, archived=True)
            if case == "busy":
                runs.sessions.add(original.child_session_id)
            topic = store.create_session().id if case == "foreign" else parent.session_id
            turn = submit(topic, "Ask that researcher a narrower question.")
            state = (await runs.wait(turn.id, timeout=3)).state
            (receipt,) = get_receipts(store, turn.id)
            if case != "continued":
                assert state == RunState.FAILED
                assert receipt.status == ("failed" if case == "busy" else "denied")
                if case == "busy":
                    assert receipt.result == {"status": "busy"}
                assert len(store.runs(original.child_session_id)) == 1
                return
            assert state == RunState.COMPLETED
            link = get_link(store, turn.id)
            assert link.child_session_id == original.child_session_id
            assert link.continued_from_run_id == original.child_run_id
            child = store.run(link.child_run_id)
            assert child.continued_from_run_id == original.child_run_id
            assert child.profile == "research"
            assert isinstance(receipt.result, dict)
            evidence = receipt.result["evidence_ids"]
            assert isinstance(evidence, list) and original.child_run_id in evidence
            assert receipt.result["report"] == "Waxwings."
            assert original.child_session_id in json.dumps(prompts[1]["messages"])
            assert "The survey found waxwings." in json.dumps(prompts[2]["messages"])
            assert "parent-only detail" not in json.dumps(prompts[2]["messages"])
            assert {tool["function"]["name"] for tool in prompts[2]["tools"]} == {"files.read"}
            copied = import_session(store, export_session(store, topic), new_id())
            copied_turn = store.runs(copied, before=0, limit=1)[0][1]
            copied_link = get_link(store, copied_turn.id)
            assert copied_link.continued_from_run_id not in (None, original.child_run_id)
            assert store.run(copied_link.child_run_id).continued_from_run_id == (
                copied_link.continued_from_run_id
            )
        finally:
            runs.sessions.discard(original.child_session_id)
            await app.stop()

    asyncio.run(scenario())


def test_isolated_child_report_and_shared_budget(profile: EffectiveProfile, tmp_path: Path) -> None:
    async def scenario() -> None:
        private = tmp_path / "soul.md"
        private.write_text("PRIVATE LOCAL IDENTITY")
        (tmp_path / "survey.txt").write_text("waxwing: 17")
        context, prompts = setup(
            profile.model_copy(
                update={
                    "soul_file": private,
                    "budgets": profile.budgets.model_copy(
                        update={"model_calls": 4, "tool_calls": 2}
                    ),
                }
            ),
            [
                DELEGATE,
                reply("", calls=[call("files.read", '{"path":"survey.txt"}')]),
                reply("17 waxwings."),
                reply("The worker reported 17 waxwings."),
            ],
        )
        assert context.store and context.runs and context.jobs and context.tools
        await context.start()
        store, runs = context.store, context.runs
        try:
            parent = start_parent(context, "Delegate the survey. Parent-only detail: confidential.")
            assert (await runs.wait(parent.id, timeout=3)).state == RunState.COMPLETED
            link = get_link(store, parent.id)
            child = store.run(link.child_run_id)
            assert child.state == RunState.COMPLETED and child.request_id == link.id
            assert child.parent_tool_call_id == link.id
            assert store.session(child.session_id).parent_session_id == parent.session_id
            assert len(prompts) == 4
            assert {tool["function"]["name"] for tool in prompts[1]["tools"]} == {"files.read"}
            assert prompts[1]["messages"][-1]["content"] == BRIEF
            assert "confidential" not in json.dumps(prompts[1])
            assert "PRIVATE LOCAL IDENTITY" not in json.dumps(prompts[1])
            assert "waxwing: 17" in json.dumps(prompts[2])
            (receipt,) = get_receipts(store, parent.id)
            assert isinstance(receipt.result, dict)
            evidence: JsonValue = receipt.result["evidence_ids"]
            assert isinstance(evidence, list) and len(evidence) == 2
            assert receipt.result == {
                "session_id": child.session_id,
                "run_id": child.id,
                "status": "completed",
                "report": "17 waxwings.",
                "evidence_ids": [child.id, evidence[1]],
            }
            assert "17 waxwings." in json.dumps(prompts[3])
            assert not prompts[3].get("tools")
            before = [r.id for r in records(store, parent.id)]
            assert before.index(link.id) < before.index(receipt.id)
            with pytest.raises(ValueError, match="delegated topics"):
                runs.submit(
                    child.session_id,
                    profile=context.profile.model_copy(update={"mode": SessionMode.ISOLATED}),
                    content="Bypass",
                    request_id=new_id(),
                )
            with pytest.raises(ValueError, match="delegated topics"):
                context.jobs.create(
                    Job(
                        origin=Origin.SCHEDULED,
                        session_id=child.session_id,
                        mode=SessionMode.ISOLATED,
                        profile="default",
                        task="Bypass",
                        schedule="cron:0 * * * *",
                        timezone="UTC",
                    )
                )
        finally:
            await context.stop()

    asyncio.run(scenario())


def test_eligible_queue_precedes_child_and_a_busy_result_creates_no_topic(
    profile: EffectiveProfile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        parent_in_model, release_parent = asyncio.Event(), asyncio.Event()

        async def respond(body: dict[str, Any]) -> bytes:
            if body["messages"][-1]["content"] == REQUEST:
                parent_in_model.set()
                await release_parent.wait()
                return wire(DELEGATE)
            return wire(reply("No child report available."))

        context, _ = setup(profile, respond)
        assert context.store and context.runs and context.jobs and context.tools
        await context.start()
        try:
            # Suppress pump notifications for this admission to exercise the synchronous
            # reservation path while eligible work is queued and a slot is still open.
            parent = start_parent(context)
            await asyncio.wait_for(parent_in_model.wait(), timeout=2)
            with monkeypatch.context() as patch:
                patch.setattr(context.runs, "_notify", lambda: None)
                waiting = start_parent(context, "Queued work")
            release_parent.set()
            result = await context.runs.wait(parent.id, timeout=3)
            assert result.state == RunState.FAILED
            assert (await context.runs.wait(waiting.id, timeout=3)).state == RunState.COMPLETED
            assert get_receipts(context.store, parent.id)[0].result == {"status": "busy"}
            assert len(context.store.list_sessions()) == 2
        finally:
            await context.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", ["child", "parent", "timeout"])
def test_parent_and_child_cancellation(profile: EffectiveProfile, cancel: str) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        async def respond(body: dict[str, Any]) -> bytes:
            last = body["messages"][-1]
            if last["content"] == BRIEF:
                entered.set()
                await asyncio.Future()
            if last["role"] == "tool":
                return wire(reply("The worker was cancelled."))
            return wire(DELEGATE)

        if cancel == "timeout":
            profile_update = profile.model_copy(
                update={"budgets": profile.budgets.model_copy(update={"tool_timeout_seconds": 1})}
            )
        else:
            profile_update = profile
        context, _ = setup(profile_update, respond)
        assert context.store and context.runs and context.jobs and context.tools
        await context.start()
        try:
            store, runs = context.store, context.runs
            parent = start_parent(context)
            await asyncio.wait_for(entered.wait(), timeout=2)
            link = get_link(store, parent.id)
            if cancel != "timeout":
                runs.cancel(parent.id if cancel == "parent" else link.child_run_id)
            parent = await runs.wait(parent.id, timeout=3)
            assert parent.state == (RunState.CANCELLED if cancel == "parent" else RunState.FAILED)
            assert store.run(link.child_run_id).state == RunState.CANCELLED
            (receipt,) = get_receipts(store, parent.id)
            assert isinstance(receipt.result, dict)
            assert receipt.result["run_id"] == link.child_run_id
            assert receipt.result["status"] == "cancelled"
            assert "report" not in receipt.result
        finally:
            await context.stop()

    asyncio.run(scenario())


def test_worker_dispatch_rechecks_parent_and_rejects_nested_and_writes(
    profile: EffectiveProfile, tmp_path: Path
) -> None:
    async def scenario() -> None:
        (tmp_path / "survey.txt").write_text("restricted")
        hidden = tmp_path / "worker"
        hidden.mkdir()
        worker_fragments = tmp_path / "worker.md"
        worker_fragments.write_text("WORKER FRAGMENT")

        async def respond(body: dict[str, Any]) -> bytes:
            last = body["messages"][-1]
            if last["content"] == REQUEST:
                return wire(DELEGATE)
            if last["content"] == BRIEF:
                assert "WORKER FRAGMENT" in json.dumps(body)
                context.profile = context.profile.model_copy(
                    update={
                        "workspace_roots": (hidden,),
                    }
                )
                return wire(
                    reply(
                        "",
                        calls=[
                            call("delegate.run", '{"task":"nested"}'),
                            call("files.write", '{"path":"created.txt","content":"forbidden"}'),
                            call("jobs.create", '{"task":"wake","schedule":"cron:0 * * * *"}'),
                            call("files.read", json.dumps({"path": str(tmp_path / "survey.txt")})),
                        ],
                    )
                )
            return wire(reply("The request could not be completed."))

        context, _ = setup(profile, respond)
        assert context.tools
        resolve = context.tools.current

        def worker_fragment(admitted: EffectiveProfile) -> EffectiveProfile:
            result = resolve(admitted)
            if admitted.name == "research":
                return result.model_copy(update={"fragments": (worker_fragments,)})
            return result

        context.tools.current = worker_fragment
        assert context.store and context.runs and context.jobs and context.tools
        await context.start()
        try:
            parent = start_parent(context)
            assert (await context.runs.wait(parent.id, timeout=3)).state == RunState.FAILED
            link = get_link(context.store, parent.id)
            receipts = get_receipts(context.store, link.child_run_id)
            assert [receipt.status for receipt in receipts] == ["denied"] * 4
            assert not (tmp_path / "created.txt").exists()
            assert context.store.jobs() == []
            assert len(context.store.list_sessions()) == 2
        finally:
            await context.stop()

    asyncio.run(scenario())


def test_restart_settles_child_committed_before_execution(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        context, _ = setup(profile, [DELEGATE])
        assert context.store and context.runs and context.jobs and context.tools
        await context.start()
        store, runs = context.store, context.runs
        activated = False

        def cut(stage: str) -> None:
            nonlocal activated
            if (
                stage == "state_commit"
                and store.db.execute("SELECT 1 FROM entities WHERE kind='delegation'").fetchone()
            ):
                activated = True
                raise OSError("cut power after child admission commit")

        store.fault = cut
        parent = start_parent(context)
        try:
            with pytest.raises(StorageError):
                await runs.wait(parent.id, timeout=3)
            assert activated
        finally:
            await context.stop()
        recovered, prompts = setup(profile, [])
        await recovered.start()
        try:
            assert recovered.store
            store = recovered.store
            link = get_link(store, parent.id)
            child = store.run(link.child_run_id)
            assert child.started_at is None and child.state == RunState.INTERRUPTED
            assert store.run(parent.id).state == RunState.INTERRUPTED
            (receipt,) = get_receipts(store, parent.id)
            assert isinstance(receipt.result, dict)
            assert receipt.result["status"] == "interrupted"
            assert receipt.result["session_id"] == child.session_id
            assert len(store.list_sessions()) == 2
            assert prompts == []
        finally:
            await recovered.stop()

    asyncio.run(scenario())


def test_api_rejects_writes_to_delegated_topic(
    profile: EffectiveProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, _ = setup(profile, [DELEGATE, reply("Child report."), reply("Worker finished.")])
    monkeypatch.setenv("SOLA_OPERATOR_PASSWORD", PASSWORD)
    with TestClient(create_app(context), base_url=BASE) as client:
        login(client)
        topic = client.post("/api/v1/sessions", json={}).json()
        submitted = client.post(
            "/api/v1/runs?wait=3",
            json={"session_id": topic["id"], "content": REQUEST},
            headers={"Idempotency-Key": new_id()},
        )
        assert submitted.status_code == 200
        run = submitted.json()["run"]
        items = client.get(f"/api/v1/runs/{run['id']}/records").json()["items"]
        link = next(row["record"] for row in items if row["kind"] == "delegation")
        child_topic = link["child_session_id"]
        child_run = link["child_run_id"]
        assert client.get(f"/api/v1/runs/{child_run}").json()["state"] == "completed"
        assert (
            client.post(
                "/api/v1/runs",
                json={"session_id": child_topic, "content": "Bypass"},
                headers={"Idempotency-Key": new_id()},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/v1/jobs",
                json={
                    "session_id": child_topic,
                    "mode": "isolated",
                    "schedule": "cron:0 * * * *",
                    "task": "Bypass",
                },
            ).status_code
            == 409
        )
