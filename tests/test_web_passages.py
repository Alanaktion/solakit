import asyncio
import json

import httpx

from solakit.core.config import Profile, SecretRef
from solakit.core.schemas import Session
from solakit.core.store import Store
from solakit.tools import Registry, ToolContext
from solakit.tools._passages import chunks, rank
from solakit.tools._remote import RemoteTools

DOC = (
    "# Guide\n\nInstall the package with pip.\n\n"
    + "Filler paragraph about nothing in particular. " * 30
    + "\n\n## Troubleshooting\n\nIf the service fails, restart the daemon and "
    "check the port configuration.\n\n" + "More filler text for the end. " * 30
)


def test_chunks_cover_text_with_offsets():
    pieces = chunks(DOC, 200)
    assert pieces[0][0] == 0
    for offset, text in pieces:
        assert DOC[offset:].lstrip().startswith(text[:20])


def test_rank_is_deterministic_and_relevant():
    hits = rank(DOC, "how do I restart the daemon?")
    assert "restart the daemon" in hits[0]["text"]
    assert hits == rank(DOC, "how do I restart the daemon?")
    assert rank(DOC, "") == [] and rank("", "x y") == []


def test_get_page_query_and_search_extracts(config, monkeypatch):
    config.enabled_tools = ["web"]
    config.profiles["interactive"] = Profile(tools=["web.*"])
    config.tool_apis.brave_api_key = SecretRef(env="BRAVE")
    config.tool_apis.cloudflare_api_token = SecretRef(env="CF")
    config.tool_apis.cloudflare_account_id = "acct"
    monkeypatch.setenv("BRAVE", "k")
    monkeypatch.setenv("CF", "k")
    monkeypatch.setattr(RemoteTools, "public_url", staticmethod(lambda url: "1.1.1.1"))

    def handler(request):
        if "brave" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {"title": "Guide", "url": "https://a.example/g"},
                            {"title": "Other", "url": "https://b.example/o"},
                        ]
                    }
                },
            )
        return httpx.Response(200, json={"success": True, "result": DOC})

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("solakit.tools._read_api.httpx.AsyncClient", client)
    registry = Registry(config)
    store = Store(config.paths.data)
    store.create_session(Session(id="s"))
    ctx = ToolContext(store.session("s"), "interactive", config, store, "r")

    def call(name, **arguments):
        return asyncio.run(registry.dispatch(name, json.dumps(arguments), ctx))

    page = call("web.get_page", url="https://a.example/g", query="restart daemon")
    data = json.loads(page.content)
    assert data["passages"][0]["text"].startswith("## Troubleshooting") or (
        "restart the daemon" in data["passages"][0]["text"]
    )
    found = json.loads(call("web.search", query="restart daemon", fetch_top=1).content)
    assert "restart the daemon" in " ".join(found["results"][0]["extracts"])
    assert "extracts" not in found["results"][1]
