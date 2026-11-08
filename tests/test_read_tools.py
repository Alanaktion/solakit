import asyncio
import json

import httpx
import pytest
from test_tools import context

from solakit.core.config import SecretRef
from solakit.tools import Registry, _read_api
from solakit.tools._arithmetic_calendar import add_days, convert, evaluate, weekday
from solakit.tools._remote import RemoteTools


def test_decimal_arithmetic():
    assert evaluate("0.1 + 0.2")["result"] == "0.3"
    assert evaluate("(123456789 * 1000) - 9")["result"] == "123456788991"
    assert evaluate("-7 // 3")["result"] == "-3"
    assert evaluate("-7 % 3")["result"] == "2"
    assert evaluate("2 ** -3")["result"] == "0.125"


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os')",
        "(1).__class__",
        "[1]",
        "True",
        "x + 1",
        "2 ** 1001",
        "2 ** 0.5",
        "1 / 0",
        "1e999999",
        "1 << 2",
        "1+" * 1001 + "1",
    ],
)
def test_rejected_arithmetic(expression):
    with pytest.raises((ValueError, ArithmeticError, SyntaxError)):
        evaluate(expression)


def test_calendar():
    assert add_days("2024-05-29", -90)["date"] == "2024-02-29"
    assert weekday("2024-02-29") == {"weekday": "Thursday", "iso_weekday": 4}
    assert (
        convert("2024-01-01T00:00:00+00:00", "America/Denver")["datetime"]
        == "2023-12-31T17:00:00-07:00"
    )
    with pytest.raises(ValueError, match="does not exist"):
        convert("2024-03-10T02:30:00", "UTC", "America/Denver")
    with pytest.raises(ValueError, match="Ambiguous"):
        convert("2024-11-03T01:30:00", "UTC", "America/Denver")
    assert (
        convert("2024-11-03T01:30:00", "UTC", "America/Denver", 1)["datetime"]
        == "2024-11-03T08:30:00+00:00"
    )


def run_with(config, monkeypatch, handler, operation):
    config.enabled_tools = ["web", "feeds", "github", "wiki", "datetime", "math"]
    config.profiles["interactive"].tools = ["*"]
    config.tool_apis.brave_api_key = SecretRef(env="BRAVE_API_KEY")
    config.tool_apis.cloudflare_account_id = "account"
    config.tool_apis.cloudflare_api_token = SecretRef(env="CLOUDFLARE_API_TOKEN")
    config.tool_apis.github_token = SecretRef(env="GITHUB_TOKEN")
    registry = Registry(config)
    ctx = context(config)
    monkeypatch.setattr(
        _read_api,
        "client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    class Calls:
        async def call(self, name, **arguments):
            result = await registry.dispatch(name, json.dumps(arguments), ctx)
            if result.error:
                raise ValueError(result.content)
            return json.loads(result.content)

    asyncio.run(operation(Calls()))


def test_registry_metadata_and_grants(config):
    config.enabled_tools = ["web", "feeds", "github", "wiki", "datetime", "math"]
    registry = Registry(config)
    assert len(registry.tools) == 11
    for name, item in registry.tools.items():
        assert item.manifest.summary and item.manifest.keywords
        assert item.schema()["function"]["parameters"]["type"] == "object"
    assert registry.schemas("interactive") == []
    config.profiles["interactive"].tools = ["*"]
    assert len(registry.schemas("interactive")) == 11
    ctx = context(config)

    async def exercise():
        result = await registry.dispatch("math.eval", '{"expression":"0.1+0.2"}', ctx)
        assert not result.error
        assert json.loads(result.content)["result"] == "0.3"
        for arguments in ('{"expression":"1", "extra":1}', '{"expression":true}'):
            assert (await registry.dispatch("math.eval", arguments, ctx)).error
        assert (
            await registry.dispatch("math.eval", '{"expression":"__import__(1)"}', ctx)
        ).error
        assert (
            await registry.dispatch(
                "datetime.add_days", '{"value":"2024-01-01", "days":true}', ctx
            )
        ).error
        config.profiles["interactive"].tools = []
        assert (await registry.dispatch("math.eval", '{"expression":"1"}', ctx)).error

    asyncio.run(exercise())


def test_brave_and_cloudflare(config, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-brave")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-cloudflare")
    monkeypatch.setattr(
        RemoteTools, "public_url", staticmethod(lambda url: "93.184.216.34")
    )

    def handler(request):
        if request.url.host == "api.search.brave.com":
            assert request.headers["X-Subscription-Token"] == "test-brave"
            assert request.url.params["q"] == "python"
            return httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "title": "Python",
                                "url": "https://python.org",
                                "description": "Code",
                            }
                        ]
                    }
                },
            )
        assert request.method == "POST"
        assert request.url.path.endswith("/browser-rendering/markdown")
        assert json.loads(request.content) == {"url": "https://example.com"}
        return httpx.Response(200, json={"success": True, "result": "# Example"})

    async def operation(registry):
        assert (await registry.call("web.search", query="python"))["results"][0][
            "title"
        ] == "Python"
        assert (await registry.call("web.get_page", url="https://example.com"))[
            "markdown"
        ] == "# Example"

    run_with(config, monkeypatch, handler, operation)


