"""Read-only external data integrations; credentials never appear in results."""

import asyncio
import re
from urllib.parse import quote, urlsplit

import httpx
from defusedxml import ElementTree

from solakit.core.config import validate_api_token
from solakit.tools import ToolContext, ToolError


class RemoteTools:
    def __init__(self, client: httpx.AsyncClient, ctx: ToolContext):
        self.client = client
        self.ctx = ctx

    async def request(self, method: str, url: str, **kwargs):
        async with self.client.stream(
            method, url, timeout=30, follow_redirects=False, **kwargs
        ) as response:
            if response.status_code >= 300:
                raise ValueError(
                    f"Upstream request failed (HTTP {response.status_code})"
                )
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 2_000_000:
                    raise ValueError("Upstream response exceeds 2 MB")
            return httpx.Response(200, content=bytes(content), headers=response.headers)

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
            raise ValueError("Provide a query and count between 1 and 20")
        response = await self.request(
            "GET",
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": count},
            headers={"X-Subscription-Token": self.credential("brave_api_key")},
        )
        return {
            "results": [
                {k: r.get(k) for k in ("title", "url", "description")}
                for r in response.json().get("web", {}).get("results", [])
            ]
        }

    async def get_page(self, url: str) -> dict:
        await asyncio.to_thread(self.public_url, url)
        account = self.ctx.config.tool_apis.cloudflare_account_id
        if not account:
            raise ToolError("Configure tool_apis.cloudflare_account_id")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", account):
            raise ValueError("Invalid Cloudflare account ID")
        response = await self.request(
            "POST",
            f"https://api.cloudflare.com/client/v4/accounts/{account}"
            "/browser-rendering/markdown",
            headers={
                "Authorization": "Bearer " + self.credential("cloudflare_api_token")
            },
            json={"url": url},
        )
        data = response.json()
        if not data.get("success"):
            raise ValueError("Cloudflare markdown conversion failed")
        return {"url": url, "markdown": data["result"]}

    async def check_feed(self, url: str, limit: int = 20) -> dict:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        await asyncio.to_thread(self.public_url, url)
        response = await self.request("GET", url)
        root = ElementTree.fromstring(response.content)
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
                    "published": e.findtext(atom + "updated")
                    or e.findtext(atom + "published"),
                }
                for e in entries[:limit]
            ]
        elif root.tag == "rss":
            channel = root.find("channel")
            if channel is None:
                raise ValueError("RSS channel is missing")
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
            raise ValueError("Expected an RSS 2.0 or Atom feed")
        return {"title": title, "items": items}

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
        return response.json()

    @staticmethod
    def repository(owner: str, repo: str) -> str:
        if not all(
            re.fullmatch(r"[\w.-]+", x) and x not in (".", "..") for x in (owner, repo)
        ):
            raise ValueError("Invalid repository owner or name")
        return f"/repos/{owner}/{repo}"

    async def notifications(self, limit: int = 30) -> dict:
        self.credential("github_token")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return {
            "notifications": await self.github_get(
                "/notifications", {"per_page": limit}
            )
        }

    async def pr_status(self, owner: str, repo: str, n: int) -> dict:
        if n < 1:
            raise ValueError("PR number must be positive")
        base = self.repository(owner, repo)
        pr = await self.github_get(f"{base}/pulls/{n}")
        sha = quote(pr["head"]["sha"], safe="")
        return {
            "pull_request": pr,
            "status": await self.github_get(f"{base}/commits/{sha}/status"),
            "checks": await self.github_get(f"{base}/commits/{sha}/check-runs"),
        }

    async def latest_release(self, owner: str, repo: str) -> dict:
        return await self.github_get(self.repository(owner, repo) + "/releases/latest")

    async def summary(self, title: str, language: str = "en") -> dict:
        if not re.fullmatch(r"[a-z]{2,12}(?:-[a-z]{2,12})?", language):
            raise ValueError("Invalid Wikipedia language")
        response = await self.request(
            "GET",
            f"https://{language}.wikipedia.org/api/rest_v1/page/summary/"
            + quote(title, safe=""),
            headers={"User-Agent": "Solakit/0.1"},
        )
        data = response.json()
        return {
            k: data.get(k)
            for k in (
                "title",
                "extract",
                "description",
                "wikibase_item",
                "content_urls",
                "type",
            )
        }
