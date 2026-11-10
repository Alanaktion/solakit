"""Contextual recall and staged notes use the existing scoped commit contract."""

import asyncio
import json

import httpx
import pytest
from test_execution import call, completion, model_list, setup

from solakit.core.config import MemoryConfig
from solakit.core.memory import MemoryClient
from solakit.core.recall import recall_query, trivial_turn
from solakit.core.schemas import Message, Origin, Session
from solakit.core.store import Store


def runtime(config, *, note=None, commit_failure=False, model_failure=False):
    config.memory = MemoryConfig(
        enabled=True,
        base_url="http://memory.invalid",
        roots=["viking://user/sola/memories"],
    )
    if note is not None:
        config.enabled_tools = ["memory"]
        config.profiles["interactive"].tools = ["memory.remember"]
    (config.paths.fragments / "recall-header.md").write_text("Recalled sources:")
    captured, queries, batches = [], [], []

    def memory_handler(request):
        path = request.url.path
        captured.append((request.method, path))
        result = {}
        if path.endswith("search/find"):
            queries.append(json.loads(request.content)["query"])
            result = {"memories": []}
        elif path.endswith("messages/batch"):
            batches.append(json.loads(request.content)["messages"])
        elif path.endswith("/commit"):
            if commit_failure:
                return httpx.Response(503)
            result = {"status": "accepted", "task_id": "extract-task"}
        elif "/tasks/" in path:
            result = {"status": "completed"}
        return httpx.Response(200, json={"status": "ok", "result": result})

    def model_handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        messages = json.loads(request.content)["messages"]
        if note is not None and messages[-1]["role"] == "user":
            return completion(
                config, calls=[call("memory.remember", json.dumps({"text": note}))]
            )
        if model_failure:
            return httpx.Response(503)
        return completion(config, "Done")

    execution = setup(config, model_handler)
    execution.memory = MemoryClient(
        config.memory, transport=httpx.MockTransport(memory_handler)
    )
    return execution, queries, batches, captured


@pytest.mark.parametrize("window,cap", [(3, 1000), (3, 16), (0, 32000)])
def test_recent_context_query_is_bounded_ordered_and_ignores_tools(config, window, cap):
    execution, queries, _, _ = runtime(config)
    config.memory.recall_window = window
    config.memory.recall_query_max_chars = cap
    texts = []
    for turn in range(4):
        for role in ("user", "assistant"):
            text = f"{role} turn {turn}"
            texts.append(text)
            execution.store.append_message(
                "s", Message(role=role, content=text, origin=Origin(), run_id="old")
            )
    execution.store.append_message(
        "s",
        Message(
            role="tool",
            content="NEVER_RECALL_TOOL_PAYLOAD",
            tool_call_id="old",
            origin=Origin(),
            run_id="old",
        ),
    )
    incoming = "And how does that apply?"

    async def exercise():
        try:
            for origin in (
                Origin(channel="job", job="scheduled"),
                Origin(channel="discord"),
            ):
                # Restore this exact fixture so both origins have the same context.
                expected_history = execution.store.transcript("s")
                expected = recall_query(expected_history, incoming, config.memory)
                run = await execution.submit("s", incoming, origin=origin)
                assert run.status == "completed" and run.recall_status == "recalled"
                assert queries[-1] == expected
                assert "NEVER_RECALL" not in queries[-1]
            first = "\n".join([*(texts[-window:] if window else []), incoming])[-cap:]
            assert queries[0] == first
        finally:
            await execution.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "text,skip",
    [
        (" thanks ", True),
        ("OK", True),
        ("👍🏽", True),
        ("👨‍👩‍👧‍👦", True),
        ("1️⃣", True),
        ("123", False),
        ("what?", False),
        ("thanks — and did we decide about X?", False),
        ("a new topic", False),
    ],
)
def test_trivial_skip_is_recorded_separately_from_extraction(config, text, skip):
    execution, queries, _, _ = runtime(config)

    async def exercise():
        try:
            run = await execution.submit("s", text)
            assert run.status == "completed" and run.memory_status == "accepted"
            assert bool(queries) != skip
            assert run.recall_status == ("skipped_trivial" if skip else "recalled")
            record = execution.store.run(run.id)
            assert record["recall_status"] == run.recall_status
            if skip:
                events = execution.store._read(
                    execution.store.root / f"runs/{run.id}.events.jsonl"
                )
                assert any(e["memory_status"] == "skipped_trivial" for e in events)
                assert run.recall is None
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_recall_skip_off_switch_custom_phrases_and_scope(config):
    execution, queries, _, _ = runtime(config)
    config.memory.recall_skip_trivial = False

    async def exercise():
        try:
            await execution.submit("s", "thanks")
            assert queries == ["thanks"]
            config.memory.recall_skip_trivial = True
            config.memory.recall_trivial_phrases.append("roger")
            run = await execution.submit("s", "  ROGER  ")
            assert run.recall_status == "skipped_trivial" and len(queries) == 1
            execution.store.create_session(
                Session(id="ephemeral", memory_scope="ephemeral")
            )
            run = await execution.submit("ephemeral", "A detailed message")
            assert run.recall_status == "ephemeral" and len(queries) == 1
            execution.store.create_session(
                Session(id="isolated", memory_scope="isolated")
            )
            run = await execution.submit("isolated", "A detailed message")
            assert run.status == "failed" and len(queries) == 1
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_remember_stages_verbatim_note_and_commits_it_once(config):
    note = "  A deliberate correction\nPreserve this wording — exactly.  "
    execution, _, batches, requests = runtime(config, note=note)

    async def exercise():
        try:
            run = await execution.submit("s", "Keep this correction")
            assert run.status == "completed" and run.memory_status == "accepted"
            record = execution.store.run(run.id)
            receipt = json.loads(record["tools"][0]["result"]["content"])
            assert receipt["status"] == "staged" and receipt["run_id"] == run.id
            assert receipt["message"].startswith("Staged")
            assert len(record["memory_notes"]) == 1
            staged = record["memory_notes"][0]
            assert staged["id"] == receipt["note_id"] and staged["text"] == note
            assert staged["session_id"] == "s" and staged["profile"] == "interactive"
            assert staged["created_at"]
            marked = [
                m
                for batch in batches
                for m in batch
                if m.get("parts", [{}])[0]
                .get("text", "")
                .startswith("[noted for memory]\n")
            ]
            assert marked == [
                {
                    "role": "assistant",
                    "parts": [{"type": "text", "text": "[noted for memory]\n" + note}],
                }
            ]
            assert not any(
                (m.get("content") or "").startswith("[noted for memory]")
                for m in record["messages"]
            )
            allowed_suffixes = ("search/find", "/config", "/messages/batch", "/commit")
            assert all(
                path.endswith(allowed_suffixes) or "/sessions/" in path
                for _, path in requests
            )
            assert not any(
                "content/write" in path or "batch-write" in path for _, path in requests
            )
            count = len(batches)
            await execution.memory.scoped(execution.store.session("s")).commit(
                execution.store, run.id, []
            )
            assert len(batches) == count
            from solakit.core.memory_ops import refresh_run

            refreshed = await refresh_run(execution, run.id)
            assert refreshed.memory_status == "completed"
            assert (
                json.loads(
                    execution.store.run(run.id)["tools"][0]["result"]["content"]
                )["status"]
                == "staged"
            )
        finally:
            await execution.close()
        restarted = Store(config.paths.data)
        assert restarted.run(run.id)["memory_notes"][0]["text"] == note

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["over_limit", "empty", "ephemeral", "disabled"])
def test_remember_refusals_are_whole_and_leave_no_notes(config, mode):
    text = (
        "x" * 11 if mode == "over_limit" else ("   " if mode == "empty" else "A note")
    )
    execution, _, batches, _ = runtime(config, note=text)
    config.memory.remember_max_chars = 10
    identity = "s"
    if mode == "ephemeral":
        identity = "temp"
        execution.store.create_session(Session(id=identity, memory_scope="ephemeral"))
    if mode == "disabled":
        # Keep the client for orderly cleanup but expose no memory to execution.
        client = execution.memory
        execution.memory = None

    async def exercise():
        try:
            run = await execution.submit(identity, "Try storing this")
            record = execution.store.run(run.id)
            assert record["tools"][0]["result"]["error"]
            assert record["memory_notes"] == []
            assert not any(
                part.get("text", "").startswith("[noted for memory]")
                for batch in batches
                for message in batch
                for part in message["parts"]
            )
        finally:
            await execution.close()
            if mode == "disabled":
                await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["commit", "model"])
