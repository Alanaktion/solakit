"""Discord event -> bounded CDN bytes -> stored images -> OpenAI vision request."""

import asyncio
import base64
import hashlib
import io
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_execution import completion, model_list, setup
from turns import plain

from solakit.channels import Channels
from solakit.channels.base import Inbound
from solakit.channels.discord import DiscordAdapter
from solakit.core.compaction import request_bytes
from solakit.core.config import Channel, SecretRef
from solakit.core.media import MAX_IMAGE_BYTES, ImageAttachment, Media, MediaError
from solakit.core.memory import memory_messages
from solakit.core.schemas import Session
from solakit.main import create_app


def png(color="red", size=(64, 32)):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def decode(part):
    url = part["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    image = Image.open(
        io.BytesIO(base64.b64decode(url.split(",", 1)[1], validate=True))
    )
    image.load()
    return image


class Target:
    def __init__(self, id=100):
        self.id, self.sent = id, []

    @asynccontextmanager
    async def typing(self):
        yield

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return SimpleNamespace(id=len(self.sent))


class Thread(Target):
    parent_id = 100


class Gateway:
    def __init__(self, **kwargs):
        self.events, self.closed = {}, asyncio.Event()
        self.user = SimpleNamespace(id=999)
        self.targets = {100: Target(), 101: Thread(101)}

    def event(self, function):
        self.events[function.__name__] = function

    async def start(self, token, reconnect):
        await self.events["on_ready"]()
        await self.closed.wait()

    async def close(self):
        self.closed.set()

    def get_channel(self, id):
        return self.targets[id]


