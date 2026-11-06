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
                "5": {
                    "class_type": "EmptySD3LatentImage",
                    "inputs": {"width": 512, "height": 1024, "batch_size": 1},
                },
            }
        )
    )
    config = tmp_path / "packs.toml"
    config.write_text(
        f'[images.studio]\nworkflow = "{graph}"\nprompt_node = "6"\nseed_node = "3"\n'
        'width_node = "5"\nheight_node = "5"\n'
    )
    monkeypatch.setenv("SOLA_PACKS_CONFIG", str(config))
    registry, ctx = pack_context(
        ["sola_packs.image"],
        ["image.generate", "image.result"],
        read=[f"path:{tmp_path}"],
        write=[f"path:{tmp_path / 'output'}"],
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
    ctx.config.profiles[ctx.profile].resources.remove("image:studio")
    assert "Missing resource" in invoke(setup).content and not calls
    ctx.config.profiles[ctx.profile].resources.append("image:studio")
    ctx.config.profiles[ctx.profile].read = "none"
    ctx.config.profiles[ctx.profile].workspace = []
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
    ctx.config.profiles[ctx.profile].resources.remove("image:studio")
    assert invoke(setup, name="image.result").error and len(calls) == 1
    ctx.config.profiles[ctx.profile].resources.append("image:studio")
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


def test_completed_png_download_uses_receipt_auth_and_normalizes(
    setup, monkeypatch, tmp_path
):
    import io

    from PIL import Image as Raster

    _, ctx, calls, _ = setup
    assert not invoke(setup).error
    buffer = io.BytesIO()
    Raster.new("RGB", (16, 9), "purple").save(buffer, format="JPEG")
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer fixture-image-secret"
        assert request.url.path == "/view"
        assert dict(request.url.params) == {
            "filename": "moon.png",
            "subfolder": "",
            "type": "output",
        }
        return httpx.Response(200, content=buffer.getvalue())

    # Keep history lookup deterministic while replacing the download transport.
    original_outcome = image.outcome
    cfg_path = tmp_path / "packs.toml"
    with cfg_path.open("a") as stream:
        stream.write('token = { env = "FIXTURE_IMAGE_TOKEN" }\n')
    monkeypatch.setenv("FIXTURE_IMAGE_TOKEN", "fixture-image-secret")
    outcome = original_outcome(
        image.resource(ctx, "images", "studio", image.Image, "read"), {}, "prompt-1"
    )
    monkeypatch.setattr(image, "outcome", lambda *args: outcome)
    monkeypatch.setattr(
        image.httpx,
        "Client",
        lambda **kwargs: HTTPClient(transport=httpx.MockTransport(handler), **kwargs),
    )
    result = asyncio.run(image.completed_png(ctx, "studio", "picture-1"))
    assert result.startswith(b"\x89PNG\r\n\x1a\n")
    assert Raster.open(io.BytesIO(result)).size == (16, 9)
    assert len(requests) == 1
    assert ctx.store.redactor.redact("fixture-image-secret") == "[redacted]"
    assert sum(r.method == "POST" for r in calls) == 1


@pytest.mark.parametrize("status", ["pending", "failed", "pending_or_missing"])
def test_completed_png_refuses_incomplete_and_never_resubmits(
    setup, monkeypatch, status
):
    from solakit.tools import ToolError

    _, ctx, calls, _ = setup
    assert not invoke(setup).error
    monkeypatch.setattr(image, "outcome", lambda *args: {"status": status})
    with pytest.raises(ToolError, match="not complete"):
        asyncio.run(image.completed_png(ctx, "studio", "picture-1"))
    assert len(calls) == 1


def test_completed_png_rechecks_resource_endpoint_and_selection(
    setup, monkeypatch, tmp_path
):
    from solakit.tools import ToolError

    _, ctx, _, _ = setup
    assert not invoke(setup).error
    with pytest.raises(ToolError, match="output is missing"):
        asyncio.run(image.completed_png(ctx, "studio", "picture-1", 1))
    ctx.config.profiles[ctx.profile].resources.clear()
    with pytest.raises(ToolError, match="Missing resource"):
        asyncio.run(image.completed_png(ctx, "studio", "picture-1"))
    ctx.config.profiles[ctx.profile].resources.append("image:studio")
    with (tmp_path / "packs.toml").open("a") as stream:
        stream.write('base_url = "http://other.invalid"\n')
    with pytest.raises(ToolError, match="endpoint changed"):
        asyncio.run(image.completed_png(ctx, "studio", "picture-1"))


@pytest.mark.parametrize(
    "reference",
    [
        {"filename": "../escape.png", "subfolder": "", "type": "output"},
        {"filename": "cover.png", "subfolder": "../escape", "type": "output"},
        {"filename": "cover.png", "subfolder": "", "type": "input"},
        {"filename": "cover.png", "subfolder": "/escape", "type": "output"},
    ],
)
def test_download_refuses_unsafe_output_references(reference):
    from solakit.tools import ToolError

    with pytest.raises(ToolError, match="safe ComfyUI"):
        image.download_png(SimpleNamespace(), {}, reference)


@pytest.mark.parametrize(
    "mode", ["redirect", "invalid", "oversize", "dimensions", "timeout"]
)
def test_download_refuses_redirect_invalid_large_and_slow_images(monkeypatch, mode):
    import io

    from PIL import Image as Raster

    from solakit.tools import ToolError

    requests = []

    def handler(request):
        requests.append(request)
        if mode == "redirect":
            return httpx.Response(
                302, headers={"Location": "http://other.invalid/private"}
            )
        if mode == "oversize":
            return httpx.Response(200, content=b"x" * 20_000_001)
        if mode == "dimensions":
            buffer = io.BytesIO()
            Raster.new("1", (6400, 6400)).save(buffer, format="PNG")
            return httpx.Response(200, content=buffer.getvalue())
        if mode == "timeout":
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, content=b"not an image")

    monkeypatch.setattr(
        image.httpx,
        "Client",
        lambda **kwargs: HTTPClient(transport=httpx.MockTransport(handler), **kwargs),
    )
    cfg = SimpleNamespace(base_url="http://fixture.invalid", timeout_s=1)
    with pytest.raises(ToolError, match="valid bounded"):
        image.download_png(
            cfg, {}, {"filename": "cover.png", "subfolder": "", "type": "output"}
        )
    assert len(requests) == 1


