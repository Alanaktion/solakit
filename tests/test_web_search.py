import json
from pathlib import Path

import httpx2
import pytest
from test_tools import changed, opened

from solakit.config import EffectiveProfile, WebSearchSettings
from solakit.records import ErrorCode, ToolCall, ToolReceipt
from solakit.web_search import BRAVE_WEB_SEARCH_URL, MAX_RESPONSE_BYTES, MAX_URL_LENGTH

KEY_ENV = "TEST_SOLA_BRAVE_API_KEY"
TOKEN = "synthetic-search-credential"


def test_search_encodes_query_and_keeps_order_in_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY_ENV, TOKEN)
    long_url = "https://example.org/".ljust(MAX_URL_LENGTH, "a")
    query = 'café + "grants" & limit=9 / policy?'
    requests = []
    with opened(tmp_path, web_search_env=KEY_ENV, grants=["web.*"]) as h:

        def provider(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            assert request.method == "GET"
            assert request.url.copy_with(query=None) == httpx2.URL(BRAVE_WEB_SEARCH_URL)
            assert dict(request.url.params) == {"q": query, "count": "5", "result_filter": "web"}
            assert request.headers["X-Subscription-Token"] == TOKEN
            assert h.dispatcher.redactor.redact(TOKEN) == "<redacted>"
            return httpx2.Response(
                200,
                json={
                    "type": "search",
                    "web": {
                        "results": [
                            {
                                "title": "<b>First</b>",
                                "url": "https://first.example/source",
                                "description": f'<strong class="x>y">A &amp; B</strong> {TOKEN}',
                            },
                            None,
                            {"title": "Wrong scheme", "url": "file:///private"},
                            {"title": "Missing host", "url": "https:///missing"},
                            {"title": "Secret link", "url": f"https://example.org/{TOKEN}"},
                            {"title": "Oversized link", "url": long_url + "x"},
                            {"title": "Bad description", "url": long_url, "description": []},
                            {
                                "title": "Second",
                                "url": "http://second.example",
                                "description": None,
                            },
                            *[
                                {"title": "λ" * 161, "url": long_url, "description": "🗺" * 401}
                                for _ in range(5)
                            ],
                        ]
                    },
                },
            )

        h.dispatcher.web_search.transport = httpx2.MockTransport(provider)
        assert h.session.eligible() == {"web.search"}
        assert len(h.session.schemas()) == 1
        assert h.store.admission_profile(h.step.run_id)["web_search"] == {"api_key_env": KEY_ENV}
        assert KEY_ENV not in json.dumps(h.profile.explain())
        receipt = h.dispatch("web.search", query=query)
        assert receipt.status == "succeeded" and receipt.effect == "none"
        assert not receipt.truncated and isinstance(receipt.result, str)
        assert len(requests) == 1
        assert json.loads(receipt.result) == [
            {
                "title": "First",
                "url": "https://first.example/source",
                "snippet": "A & B <redacted>",
            },
            {"title": "Second", "url": "http://second.example", "snippet": ""},
            *[{"title": "λ" * 160, "url": long_url, "snippet": "🗺" * 400} for _ in range(3)],
        ]
        records = [record for _, record in h.store.run_records(h.step.run_id)]
        call = next(record for record in records if isinstance(record, ToolCall))
        assert call.arguments == {"query": query}
        assert next(record for record in records if isinstance(record, ToolReceipt)) == receipt
        assert all(TOKEN.encode() not in path.read_bytes() for path in h.store.log.paths())


@pytest.mark.parametrize("case", ["disabled", "ungranted", "revoked", "rotated", "old", "recheck"])
def test_search_denied_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    monkeypatch.setenv(KEY_ENV, TOKEN)
    with opened(
        tmp_path,
        web_search_env=None if case == "disabled" else KEY_ENV,
        grants=["runtime.status"] if case == "ungranted" else ["runtime.status", "web.search"],
    ) as h:
        checked = 0

        def current(admitted: EffectiveProfile) -> EffectiveProfile:
            nonlocal checked
            checked += 1
            if case == "revoked" or (case == "recheck" and checked > 1):
                return changed(h.profile, tools=frozenset({"runtime.status"}))
            if case == "rotated":
                return changed(h.profile, web_search=WebSearchSettings(api_key_env="ROTATED_KEY"))
            return h.profile

        h.dispatcher.current = current
        if case == "old":
            snapshot = h.session.admitted.model_dump(exclude={"web_search"})
            h.session.admitted = EffectiveProfile.model_validate(snapshot)
        if case != "recheck":
            assert h.session.eligible() == {"runtime.status"}

        def unexpected(request: httpx2.Request) -> httpx2.Response:
            pytest.fail(f"Unexpected outbound request to {request.url.host}")

        h.dispatcher.web_search.transport = httpx2.MockTransport(unexpected)
        receipt = h.dispatch("web.search", query="public search terms")
        assert receipt.status == "denied" and receipt.effect == "none"
        assert receipt.failure and receipt.failure.code == ErrorCode.TOOL_DENIED
        assert h.dispatcher.redactor.redact(TOKEN) == TOKEN
        assert h.dispatch("runtime.status").status == "succeeded"


@pytest.mark.parametrize(
    ("response", "expected", "code"),
    [
        (None, "credential is missing", ErrorCode.CONFIGURATION),
        ({"type": "search"}, "[]", None),
        ({"type": "search", "web": None}, "[]", None),
        ({"type": "search", "web": {"results": []}}, "[]", None),
        ({}, "Malformed", ErrorCode.TOOL_FAILED),
        ({"type": "search", "web": {"results": {}}}, "Malformed", ErrorCode.TOOL_FAILED),
        (b"not json", "Malformed", ErrorCode.TOOL_FAILED),
        (b"x" * (MAX_RESPONSE_BYTES + 1), "byte limit", ErrorCode.TOOL_FAILED),
        (302, "HTTP 302", ErrorCode.TOOL_FAILED),
        (429, "rate limit", ErrorCode.TOOL_FAILED),
        (503, "HTTP 503", ErrorCode.TOOL_FAILED),
        ("timeout", "timed out", ErrorCode.TIMEOUT),
        ("network error", "provider request failed", ErrorCode.TOOL_FAILED),
    ],
)
def test_search_empty_and_failure_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object] | bytes | int | str | None,
    expected: str,
    code: ErrorCode | None,
) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    with opened(tmp_path, web_search_env=KEY_ENV) as h:
        if response is not None:
            monkeypatch.setenv(KEY_ENV, TOKEN)
        requests = []

        def provider(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            if response == "timeout":
                raise httpx2.ReadTimeout(TOKEN)
            if response == "network error":
                raise httpx2.ConnectError(TOKEN)
            if isinstance(response, int):
                return httpx2.Response(
                    response, headers={"Location": "https://other.example/"}, content=TOKEN
                )
            if isinstance(response, bytes):
                return httpx2.Response(200, content=response)
            return httpx2.Response(200, json=response)

        h.dispatcher.web_search.transport = httpx2.MockTransport(provider)
        receipt = h.dispatch("web.search", query="public search terms", limit=1)
        assert len(requests) == (response is not None)
        assert receipt.effect == "none"
        if code is None:
            assert receipt.status == "succeeded" and receipt.result == expected
        else:
            assert receipt.status == "failed" and receipt.failure and receipt.failure.code == code
            assert expected in receipt.failure.message and TOKEN not in receipt.failure.message


def test_search_validates_arguments_before_resolving_credentials(tmp_path: Path) -> None:
    with opened(tmp_path, web_search_env=KEY_ENV) as h:
        for arguments in (
            {"query": " \n "},
            {"query": "x" * 601},
            {"query": "a " * 76},
            {"query": "test", "limit": 0},
            {"query": "test", "limit": 11},
            {"query": "test", "limit": "2"},
            {"query": "test", "offset": 1},
        ):
            receipt = h.dispatch("web.search", **arguments)
            assert receipt.failure and receipt.failure.code == ErrorCode.INVALID_INPUT