def test_staged_notes_remain_evidence_without_claiming_storage(config, failure):
    execution, _, batches, requests = runtime(
        config,
        note="Keep this exact note",
        commit_failure=failure == "commit",
        model_failure=failure == "model",
    )

    async def exercise():
        try:
            run = await execution.submit("s", "Remember this")
            record = execution.store.run(run.id)
            assert (
                json.loads(record["tools"][0]["result"]["content"])["status"]
                == "staged"
            )
            assert record["memory_notes"][0]["text"] == "Keep this exact note"
            if failure == "commit":
                assert run.memory_status == "commit_failed_or_uncertain"
                assert sum(path.endswith("/commit") for _, path in requests) == 1
            else:
                assert run.status == "model_unavailable" and batches == []
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_recall_configuration_and_emoji_boundaries():
    for values in (
        {"recall_window": -1},
        {"recall_query_max_chars": 0},
        {"remember_max_chars": 0},
    ):
        with pytest.raises(ValueError):
            MemoryConfig(**values)
    cfg = MemoryConfig()
    for text in ("", "?", "∞", "€", "1", "*", "hello👍", "👍?", "\u200d"):
        assert not trivial_turn(text, cfg)
    assert trivial_turn("👍 👍", cfg)


def test_note_evidence_in_api_and_web_ui_is_escaped(config, mock_model, auth):
    from fastapi.testclient import TestClient

    from solakit.main import create_app

    text = "<original & deliberate>"
    execution, _, _, _ = runtime(config, note=text)

    async def exercise():
        try:
            return await execution.submit("s", "Remember a correction")
        finally:
            await execution.close()

    run = asyncio.run(exercise())
    config.memory.enabled = False  # Inspection only; no external memory calls.
    with TestClient(create_app(config=config, model=mock_model)) as client:
        response = client.get(f"/api/runs/{run.id}", headers=auth)
        assert response.status_code == 200
        assert response.json()["memory_notes"][0]["text"] == text
        client.post("/login", data={"token": "operator-test-token"})
        page = client.get(f"/settings/evidence/runs/{run.id}")
        assert page.status_code == 200
        assert "Staged memory notes" in page.text and "Recall" in page.text
        assert "<original & deliberate>" not in page.text
        assert "original" in page.text and "deliberate" in page.text
