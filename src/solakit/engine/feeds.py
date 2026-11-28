"""Deterministic feed ingestion and operator-owned reader state."""

import asyncio
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from ipaddress import ip_address
from itertools import islice
from typing import Literal
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

import httpx2
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]

from solakit.config import FeedSettings
from solakit.records import FeedItem, FeedSubscription, Origin, Run, utc_now
from solakit.storage.evidence import digest
from solakit.storage.store import Store
from solakit.tools.web_fetch import MAX_RESPONSE_BYTES, WebFetch

FETCH_TIMEOUT = 15
ATOM = "{http://www.w3.org/2005/Atom}"
XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
REDIRECTS = {301, 302, 303, 307, 308}
type FeedStatus = Literal["active", "pending", "paused"]
type Resolver = Callable[[str, int], Awaitable[list[str]]]


def feed_url(value: str) -> str:
    try:
        canonical = WebFetch(url=value).url
    except ValueError:
        raise ValueError("Expected a public HTTP(S) URL without credentials") from None
    if len(canonical) > 2048:
        raise ValueError("Expected a public HTTP(S) URL of at most 2,048 characters")
    return canonical.partition("#")[0]


async def resolve(host: str, port: int) -> list[str]:
    answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(answer[4][0]) for answer in answers]


async def fetch(
    url: str,
    resolver: Resolver,
    transport: httpx2.AsyncBaseTransport | None,
    evidence: dict[str, object],
    enabled: Callable[[], bool],
) -> tuple[bytes, str]:
    visited: set[str] = set()
    for hop in range(6):
        url = feed_url(url)
        evidence.update(fetched_url=url, http_status=None)
        if url in visited:
            raise ValueError("Feed redirect loop")
        visited.add(url)
        original = httpx2.URL(url)
        server_name = original.raw_host.decode("ascii")
        port = original.port or (443 if original.scheme == "https" else 80)
        addresses = [ip_address(value) for value in await resolver(server_name, port)]
        if not addresses or any(
            not address.is_global or address.is_multicast for address in addresses
        ):
            raise ValueError("Feed destination must resolve only to public IP addresses")
        if not enabled():
            raise ValueError("Feed polling is disabled")
        address = addresses[0]
        pinned = original.copy_with(host=str(address))
        # The socket uses the numeric address. Keep the feed origin for HTTP and verified TLS.
        async with (
            httpx2.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                transport=transport,
                timeout=FETCH_TIMEOUT,
            ) as client,
            client.stream(
                "GET",
                pinned,
                headers={
                    "Host": urlsplit(url).netloc,
                    "Accept": "application/rss+xml, application/atom+xml, "
                    "application/xml, text/xml, */*",
                    "Accept-Encoding": "identity",
                },
                extensions={"sni_hostname": server_name},
            ) as response,
        ):
            evidence["http_status"] = response.status_code
            if response.status_code in REDIRECTS:
                location = response.headers.get("location")
                if not location or hop == 5:
                    raise ValueError("Feed redirect is missing a location or exceeds five hops")
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                raise ValueError(f"Feed returned HTTP {response.status_code}")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("Feed returned an unexpected content encoding")
            body = bytearray()
            async for chunk in response.aiter_raw():
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError("Feed response exceeds 1 MiB")
                body.extend(chunk)
            return bytes(body), url
    raise AssertionError("unreachable redirect limit")


