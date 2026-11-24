import asyncio
import json
import struct
import zlib
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import ValidationError
from test_tools import opened

from solakit.config import ImageSettings
from solakit.records import ImageGeneration, ToolReceipt
from solakit.sdk import ToolError
from solakit.storage.evidence import canonical, digest
from solakit.storage.store import Store
from solakit.tools.builtins import GenerateImage
from solakit.tools.images import MAX_IMAGE_BYTES, ImageService, validate_png, workflow


def png(width: int = 768, height: int = 768) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + b"\x80\0\xff" * width) * height))
        + chunk(b"IEND", b"")
    )


class Provider:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.ready = True
        self.lost_response = False
        self.reject = False
        self.failed = False
        self.bad_output = False
        self.filename = "test.png"

    async def respond(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            assert request.url.path == "/prompt"
            body = json.loads(request.content)
            self.requests.append(body)
            if self.lost_response:
                raise httpx2.ReadTimeout("synthetic lost response")
            return httpx2.Response(
                400 if self.reject else 200, json={"prompt_id": body["prompt_id"]}
            )
        if request.url.path.startswith("/history/"):
            if not self.requests or not self.ready:
                return httpx2.Response(200, json={})
            body = self.requests[-1]
            identity = body["prompt_id"]
            return httpx2.Response(
                200,
                json={
                    identity: {
                        "prompt": [
                            0,
                            identity,
                            body["prompt"],
                            {"client_id": body["client_id"]},
                            ["51"],
                        ],
                        "outputs": {
                            "51": {
                                "images": [
                                    {"filename": self.filename, "subfolder": "", "type": "output"}
                                ]
                            }
                        },
                        "status": {
                            "status_str": "error" if self.failed else "success",
                            "completed": True,
                        },
                    }
                },
            )
        assert request.url.path == "/view"
        args = self.requests[-1]["prompt"]["41"]["inputs"]
        return httpx2.Response(
            200, content=b"invalid" if self.bad_output else png(args["width"], args["height"])
        )


def service(store: Store, provider: Provider, *, max_checks: int = 300) -> ImageService:
    result = ImageService(store, ImageSettings(enabled=True, max_checks=max_checks))
    result.adapter.transport = httpx2.MockTransport(provider.respond)
    return result


async def until_submitted(images: ImageService) -> ImageGeneration:
    async with asyncio.timeout(3):
        while not images.unfinished() or images.unfinished()[0].state != "submitted":
            await asyncio.sleep(0)
        return images.unfinished()[0]


def test_schema_defaults_and_exact_workflow() -> None:
    schema = GenerateImage.model_json_schema()
    assert set(schema["properties"]) == {"prompt", "width", "height", "seed"}
    assert schema["required"] == ["prompt"]
    args = GenerateImage(prompt="café 🐈")
    assert args.width == args.height == 768
    assert 2**52 <= args.seed < 2**53
    raw, graph = workflow(args)
    original = json.loads(Path("examples/image-workflow.json").read_bytes())
    assert raw == Path("examples/image-workflow.json").read_bytes()
    original["45"]["inputs"]["text"] = args.prompt
    original["44"]["inputs"]["seed"] = args.seed
    assert graph == original and graph["41"]["inputs"]["batch_size"] == 1
    explicit = GenerateImage(prompt="revision", width=1024, height=512, seed=0)
    _, graph = workflow(explicit)
    original["45"]["inputs"]["text"] = explicit.prompt
    original["41"]["inputs"].update(width=1024, height=512)
    original["44"]["inputs"]["seed"] = 0
    assert graph == original


@pytest.mark.parametrize(
    "values",
    [
        {"prompt": ""},
        {"prompt": 12},
        {"prompt": "x", "width": True},
        {"prompt": "x", "height": 767},
        {"prompt": "x", "width": 4096},
        {"prompt": "x", "seed": -1},
        {"prompt": "x", "seed": 2**64},
        {"prompt": "x", "seed": None},
        {"prompt": "x", "workflow": "other"},
    ],
)
def test_invalid_arguments(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GenerateImage.model_validate(values)


def test_generation_receipt_and_artifact(tmp_path: Path) -> None:
    with opened(tmp_path) as h:

        async def scenario() -> None:
            provider = Provider()
            images = service(h.store, provider)
            h.dispatcher.images = images
            await images.start()
            try:
                receipt = await h.session.dispatch(h.call("images.generate", prompt="synthetic"))
                assert receipt.status == "succeeded" and receipt.effect == "confirmed"
                assert receipt.artifact and receipt.artifact.media_type == "image/png"
                generation = images.get(provider.requests[0]["client_id"])
                assert generation.state == "downloaded" and generation.artifact == receipt.artifact
                assert generation.fingerprint == digest(canonical(provider.requests[0]))
                assert (
                    generation.parameters["seed"]
                    == provider.requests[0]["prompt"]["44"]["inputs"]["seed"]
                )
                raw = (h.profile.data_dir / receipt.artifact.path).read_bytes()
                assert digest(raw) == receipt.artifact.sha256
                assert len(provider.requests) == 1
                states = [
                    json.loads(row[0])["record"]["state"]
                    for row in h.store.db.execute(
                        "SELECT body FROM outbox WHERE json_extract(body,'$.kind')='image' "
                        "ORDER BY ordinal"
                    )
                ]
                assert states[0] == "intent" and states[-1] == "downloaded"
                assert {"submitted", "completed"} <= set(states)
            finally:
                await images.stop()

        asyncio.run(scenario())


def test_lost_response_cancellation_and_bounded_observation(tmp_path: Path) -> None:
    with opened(tmp_path) as h:

        async def scenario() -> None:
            provider = Provider()
            provider.lost_response = True
            images = service(h.store, provider, max_checks=2)
            h.dispatcher.images = images
            await images.adapter.start()
            try:
                receipt = await h.session.dispatch(h.call("images.generate", prompt="lost"))
                assert receipt.status == "uncertain"
                second = await h.session.dispatch(h.call("images.generate", prompt="lost"))
                assert second.status == "failed" and len(provider.requests) == 1
                generation = images.unfinished()[0]
                await images.check(generation)
                assert images.get(generation.id).state == "downloaded"
                assert len(provider.requests) == 1
            finally:
                await images.adapter.stop()

        asyncio.run(scenario())


def test_cancel_keeps_observing_without_resubmission(tmp_path: Path) -> None:
    with opened(tmp_path) as h:

        async def scenario() -> None:
            provider = Provider()
            provider.ready = False
            images = service(h.store, provider)
            h.dispatcher.images = images
            await images.start()
            task = asyncio.create_task(
                h.session.dispatch(h.call("images.generate", prompt="cancel"))
            )
            try:
                generation = await until_submitted(images)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                receipts = [
                    r for _, r in h.store.run_records(h.step.run_id) if isinstance(r, ToolReceipt)
                ]
                assert receipts[-1].status == "uncertain"
                provider.ready = True
                async with asyncio.timeout(4):
                    while images.get(generation.id).state != "downloaded":
                        await asyncio.sleep(0.05)
                assert len(provider.requests) == 1
            finally:
                await images.stop()

        asyncio.run(scenario())


class Crash(BaseException):
    pass


@pytest.mark.parametrize(
    "stage",
    ["image_intent", "image_accepted", "image_completed", "image_artifact", "image_downloaded"],
)
def test_crash_recovery_never_submits_again(tmp_path: Path, stage: str) -> None:
    with opened(tmp_path) as h:

        async def scenario() -> None:
            provider = Provider()
            images = service(h.store, provider)
            await images.adapter.start()
            fired = False

            def fault(point: str) -> None:
                nonlocal fired
                if point == stage and not fired:
                    fired = True
                    raise Crash

            h.store.fault = fault
            args = GenerateImage(prompt="crash", seed=123)
            task = asyncio.create_task(
                images.generate(
                    h.call("images.generate", prompt="crash"), args, lambda: None, lambda: None
                )
            )
            try:
                if stage in ("image_intent", "image_accepted"):
                    with pytest.raises(Crash):
                        await task
                else:
                    generation = await until_submitted(images)
                    with pytest.raises(Crash):
                        await images.check(generation)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                assert fired
            finally:
                await images.adapter.stop()
            submissions = len(provider.requests)
            h.store.fault = lambda point: None
            await h.store.stop()
            await h.store.start()
            recovered = service(h.store, provider, max_checks=3)
            await recovered.start()
            try:
                async with asyncio.timeout(3):
                    while any(
                        g.checks == 0 or g.state != "uncertain" for g in recovered.unfinished()
                    ):
                        await asyncio.sleep(0)
                assert len(provider.requests) == submissions
                row = h.store.db.execute("SELECT id FROM entities WHERE kind='image'").fetchone()
                generation = recovered.get(row[0])
                assert generation.state == (
                    "uncertain" if stage == "image_intent" else "downloaded"
                )
                assert generation.parameters["seed"] == 123
            finally:
                await recovered.stop()

        asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind", ["outage", "rejected", "failed", "invalid_output", "unsafe_output"]
)
def test_failure_is_not_success(tmp_path: Path, kind: str) -> None:
    with opened(tmp_path) as h:

        async def scenario() -> None:
            provider = Provider()
            provider.ready = kind != "outage"
            provider.reject = kind == "rejected"
            provider.failed = kind == "failed"
            provider.bad_output = kind == "invalid_output"
            provider.filename = ".." if kind == "unsafe_output" else "test.png"
            images = service(h.store, provider, max_checks=1)
            h.dispatcher.images = images
            await images.start()
            try:
                receipt = await h.session.dispatch(h.call("images.generate", prompt=kind))
                assert receipt.status in ("failed", "uncertain") and receipt.failure
                assert receipt.artifact is None and len(provider.requests) == 1
            finally:
                await images.stop()

        asyncio.run(scenario())


def test_png_validation() -> None:
    validate_png(png(64, 64), 64, 64)
    for raw in (png()[:-10], png(64, 64), b"not png", b"x" * (MAX_IMAGE_BYTES + 1)):
        with pytest.raises(ToolError):
            validate_png(raw, 768, 768)
    oversized = bytearray(png(128, 128))
    oversized[16:24] = struct.pack(">II", 64, 64)
    oversized[29:33] = struct.pack(">I", zlib.crc32(oversized[12:29]))
    with pytest.raises(ToolError):
        validate_png(bytes(oversized), 64, 64)
    corrupt = bytearray(png(64, 64))
    corrupt[-5] ^= 1
    with pytest.raises(ToolError):
        validate_png(bytes(corrupt), 64, 64)


def test_crash_after_local_receipt_preserves_success_without_resubmission(tmp_path: Path) -> None:
    with opened(tmp_path) as h:

        async def scenario() -> None:
            provider = Provider()
            images = service(h.store, provider)
            h.dispatcher.images = images
            await images.start()

            def fault(stage: str) -> None:
                if stage == "image_receipt":
                    raise Crash

            h.store.fault = fault
            try:
                with pytest.raises(Crash):
                    await h.session.dispatch(h.call("images.generate", prompt="receipt crash"))
            finally:
                await images.stop()
            h.store.fault = lambda stage: None
            await h.store.stop()
            await h.store.start()
            receipts = [
                r for _, r in h.store.run_records(h.step.run_id) if isinstance(r, ToolReceipt)
            ]
            assert len(receipts) == 1 and receipts[0].status == "succeeded"
            assert receipts[0].artifact and len(provider.requests) == 1

        asyncio.run(scenario())


def test_verified_reconciliation_of_older_provider_id(tmp_path: Path) -> None:
    from uuid import uuid4

    with opened(tmp_path) as h:

        async def scenario() -> None:
            provider = Provider()
            provider.lost_response = True
            images = service(h.store, provider)
            h.dispatcher.images = images
            await images.adapter.start()
            try:
                receipt = await h.session.dispatch(
                    h.call("images.generate", prompt="lost older provider response")
                )
                generation = images.unfinished()[0]
                actual_id = str(uuid4())
                provider.requests[0]["prompt_id"] = actual_id
                with pytest.raises(ValueError):
                    await images.reconcile(generation.id, str(uuid4()))
                reconciled = await images.reconcile(generation.id, actual_id)
                assert reconciled.state == "downloaded" and reconciled.submission_id == actual_id
                assert h.store._load("receipt", receipt.id, ToolReceipt).status == "uncertain"
                assert len(provider.requests) == 1
            finally:
                await images.adapter.stop()

        asyncio.run(scenario())
