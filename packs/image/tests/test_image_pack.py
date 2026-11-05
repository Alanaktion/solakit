import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from sola_packs import image

HTTPClient = httpx.Client


@pytest.fixture
def setup(tmp_path, monkeypatch, pack_context):
    graph = tmp_path / "workflow.json"
    graph.write_text(
        json.dumps(
            {
                "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "default"}},
                "3": {"class_type": "KSampler", "inputs": {"seed": 0, "steps": 9}},
            }
        )
    )
    config = tmp_path / "packs.toml"
    config.write_text(
        f'[images.studio]\nworkflow = "{graph}"\nprompt_node = "6"\nseed_node = "3"\n'
    )
    monkeypatch.setenv("SOLA_PACKS_CONFIG", str(config))
    registry, ctx = pack_context(
        ["sola_packs.image"],
        ["image.generate", "image.result"],
        read=[f"path:{tmp_path}"],
        capabilities=["image.generate", "image:studio:generate", "image:studio:read"],
    )
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                200, json={"prompt_id": "prompt-1", "node_errors": {}}
            )
        return httpx.Response(
            200,
            json={
                "prompt-1": {
                    "status": {"status_str": "success", "completed": True},
                    "outputs": {
                        "9": {
                            "images": [
                                {
                                    "filename": "moon.png",
                                    "subfolder": "",
                                    "type": "output",
                                }
                            ]
                        }
                    },
                }
            },
        )

    client = httpx.Client
    monkeypatch.setattr(
        image.httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return registry, ctx, calls, graph


def invoke(setup, name="image.generate", **kwargs):
    registry, ctx, _, _ = setup
    args = {"workflow": "studio", "operation_id": "picture-1"}
    if name == "image.generate":
        args.update(prompt="A moonlit garden", seed=42)
    args.update(kwargs)
    return asyncio.run(registry.dispatch(name, json.dumps(args), ctx))


def test_generate_preserves_workflow_inputs_and_idempotent_receipt(setup):
    _, _, calls, graph = setup
    original = graph.read_bytes()
    reply = invoke(setup)
    assert not reply.error and json.loads(reply.content)["status"] == "submitted"
    submitted = json.loads(calls[0].content)["prompt"]
    assert submitted["6"]["inputs"]["text"] == "A moonlit garden"
    assert submitted["3"]["inputs"] == {"seed": 42, "steps": 9}
    assert graph.read_bytes() == original
    assert invoke(setup).content == reply.content and len(calls) == 1
    assert invoke(setup, seed=43).error and len(calls) == 1
    result = invoke(setup, name="image.result")
    assert not result.error and '"filename": "moon.png"' in result.content
    assert result.artifacts == [] and len(calls) == 2


def test_uncertain_submission_never_replays(setup, monkeypatch):
    calls = []

    def broken(*args):
        calls.append(args)
        raise httpx.ReadTimeout("private server detail")

    monkeypatch.setattr(image, "request", broken)
    assert "uncertain" in invoke(setup).content
    assert "uncertain" in invoke(setup).content
    assert "uncertain" in invoke(setup, name="image.result").content
    assert len(calls) == 1


def test_grants_and_workflow_scope(setup):
    _, ctx, calls, _ = setup
    ctx.config.profiles[ctx.profile].capabilities.remove("image:studio:generate")
    assert "Missing capability" in invoke(setup).content and not calls
    ctx.config.profiles[ctx.profile].capabilities.append("image:studio:generate")
    ctx.config.profiles[ctx.profile].read_scopes = []
    assert "outside profile" in invoke(setup).content and not calls


def test_torn_receipt_blocks_post_and_result(setup):
    _, ctx, calls, _ = setup
    assert not invoke(setup).error
    receipt = next((ctx.store.root / "pack-operations/image").glob("*.jsonl"))
    with receipt.open("ab") as stream:
        stream.write(b'{"torn"')
    assert invoke(setup).error
    assert invoke(setup, name="image.result").error
    assert len(calls) == 1


@pytest.mark.parametrize(
    "reply, expected",
    [
        ({}, "pending_or_missing"),
        ({"prompt-1": {"status": {"status_str": "error"}}}, "failed"),
        ({"prompt-1": {"status": {"completed": False}}}, "pending"),
    ],
)
def test_result_states_without_resubmission(setup, monkeypatch, reply, expected):
    assert not invoke(setup).error
    calls = []

    def response(cfg, auth, method, path, body=None):
        calls.append(method)
        return reply

    monkeypatch.setattr(image, "request", response)
    assert f'"status": "{expected}"' in invoke(setup, name="image.result").content
    assert calls == ["GET"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"seed": -1},
        {"prompt": ""},
        {"operation_id": "../bad"},
        {"workflow": "../studio"},
    ],
)
def test_invalid_arguments_never_submit(setup, kwargs):
    assert invoke(setup, **kwargs).error
    assert not setup[2]