class FeedXML(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ValueError("Feed XML cannot contain a document type declaration")


class Excerpt(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def node_text(node: ET.Element | None, *, markup: bool = False) -> str:
    if node is None:
        return ""
    text = "".join(node.itertext())
    if markup:
        parser = Excerpt()
        parser.feed(text)
        parser.close()
        text = " ".join(parser.parts)
    return " ".join(text.split())


def article_url(base: str, value: str) -> str:
    if value.strip():
        try:
            link = WebFetch(url=urljoin(base, value.strip())).url
            return link if len(link) <= 2048 else ""
        except ValueError:
            pass
    return ""


def base_url(base: str, node: ET.Element) -> str:
    try:
        return urljoin(base, node.get(XML_BASE, ""))
    except ValueError:
        return ""


def published(value: str, atom: bool, fallback: datetime) -> datetime:
    try:
        date = datetime.fromisoformat(value) if atom else parsedate_to_datetime(value)
        return date.replace(tzinfo=date.tzinfo or UTC).astimezone(UTC)
    except ValueError, OverflowError:
        return fallback


def parse(
    body: bytes,
    url: str,
    feed_id: str,
    fetched_at: datetime,
) -> tuple[str, list[FeedItem]]:
    try:
        root = ET.fromstring(body, parser=ET.XMLParser(target=FeedXML()))
    except (ET.ParseError, LookupError) as error:
        raise ValueError("Malformed feed XML") from error
    base = base_url(url, root)
    atom = root.tag == ATOM + "feed"
    if atom:
        channel, ns, entry_tag = root, ATOM, "entry"
    elif (
        root.tag == "rss"
        and root.get("version") == "2.0"
        and (channel_node := root.find("channel")) is not None
    ):
        channel, ns, entry_tag = channel_node, "", "item"
        base = base_url(base, channel)
    else:
        raise ValueError("Expected an RSS 2.0 or Atom feed")
    title = node_text(channel.find(ns + "title"))[:200]
    items = []
    for entry in islice(channel.iterfind(ns + entry_tag), 100):
        ident = (entry.findtext(ns + ("id" if atom else "guid")) or "").strip()
        entry_base = base_url(base, entry)
        link = ""
        for node in entry.iterfind(ns + "link"):
            if atom and node.get("rel", "alternate") != "alternate":
                continue
            value = node.get("href", "") if atom else "".join(node.itertext())
            if link := article_url(base_url(entry_base, node), value):
                break
        if not ident and not link:
            continue
        summary = entry.find(ns + ("summary" if atom else "description"))
        items.append(
            FeedItem(
                origin=Origin.SYSTEM,
                feed_id=feed_id,
                guid_hash=digest(("id:" + ident if ident else "link:" + link).encode()),
                title=node_text(entry.find(ns + "title"))[:200],
                link=link,
                summary=node_text(
                    summary,
                    markup=not atom or (summary is not None and summary.get("type") == "html"),
                )[:2000],
                published_at=published(
                    entry.findtext(ns + ("updated" if atom else "pubDate"), ""),
                    atom,
                    fetched_at,
                ),
                fetched_at=fetched_at,
                created_at=fetched_at,
            )
        )
    return title, items


def prune(item: FeedItem) -> FeedItem:
    return item.model_copy(
        update={
            "deleted": True,
            "version": item.version + 1,
            "title": "",
            "link": "",
            "summary": "",
            "published_at": None,
        }
    )


class FeedService:
    def __init__(
        self,
        store: Store,
        settings: Callable[[], FeedSettings],
        *,
        clock: Callable[[], datetime] = utc_now,
        resolver: Resolver = resolve,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.store, self.settings, self.clock = store, settings, clock
        self.resolver, self.transport = resolver, transport
        self.scheduler: AsyncIOScheduler | None = None
        self.polls: dict[str, asyncio.Task[FeedSubscription]] = {}

    def visible_feed(self, identity: str) -> FeedSubscription:
        feed = self.store.feed(identity)
        if feed.deleted:
            raise KeyError(identity)
        return feed

    def visible_item(self, identity: str) -> FeedItem:
        item = self.store.feed_item(identity)
        if item.deleted:
            raise KeyError(identity)
        return item

    def add(
        self,
        url: str,
        *,
        title: str = "",
        keywords: tuple[str, ...] = (),
        exclude_keywords: tuple[str, ...] = (),
        run: Run | None = None,
        reason: str = "",
        discovered_from: str = "",
        ceiling: FeedSettings | None = None,
    ) -> FeedSubscription:
        try:
            url = feed_url(url)
        except ValueError:
            raise ValueError("Expected a public HTTP(S) URL without credentials") from None
        settings = ceiling or self.settings()
        now = self.clock()
        feeds = self.store.feeds()
        if any(feed.url == url for feed in feeds):
            raise ValueError("This feed URL is already subscribed or proposed")
        if len(feeds) >= settings.max_feeds:
            raise ValueError("Feed subscription limit reached")
        if run is not None:
            if sum(feed.source == "agent" for feed in feeds) >= settings.max_agent_feeds:
                raise ValueError("Agent feed subscription limit reached")
            recent = self.store.db.execute(
                "SELECT count(*) FROM entities WHERE kind='feed' AND created_at>? "
                "AND json_extract(body,'$.source')='agent'",
                ((now - timedelta(days=1)).isoformat(),),
            ).fetchone()[0]
            if recent >= settings.max_agent_additions_per_day:
                raise ValueError("Agent feed proposal limit reached for the last 24 hours")
        feed = FeedSubscription(
            url=url,
            title=title,
            keywords=keywords,
            exclude_keywords=exclude_keywords,
            source="operator" if run is None else "agent",
            status="active" if run is None else "pending",
            origin=Origin.INTERACTIVE if run is None else run.origin,
            discovery_reason=reason if run else None,
            discovered_from=discovered_from or None,
            discovered_run_id=run.id if run else None,
            discovered_session_id=run.session_id if run else None,
            created_at=now,
        )
        self.store._commit((feed,))
        return feed

    def edit(self, identity: str, **changes: object) -> FeedSubscription:
        feed = self.visible_feed(identity)
        return self._update(feed, changes)

    def change_status(self, identity: str, status: FeedStatus) -> FeedSubscription:
        feed = self.visible_feed(identity)
        if (feed.status, status) not in {
            ("pending", "active"),
            ("active", "paused"),
            ("paused", "active"),
        }:
            raise ValueError("Feed status does not allow this action")
        return self._update(feed, {"status": status})

    def _update(self, feed: FeedSubscription, changes: dict[str, object]) -> FeedSubscription:
        updated = FeedSubscription.model_validate(
            feed.model_dump() | changes | {"version": feed.version + 1}
        )
        self.store._commit((updated,))
        return updated

    def remove(self, identity: str) -> FeedSubscription:
        feed = self.visible_feed(identity)
        deleted = feed.model_copy(update={"deleted": True, "version": feed.version + 1})
        self.store._commit(
            (
                deleted,
                *(prune(item) for item in self.store.feed_items(feed_ids=(identity,))),
            )
        )
        return deleted

    def mark_read(self, items: list[FeedItem]) -> int:
        changed = tuple(
            item.model_copy(update={"read": True, "version": item.version + 1})
            for item in items
            if not item.read
        )
        if changed:
            self.store._commit(changed)
        return len(changed)

    async def poll(self, identity: str) -> FeedSubscription:
        feed = self.visible_feed(identity)
        if not self.settings().enabled or feed.status != "active":
            raise ValueError("Only active feeds can poll when feed polling is enabled")
        if task := self.polls.get(identity):
            return await asyncio.shield(task)
        task = asyncio.create_task(self._poll(feed))
        self.polls[identity] = task
        return await asyncio.shield(task)

    async def _poll(self, feed: FeedSubscription) -> FeedSubscription:
        try:
            return await self._fetch_and_store(feed)
        finally:
            self.polls.pop(feed.id)

    async def _fetch_and_store(self, feed: FeedSubscription) -> FeedSubscription:
        details: dict[str, object] = {
            "operation": "feed.poll",
            "feed_id": feed.id,
            "fetched_url": feed.url,
            "http_status": None,
            "stored_items": 0,
            "duplicates": 0,
            "failure_reason": None,
        }
        failure = None
        items: list[FeedItem] = []
        title = ""
        try:
            async with asyncio.timeout(FETCH_TIMEOUT):
                body, url = await fetch(
                    feed.url,
                    self.resolver,
                    self.transport,
                    details,
                    lambda: self._poll_current(feed),
                )
                title, items = parse(body, url, feed.id, self.clock())
        except TimeoutError:
            failure = "Feed request timed out"
        except httpx2.HTTPError, OSError:
            failure = "Feed network request or DNS resolution failed"
        except ValueError as error:
            failure = str(error)[:256]
        current = self.store.feed(feed.id)
        if current != feed or not self.settings().enabled:
            return current
        now = self.clock()
        settings = self.settings()
        changes: dict[str, object] = {"version": feed.version + 1}
        added: list[FeedItem] = []
        excess: list[FeedItem] = []
        if failure:
            failures = feed.consecutive_failures + 1
            changes.update(consecutive_failures=failures, last_failure_reason=failure)
            if failures >= settings.max_consecutive_failures:
                changes["status"] = "paused"
            details["failure_reason"] = failure
        else:
            added, duplicates = self._new_items(feed, items)
            details.update(stored_items=len(added), duplicates=duplicates)
            changes.update(
                title=feed.title or title,
                last_fetched_at=now,
                consecutive_failures=0,
                last_failure_reason=None,
            )
            if added:
                changes["last_item_at"] = now
            visible = self.store.feed_items(feed_ids=(feed.id,))
            ordered = sorted(
                [*visible, *added], key=lambda item: (item.fetched_at, item.id), reverse=True
            )
            excess = ordered[settings.max_items_per_feed :]
        updated = FeedSubscription.model_validate(feed.model_dump() | changes)
        self.store._commit((updated, *added, *(prune(item) for item in excess)), details=details)
        return updated

    def _poll_current(self, feed: FeedSubscription) -> bool:
        return self.settings().enabled and self.store.feed(feed.id) == feed

    def _new_items(
        self,
        feed: FeedSubscription,
        items: list[FeedItem],
    ) -> tuple[list[FeedItem], int]:
        added = []
        seen: set[str] = set()
        duplicates = 0
        include = tuple(keyword.casefold() for keyword in feed.keywords)
        exclude = tuple(keyword.casefold() for keyword in feed.exclude_keywords)
        for item in items:
            text = (item.title + " " + item.summary).casefold()
            if (include and not any(word in text for word in include)) or any(
                word in text for word in exclude
            ):
                continue
            if item.guid_hash in seen or self.store.feed_seen(feed.id, item.guid_hash):
                duplicates += 1
                continue
            seen.add(item.guid_hash)
            added.append(item)
        return added, duplicates

    async def tick(self) -> None:
        settings = self.settings()
        now = self.clock()
        cutoff = now - timedelta(days=settings.item_retention_days)
        aged = tuple(prune(item) for item in self.store.feed_items(fetched_before=cutoff))
        if aged:
            self.store._commit(aged, details={"operation": "feed.retention"})
        if not settings.enabled:
            return
        due = now - timedelta(minutes=settings.poll_interval_minutes)
        outcomes = await asyncio.gather(
            *(
                self.poll(feed.id)
                for feed in self.store.feeds()
                if feed.status == "active"
                and (feed.last_fetched_at is None or feed.last_fetched_at <= due)
            ),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                raise outcome

    async def start(self) -> None:
        if self.scheduler is None:
            self.scheduler = AsyncIOScheduler(timezone=UTC)
            self.scheduler.add_job(
                self.tick,
                "interval",
                minutes=self.settings().poll_interval_minutes,
                coalesce=True,
                max_instances=1,
            )
            self.scheduler.start()

    async def stop(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
            await asyncio.sleep(0)
        tasks = tuple(self.polls.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.polls.clear()
