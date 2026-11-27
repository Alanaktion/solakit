import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from test_run_loop import call, harness, reply
from test_tools import changed, opened

from solakit.config import EffectiveProfile
from solakit.records import ErrorCode, RunState, ToolReceipt, new_id
from solakit.tools.web_fetch import MAX_RESPONSE_BYTES

ACCOUNT = "0" * 32
TOKEN_ENV = "TEST_CLOUDFLARE_TOKEN"
TOKEN = "synthetic-browser-token"
SETTINGS = {"account_id": ACCOUNT, "api_token_env": TOKEN_ENV}
URL = "https://example.org/guide?lang=en#start"


def test_web_fetch_is_disclosed_and_projected_to_the_next_model_call(
    profile: EffectiveProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    markdown = "# Guide\n\nThe workshop is on Monday."

    async def scenario() -> None:
        context, requests = harness(
            changed(profile, web_fetch=SETTINGS),
            [reply("", calls=[call("web.fetch", json.dumps({"url": URL}))]), reply("Monday.")],
        )
        assert context.tools and context.store and context.runs
        context.tools.web_fetch.transport = httpx2.MockTransport(
            lambda _: httpx2.Response(200, json={"success": True, "result": markdown})
        )
        await context.start()
        try:
            session = context.store.create_session()
            run = context.runs.submit(
                session.id,
                profile=context.profile,
                request_id=new_id(),
                content=f'Read "{URL}" and tell me when the workshop is.',
            )
            assert (await context.runs.wait(run.id, timeout=3)).state == RunState.COMPLETED
            assert any(t["function"]["name"] == "web.fetch" for t in requests[0]["tools"])
            message = requests[1]["messages"][-1]
            assert (
                message["role"] == "tool" and json.loads(message["content"])["result"] == markdown
            )
        finally:
            await context.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("cap", [64, 1024])
def test_fetch_returns_markdown_with_standard_receipt_and_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cap: int
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    markdown = "# Heading\n\n" + "🗺" * 30 + f"\n{TOKEN}"
    safe = markdown.replace(TOKEN, "<redacted>")
    with opened(
        tmp_path, web_fetch=SETTINGS, grants=["web.*"], budgets={"tool_result_bytes": cap}
    ) as h:
        requests = []

        def provider(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            assert request.method == "POST"
            assert str(request.url) == (
                f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/browser-run/markdown"
            )
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            assert request.headers["Accept-Encoding"] == "identity"
            assert json.loads(request.content) == {"url": URL}
            assert h.dispatcher.redactor.redact(TOKEN) == "<redacted>"
            return httpx2.Response(200, json={"success": True, "result": markdown})

        h.dispatcher.web_fetch.transport = httpx2.MockTransport(provider)
        assert h.session.eligible() == {"web.fetch"} and len(h.session.schemas()) == 1
        assert h.store.admission_profile(h.step.run_id)["web_fetch"] == SETTINGS
        assert TOKEN_ENV not in json.dumps(h.profile.explain())
        receipt = h.dispatch("web.fetch", url=URL)
        assert len(requests) == 1
        assert receipt.status == "succeeded" and receipt.effect == "none"
        if cap == 64:
            assert receipt.truncated and receipt.artifact
            assert isinstance(receipt.result, str) and receipt.result.endswith("[truncated]")
            assert len(receipt.result.encode()) <= cap
            assert (h.profile.data_dir / receipt.artifact.path).read_text() == safe
        else:
            assert not receipt.truncated and receipt.result == safe
        assert [
            record
            for _, record in h.store.run_records(h.step.run_id)
            if isinstance(record, ToolReceipt)
        ] == [receipt]
        assert all(TOKEN.encode() not in path.read_bytes() for path in h.store.log.paths())


@pytest.mark.parametrize("case", ["disabled", "ungranted", "old", "changed_account", "recheck"])
def test_fetch_enforces_admission_and_current_grant_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    with opened(
        tmp_path,
        web_fetch=None if case == "disabled" else SETTINGS,
        grants=["runtime.status"] if case == "ungranted" else ["web.fetch", "runtime.status"],
    ) as h:
        checks = 0

        def current(admitted: EffectiveProfile) -> EffectiveProfile:
            nonlocal checks
            checks += 1
            if case == "changed_account":
                return changed(h.profile, web_fetch=SETTINGS | {"account_id": "1" * 32})
            if case == "recheck" and checks > 1:
                return changed(h.profile, granted_tools=frozenset({"runtime.status"}))
            return h.profile

        h.dispatcher.current = current
        if case == "old":
            h.session.admitted = EffectiveProfile.model_validate(
                h.session.admitted.model_dump(exclude={"web_fetch"})
            )
        if case != "recheck":
            assert h.session.eligible() == {"runtime.status"}

        def unexpected(request: httpx2.Request) -> httpx2.Response:
            pytest.fail(f"Unexpected request to {request.url.host}")

        h.dispatcher.web_fetch.transport = httpx2.MockTransport(unexpected)
        receipt = h.dispatch("web.fetch", url=URL)
        assert receipt.status == "denied" and receipt.effect == "none"
        assert receipt.failure and receipt.failure.code == ErrorCode.TOOL_DENIED
        assert h.dispatcher.redactor.redact(TOKEN) == TOKEN


@pytest.mark.parametrize(
    "url",
    [
        "https:///example.org",
        "file:///private/page",
        "https://name:password@example.org/",
        "https://localhost:8080/",
        "https://test.local./",
        "http://0x7f000001/",
        "http://[::1]/",
        "http://192.168.1.4/",
        "https://example.org/a b",
        "https://example.org/" + "x" * 2048,
    ],
)
def test_fetch_rejects_invalid_and_local_urls(tmp_path: Path, url: str) -> None:
    with opened(tmp_path, web_fetch=SETTINGS) as h:
        receipt = h.dispatch("web.fetch", url=url)
        assert receipt.failure and receipt.failure.code == ErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    ("response", "expected", "code"),
    [
        (None, "token is missing", ErrorCode.CONFIGURATION),
        (302, "HTTP 302", ErrorCode.TOOL_FAILED),
        (429, "rate limit", ErrorCode.TOOL_FAILED),
        (503, "HTTP 503", ErrorCode.TOOL_FAILED),
        ("timeout", "timed out", ErrorCode.TIMEOUT),
        (b"[" * (MAX_RESPONSE_BYTES + 1), "byte limit", ErrorCode.TOOL_FAILED),
        (b"not json", "Malformed", ErrorCode.TOOL_FAILED),
        ({"success": True, "result": []}, "Malformed", ErrorCode.TOOL_FAILED),
        ({"success": False, "errors": [TOKEN]}, "could not extract", ErrorCode.TOOL_FAILED),
        ({"success": True, "result": ""}, "", None),
    ],
)
def test_fetch_failures_and_empty_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object] | bytes | int | str | None,
    expected: str,
    code: ErrorCode | None,
) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    if response is not None:
        monkeypatch.setenv(TOKEN_ENV, TOKEN)
    with opened(tmp_path, web_fetch=SETTINGS) as h:
        requests = []

        def provider(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            if response == "timeout":
                raise httpx2.ReadTimeout(TOKEN)
            if isinstance(response, int):
                return httpx2.Response(
                    response, headers={"Location": "https://other.example/"}, content=TOKEN
                )
            if isinstance(response, bytes):
                return httpx2.Response(200, content=response)
            return httpx2.Response(200, json=response)

        h.dispatcher.web_fetch.transport = httpx2.MockTransport(provider)
        receipt = h.dispatch("web.fetch", url=URL)
        assert receipt.effect == "none" and len(requests) == (response is not None)
        if code is None:
            assert receipt.status == "succeeded" and receipt.result == expected
        else:
            assert receipt.status == "failed" and receipt.failure and receipt.failure.code == code
            assert expected in receipt.failure.message and TOKEN not in receipt.failure.message