@pytest.mark.parametrize(
    "xml",
    [
        "<rss><channel><title>News</title><item><title>One</title><guid>1</guid>"
        "<link>https://example.com/1</link></item></channel></rss>",
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title>'
        '<entry><title>One</title><id>1</id><link href="https://example.com/1"/>'
        "</entry></feed>",
    ],
)
def test_feeds(config, monkeypatch, xml):
    monkeypatch.setattr(
        RemoteTools, "public_url", staticmethod(lambda url: "93.184.216.34")
    )

    async def operation(registry):
        data = await registry.call("feeds.check", url="https://example.com/feed")
        assert data["title"] == "News"
        assert data["items"][0]["url"] == "https://example.com/1"

    run_with(config, monkeypatch, lambda r: httpx.Response(200, text=xml), operation)


def test_github_and_wikipedia(config, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-github")
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.method == "GET"
        if request.url.host == "api.github.com":
            assert request.headers["Authorization"] == "Bearer test-github"
            if "/pulls/" in request.url.path:
                return httpx.Response(200, json={"head": {"sha": "abc"}})
            if request.url.path == "/notifications":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "1",
                            "unread": True,
                            "subject": {"title": "Issue", "type": "Issue"},
                            "repository": {"full_name": "a/b"},
                        }
                    ],
                )
            if request.url.path.endswith("/check-runs"):
                return httpx.Response(
                    200,
                    json={
                        "total_count": 1,
                        "check_runs": [
                            {
                                "name": "CI",
                                "status": "completed",
                                "conclusion": "success",
                            }
                        ],
                    },
                )
            if request.url.path.endswith("/status"):
                return httpx.Response(
                    200,
                    json={
                        "state": "success",
                        "total_count": 1,
                        "statuses": [{"context": "CI", "state": "success"}],
                    },
                )
            return httpx.Response(
                200, json={"tag_name": "v1", "body": "Release notes", "assets": []}
            )
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"title": "Python", "wikibase_item": "Q28865"})

    async def operation(registry):
        await registry.call("github.notifications")
        await registry.call("github.pr_status", owner="a", repo="b", n=1)
        await registry.call("github.latest_release", owner="a", repo="b")
        assert (await registry.call("wiki.summary", title="Python"))[
            "wikibase_item"
        ] == "Q28865"

    run_with(config, monkeypatch, handler, operation)
    assert "/repos/a/b/commits/abc/status" in paths
    assert "/repos/a/b/commits/abc/check-runs" in paths


