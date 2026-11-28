import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from test_api import BASE, PASSWORD, login
from test_jobs import Clock
from test_run_loop import call, harness, reply, wire

from solakit.api import create_app
from solakit.config import BackgroundSettings, EffectiveProfile, FeedSettings, MemorySettings
from solakit.engine.feeds import FeedService, fetch
from solakit.engine.run_loop import records
from solakit.records import (
    Delivery,
    DeliveryState,
    FeedItem,
    Job,
    MemoryCommit,
    MemoryRecall,
    Message,
    Origin,
    RunState,
    SessionMode,
    ToolReceipt,
    new_id,
)
from solakit.storage.backup import backup, restore
from solakit.storage.evidence import checked_json, read_artifact
from solakit.storage.search import search
from solakit.storage.store import Store
from solakit.tools.web_fetch import MAX_RESPONSE_BYTES

FIXTURES = Path(__file__).parent / "fixtures/feeds"
RSS = (FIXTURES / "rss.xml").read_bytes()
ATOM = (FIXTURES / "atom.xml").read_bytes()
PUBLIC = "93.184.215.14"


class Body(httpx2.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for offset in range(0, len(self.body), 32768):
            yield self.body[offset : offset + 32768]


async def public(host: str, port: int) -> list[str]:
    return [PUBLIC]


class LocalHTTP(httpx2.AsyncHTTPTransport):
    """Route validated fixture requests into a disposable loopback server."""

    def __init__(self, port: int) -> None:
        super().__init__(trust_env=False)
        self.port = port

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == PUBLIC and request.headers["Host"] == "publisher.example"
        request.url = request.url.copy_with(host="127.0.0.1", port=self.port)
        return await super().handle_async_request(request)


def test_poll_local_http_fixtures_and_retains_dedupe_across_restore(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "store")
        await store.start()
        clock = Clock()
        served: dict[bytes, bytes] = {b"/rss": RSS, b"/feeds/atom": ATOM}
        wire_requests: list[bytes] = []

        async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            raw = await reader.readuntil(b"\r\n\r\n")
            wire_requests.append(raw)
            body = served[raw.split(b" ")[1]]
            writer.write(
                b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        settings = FeedSettings(enabled=True, max_items_per_feed=2, item_retention_days=1)
        server = await asyncio.start_server(respond, "127.0.0.1", 0)
        transport = LocalHTTP(server.sockets[0].getsockname()[1])
        service = FeedService(
            store, lambda: settings, clock=clock, resolver=public, transport=transport
        )
        async with server:
            rss = service.add("http://publisher.example/rss")
            atom = service.add("http://publisher.example/feeds/atom")
            assert (await service.poll(rss.id)).title == "Fixture Gazette"
            settings = settings.model_copy(update={"max_items_per_feed": 3})
            assert (await service.poll(atom.id)).title == "Fixture Journal"
            assert len(store.feed_items(feed_ids=[atom.id])) == 3
            entries = {item.title: item for item in store.feed_items()}
            relative = entries["Relative link"]
            assert relative.link == "http://publisher.example/news/pages/first"
            assert relative.summary == "Atom excerpt."
            assert relative.published_at == datetime(2026, 11, 27, 15, 30, tzinfo=UTC)
            assert entries["Identifier only"].link == ""
            assert entries["Identifier only"].published_at == clock.now
            assert entries["Identifier only"].guid_hash != relative.guid_hash
            first = entries["A first story"]
            assert first.link == "https://example.org/articles/first"
            assert first.summary == "First external excerpt."
            assert search(store.root, "First external excerpt") == []
            assert first.published_at == datetime(2026, 11, 27, 16, 30, tzinfo=UTC)
            assert entries["Link only"].link == "http://publisher.example/news/story"

            settings = settings.model_copy(update={"max_items_per_feed": 1})
            await service.poll(rss.id)
            visible = store.feed_items(feed_ids=[rss.id])
            assert len(visible) == 1
            dropped = next(
                item
                for item in entries.values()
                if (item.feed_id == rss.id and item.id != visible[0].id)
            )
            assert store.feed_item(dropped.id).deleted and not store.feed_item(dropped.id).summary
            await service.poll(rss.id)
            assert store.feed_items(feed_ids=[rss.id]) == visible
            service.mark_read(visible)
            service.change_status(rss.id, "paused")
            backup(store, tmp_path / "backup")
            await store.stop()
            restore(tmp_path / "backup", tmp_path / "restored")
            service.store = store = Store(tmp_path / "restored")
            await store.start()
            assert store.feed_item(visible[0].id).read
            assert store.feed(rss.id).status == "paused"
            clock.now += timedelta(days=2)
            await service.tick()
            assert store.feed_items() == []
            service.change_status(rss.id, "active")
            await service.poll(rss.id)
            assert store.feed_items() == []
            assert any(
                ref.record.get("id") == dropped.id and ref.record.get("summary") == dropped.summary
                for ref in (store.log.read(r) for r in store.log.references.values())
            )
            service.remove(rss.id)
            replacement = service.add(rss.url)
            assert replacement.id != rss.id
            await service.poll(replacement.id)
            assert store.feed_counts()[replacement.id] == (1, 1)
            assert all(b"Host: publisher.example\r\n" in raw for raw in wire_requests)
            assert all(
                b"Authorization:" not in raw and b"Cookie:" not in raw for raw in wire_requests
            )
            await store.stop()

    asyncio.run(scenario())


def test_poll_rejects_dns_and_redirect_hops_before_connecting() -> None:
    async def scenario() -> None:
        requests: list[tuple[str, str, str, str]] = []

        async def resolver(host: str, port: int) -> list[str]:
            return [PUBLIC, "10.0.0.1"] if host == "mixed.example" else [PUBLIC, "1.1.1.1"]

        def respond(request: httpx2.Request) -> httpx2.Response:
            requests.append(
                (
                    request.url.host,
                    request.headers["Host"],
                    request.extensions["sni_hostname"],
                    request.url.path,
                )
            )
            return httpx2.Response(
                302, headers={"Location": "https://mixed.example/bad", "Set-Cookie": "private=x"}
            )

        transport = httpx2.MockTransport(respond)
        with pytest.raises(ValueError, match="public IP"):
            await fetch("https://mixed.example/feed", resolver, transport, {}, lambda: True)
        assert requests == []
        evidence: dict[str, object] = {}
        with pytest.raises(ValueError, match="public IP"):
            await fetch(
                "https://publisher.example:9443/feed", resolver, transport, evidence, lambda: True
            )
        assert requests == [(PUBLIC, "publisher.example:9443", "publisher.example", "/feed")]
        assert evidence["fetched_url"] == "https://mixed.example/bad"
        assert evidence["http_status"] is None

    asyncio.run(scenario())


def test_poll_errors_autopause_and_keyword_edits(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        store = Store(profile.data_dir)
        await store.start()
        body, requests = b"<rss", 0

        def respond(_: httpx2.Request) -> httpx2.Response:
            nonlocal requests
            requests += 1
            return httpx2.Response(200, stream=Body(body))

        service = FeedService(
            store,
            lambda: FeedSettings(enabled=True),
            resolver=public,
            transport=httpx2.MockTransport(respond),
        )
        feed = service.add("https://example.org/feed", keywords=("FIRST",))
        for index in range(1, 6):
            polled = await service.poll(feed.id)
            assert polled.consecutive_failures == index
            assert polled.last_failure_reason == "Malformed feed XML"
            assert store.feed_items() == []
        assert polled.status == "paused"
        await service.tick()
        assert requests == 5
        service.change_status(feed.id, "active")
        body = b" " * (MAX_RESPONSE_BYTES + 1)
        assert "1 MiB" in ((await service.poll(feed.id)).last_failure_reason or "")
        service.change_status(feed.id, "active")
        body = b'<!DOCTYPE rss [<!ENTITY test "expanded">]>' + RSS.split(b"?>", 1)[1]
        assert "document type" in ((await service.poll(feed.id)).last_failure_reason or "")
        service.change_status(feed.id, "active")
        body = RSS
        assert (await service.poll(feed.id)).consecutive_failures == 0
        assert len(store.feed_items()) == 1
        service.edit(feed.id, keywords=(), exclude_keywords=("FIRST",))
        await service.poll(feed.id)
        assert len(store.feed_items()) == 2
        await store.stop()

    asyncio.run(scenario())


def test_proposals_authority_pending_caps_and_disabled(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        def propose(domain: str) -> dict[str, Any]:
            return reply(
                "",
                calls=[
                    call(
                        "feeds.propose",
                        json.dumps(
                            {
                                "url": f"https://{domain}/feed",
                                "reason": "Relevant updates",
                                "discovered_from": "https://example.org/topic",
                            }
                        ),
                    )
                ],
            )

        configured = profile.model_copy(
            update={
                "feeds": FeedSettings(
                    enabled=True,
                    max_feeds=2,
                    max_agent_feeds=1,
                    max_agent_additions_per_day=2,
                )
            }
        )
        context, _ = harness(
            configured,
            [
                propose("example.org"),
                reply(),
                propose("other.example"),
                reply(),
                propose("other.example"),
                reply(),
                propose("last.example"),
                reply(),
            ],
        )
        assert context.feeds and context.store and context.runs and context.jobs
        store, jobs, runs = context.store, context.jobs, context.runs

        async def no_network(host: str, port: int) -> list[str]:
            pytest.fail("A pending, paused or removed feed resolved DNS")

        context.feeds.resolver = no_network
        await context.start()
        try:
            topic = store.create_session()

            async def interactive() -> ToolReceipt:
                run = runs.submit(
                    topic.id,
                    profile=context.profile,
                    content="Propose a feed.",
                    request_id=new_id(),
                )
                await runs.wait(run.id, timeout=3)
                return next(r for r in records(store, run.id) if isinstance(r, ToolReceipt))

            async def autonomous() -> ToolReceipt:
                definition = jobs.create(
                    Job(
                        origin=Origin.SCHEDULED,
                        profile=context.profile.name,
                        session_id=topic.id,
                        task="Propose a feed.",
                        schedule="cron:0 0 * * *",
                        tools=("feeds.propose",),
                    )
                )
                occurrence = jobs.manual(definition.id, request_id=new_id())
                assert occurrence.run_id
                await runs.wait(occurrence.run_id, timeout=3)
                return next(
                    r for r in records(store, occurrence.run_id) if isinstance(r, ToolReceipt)
                )

            for url in ("https://user:pass@example.org", "http://localhost", "http://127.0.0.1"):
                with pytest.raises(ValueError, match="public HTTP"):
                    context.feeds.add(url)
            first_receipt = await interactive()
            first = store.feeds()[0]
            assert first.status == "pending" and first.discovered_session_id == topic.id
            assert first.discovered_run_id == first_receipt.run_id
            await context.feeds.tick()
            context.feeds.change_status(first.id, "active")
            context.feeds.change_status(first.id, "paused")
            denied = await autonomous()
            assert denied.failure and "Agent feed subscription limit" in denied.failure.message
            operator_feed = context.feeds.add("https://reader.example/rss")
            context.feeds.change_status(operator_feed.id, "paused")
            with pytest.raises(ValueError, match="Feed subscription limit"):
                context.feeds.add("https://reader.example/another")
            context.feeds.remove(operator_feed.id)
            context.feeds.remove(first.id)
            second_receipt = await autonomous()
            second = store.feeds()[0]
            assert second.status == "pending" and second.discovered_run_id == second_receipt.run_id
            context.feeds.remove(second.id)
            denied = await interactive()
            assert denied.failure and "last 24 hours" in denied.failure.message
            await context.feeds.tick()
            assert len(store.feeds(include_deleted=True)) == 3
        finally:
            await context.stop()

    asyncio.run(scenario())


def test_digest_batches_survive_deferred_delivery_and_restart(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        clock = Clock(datetime(2026, 11, 27, 23, 0, tzinfo=UTC))
        settings = FeedSettings(enabled=True)
        configured = profile.model_copy(
            update={
                "feeds": settings,
                "memory": MemorySettings(enabled=True),
                "background": BackgroundSettings(
                    timezone="UTC", quiet_start=22, quiet_end=7, notification_ttl_seconds=43200
                ),
            }
        )
        returned: list[list[str]] = []

        async def respond(body: dict[str, Any]) -> bytes:
            messages = body["messages"]
            if any(
                "Correct rejected arguments" in message.get("content", "")
                for message in messages[-2:]
            ):
                return wire(
                    reply(
                        "",
                        calls=[
                            call("feeds.acknowledge", json.dumps({"ids": returned[-1]})),
                        ],
                    )
                )
            if messages[-1]["role"] != "tool":
                return wire(reply("", calls=[call("feeds.items", '{"undigested_only":true}')]))
            last = json.loads(messages[-1]["content"])
            result = json.loads(last["result"]) if last["result"] else {}
            if "items" in result:
                assert result["source"] == "untrusted external feed data"
                ids = [item["id"] for item in result["items"]]
                returned.append(ids)
                if not ids:
                    return wire(reply("NO_ACTION"))
                return wire(
                    reply(
                        "",
                        calls=[
                            call("feeds.acknowledge", json.dumps({"ids": [new_id()]})),
                        ],
                    )
                )
            assert result["staged_ids"] == returned[-1]
            return wire(reply(f"Published batch of {len(returned[-1])} excerpts."))

        context, _ = harness(configured, respond)
        assert context.store and context.jobs and context.feeds and context.runs and context.memory
        context.jobs.clock = clock
        await context.start()
        store = context.store
        try:
            feed = context.feeds.add("https://publisher.example/feed")
            items = [
                FeedItem(
                    origin=Origin.SYSTEM,
                    feed_id=feed.id,
                    guid_hash=str(i),
                    title=f"Story {i}",
                    fetched_at=clock.now + timedelta(seconds=i),
                    read=i == 0,
                )
                for i in range(25)
            ]
            store._commit(tuple(items))
            topic = store.create_session(mode=SessionMode.ISOLATED)
            job = context.jobs.create(
                Job(
                    origin=Origin.SCHEDULED,
                    profile=configured.name,
                    mode=topic.mode,
                    session_id=topic.id,
                    task=(
                        "Use feeds.items to summarize fresh items. Call feeds.acknowledge for "
                        "covered IDs, then publish the digest. Return NO_ACTION for an empty batch."
                    ),
                    schedule="cron:* * * * *",
                    timezone="UTC",
                    enabled=True,
                    tools=("feeds.items", "feeds.acknowledge"),
                )
            )
            assert job.next_due
            clock.now = job.next_due
            await context.jobs.tick()
            row = store.db.execute("SELECT id FROM entities WHERE kind='run'").fetchone()
            run = await context.runs.wait(row[0], timeout=5)
            assert run.state == RunState.COMPLETED, run.reason
            stage = [r for r in records(store, run.id) if isinstance(r, ToolReceipt)]
            assert [r.status for r in stage] == ["succeeded", "failed", "succeeded"]
            assert returned == [[item.id for item in items[:20]]]
            assert not any(item.digested for item in store.feed_items())
            trace = records(store, run.id)
            assert not any(isinstance(r, MemoryCommit) for r in trace)
            assert next(r for r in trace if isinstance(r, MemoryRecall)).status == "disabled"
            pending = next(r for r in records(store, run.id) if isinstance(r, Delivery))
            assert (
                pending.state == DeliveryState.PENDING
                and list(pending.feed_item_ids) == returned[0]
            )
        finally:
            await context.stop()

        context.jobs.clock = clock
        await context.start()
        try:
            clock.now = datetime(2026, 11, 28, 7, 0, tzinfo=UTC)
            context.jobs.deliver()
            assert store.feed_item(items[0].id).read
            assert store.feed_item(items[0].id).digested
            assert not store.feed_item(items[1].id).read
            assert not store.feed_item(items[-1].id).digested
            assert len([m for _, m in store.messages(topic.id) if m.role == "assistant"]) == 1
            for _ in range(2):
                occurrence = context.jobs.manual(job.id, request_id=new_id())
                assert occurrence.run_id
                run = await context.runs.wait(occurrence.run_id, timeout=5)
                assert run.state == RunState.COMPLETED
            assert returned[1:] == [[item.id for item in items[20:]], []]
            assert len([m for _, m in store.messages(topic.id) if m.role == "assistant"]) == 2
            assert all(item.digested for item in store.feed_items())
        finally:
            await context.stop()

        await context.start()
        try:
            assert all(item.digested for item in store.feed_items())
            assert store.feed_item(items[0].id).read
            assert len([m for _, m in store.messages(topic.id) if m.role == "assistant"]) == 2
        finally:
            await context.stop()

    asyncio.run(scenario())


def test_reader_auth_isolated_ask_and_external_evidence(
    profile: EffectiveProfile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLA_OPERATOR_PASSWORD", PASSWORD)
    configured = profile.model_copy(
        update={
            "feeds": FeedSettings(enabled=True),
            "memory": MemorySettings(enabled=True, roots=("viking://user/default/memories",)),
        }
    )
    context, requests = harness(configured, [reply("An isolated answer.")])
    assert context.feeds and context.store and context.memory
    memory_requests = []

    def memory_provider(request: httpx2.Request) -> httpx2.Response:
        memory_requests.append(request)
        return httpx2.Response(500)

    context.memory.adapter.transport = httpx2.MockTransport(memory_provider)
    context.feeds.resolver = public
    context.feeds.transport = httpx2.MockTransport(lambda _: httpx2.Response(200, stream=Body(RSS)))
    with TestClient(create_app(context), base_url=BASE) as client:
        assert client.get("/api/v1/feeds").status_code == 401
        login(client)
        assert not any(
            tool["function"]["name"] == "feeds.items"
            for tool in client.get("/api/v1/tools").json()["items"]
        )
        assert (
            client.post("/api/v1/feeds", json={"url": "http://localhost/feed"}).status_code == 409
        )
        feed = client.post("/api/v1/feeds", json={"url": "https://example.org/rss"}).json()
        identity = feed["id"]
        assert client.post(f"/api/v1/feeds/{identity}/poll").json()["consecutive_failures"] == 0
        items = client.get("/api/v1/feed-items").json()["items"]
        first = next(item for item in items if item["title"] == "A first story")
        endpoint = f"/api/v1/feed-items/{first['id']}"
        assert client.post(f"{endpoint}/read").json()["read"] is True
        key = new_id()
        ask = client.post(
            f"{endpoint}/ask",
            headers={"Idempotency-Key": key},
            json={"content": "What is the story about?"},
        )
        assert ask.status_code == 202, ask.text
        run = ask.json()
        assert client.get(f"/api/v1/sessions/{run['session_id']}").json()["mode"] == "isolated"
        assert client.get(f"/api/v1/runs/{run['id']}?wait=3").json()["state"] == "completed"
        assert (
            client.post(
                f"{endpoint}/ask",
                headers={"Idempotency-Key": key},
                json={"content": "What is the story about?"},
            ).json()["id"]
            == run["id"]
        )
        assert client.portal
        trace = client.portal.call(records, context.store, run["id"])
        user = next(
            record for record in trace if isinstance(record, Message) and record.role == "user"
        )
        assert user.content == "What is the story about?"
        assert not any(isinstance(record, MemoryCommit) for record in trace)
        assert (
            next(record for record in trace if isinstance(record, MemoryRecall)).status
            == "disabled"
        )
        excerpt = checked_json(read_artifact(context.store.root, user.documents[0]))
        assert isinstance(excerpt, dict)
        assert excerpt["untrusted_external_feed_excerpt"]["item_id"] == first["id"]
        assert any(
            "untrusted_external_feed_excerpt" in message["content"]
            for message in requests[0]["messages"]
        )
        assert client.post(f"/api/v1/feeds/{identity}/read").json() == {"marked": 1}
        assert not client.get("/api/v1/feed-items").json()["items"]
        disabled = context.profile.model_copy(update={"feeds": FeedSettings(enabled=False)})
        client.portal.call(setattr, context, "profile", disabled)
        assert client.get("/api/v1/feeds").json()["enabled"] is False
        assert len(client.get("/api/v1/feed-items?unread_only=false").json()["items"]) == 2
        assert client.post(f"/api/v1/feeds/{identity}/poll").status_code == 409
        assert all(
            not tool["function"]["name"].startswith("feeds.")
            for tool in client.get("/api/v1/tools").json()["items"]
        )
        assert client.delete(f"/api/v1/feeds/{identity}").json()["deleted"]
    assert not memory_requests


@pytest.mark.parametrize("outcome", ["failed", "no_action", "expired", "coalesced"])
def test_staging_without_delivery_preserves_eligibility(
    profile: EffectiveProfile,
    outcome: str,
) -> None:
    async def scenario() -> None:
        clock = Clock(datetime(2026, 11, 27, 23, tzinfo=UTC))
        configured = profile.model_copy(
            update={
                "feeds": FeedSettings(enabled=True),
                "background": BackgroundSettings(
                    timezone="UTC",
                    quiet_start=22,
                    quiet_end=7,
                    topic_cooldown_seconds=1,
                    notification_ttl_seconds=300 if outcome == "expired" else 43200,
                ),
            }
        )
        requested_id = ""
        responded: list[str] = []

        async def respond(body: dict[str, Any]) -> bytes:
            last = body["messages"][-1]
            if last["role"] != "tool":
                return wire(
                    reply(
                        "",
                        calls=[
                            call(
                                "feeds.items",
                                json.dumps({"feed_ids": [requested_id], "undigested_only": True}),
                            )
                        ],
                    )
                )
            result = json.loads(json.loads(last["content"])["result"])
            if "items" in result:
                identity = result["items"][0]["id"]
                return wire(
                    reply(
                        "",
                        calls=[
                            call(
                                "feeds.acknowledge",
                                json.dumps({"ids": [identity]}),
                            )
                        ],
                    )
                )
            responded.extend(result["staged_ids"])
            if outcome == "failed":
                return b"invalid model stream"
            return wire(reply("NO_ACTION" if outcome == "no_action" else f"Digest {requested_id}"))

        context, _ = harness(configured, respond)
        assert context.jobs and context.store and context.feeds and context.runs
        store, jobs, runs = context.store, context.jobs, context.runs
        jobs.clock = clock
        await context.start()
        try:
            topic = store.create_session(mode=SessionMode.ISOLATED)
            sources = [context.feeds.add(f"https://example.org/{index}") for index in range(2)]
            items = [
                FeedItem(
                    origin=Origin.SYSTEM,
                    feed_id=source.id,
                    guid_hash="key",
                    title="An update",
                    fetched_at=clock.now,
                )
                for source in sources
            ]
            store._commit(tuple(items))
            definition = jobs.create(
                Job(
                    origin=Origin.SCHEDULED,
                    profile=profile.name,
                    session_id=topic.id,
                    mode=topic.mode,
                    task="Summarize feed items and stage any coverage.",
                    schedule="cron:* * * * *",
                    timezone="UTC",
                    enabled=True,
                    tools=("feeds.items", "feeds.acknowledge"),
                )
            )

            async def next_run(index: int) -> str:
                nonlocal requested_id
                requested_id = sources[index].id
                due = store.job(definition.id).next_due
                assert due
                clock.now = due
                await jobs.tick()
                row = store.db.execute(
                    "SELECT id FROM entities WHERE kind='run' ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                run = await runs.wait(row[0], timeout=5)
                assert (
                    run.state == RunState.FAILED
                    if outcome == "failed"
                    else (run.state == RunState.COMPLETED)
                )
                return run.id

            first = await next_run(0)
            assert responded == [items[0].id]
            assert not store.feed_item(items[0].id).digested
            if outcome == "coalesced":
                await next_run(1)
                assert not any(item.digested for item in store.feed_items())
            clock.now = datetime(2026, 11, 28, 7, tzinfo=UTC)
            jobs.deliver()
            assert not store.feed_item(items[0].id).digested
            deliveries = [r for r in records(store, first) if isinstance(r, Delivery)]
            if outcome in {"expired", "coalesced"}:
                assert [r.state for r in deliveries] == [DeliveryState.CANCELLED]
            else:
                assert not deliveries
            assert store.feed_item(items[1].id).digested is (outcome == "coalesced")
        finally:
            await context.stop()

    asyncio.run(scenario())


def test_feed_tool_whole_items_and_memory_revocation(profile: EffectiveProfile) -> None:
    async def scenario() -> None:
        configured = profile.model_copy(
            update={
                "feeds": FeedSettings(enabled=True),
                "budgets": profile.budgets.model_copy(update={"tool_result_bytes": 1024}),
            }
        )
        response = reply("", calls=[call("feeds.items", '{"undigested_only":true}')])
        context, requests = harness(configured, [response, reply("Read the feed item."), response])
        assert context.store and context.feeds and context.runs
        store, runs = context.store, context.runs
        await context.start()
        try:
            feed = context.feeds.add("https://example.org/feed")
            now = datetime.now(UTC)
            first = FeedItem(
                origin=Origin.SYSTEM,
                feed_id=feed.id,
                guid_hash="one",
                title="Short",
                summary='Exact "excerpt"',
                fetched_at=now,
            )
            second = FeedItem(
                origin=Origin.SYSTEM,
                feed_id=feed.id,
                guid_hash="two",
                title="Long",
                summary="x" * 2000,
                fetched_at=now + timedelta(seconds=1),
            )
            store._commit((first, second))
            isolated = store.create_session(mode=SessionMode.ISOLATED)
            run = runs.submit(
                isolated.id,
                profile=context.profile.model_copy(
                    update={
                        "mode": isolated.mode,
                        "memory_mode": "off",
                    }
                ),
                content="Read feeds.items.",
                request_id=new_id(),
            )
            assert (await runs.wait(run.id, timeout=5)).state == RunState.COMPLETED
            receipt = next(r for r in records(store, run.id) if isinstance(r, ToolReceipt))
            assert not receipt.truncated and isinstance(receipt.result, str)
            result = json.loads(receipt.result)
            assert result["has_more"]
            assert [r["id"] for r in result["items"]] == [first.id]
            assert result["items"][0]["summary"] == first.summary
            assert len(requests[1]["messages"][-1]["content"].encode()) <= 1024

            shared = store.create_session()
            run = runs.submit(
                shared.id,
                profile=context.profile,
                content="Read feeds.items.",
                request_id=new_id(),
            )
            assert (await runs.wait(run.id, timeout=5)).state == RunState.FAILED
            assert not any(
                tool["function"]["name"] == "feeds.items" for tool in requests[-1]["tools"]
            )
            receipt = next(r for r in records(store, run.id) if isinstance(r, ToolReceipt))
            assert receipt.status == "denied"
        finally:
            await context.stop()

    asyncio.run(scenario())
