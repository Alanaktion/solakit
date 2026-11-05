"""Bounded image ingestion, immutable local evidence and model-only projection."""

import asyncio
import base64
import hashlib
import io
import os
import re
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

MAX_IMAGE_BYTES = 8_000_000
MAX_TOTAL_BYTES = 16_000_000
MAX_IMAGES = 4
MAX_PIXELS = 16_000_000
MAX_EDGE = 1024
IMAGE_CONTEXT_BYTES = 8192


class MediaError(ValueError):
    """Safe input/evidence failure; never silently discard an attached image."""


def read_blob(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ValueError("Invalid image file")
        with os.fdopen(descriptor, "rb", closefd=False) as image:
            return image.read(limit + 1)
    finally:
        os.close(descriptor)


class ImageRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    original_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    filename: str = Field(max_length=255)
    width: int = Field(ge=1, le=MAX_EDGE)
    height: int = Field(ge=1, le=MAX_EDGE)
    bytes: int = Field(ge=1, le=2_000_000)
    original_bytes: int = Field(ge=1, le=MAX_IMAGE_BYTES)
    original_media_type: Literal["image/png", "image/jpeg", "image/webp"]


@dataclass(frozen=True)
class ImageAttachment:
    filename: str
    size: int
    read: Callable[[], Awaitable[bytes]]


class Media:
    def __init__(self, root: Path):
        self.root = root / "media"

    def _save(self, path, payload):
        from solakit.core.store import atomic_write, sync_directory

        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        sync_directory(self.root.parent)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        sync_directory(self.root)
        if path.exists():
            try:
                existing = read_blob(path, len(payload))
            except (OSError, ValueError) as exc:
                raise MediaError(
                    "Stored image evidence is damaged; restore from backup"
                ) from exc
            if existing != payload:
                raise MediaError(
                    "Stored image evidence is damaged; restore from backup"
                )
        else:
            atomic_write(path, payload)

    def save(self, payload: bytes, filename: str) -> ImageRef:
        if not payload or len(payload) > MAX_IMAGE_BYTES:
            raise MediaError("Each image must contain 1–8000000 bytes")
        try:
            with Image.open(io.BytesIO(payload)) as image:
                media_type = {
                    "PNG": "image/png",
                    "JPEG": "image/jpeg",
                    "WEBP": "image/webp",
                }.get(image.format)
                if media_type is None or getattr(image, "n_frames", 1) != 1:
                    raise MediaError("Use a static PNG, JPEG or WebP image")
                if image.width * image.height > MAX_PIXELS:
                    raise MediaError("Image exceeds the 16 megapixel limit")
                image.load()
                image = ImageOps.exif_transpose(image)
                image.thumbnail((MAX_EDGE, MAX_EDGE))
                rgba = image.convert("RGBA")
                rgb = Image.new("RGB", rgba.size, "white")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
                output = io.BytesIO()
                rgb.save(output, format="JPEG", quality=95)
                normalized = output.getvalue()
        except MediaError:
            raise
        except (
            OSError,
            ValueError,
            Image.DecompressionBombError,
            UnidentifiedImageError,
        ) as exc:
            raise MediaError(
                "Image is invalid or unsupported; use PNG, JPEG or WebP"
            ) from exc
        original_hash = hashlib.sha256(payload).hexdigest()
        digest = hashlib.sha256(normalized).hexdigest()
        if len(normalized) > 2_000_000:
            raise MediaError("Normalized image exceeds the model input byte limit")
        ref = ImageRef(
            sha256=digest,
            original_sha256=original_hash,
            filename=filename[:255],
            width=rgb.width,
            height=rgb.height,
            bytes=len(normalized),
            original_bytes=len(payload),
            original_media_type=media_type,
        )
        self._save(self.root / "originals" / original_hash, payload)
        self._save(self.root / f"{digest}.jpg", normalized)
        return ref

    async def ingest(self, attachments) -> list[ImageRef]:
        from solakit.core.io import durable

        if len(attachments) > MAX_IMAGES:
            raise MediaError("Send at most four images per message")
        if any(not 0 < item.size <= MAX_IMAGE_BYTES for item in attachments):
            raise MediaError("Each image must contain 1–8000000 bytes")
        if sum(item.size for item in attachments) > MAX_TOTAL_BYTES:
            raise MediaError("Images exceed the 16 MB message limit")
        results, total = [], 0
        for item in attachments:
            try:
                async with asyncio.timeout(15):
                    payload = await item.read()
            except (OSError, TimeoutError) as exc:
                raise MediaError(
                    "Image download failed; resend the attachment"
                ) from exc
            total += len(payload)
            if total > MAX_TOTAL_BYTES:
                raise MediaError("Images exceed the 16 MB message limit")
            results.append(await durable(self.save, payload, item.filename))
        return results

    def read_hash(self, digest: str) -> bytes:
        try:
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("Invalid image identity")
            data = read_blob(self.root / f"{digest}.jpg", 2_000_000)
            if len(data) > 2_000_000 or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("Image checksum mismatch")
            return data
        except (OSError, ValueError) as exc:
            raise MediaError(
                "Stored image is missing or damaged; restore its evidence"
            ) from exc

    def read(self, ref: ImageRef) -> bytes:
        data = self.read_hash(ref.sha256)
        if len(data) != ref.bytes:
            raise MediaError("Stored image metadata does not match its evidence")
        return data

    def project(self, messages: list[dict]) -> list[dict]:
        result, count = [], 0
        for message in messages:
            refs = [ImageRef.model_validate(ref) for ref in message.get("images", [])]
            if refs and message.get("role") != "user":
                raise MediaError("Only user messages may contain image inputs")
            count += len(refs)
            if count > 16:
                raise MediaError(
                    "Too many images in history; compact or archive the conversation"
                )
            projected = {
                key: value for key, value in message.items() if key != "images"
            }
            if refs:
                parts = (
                    [{"type": "text", "text": message["content"]}]
                    if message.get("content")
                    else []
                )
                for ref in refs:
                    encoded = base64.b64encode(self.read(ref)).decode("ascii")
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64," + encoded},
                        }
                    )
                projected["content"] = parts
            result.append(projected)
        return result