def test_dimensions_default_custom_and_receipt_binding(setup):
    _, _, calls, graph = setup
    original = graph.read_bytes()
    assert not invoke(setup).error
    assert json.loads(calls[-1].content)["prompt"]["5"]["inputs"] == {
        "width": 768,
        "height": 768,
        "batch_size": 1,
    }
    assert not invoke(setup, width=768, height=768).error
    assert len(calls) == 1
    assert invoke(setup, width=1024, height=576).error
    assert len(calls) == 1
    assert not invoke(setup, operation_id="landscape", width=1024, height=576).error
    assert json.loads(calls[-1].content)["prompt"]["5"]["inputs"] == {
        "width": 1024,
        "height": 576,
        "batch_size": 1,
    }
    assert graph.read_bytes() == original
    schema = setup[0].tools["image.generate"].schema()["function"]["parameters"]
    assert schema["properties"]["width"]["default"] == 768
    assert schema["properties"]["height"]["default"] == 768


@pytest.mark.parametrize(
    "dimensions",
    [
        {"width": 0},
        {"height": -8},
        {"width": 4097},
        {"height": 770},
        {"width": True},
        {"height": "768"},
        {"width": 768.0},
    ],
)
def test_bad_dimensions_never_submit(setup, dimensions):
    assert invoke(setup, **dimensions).error
    assert not setup[2]


def test_unmapped_workflow_compatibility_and_custom_size_refusal(setup):
    _, _, calls, graph = setup
    sidecar = graph.parent / "packs.toml"
    sidecar.write_text(
        sidecar.read_text().replace('width_node = "5"\nheight_node = "5"\n', "")
    )
    assert not invoke(setup).error
    assert json.loads(calls[-1].content)["prompt"]["5"]["inputs"]["width"] == 512
    assert "mappings" in invoke(setup, operation_id="custom", width=1024).content
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["missing", "linked", "bool", "partial", "collision"])
def test_bad_dimension_mappings_never_submit(setup, mode):
    _, _, _, graph = setup
    data = json.loads(graph.read_text())
    if mode == "missing":
        del data["5"]["inputs"]["width"]
    elif mode == "linked":
        data["5"]["inputs"]["height"] = ["other", 0]
    elif mode == "bool":
        data["5"]["inputs"]["width"] = True
    else:
        sidecar = graph.parent / "packs.toml"
        text = sidecar.read_text()
        if mode == "partial":
            text = text.replace('height_node = "5"\n', "")
        else:
            text += 'height_input = "width"\n'
        sidecar.write_text(text)
    graph.write_text(json.dumps(data))
    assert invoke(setup).error
    assert not setup[2]


def test_reference_workflow_maps_dimensions_and_is_preserved(setup):
    from pathlib import Path

    _, _, calls, graph = setup
    reference = Path(__file__).parents[1] / "workflow.json"
    graph.write_bytes(reference.read_bytes())
    sidecar = graph.parent / "packs.toml"
    sidecar.write_text(
        sidecar.read_text()
        .replace('prompt_node = "6"', 'prompt_node = "45"')
        .replace('seed_node = "3"', 'seed_node = "44"')
        .replace('width_node = "5"', 'width_node = "41"')
        .replace('height_node = "5"', 'height_node = "41"')
    )
    assert not invoke(setup, width=1024, height=576).error
    submitted = json.loads(calls[-1].content)["prompt"]
    expected = json.loads(reference.read_text())
    expected["45"]["inputs"]["text"] = "A moonlit garden"
    expected["44"]["inputs"]["seed"] = 42
    expected["41"]["inputs"].update(width=1024, height=576)
    assert submitted == expected
    assert graph.read_bytes() == reference.read_bytes()


def test_shared_size_node_shorthand(setup):
    _, _, calls, graph = setup
    sidecar = graph.parent / "packs.toml"
    sidecar.write_text(
        sidecar.read_text().replace(
            'width_node = "5"\nheight_node = "5"\n', 'size_node = "5"\n'
        )
    )
    assert not invoke(setup, width=1024, height=576).error
    assert json.loads(calls[-1].content)["prompt"]["5"]["inputs"] == {
        "width": 1024,
        "height": 576,
        "batch_size": 1,
    }