@pytest.mark.parametrize("status", [302, 401, 403, 404, 429, 500])
def test_upstream_errors(config, monkeypatch, status):
    monkeypatch.setenv("BRAVE_API_KEY", "secret")

    async def operation(registry):
        with pytest.raises(ValueError, match=f"HTTP {status}") as error:
            await registry.call("web.search", query="test")
        assert "secret" not in str(error.value)

    run_with(
        config, monkeypatch, lambda r: httpx.Response(status, text="secret"), operation
    )


def test_missing_credentials(config, monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    async def operation(registry):
        with pytest.raises(ValueError, match="brave_api_key"):
            await registry.call("web.search", query="test")

    run_with(
        config,
        monkeypatch,
        lambda r: pytest.fail("Network should not be called"),
        operation,
    )


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "http://127.0.0.1", "http://[::1]"]
)
def test_private_urls_rejected(url):
    with pytest.raises(ValueError):
        RemoteTools.public_url(url)


def test_response_size_limit(config, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test")

    async def operation(registry):
        with pytest.raises(ValueError, match="exceeds 2 MB"):
            await registry.call("web.search", query="test")

    run_with(
        config,
        monkeypatch,
        lambda r: httpx.Response(200, content=b"x" * 2_000_001),
        operation,
    )


def test_xml_entities_rejected(config, monkeypatch):
    monkeypatch.setattr(
        RemoteTools, "public_url", staticmethod(lambda url: "93.184.216.34")
    )
    xml = '<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel>&x;</channel></rss>'

    async def operation(registry):
        with pytest.raises(ValueError):
            await registry.call("feeds.check", url="https://example.com/feed")

    run_with(config, monkeypatch, lambda r: httpx.Response(200, text=xml), operation)


def test_cloudflare_failure(config, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test")
    monkeypatch.setattr(
        RemoteTools, "public_url", staticmethod(lambda url: "93.184.216.34")
    )

    async def operation(registry):
        with pytest.raises(ValueError, match="conversion failed"):
            await registry.call("web.get_page", url="https://example.com")

    run_with(
        config,
        monkeypatch,
        lambda r: httpx.Response(200, json={"success": False}),
        operation,
    )


def test_new_tools_deferred_index(config):
    config.enabled_tools = ["web", "feeds", "github", "wiki", "datetime", "math"]
    profile = config.profiles["interactive"]
    profile.tools = ["*"]
    profile.tool_disclosure = "deferred"
    registry = Registry(config)
    view = registry.view("interactive")
    for name in registry.tools:
        assert f"{name} — {registry.summary(name)}" in view.index_text
    assert sum(" — " in line for line in view.index_text.splitlines()) == 11
    assert [s["function"]["name"] for s in view.schemas()] == [
        "tools.search",
        "tools.load",
    ]


def test_secret_reference_registration(config, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "private-brave-token")
    config.tool_apis.brave_api_key = SecretRef(env="BRAVE_API_KEY")
    ctx = context(config)

    async def exercise():
        async with httpx.AsyncClient() as client:
            remote = RemoteTools(client, ctx)
            assert remote.credential("brave_api_key") == "private-brave-token"
            assert ctx.store.redactor.redact("private-brave-token") == "[redacted]"

    asyncio.run(exercise())


def test_feed_connection_pins_address_and_preserves_host(config, monkeypatch):
    resolutions = []

    def resolve(url):
        resolutions.append(url)
        return "93.184.216.34"

    monkeypatch.setattr(RemoteTools, "public_url", staticmethod(resolve))

    def handler(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        assert request.headers["connection"] == "close"
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/feed"})
        return httpx.Response(
            200, text="<rss><channel><title>News</title></channel></rss>"
        )

    async def operation(registry):
        result = await registry.call("feeds.check", url="https://example.com/start")
        assert result["final_url"] == "https://example.com/feed"

    run_with(config, monkeypatch, handler, operation)
    assert resolutions == ["https://example.com/start", "https://example.com/feed"]
