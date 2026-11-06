"""Read-only external data integrations; credentials never appear in results."""

import asyncio
import json
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from solakit.core.config import validate_api_token
from solakit.tools import ToolContext, ToolError


@dataclass(frozen=True)
class RemoteResponse:
    status: int
    headers: httpx.Headers
    content: bytes
    final_url: str

    def json(self):
        try:
            return json.loads(self.content)
        except (ValueError, UnicodeError):
            raise ToolError("Remote service returned invalid JSON") from None


def object_data(value):
    if not isinstance(value, dict):
        raise ToolError("Remote service returned a malformed object")
    return value


def array_data(value):
    if not isinstance(value, list):
        raise ToolError("Remote service returned a malformed list")
    return [object_data(item) for item in value]


def project(data, fields):
    """Copy only task-relevant fields and bound upstream text before encoding."""
    result = {}
    for key in fields:
        value = data.get(key)
        if isinstance(value, str) and len(value) > 256:
            result[key] = value[:256]
            result[key + "_truncated"] = True
        elif value is None or isinstance(value, (str, bool, int, float)):
            result[key] = value
        else:
            raise ToolError("Remote service returned a malformed field")
    return result


class RemoteTools:
    def __init__(self, client: httpx.AsyncClient, ctx: ToolContext):
        self.client = client
        self.ctx = ctx

    async def request(self, method: str, url: str, *, redirects=False, **kwargs):
        if redirects and method != "GET":
            raise ToolError("Redirect following supports GET only")
        headers = {"User-Agent": "Solakit/0.1", **kwargs.pop("headers", {})}
        for hop in range(6):
            async with self.client.stream(
                method,
                url,
                timeout=30,
                follow_redirects=False,
                headers=headers,
                **kwargs,
            ) as response:
                if redirects and response.status_code in (301, 302, 303, 307, 308):
                    if hop == 5:
                        raise ToolError("Feed URL redirects too many times")
                    location = response.headers.get("location")
                    if not location:
                        raise ToolError("Feed redirect is missing Location")
                    try:
                        target = urljoin(str(response.url), location)
                        await asyncio.to_thread(self.public_url, target)
                    except (ValueError, OSError, httpx.InvalidURL):
                        raise ToolError(
                            "Feed redirect target is not a public URL"
                        ) from None
                    if urlsplit(url).netloc != urlsplit(target).netloc:
                        headers = {
                            k: v
                            for k, v in headers.items()
                            if k.lower() != "authorization"
                        }
                    url = target
                    continue
                if response.status_code >= 300 and not (
                    redirects and response.status_code == 304
                ):
                    raise ToolError(
                        f"Upstream request failed (HTTP {response.status_code})"
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 2_000_000:
                        raise ToolError("Upstream response exceeds 2 MB")
                return RemoteResponse(
                    response.status_code,
                    response.headers,
                    bytes(content),
                    str(response.url),
                )
        raise ToolError("Feed URL redirects too many times")

    def credential(self, name: str) -> str:
        reference = getattr(self.ctx.config.tool_apis, name)
        if reference is None:
            raise ToolError(f"Configure tool_apis.{name} to use this tool")
        try:
            value = validate_api_token(reference.resolve())
        except (ValueError, OSError):
            raise ToolError(f"Credential unavailable: tool_apis.{name}") from None
        redactor = self.ctx.store.redactor
        redactor.values = tuple(
            sorted(set((*redactor.values, value)), key=len, reverse=True)
        )
        return value

    @staticmethod
    def public_url(url: str):
        import ipaddress
        import socket

        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("A public HTTP(S) URL is required")
        if parts.username or parts.password or parts.port not in (None, 80, 443):
            raise ValueError("URL credentials and nonstandard ports are not allowed")
        addresses = socket.getaddrinfo(parts.hostname, parts.port or 443)
        if not addresses or any(
            not ipaddress.ip_address(a[4][0]).is_global for a in addresses
        ):
            raise ValueError("URL must resolve only to public addresses")

    async def search(self, query: str, count: int = 5) -> dict:
        if not query.strip() or not 1 <= count <= 20:
            raise ToolError("Provide a query and count between 1 and 20")
        response = await self.request(
            "GET",
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": count},
            headers={"X-Subscription-Token": self.credential("brave_api_key")},
        )
        return {
            "results": [
                project(r, ("title", "url", "description"))
                for r in array_data(
                    object_data(object_data(response.json()).get("web", {})).get(
                        "results", []
                    )
                )[:count]
            ]
        }

    async def get_page(self, url: str, offset: int = 0) -> dict:
        await asyncio.to_thread(self.public_url, url)
        account = self.ctx.config.tool_apis.cloudflare_account_id
        if not account:
            raise ToolError("Configure tool_apis.cloudflare_account_id")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", account):
            raise ToolError("Invalid Cloudflare account ID")
        response = await self.request(
            "POST",
            f"https://api.cloudflare.com/client/v4/accounts/{account}"
            "/browser-rendering/markdown",
            headers={
                "Authorization": "Bearer " + self.credential("cloudflare_api_token")
            },
            json={"url": url},
        )
        data = object_data(response.json())
        if not data.get("success"):
            errors = data.get("errors", [])
            messages = (
                [
                    str(e.get("message", ""))[:500]
                    for e in errors[:5]
                    if isinstance(e, dict)
                ]
                if isinstance(errors, list)
                else []
            )
            raise ToolError(
                "Cloudflare markdown conversion failed"
                + (": " + "; ".join(messages) if messages else "")
            )
        markdown = data.get("result")
        if not isinstance(markdown, str):
            raise ToolError("Cloudflare markdown conversion returned no result")
        if offset < 0:
            raise ToolError("offset must be nonnegative")
        cap = self.ctx.config.profiles[self.ctx.profile].tool_output_max_chars
        size = min(cap, 16000, max(0, len(markdown) - offset))

        def page(length):
            return {
                "url": url,
                "markdown": markdown[offset : offset + length],
                "returned_chars": length,
                "total_chars": len(markdown),
                "truncated": offset + length < len(markdown),
            }

        low, high = 0, size
        while low < high:
            mid = (low + high + 1) // 2
            if len(json.dumps(page(mid), ensure_ascii=False)) <= cap:
                low = mid
            else:
                high = mid - 1
        result = page(low)
        if len(json.dumps(result, ensure_ascii=False)) > cap or (size and low == 0):
            raise ToolError("Output cap too small")
        return result

    async def check_feed(
        self,
        url: str,
        limit: int = 20,
        if_none_match: str | None = None,
        if_modified_since: str | None = None,
    ) -> dict:
        if not 1 <= limit <= 100:
            raise ToolError("limit must be between 1 and 100")
        await asyncio.to_thread(self.public_url, url)
        headers = {}
        if if_none_match is not None:
            headers["If-None-Match"] = if_none_match
        if if_modified_since is not None:
            headers["If-Modified-Since"] = if_modified_since
        response = await self.request("GET", url, redirects=True, headers=headers)
        metadata = {
            "final_url": response.final_url,
            "not_modified": response.status == 304,
        }
        for field, header in (("etag", "etag"), ("last_modified", "last-modified")):
            if header in response.headers:
                metadata[field] = response.headers[header]
        if response.status == 304:
            return {**metadata, "items": []}
        try:
            root = ElementTree.fromstring(response.content)
        except (DefusedXmlException, ParseError):
            raise ToolError("Expected a safe RSS 2.0 or Atom feed") from None
        atom = "{http://www.w3.org/2005/Atom}"
        if root.tag == atom + "feed":
            entries = root.findall(atom + "entry")
            title = root.findtext(atom + "title")
            items = [
                {
                    "id": e.findtext(atom + "id"),
                    "title": e.findtext(atom + "title"),
                    "url": next(
                        (
                            a.get("href")
                            for a in e.findall(atom + "link")
                            if a.get("rel", "alternate") == "alternate"
                        ),
                        None,
                    ),
                    "published": e.findtext(atom + "published"),
                    "updated": e.findtext(atom + "updated"),
                }
                for e in entries[:limit]
            ]
        elif root.tag == "rss":
            channel = root.find("channel")
            if channel is None:
                raise ToolError("RSS channel is missing")
            title = channel.findtext("title")
            items = [
                {
                    "id": e.findtext("guid"),
                    "title": e.findtext("title"),
                    "url": e.findtext("link"),
                    "published": e.findtext("pubDate"),
                }
                for e in channel.findall("item")[:limit]
            ]
        else:
            raise ToolError("Expected an RSS 2.0 or Atom feed")
        return {
            **metadata,
            **project({"title": title}, ("title",)),
            "items": [project(item, item.keys()) for item in items],
        }

    async def github_get(self, path: str, params: dict | None = None):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = (
            self.credential("github_token")
            if self.ctx.config.tool_apis.github_token
            else None
        )
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = await self.request(
            "GET", "https://api.github.com" + path, headers=headers, params=params
        )
        return response.json(), response.headers.get("x-ratelimit-remaining")

    @staticmethod
    def repository(owner: str, repo: str) -> str:
        if not all(
            re.fullmatch(r"[\w.-]+", x) and x not in (".", "..") for x in (owner, repo)
        ):
            raise ToolError("Invalid repository owner or name")
        return f"/repos/{owner}/{repo}"

    @staticmethod
    def quota(result, remaining):
        if remaining is not None:
            result["rate_remaining"] = remaining
        return result

    async def notifications(self, limit: int = 30) -> dict:
        self.credential("github_token")
        if not 1 <= limit <= 100:
            raise ToolError("limit must be between 1 and 100")
        data, remaining = await self.github_get("/notifications", {"per_page": limit})
        items = []
        for item in array_data(data)[:limit]:
            items.append(
                {
                    **project(item, ("id", "unread", "reason", "updated_at")),
                    "subject": project(
                        object_data(item.get("subject", {})), ("title", "type", "url")
                    ),
                    "repository": project(
                        object_data(item.get("repository", {})), ("full_name",)
                    ),
                }
            )
        return self.quota({"notifications": items}, remaining)

    async def pr_status(self, owner: str, repo: str, n: int) -> dict:
        if n < 1:
            raise ToolError("PR number must be positive")
        base = self.repository(owner, repo)
        pr, _ = await self.github_get(f"{base}/pulls/{n}")
        pr = object_data(pr)
        head = object_data(pr.get("head", {}))
        sha = head.get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[a-zA-Z0-9]{1,64}", sha):
            raise ToolError("GitHub pull request returned no valid head SHA")
        sha = quote(sha, safe="")
        status, _ = await self.github_get(
            f"{base}/commits/{sha}/status", {"per_page": 100}
        )
        checks, remaining = await self.github_get(
            f"{base}/commits/{sha}/check-runs", {"per_page": 100}
        )
        status, checks = object_data(status), object_data(checks)
        runs = [
            project(r, ("name", "status", "conclusion", "html_url"))
            for r in array_data(checks.get("check_runs", []))[:100]
        ]
        summary = Counter((r.get("conclusion") or "pending") for r in runs)
        result = {
            **project(
                pr,
                (
                    "number",
                    "title",
                    "state",
                    "draft",
                    "merged",
                    "html_url",
                    "created_at",
                    "updated_at",
                ),
            ),
            "author": project(object_data(pr.get("user", {})), ("login",))["login"],
            "head": project(head, ("ref", "sha")),
            "base": project(object_data(pr.get("base", {})), ("ref",)),
            "status": {
                **project(status, ("state", "total_count")),
                "contexts": [
                    project(c, ("context", "state"))
                    for c in array_data(status.get("statuses", []))[:100]
                ],
            },
            "checks": {**project(checks, ("total_count",)), "runs": runs},
            "checks_summary": dict(summary),
        }
        return self.quota(result, remaining)

    async def latest_release(self, owner: str, repo: str) -> dict:
        data, remaining = await self.github_get(
            self.repository(owner, repo) + "/releases/latest"
        )
        data = object_data(data)
        body = data.get("body") or ""
        if not isinstance(body, str):
            raise ToolError("GitHub release returned an invalid body")
        return self.quota(
            {
                **project(
                    data,
                    (
                        "tag_name",
                        "name",
                        "published_at",
                        "html_url",
                        "prerelease",
                        "draft",
                    ),
                ),
                "body": body[:4000],
                "body_truncated": len(body) > 4000,
                "assets": [
                    project(a, ("name", "size", "download_count"))
                    for a in array_data(data.get("assets", []))[:100]
                ],
            },
            remaining,
        )

    async def summary(self, title: str, language: str = "en") -> dict:
        if not re.fullmatch(r"[a-z]{2,12}(?:-[a-z]{2,12})?", language):
            raise ToolError("Invalid Wikipedia language")
        response = await self.request(
            "GET",
            f"https://{language}.wikipedia.org/api/rest_v1/page/summary/"
            + quote(title, safe=""),
            headers={"User-Agent": "Solakit/0.1"},
        )
        data = object_data(response.json())
        extract = data.get("extract") or ""
        if not isinstance(extract, str):
            raise ToolError("Wikipedia returned an invalid extract")
        urls = object_data(data.get("content_urls", {}))
        return {
            **project(data, ("title", "description", "wikibase_item", "type")),
            "extract": extract[:4000],
            "extract_truncated": len(extract) > 4000,
            "content_urls": {
                kind: project(object_data(urls[kind]), ("page",))
                for kind in ("desktop", "mobile")
                if kind in urls
            },
        }