def test_concurrent_and_cancelled_submission_finishes_receipt(setup, monkeypatch):
    _, ctx, _, _ = setup
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return {"prompt_id": "prompt-1"}

    monkeypatch.setattr(image, "request", slow)

    async def exercise():
        args = dict(
            ctx=ctx, workflow="studio", prompt="Moon", seed=0, operation_id="slow"
        )
        task = asyncio.create_task(image.generate(**args))
        assert await asyncio.to_thread(entered.wait, 5)
        try:
            with pytest.raises(image.ToolError, match="already in progress"):
                await image.generate(**args)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not (await image.generate(**args)).error

    asyncio.run(exercise())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status, body",
    [
        (302, b""),
        (200, b"x" * 1_000_001),
        (200, b"{}"),
        (200, b'{"prompt_id":"../escape"}'),
    ],
)
def test_bad_http_acknowledgements_are_uncertain_and_never_follow_or_replay(
    setup, monkeypatch, status, body
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, content=body, headers={"Location": "https://elsewhere.invalid"}
        )

    monkeypatch.setattr(
        image.httpx,
        "Client",
        lambda **kwargs: HTTPClient(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert "uncertain" in invoke(setup).content
    assert "uncertain" in invoke(setup).content
    assert len(calls) == 1


def test_endpoint_change_and_read_capability_cannot_bypass_receipt(setup):
    _, ctx, calls, graph = setup
    assert invoke(setup, name="image.result").error and not calls
    assert not invoke(setup).error
    ctx.config.profiles[ctx.profile].capabilities.remove("image:studio:read")
    assert invoke(setup, name="image.result").error and len(calls) == 1
    ctx.config.profiles[ctx.profile].capabilities.append("image:studio:read")
    sidecar = graph.parent / "packs.toml"
    sidecar.write_text(sidecar.read_text() + 'base_url = "http://localhost:8188"\n')
    assert "endpoint changed" in invoke(setup, name="image.result").content
    assert len(calls) == 1


def test_pack_auth_registered_before_request_and_masked(setup, monkeypatch):
    _, ctx, calls, graph = setup
    monkeypatch.setenv("COMFY_TOKEN", "private-image-auth")
    sidecar = graph.parent / "packs.toml"
    sidecar.write_text(sidecar.read_text() + 'token = { env = "COMFY_TOKEN" }\n')
    assert not invoke(setup).error
    assert calls[0].headers["Authorization"] == "Bearer private-image-auth"
    assert ctx.store.redactor.redact("private-image-auth") == "[redacted]"


def test_deadline_checks_small_chunks_without_waiting_for_a_full_buffer(monkeypatch):
    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            yield b"{"
            yield b"}"

    ticks = iter([0, 0, 16])
    monkeypatch.setattr(image, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(
        image.httpx,
        "Client",
        lambda **kwargs: HTTPClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=Trickle())
            ),
            **kwargs,
        ),
    )
    cfg = image.Image(workflow="/tmp/workflow.json", prompt_node="6", seed_node="3")
    with pytest.raises(TimeoutError, match="deadline"):
        image.request(cfg, {}, "GET", "/history/prompt-1")