def fake_cdn(monkeypatch, payload, *, status=200, encoding="identity"):
    calls = []

    class Response:
        headers = {"Content-Encoding": encoding}

        def __init__(self):
            self.status, self.content = status, self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def iter_chunked(self, size):
            for index in range(0, len(payload), size):
                yield payload[index : index + size]

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["auto_decompress"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            assert kwargs == {
                "allow_redirects": False,
                "headers": {"Accept-Encoding": "identity"},
            }
            calls.append(url)
            return Response()

    monkeypatch.setattr("solakit.channels.discord.aiohttp.ClientSession", Client)
    return calls


@pytest.mark.parametrize("caption, thread", [("What do you see?", False), ("", True)])
def test_discord_attachment_pixels_reach_model_with_history_and_tool_turns(
    config, monkeypatch, caption, thread
):
    config.channels = [
        Channel(
            id="vision",
            type="discord",
            channel_id="100",
            session="vision",
            enabled=True,
            trigger_authors=["42"],
        )
    ]
    config.enabled_tools = ["runtime"]
    config.profiles["interactive"].tools = ["runtime.status"]
    monkeypatch.setenv("VISION_KEY", "vision-fixture-key")
    config.model.api_key = SecretRef(env="VISION_KEY")
    config.model.model_id = "vision-fixture"
    requests = []

    def model(request):
        assert request.headers["authorization"] == "Bearer vision-fixture-key"
        if request.url.path.endswith("/models"):
            return model_list(config)
        body = json.loads(request.content)
        assert body["model"] == "vision-fixture"
        requests.append(body)
        user = next(m for m in body["messages"] if m["role"] == "user")
        parts = user["content"]
        image = decode(parts[-1])
        assert image.size == (64, 32) and image.getpixel((10, 10))[0] > 240
        assert (plain(parts[0]["text"]) if len(parts) > 1 else "") == caption
        assert (
            "images" not in user
            and "cdn.discordapp.com" not in request.content.decode()
        )
        if len(requests) == 1:
            return completion(
                config,
                calls=[
                    {
                        "id": "status",
                        "type": "function",
                        "function": {"name": "runtime.status", "arguments": "{}"},
                    }
                ],
            )
        return completion(config, "A red rectangle.")

    source = png()
    downloads = fake_cdn(monkeypatch, source)
    monkeypatch.setattr("solakit.channels.discord.discord.Client", Gateway)
    monkeypatch.setattr("solakit.channels.discord.discord.Thread", Thread)
    execution = setup(config, model)
    execution.registry.runtime_status = lambda profile: {"profile": profile}
    adapter = DiscordAdapter(config, "fixture-bot-token")
    channels = Channels(config, execution, {"discord": adapter})

    async def exercise():
        try:
            await channels.start()
            target = adapter.client.targets[101 if thread else 100]
            message = SimpleNamespace(
                channel=target,
                author=SimpleNamespace(id=42, bot=False),
                content=caption,
                id=123,
                guild=None,
                mentions=[],
                attachments=[
                    SimpleNamespace(
                        filename="red.png",
                        size=len(source),
                        url="https://cdn.discordapp.com/attachments/100/123/red.png?hm=signed",
                    )
                ],
            )
            await adapter.client.events["on_message"](message)
            assert len(downloads) == 1 and len(requests) == 2
            history = execution.store.transcript("vision")
            assert history[0]["content"] == caption and len(history[0]["images"]) == 1
            ref = history[0]["images"][0]
            assert ref["original_sha256"] == hashlib.sha256(source).hexdigest()
            assert (
                execution.store.root / "media/originals" / ref["original_sha256"]
            ).read_bytes() == source
            assert target.sent[-1][0] == ("A red rectangle.",)
            assert target.sent[-1][1]["allowed_mentions"].everyone is False
            # The next textual turn still contains the original image pixels.
            await channels.receive(
                Inbound("discord", "100", "42", "What color was it?", "124")
            )
            assert len(requests) == 3 and len(downloads) == 1
            evidence = execution.store.run(history[0]["run_id"])
            assert evidence["input_images"][0]["sha256"] == ref["sha256"]
            assert "base64" not in json.dumps(evidence)
            memory = memory_messages(history)
            assert "Image attachment metadata" in json.dumps(memory)
            assert "base64" not in json.dumps(memory)
            execution.store.rebuild()
            assert (
                execution.store.search("see")
                if caption
                else not execution.store.search("base64")
            )
        finally:
            await execution.close()
            await channels.stop()

    asyncio.run(exercise())


def test_gating_precedes_download_and_rejected_images_do_not_reach_model(
    config, monkeypatch
):
    config.channels = [
        Channel(
            id="v",
            type="discord",
            channel_id="100",
            session="v",
            enabled=True,
            trigger_authors=["42"],
        )
    ]
    monkeypatch.setattr("solakit.channels.discord.discord.Client", Gateway)
    downloads = fake_cdn(monkeypatch, b"not an image")
    calls = []
    execution = setup(config, lambda request: calls.append(request))
    adapter = DiscordAdapter(config, "fixture")
    channels = Channels(config, execution, {"discord": adapter})

    async def exercise():
        try:
            await channels.start()
            target = adapter.client.targets[100]
            message = SimpleNamespace(
                channel=target,
                author=SimpleNamespace(id=7, bot=False),
                content="",
                id=1,
                guild=None,
                mentions=[],
                attachments=[
                    SimpleNamespace(
                        filename="bad.png",
                        size=12,
                        url="https://cdn.discordapp.com/attachments/100/1/bad.png",
                    )
                ],
            )
            await adapter.client.events["on_message"](message)
            assert not downloads and not calls
            message.author.id = 42
            await adapter.client.events["on_message"](message)
            assert len(downloads) == 1 and not calls
            assert "invalid" in target.sent[-1][0][0].lower()
            assert execution.store.list_records("runs") == []
        finally:
            await execution.close()
            await channels.stop()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.discordapp.com/attachments/a",
        "https://evil.invalid/attachments/a",
        "https://cdn.discordapp.com@example.invalid/attachments/a",
        "https://cdn.discordapp.com/other",
        "https://cdn.discordapp.com:444/attachments/a",
    ],
)
def test_cdn_download_rejects_untrusted_locations(config, monkeypatch, url):
    calls = fake_cdn(monkeypatch, png())
    with pytest.raises(MediaError):
        asyncio.run(DiscordAdapter(config, "fixture").download(url))
    assert not calls


@pytest.mark.parametrize(
    "status, encoding, payload",
    [
        (302, "identity", b""),
        (200, "gzip", b""),
        (200, "identity", b"x" * (MAX_IMAGE_BYTES + 1)),
    ],
)
def test_cdn_rejects_redirect_encoding_and_oversize(
    config, monkeypatch, status, encoding, payload
):
    fake_cdn(monkeypatch, payload, status=status, encoding=encoding)
    with pytest.raises(MediaError):
        asyncio.run(
            DiscordAdapter(config, "fixture").download(
                "https://cdn.discordapp.com/attachments/100/1/red.png"
            )
        )


