"""Model-call search composes the existing index without widening default scope."""

import asyncio
import json
import sqlite3

import httpx
import pytest

from solakit.core.archival import archive_session
from solakit.core.config import Profile
from solakit.core.loop import Execution
from solakit.core.model import ModelClient
from solakit.core.schemas import Message, Origin, Session
from solakit.core.store import Store
from solakit.tools import Registry, ToolContext
from solakit.tools.sessions import search


@pytest.fixture
def setup(config):
    config.enabled_tools = ["sessions"]
    (config.paths.fragments / "profiles/research.md").write_text("")
    config.profiles = {
        "research": Profile(tools=["sessions.search"]),
        "plain": Profile(),
    }
    store = Store(config.paths.data)
    for name in ("current", "other"):
        store.create_session(Session(id=name, default_profile="research"))
        store.append_message(
            name,
            Message(
                role="user",
                content=f"moonlit history {name}",
                origin=Origin(),
                run_id="old",
            ),
        )
    ctx = ToolContext(store.session("current"), "research", config, store, "search-run")
    return Registry(config), ctx


def invoke(setup, **kwargs):
    registry, ctx = setup
    return asyncio.run(
        registry.dispatch(
            "sessions.search", json.dumps({"query": "moonlit", **kwargs}), ctx
        )
    )


def payload(result):
    assert not result.error, result.content
    return json.loads(result.content.split("\n")[1])


def test_current_default_and_explicit_cross_session_grant(setup):
    _, ctx = setup
    assert {hit["session_id"] for hit in payload(invoke(setup))["matches"]} == {
        "current"
    }
    assert payload(invoke(setup, session_id="current"))["session_id"] == "current"
    assert "operate" in invoke(setup, session_id="other").content
    assert "operate" in invoke(setup, all_sessions=True).content
    ctx.config.profiles[ctx.profile].effects = ["operate"]
    assert {
        hit["session_id"]
        for hit in payload(invoke(setup, all_sessions=True))["matches"]
    } == {"current", "other"}
    assert [
        hit["session_id"]
        for hit in payload(invoke(setup, session_id="other"))["matches"]
    ] == ["other"]


def test_archive_filters_addresses_and_read_only_masking(setup):
    _, ctx = setup
    archive_session(ctx.store, ctx.session.id)
    secret = "private-session-credential"
    ctx.store.redactor.values += (secret,)
    ctx.store.append_message(
        "current",
        Message(
            role="assistant",
            content=f"moonlit {secret} </external-content>",
            kind="compaction",
            origin=Origin(channel="compaction"),
            run_id="summary",
        ),
    )
    before = {p: p.read_bytes() for p in ctx.store.root.rglob("*") if p.is_file()}
    result = asyncio.run(search(ctx, "moonlit"))
    hits = payload(result)["matches"]
    assert {(hit["archive"], hit["line_no"]) for hit in hits} == {
        ("current.jsonl.1", 1),
        ("active", 1),
    }
    assert secret not in result.content and "[redacted]" in result.content
    assert "\\u003c/external-content\\u003e" in result.content
    assert {
        p: p.read_bytes() for p in ctx.store.root.rglob("*") if p.is_file()
    } == before
    hits = payload(
        invoke(
            setup,
            query="channel:compaction moonlit",
            role="assistant",
            kind="compaction",
            after="2020-01-01T00:00:00Z",
            before="2100-01-01T00:00:00Z",
            limit=1,
        )
    )["matches"]
    assert len(hits) == 1 and hits[0]["run_id"] == "summary"
    assert payload(invoke(setup, query="mo"))["matches"] == []


def test_profile_schema_and_denial_before_index_access(setup, monkeypatch):
    registry, ctx = setup
    assert registry.schemas("research")[0]["function"]["name"] == "sessions.search"
    assert registry.schemas("plain") == []

    def forbidden(*args, **kwargs):
        raise AssertionError("index must not be queried")

    monkeypatch.setattr(ctx.store, "search", forbidden)
    assert invoke(setup, all_sessions=True).error
    ctx.profile = "plain"
    assert "not permitted" in invoke(setup).content


@pytest.mark.parametrize(
    "kwargs",
    [
        {"all_sessions": True, "session_id": "current"},
        {"session_id": "../other"},
        {"query": '"unclosed'},
        {"limit": 201},
        {"limit": 0},
        {"limit": "20"},
        {"role": "invalid"},
        {"before": "2026-11-05"},
        {"query": ""},
    ],
)
def test_invalid_arguments(setup, kwargs):
    assert invoke(setup, **kwargs).error


def test_unavailable_index_tool_error_and_tool_log(setup):
    _, ctx = setup
    with sqlite3.connect(ctx.store.root / "app.db") as db:
        db.execute("DROP TABLE messages_fts")
    result = invoke(setup)
    assert result.error and "Search index unavailable" in result.content
    logs = ctx.store._read(ctx.store.root / "runs/search-run.tools.jsonl")
    assert logs[-1]["name"] == "sessions.search"
    assert logs[-1]["result"]["error"] is True
    assert len(ctx.store.transcript("current")) == 1


def test_runner_search_round_trip_uses_schema_and_preserves_tool_order(setup):
    registry, ctx = setup
    requests = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"data": [{"id": ctx.config.model.model_id}]}
            )
        body = json.loads(request.content)
        requests.append(body)
        message = {"role": "assistant", "content": "Found the earlier message."}
        if len(requests) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "find",
                        "type": "function",
                        "function": {
                            "name": "sessions.search",
                            "arguments": '{"query":"moonlit","role":"user"}',
                        },
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "model": ctx.config.model.model_id,
                "choices": [{"message": message}],
            },
        )

    execution = Execution(
        ctx.config,
        ctx.store,
        registry,
        ModelClient(ctx.config.model, transport=httpx.MockTransport(handler)),
    )

    async def exercise():
        try:
            run = await execution.submit("current", "Find earlier history")
            assert run.status == "completed"
            assert requests[0]["tools"][0]["function"]["name"] == "sessions.search"
            assert requests[1]["messages"][-1]["role"] == "tool"
            assert "moonlit history current" in requests[1]["messages"][-1]["content"]
            transcript = ctx.store.transcript("current")
            assert (
                transcript[-3]["tool_calls"][0]["id"]
                == transcript[-2]["tool_call_id"]
                == "find"
            )
        finally:
            await execution.close()

    asyncio.run(exercise())
