"""One fixed ComfyUI workflow; durable submission and read-only recovery."""

import asyncio
import json
import struct
import zlib
from collections.abc import Callable
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2
from pydantic import JsonValue

from solakit.config import ImageSettings
from solakit.evidence import canonical, checked_json, digest
from solakit.records import Artifact, ErrorCode, ImageGeneration, ToolCall, new_id
from solakit.sdk import ToolError
from solakit.store import Store
from solakit.tools import GenerateImage

MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024


def workflow(args: GenerateImage) -> tuple[bytes, dict[str, Any]]:
    raw = files("solakit").joinpath("image-workflow.json").read_bytes()
    graph = json.loads(raw)
    graph["45"]["inputs"]["text"] = args.prompt
    graph["41"]["inputs"]["width"] = args.width
    graph["41"]["inputs"]["height"] = args.height
    graph["44"]["inputs"]["seed"] = args.seed
    return raw, graph


def validate_png(raw: bytes, width: int, height: int) -> None:
    if (
        not raw.startswith(b"\x89PNG\r\n\x1a\n")
        or len(raw) > MAX_IMAGE_BYTES
        or not 64 <= width <= 2048
        or not 64 <= height <= 2048
    ):
        raise ToolError("Provider output is not a bounded PNG image")
    offset = 8
    compressed = bytearray()
    channels = 0
    first = True
    while offset + 12 <= len(raw):
        length = int.from_bytes(raw[offset : offset + 4])
        kind = raw[offset + 4 : offset + 8]
        if (kind == b"IHDR" and not first) or kind in (b"acTL", b"fcTL", b"fdAT"):
            break
        end = offset + 8 + length
        if end + 4 > len(raw) or zlib.crc32(raw[offset + 4 : end]) != int.from_bytes(
            raw[end : end + 4]
        ):
            break
        if first and (
            kind != b"IHDR"
            or length != 13
            or struct.unpack(">II", raw[offset + 8 : offset + 16]) != (width, height)
        ):
            break
        if first:
            depth, color, compression, filtering, interlace = raw[offset + 16 : offset + 21]
            if depth != 8 or color not in (2, 6) or compression or filtering or interlace:
                break
            channels = 3 if color == 2 else 4
        first = False
        if kind == b"IDAT":
            compressed.extend(raw[offset + 8 : end])
        if kind == b"IEND":
            if length == 0 and compressed and end + 4 == len(raw):
                expected = height * (width * channels + 1)
                decoder = zlib.decompressobj()
                try:
                    pixels = decoder.decompress(compressed, expected + 1)
                except zlib.error:
                    break
                if (
                    len(pixels) == expected
                    and decoder.eof
                    and not decoder.unused_data
                    and not decoder.unconsumed_tail
                    and all(pixels[row] <= 4 for row in range(0, expected, width * channels + 1))
                ):
                    return
            break
        offset = end + 4
    raise ToolError("Provider PNG is incomplete, corrupt, or has unexpected dimensions")


class ComfyUI:
    def __init__(self, settings: ImageSettings) -> None:
        self.settings = settings
        self.transport: httpx2.AsyncBaseTransport | None = None
        self.client: httpx2.AsyncClient | None = None

    async def start(self) -> None:
        self.client = httpx2.AsyncClient(
            base_url=self.settings.base_url + "/",
            timeout=self.settings.timeout_seconds,
            transport=self.transport,
            follow_redirects=False,
            trust_env=False,
        )

    async def stop(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        params: dict[str, str] | None = None,
        cap: int = MAX_RESPONSE_BYTES,
    ) -> tuple[int, bytes]:
        if self.client is None:
            raise ToolError("Image service is unavailable")
        async with (
            asyncio.timeout(self.settings.timeout_seconds),
            self.client.stream(
                method,
                path,
                content=body,
                params=params,
                headers={"Content-Type": "application/json"} if body else None,
            ) as response,
        ):
            raw = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                raw.extend(chunk)
                if len(raw) > cap:
                    raise ToolError("Image provider response exceeds its byte limit")
            return response.status_code, bytes(raw)

    async def history(self, identity: str) -> dict[str, Any] | None:
        status, raw = await self.request("GET", "history/" + identity)
        if status != 200:
            raise ToolError("Image provider history is unavailable")
        body = checked_json(raw)
        canonical(body)
        if not isinstance(body, dict):
            raise ToolError("Invalid image history response")
        item = body.get(identity)
        if item is not None and not isinstance(item, dict):
            raise ToolError("Invalid image history entry")
        return item