def test_media_limits_and_resize_keep_original_bytes(tmp_path):
    media = Media(tmp_path)
    source = png(size=(2048, 1024))
    ref = media.save(source, "wide.png")
    assert (ref.width, ref.height) == (1024, 512)
    assert (tmp_path / "media/originals" / ref.original_sha256).read_bytes() == source
    assert media.save(source, "again.png").sha256 == ref.sha256

    async def read():
        raise AssertionError("oversize/count checks must precede downloads")

    for attachments in (
        [ImageAttachment("x.png", 1, read)] * 5,
        [ImageAttachment("x.png", MAX_IMAGE_BYTES + 1, read)],
    ):
        with pytest.raises(MediaError):
            asyncio.run(media.ingest(attachments))
    (tmp_path / "media" / f"{ref.sha256}.jpg").write_bytes(b"damaged")
    with pytest.raises(MediaError):
        media.read(ref)


def test_authenticated_ui_and_missing_image_fail_closed(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        media = Media(app.state.store.root)
        ref = media.save(png(), 'photo"<script>.png')
        app.state.store.create_session(Session(id="vision"))
        assert client.get(f"/api/media/{ref.sha256}").status_code == 401
        client.post("/login", data={"token": "operator-test-token"})
        run = client.portal.call(
            lambda: app.state.execution.submit("vision", "", images=[ref])
        )
        assert run.status == "completed"
        response = client.get(f"/api/media/{ref.sha256}")
        assert (
            response.status_code == 200
            and response.headers["content-type"] == "image/jpeg"
        )
        assert response.headers["cache-control"] == "no-store"
        page = client.get("/sessions/vision")
        assert f'src="/api/media/{ref.sha256}"' in page.text
        assert "&lt;script&gt;" in page.text and "base64" not in page.text
        (media.root / f"{ref.sha256}.jpg").unlink()
        failed = client.portal.call(
            lambda: app.state.execution.submit("vision", "follow up")
        )
        assert failed.status == "model_unavailable"
        assert len(app.state.store.transcript("vision")) == 2
        assert client.get(f"/api/media/{ref.sha256}").status_code == 404


def test_streaming_image_request_preserves_pixels(config):
    from test_ui_streaming import Stream, frame

    requests = []

    def model(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        body = json.loads(request.content)
        requests.append(body)
        assert body["stream"] is True
        assert decode(body["messages"][-1]["content"][-1]).getpixel((0, 0))[0] > 240
        return httpx.Response(
            200,
            stream=Stream(
                [
                    frame(config, {"role": "assistant", "content": "Red"}),
                    frame(config, {}, "stop"),
                    b"data: [DONE]\n\n",
                ]
            ),
        )

    execution = setup(config, model)

    async def exercise():
        try:
            ref = Media(execution.store.root).save(png(), "photo.png")
            run = await execution.submit("s", "Describe", images=[ref], stream=True)
            assert run.status == "completed" and len(requests) == 1
            assert execution.store.transcript("s")[-1]["content"] == "Red"
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_vision_backend_rejection_never_retries_text_only(config):
    requests = []

    def model(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={"error": "vision unsupported"})

    execution = setup(config, model)

    async def exercise():
        try:
            ref = Media(execution.store.root).save(png(), "photo.png")
            run = await execution.submit("s", "Describe", images=[ref])
            assert run.status == "model_unavailable" and len(requests) == 1
            assert execution.store.transcript("s") == []
            assert (
                execution.store.run(run.id)["input_images"][0]["sha256"] == ref.sha256
            )
        finally:
            await execution.close()

    asyncio.run(exercise())


def test_image_budget_counts_visual_allowance_without_base64_payload(tmp_path):
    ref = Media(tmp_path).save(png(), "photo.png")
    plain = [{"role": "user", "content": "Describe"}]
    visual = [{**plain[0], "images": [ref.model_dump()]}]
    assert (
        request_bytes("model", visual, [], 10)
        >= request_bytes("model", plain, [], 10) + 8192
    )
    assert request_bytes("model", visual, [], 10) < 16384


def test_unsupported_animated_and_oversized_pixel_inputs_fail(tmp_path):
    import struct
    import zlib

    media = Media(tmp_path)
    animated = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(animated, format="GIF")
    with pytest.raises(MediaError, match="static"):
        media.save(animated.getvalue(), "image.gif")
    data = bytearray(png())
    data[16:24] = struct.pack(">II", 5000, 4000)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
    with pytest.raises(MediaError, match="megapixel"):
        media.save(bytes(data), "large.png")
