"""Dispatch-level contracts for usable payloads, failures and discovery."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from test_tools import context

from solakit.core.config import Config, Profile, SecretRef
from solakit.tools import Registry, _read_api
from solakit.tools._remote import RemoteTools
from solakit.tools.disclosure import meta_schemas

MODULES = [
    "datetime",
    "delegate",
    "feeds",
    "files",
    "github",
    "jobs",
    "math",
    "memory",
    "messages",
    "repo_git",
    "runtime",
    "sessions",
    "shell",
    "web",
    "wiki",
    *[
        f"sola_packs.{p}"
        for p in ("journal", "letters", "email", "printer", "image", "calendar")
    ],
]


@pytest.fixture
def dispatch(config, monkeypatch):
    config.enabled_tools = MODULES
    config.profiles["interactive"] = Profile(
        tools=["*"],
        write="workspace",
        effects=["push", "send", "print", "shell", "operate"],
        tool_disclosure="deferred",
    )
    for name in ("github_token", "brave_api_key", "cloudflare_api_token"):
        monkeypatch.setenv(name.upper(), "private-test-token")
        setattr(config.tool_apis, name, SecretRef(env=name.upper()))
    config.tool_apis.cloudflare_account_id = "test"
    registry = Registry(config)
    ctx = context(config)
    ctx.tool_view = registry.view(ctx.profile)
    ctx.active_tools = set(ctx.tool_view.effective)
    ctx.activate_tools = ctx.active_tools.update
    ctx.configured_tool_names = frozenset(registry.tools)

    async def call(name, **arguments):
        return await registry.dispatch(name, json.dumps(arguments), ctx)

    return call, ctx, registry


def transport(monkeypatch, handler):
    monkeypatch.setattr(
        _read_api,
        "client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize(
    "arguments,message",
    [
        (
            {
                "value": "2024-11-03T01:30:00",
                "from_zone": "America/Denver",
                "to_zone": "UTC",
            },
            "Ambiguous local time; specify fold=0 or fold=1",
        ),
        (
            {
                "value": "2024-03-10T02:30:00",
                "from_zone": "America/Denver",
                "to_zone": "UTC",
            },
            "Local time does not exist due to DST",
        ),
        (
            {"value": "2024-01-01T00:00:00", "to_zone": "UTC"},
            "Naive timestamps require from_zone",
        ),
        (
            {"value": "2024-01-01T00:00:00+00:00", "to_zone": "Unknown/Zone"},
            "Unknown time zone: Unknown/Zone",
        ),
        (
            {"value": "bad date", "to_zone": "UTC"},
            "Invalid isoformat string: 'bad date'",
        ),
    ],
)
def test_datetime_errors_reach_model_without_tracebacks(dispatch, arguments, message):
    call, ctx, _ = dispatch
    result = asyncio.run(call("datetime.convert", **arguments))
    assert result.error and result.content == message
    logs = ctx.store._read(ctx.store.root / "runs/r.tools.jsonl")
    assert logs[-1]["traceback"] is None


def test_invalid_proxy_url_is_actionable(dispatch, monkeypatch):
    call, ctx, _ = dispatch

    def broken():
        raise httpx.InvalidURL("private proxy value")

    monkeypatch.setattr(_read_api, "client", broken)
    result = asyncio.run(call("web.search", query="test"))
    assert (
        result.content
        == "Remote service unavailable: invalid URL or proxy configuration"
    )
    assert (
        ctx.store._read(ctx.store.root / "runs/r.tools.jsonl")[-1]["traceback"] is None
    )


def test_large_github_projections_survive_dispatch(dispatch, monkeypatch):
    call, _, _ = dispatch

    def handler(request):
        assert request.headers["User-Agent"] == "Solakit/0.1"
        path = request.url.path
        common = {"headers": {"X-RateLimit-Remaining": "4999"}}
        if "/pulls/" in path:
            return httpx.Response(
                200,
                json={
                    "number": 1982,
                    "title": "Real pull request",
                    "state": "open",
                    "draft": False,
                    "merged": False,
                    "user": {"login": "author", "ignored": "x" * 30000},
                    "body": "x" * 30000,
                    "head": {"ref": "topic", "sha": "abc123"},
                    "base": {"ref": "main"},
                    "html_url": "https://github.com/a/b/pull/1982",
                },
                **common,
            )
        if path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "total_count": 12,
                    "check_runs": [
                        {
                            "name": f"CI {i}",
                            "status": "completed" if i < 11 else "in_progress",
                            "conclusion": "success"
                            if i < 10
                            else "failure"
                            if i == 10
                            else None,
                            "output": {"text": "x" * 10000},
                        }
                        for i in range(12)
                    ],
                },
                **common,
            )
        if path.endswith("/status"):
            return httpx.Response(
                200,
                json={
                    "state": "pending",
                    "total_count": 3,
                    "statuses": [
                        {
                            "context": "CI",
                            "state": "success",
                            "creator": {"large": "x" * 30000},
                        }
                    ],
                },
                **common,
            )
        if path == "/notifications":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": str(i),
                        "unread": True,
                        "reason": "subscribed",
                        "updated_at": "2026-11-06",
                        "subject": {
                            "title": "Issue",
                            "type": "Issue",
                            "url": "https://api.github.com/issue/1",
                            "latest_comment_url": "discard",
                        },
                        "repository": {
                            "full_name": "a/b",
                            "owner": {"large": "x" * 10000},
                        },
                    }
                    for i in range(30)
                ],
                **common,
            )
        return httpx.Response(
            200,
            json={
                "tag_name": "v1",
                "name": "Release",
                "body": "notes" * 10000,
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": "package.zip",
                        "size": 123,
                        "download_count": 4,
                        "uploader": {"large": "x" * 10000},
                    }
                ],
            },
            **common,
        )

    transport(monkeypatch, handler)

    async def exercise():
        for name, args in [
            ("github.pr_status", {"owner": "a", "repo": "b", "n": 1982}),
            ("github.notifications", {}),
            ("github.latest_release", {"owner": "a", "repo": "b"}),
        ]:
            result = await call(name, **args)
            assert not result.error, result.content
            assert result.content_complete and len(result.content) <= 12000
            data = json.loads(result.content)
            assert data["rate_remaining"] == "4999"
            if name.endswith("pr_status"):
                assert data["number"] == 1982
                assert data["head"]["sha"] == "abc123"
                assert data["checks_summary"] == {
                    "success": 10,
                    "failure": 1,
                    "pending": 1,
                }
                assert len(data["checks"]["runs"]) == 12
                assert "output" not in data["checks"]["runs"][0]
            elif name.endswith("notifications"):
                assert len(data["notifications"]) == 30
                assert data["notifications"][0]["repository"] == {"full_name": "a/b"}
                assert "latest_comment_url" not in data["notifications"][0]["subject"]
            else:
                assert data["body_truncated"] and len(data["body"]) == 4000
                assert data["assets"] == [
                    {"name": "package.zip", "size": 123, "download_count": 4}
                ]

    asyncio.run(exercise())


@pytest.mark.parametrize("cap", [32, 500, 1000, 20000])
def test_structured_outputs_do_not_break_at_small_caps(dispatch, monkeypatch, cap):
    call, ctx, _ = dispatch
    ctx.config.profiles[ctx.profile].tool_output_max_chars = cap
    transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"tag_name": "v1", "body": "long notes" * 10000, "assets": []}
        ),
    )
    result = asyncio.run(call("github.latest_release", owner="a", repo="b"))
    assert len(result.content) <= cap and result.content_complete
    if result.error:
        assert result.content == "Output cap too small"
    else:
        assert json.loads(result.content)["body_truncated"]


@pytest.mark.parametrize("markdown", ["x" * 40000, '\n"\\' * 13000])
def test_page_offsets_reassemble_source(dispatch, monkeypatch, markdown):
    call, _, _ = dispatch
    monkeypatch.setattr(
        RemoteTools, "public_url", staticmethod(lambda url: "93.184.216.34")
    )
    transport(
        monkeypatch,
        lambda r: httpx.Response(200, json={"success": True, "result": markdown}),
    )

    async def exercise():
        chunks = []
        offset = 0
        while offset < len(markdown):
            result = await call(
                "web.get_page", url="https://example.com", offset=offset
            )
            assert not result.error and result.content_complete, result.content
            data = json.loads(result.content)
            assert data["total_chars"] == len(markdown)
            assert data["returned_chars"] == len(data["markdown"])
            assert 0 < data["returned_chars"] <= 16000
            chunks.append(data["markdown"])
            offset += data["returned_chars"]
            assert data["truncated"] == (offset < len(markdown))
        assert "".join(chunks) == markdown
        if markdown == "x" * 40000:
            assert len(chunks) == 3

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "payload,message",
    [
        (
            {"success": False, "errors": [{"message": "Conversion quota exceeded"}]},
            "Conversion quota exceeded",
        ),
        ({"success": True}, "Cloudflare markdown conversion returned no result"),
    ],
)
def test_cloudflare_errors_and_malformed_results(
    dispatch, monkeypatch, payload, message
):
    call, ctx, _ = dispatch
    monkeypatch.setattr(
        RemoteTools, "public_url", staticmethod(lambda url: "93.184.216.34")
    )
    transport(monkeypatch, lambda r: httpx.Response(200, json=payload))
    result = asyncio.run(call("web.get_page", url="https://example.com"))
    assert result.error and message in result.content
    assert (
        ctx.store._read(ctx.store.root / "runs/r.tools.jsonl")[-1]["traceback"] is None
    )


def test_github_missing_head_is_expected_error(dispatch, monkeypatch):
    call, ctx, _ = dispatch
    transport(monkeypatch, lambda r: httpx.Response(200, json={"head": None}))
    result = asyncio.run(call("github.pr_status", owner="a", repo="b", n=1))
    assert result.error and "malformed" in result.content
    assert (
        ctx.store._read(ctx.store.root / "runs/r.tools.jsonl")[-1]["traceback"] is None
    )


@pytest.mark.parametrize(
    "query,required,forbidden",
    [
        (
            "unread mail in the inbox",
            {"email.search"},
            {"datetime.weekday", "jobs.create"},
        ),
        (
            "what did I say about the migration",
            {"sessions.search"},
            {"github.latest_release"},
        ),
        ("schedule a job", {"jobs.create"}, set()),
        ("calculate", {"math.eval"}, set()),
        ("read a webpage", {"web.get_page"}, set()),
        ("print a note", {"printer.print_text"}, set()),
        ("search", {"web.search", "memory.search", "sessions.search"}, set()),
        ("quuxnonexistent", set(), set()),
    ],
)
def test_discovery_golden_queries(dispatch, query, required, forbidden):
    call, ctx, _ = dispatch
    ctx.active_tools.clear()
    result = asyncio.run(call("tools.search", query=query))
    assert not result.error
    names = {row["name"] for row in json.loads(result.content)["matches"]}
    assert required <= names
    assert not forbidden & names
    assert names == ctx.active_tools
    if query == "quuxnonexistent":
        assert not names


def test_schema_metadata_contract_and_snapshot():
    registry = Registry(Config(enabled_tools=MODULES))
    schemas = [r.schema() for _, r in sorted(registry.tools.items())]
    parameters = []
    for name, item in registry.tools.items():
        assert item.manifest.summary, name
        assert item.manifest.keywords, name
        for key, value in item.schema()["function"]["parameters"]["properties"].items():
            assert value.get("description"), (name, key)
            parameters.append(value)
    for schema in meta_schemas():
        assert all(
            p.get("description")
            for p in schema["function"]["parameters"]["properties"].values()
        )
    measured = {
        "tools": len(schemas),
        "parameters": len(parameters),
        "described_parameters": sum(bool(p.get("description")) for p in parameters),
        "schema_bytes": len(json.dumps(schemas).encode()),
    }
    expected = json.loads(
        (Path(__file__).parent / "fixtures/tool-catalog-metrics.json").read_text()
    )
    assert measured == expected


def test_feed_redirects_and_conditional_metadata(dispatch, monkeypatch):
    call, _, _ = dispatch
    validated = []

    def resolve(url):
        validated.append(url)
        return "93.184.216.34"

    monkeypatch.setattr(RemoteTools, "public_url", staticmethod(resolve))
    requests = []
    xml = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title>'
        "<entry><title>Entry</title><id>one</id><published>original-date</published>"
        "<updated>revised-date</updated></entry></feed>"
    )

    def handler(request):
        requests.append(request)
        assert request.headers["User-Agent"] == "Solakit/0.1"
        assert request.headers["If-None-Match"] == '"previous"'
        assert request.headers["If-Modified-Since"] == "yesterday"
        if request.url.path == "/start":
            return httpx.Response(
                301, headers={"Location": "https://feeds.example.com/middle"}
            )
        if request.url.path == "/middle":
            return httpx.Response(302, headers={"Location": "/canonical"})
        return httpx.Response(
            200, text=xml, headers={"ETag": '"current"', "Last-Modified": "today"}
        )

    transport(monkeypatch, handler)
    result = asyncio.run(
        call(
            "feeds.check",
            url="http://example.com/start",
            if_none_match='"previous"',
            if_modified_since="yesterday",
        )
    )
    assert not result.error, result.content
    data = json.loads(result.content)
    assert data["final_url"] == "https://feeds.example.com/canonical"
    assert data["etag"] == '"current"' and data["last_modified"] == "today"
    assert not data["not_modified"]
    assert data["items"][0]["published"] == "original-date"
    assert data["items"][0]["updated"] == "revised-date"
    assert validated == [
        "http://example.com/start",
        "https://feeds.example.com/middle",
        "https://feeds.example.com/canonical",
    ]
    assert all(r.url.host == "93.184.216.34" for r in requests)
    transport(monkeypatch, lambda r: httpx.Response(304, headers={"ETag": '"current"'}))
    result = asyncio.run(
        call("feeds.check", url=data["final_url"], if_none_match=data["etag"])
    )
    assert json.loads(result.content) == {
        "final_url": data["final_url"],
        "not_modified": True,
        "etag": '"current"',
        "items": [],
    }


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/private",
        "http://[::1]/",
        "http://user:password@example.com",
        "http://example.com:8080/",
    ],
)
def test_redirect_rejects_private_or_disallowed_hop_before_request(
    dispatch, monkeypatch, target
):
    import socket

    call, _, _ = dispatch
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (
                    "127.0.0.1"
                    if host == "127.0.0.1"
                    else "::1"
                    if host == "::1"
                    else "93.184.216.34",
                    port,
                ),
            )
        ],
    )
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": target})

    transport(monkeypatch, handler)
    result = asyncio.run(call("feeds.check", url="https://example.com/feed"))
    assert result.error and result.content == "Feed redirect target is not a public URL"
    assert requests == ["https://93.184.216.34/feed"]


def test_redirect_limit_and_cross_host_auth_strip(dispatch, monkeypatch):
    call, ctx, _ = dispatch
    monkeypatch.setattr(
        RemoteTools, "public_url", staticmethod(lambda url: "93.184.216.34")
    )
    requests = []

    def loop(request):
        requests.append(request)
        return httpx.Response(301, headers={"Location": "/again"})

    transport(monkeypatch, loop)
    result = asyncio.run(call("feeds.check", url="https://example.com/feed"))
    assert result.content == "Feed URL redirects too many times"
    assert len(requests) == 6
    requests.clear()

    def success(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                301, headers={"Location": "https://other.example.com/feed"}
            )
        assert "authorization" not in request.headers
        return httpx.Response(200, text="ok")

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as client:
            response = await RemoteTools(client, ctx).request(
                "GET",
                "https://example.com/feed",
                redirects=True,
                headers={"Authorization": "Bearer private"},
            )
            assert response.status == 200 and response.content == b"ok"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "name,args,field",
    [
        ("web.search", {"query": "test", "count": 21}, "count"),
        ("feeds.check", {"url": "https://example.com", "limit": 101}, "limit"),
        ("github.notifications", {"limit": 0}, "limit"),
        ("github.pr_status", {"owner": "a", "repo": "b", "n": 0}, "n"),
        ("web.get_page", {"url": "https://example.com", "offset": -1}, "offset"),
    ],
)
def test_schema_bounds_precede_network(dispatch, monkeypatch, name, args, field):
    call, _, _ = dispatch
    monkeypatch.setattr(
        _read_api,
        "client",
        lambda: pytest.fail("invalid arguments must not create a client"),
    )
    result = asyncio.run(call(name, **args))
    assert result.error and f"Invalid tool arguments: {field}:" in result.content


def test_calendar_bad_bounds_retain_domain_error(dispatch, monkeypatch):
    from types import SimpleNamespace

    from sola_packs import calendar

    call, ctx, _ = dispatch
    monkeypatch.setattr(calendar, "resource", lambda *args: SimpleNamespace())
    result = asyncio.run(
        call("calendar.events", calendar="main", after="bad", before="bad")
    )
    assert (
        result.error
        and result.content == "Calendar bounds require ISO timestamps with a timezone"
    )
    assert (
        ctx.store._read(ctx.store.root / "runs/r.tools.jsonl")[-1]["traceback"] is None
    )


def test_discovery_requires_signal_and_keeps_exact_alias_lookup(dispatch):
    from dataclasses import replace

    call, ctx, _ = dispatch
    ctx.tool_view = replace(
        ctx.tool_view,
        entries=(
            ("math.eval", "Calculate", ("arithmetic",), "incidentalword " * 100, False),
        ),
    )
    ctx.active_tools.clear()
    result = asyncio.run(call("tools.search", query="incidentalword"))
    assert json.loads(result.content)["matches"] == []
    assert not ctx.active_tools
    result = asyncio.run(call("tools.search", query="math_eval"))
    assert json.loads(result.content)["matches"][0]["name"] == "math.eval"