class ImageService:
    def __init__(self, store: Store, settings: ImageSettings) -> None:
        self.store, self.settings = store, settings
        self.adapter = ComfyUI(settings)
        self.task: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        self.failure: str | None = None

    def get(self, identity: str) -> ImageGeneration:
        return self.store._load("image", identity, ImageGeneration)

    def unfinished(self) -> list[ImageGeneration]:
        rows = self.store.db.execute(
            "SELECT id FROM entities WHERE kind='image' AND "
            "json_extract(body,'$.state') NOT IN ('downloaded','failed')"
        ).fetchall()
        return [self.get(row[0]) for row in rows]

    def save(self, generation: ImageGeneration, **changes: Any) -> ImageGeneration:
        value = ImageGeneration.model_validate(generation.model_dump() | changes)
        self.store._commit((value,))
        return value

    async def start(self) -> None:
        if not self.settings.enabled:
            return
        await self.adapter.start()
        for generation in self.unfinished():
            if generation.state == "intent":
                self.save(
                    generation,
                    state="uncertain",
                    error="Process interrupted before acceptance was recorded",
                )
        self.task = asyncio.create_task(self.observe())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        await self.adapter.stop()

    async def generate(
        self,
        call: ToolCall,
        args: GenerateImage,
        begin: Callable[[], None],
        confirm: Callable[[], None],
    ) -> ImageGeneration:
        if not self.settings.enabled or self.adapter.client is None:
            raise ToolError("ComfyUI image generation is not enabled")
        if not args.prompt.strip():
            raise ToolError("Image prompt must contain text", ErrorCode.INVALID_INPUT)
        async with self.lock:
            if self.unfinished():
                raise ToolError(
                    "An image is pending or uncertain; reconcile before generating again"
                )
            raw, graph = workflow(args)
            identity, requested_id = new_id(), str(uuid4())
            request = canonical({"prompt": graph, "client_id": identity, "prompt_id": requested_id})
            generation = ImageGeneration(
                id=identity,
                origin=call.origin,
                session_id=call.session_id,
                run_id=call.run_id,
                tool_call_id=call.id,
                endpoint=self.settings.base_url,
                workflow_sha256=digest(raw),
                parameters=args.model_dump(mode="json"),
                fingerprint=digest(request),
                body=Artifact.model_validate(self.store.artifact(new_id(), request)),
                requested_id=requested_id,
            )
            # Authority is checked before intent; no submission can precede the durable seed.
            begin()
            self.store._commit((generation,))
            self.store.fault("image_intent")
            try:
                status, response = await self.adapter.request("POST", "prompt", body=request)
                self.store.fault("image_accepted")
                if status == 400:
                    generation = self.save(
                        generation, state="failed", error="ComfyUI rejected the workflow"
                    )
                    confirm()
                    raise ToolError("ComfyUI rejected the configured workflow")
                if status != 200:
                    raise ToolError("Image submission acceptance is unknown")
                submission = cast(dict[str, Any], checked_json(response))["prompt_id"]
                if not isinstance(submission, str) or str(UUID(submission)) != submission:
                    raise ToolError("Provider returned an invalid submission ID")
                generation = self.save(generation, submission_id=submission, state="submitted")
            except BaseException:
                if not self.store.failed and generation.state == "intent":
                    self.save(
                        generation,
                        state="uncertain",
                        error="Submission acceptance is unknown; never resubmit automatically",
                    )
                raise
        while True:
            generation = self.get(generation.id)
            if generation.state == "downloaded":
                confirm()
                return generation
            if generation.state == "failed":
                confirm()
                raise ToolError(generation.error or "Image generation failed")
            if generation.state == "uncertain":
                raise ToolError("Image outcome is uncertain; inspect image evidence before retry")
            await asyncio.sleep(0.1)

    async def observe(self) -> None:
        while True:
            try:
                async with self.lock:
                    for generation in self.unfinished():
                        if generation.checks < self.settings.max_checks:
                            await self.check(generation)
                self.failure = None
            except Exception:
                self.failure = (
                    "Image observation failed; inspect evidence and provider availability"
                )
            await asyncio.sleep(self.settings.poll_seconds)

    async def check(self, generation: ImageGeneration) -> None:
        # Recovery observes recorded work only, even after cancellation/revocation.
        if generation.endpoint != self.settings.base_url:
            return
        generation = self.save(generation, checks=generation.checks + 1)
        try:
            if generation.state != "completed":
                identity = generation.submission_id or generation.requested_id
                history = await self.adapter.history(identity)
                if history is None:
                    if generation.checks >= self.settings.max_checks:
                        self.save(
                            generation,
                            state="uncertain",
                            error="Observation limit reached; provider work may still be running",
                        )
                    return
                prompt = history["prompt"]
                from solakit.compaction import read_artifact

                request_bytes = read_artifact(self.store, generation.body)
                if digest(request_bytes) != generation.fingerprint:
                    raise ToolError("Recorded image request fingerprint differs")
                request = json.loads(request_bytes)
                if (
                    not isinstance(prompt, list)
                    or len(prompt) < 4
                    or not isinstance(prompt[3], dict)
                    or prompt[1] != identity
                    or prompt[2] != request["prompt"]
                    or prompt[3].get("client_id") != generation.id
                ):
                    raise ToolError("Provider history does not match the recorded image request")
                status = history.get("status", {})
                if not isinstance(status, dict):
                    raise ToolError("Invalid provider image status")
                if status.get("status_str") == "error":
                    self.save(
                        generation,
                        submission_id=identity,
                        state="failed",
                        error="ComfyUI reported generation failure",
                    )
                    return
                if status.get("completed") is not True:
                    raise ToolError("Image history is not terminal")
                images = history["outputs"]["51"]["images"]
                if not isinstance(images, list) or len(images) != 1:
                    raise ToolError("Workflow must return exactly one image")
                output = images[0]
                if not isinstance(output, dict):
                    raise ToolError("Invalid provider output reference")
                output = {key: output[key] for key in ("filename", "subfolder", "type")}
                if (
                    any(
                        not isinstance(value, str) or "\\" in value or "\x00" in value
                        for value in output.values()
                    )
                    or output["type"] != "output"
                    or not output["filename"]
                    or output["filename"] in (".", "..")
                    or PurePosixPath(output["filename"]).name != output["filename"]
                    or PurePosixPath(output["subfolder"]).is_absolute()
                    or ".." in PurePosixPath(output["subfolder"]).parts
                ):
                    raise ToolError("Invalid provider output path")
                generation = self.save(
                    generation, submission_id=identity, state="completed", output=output, error=None
                )
                self.store.fault("image_completed")
            assert generation.output is not None
            status_code, raw = await self.adapter.request(
                "GET", "view", params=generation.output, cap=MAX_IMAGE_BYTES
            )
            if status_code != 200:
                raise ToolError("Generated image download is unavailable")
            parameters = GenerateImage.model_validate(generation.parameters)
            validate_png(raw, parameters.width, parameters.height)
            artifact = Artifact.model_validate(
                self.store.artifact(generation.id, raw) | {"media_type": "image/png"}
            )
            self.store.fault("image_artifact")
            self.save(generation, state="downloaded", artifact=artifact, error=None)
            self.store.fault("image_downloaded")
        except httpx2.HTTPError, TimeoutError, ValueError, KeyError, TypeError, ToolError:
            self.save(
                generation,
                error="Provider observation or download failed; no resubmission",
                state="uncertain"
                if generation.checks >= self.settings.max_checks
                else generation.state,
            )

    def result(self, generation: ImageGeneration) -> JsonValue:
        return cast(JsonValue, generation.model_dump(mode="json"))

    async def reconcile(self, identity: str, submission_id: str) -> ImageGeneration:
        from solakit.compaction import read_artifact

        if str(UUID(submission_id)) != submission_id:
            raise ValueError("submission ID must be a canonical UUID")
        async with self.lock:
            generation = self.get(identity)
            if generation.endpoint != self.settings.base_url or generation.state in (
                "downloaded",
                "failed",
            ):
                raise ValueError("image is terminal or belongs to another endpoint")
            history = await self.adapter.history(submission_id)
            request = json.loads(read_artifact(self.store, generation.body))
            prompt = history.get("prompt") if history else None
            if (
                not isinstance(prompt, list)
                or len(prompt) < 4
                or prompt[1] != submission_id
                or prompt[2] != request["prompt"]
                or not isinstance(prompt[3], dict)
                or prompt[3].get("client_id") != generation.id
            ):
                raise ValueError("provider history must match this exact recorded request")
            generation = self.save(
                generation,
                submission_id=submission_id,
                state="submitted",
                checks=0,
                error="Operator requested verified read-only reconciliation",
            )
            await self.check(generation)
            return self.get(identity)
