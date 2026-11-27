import asyncio
import base64
import json
import os
import threading
import time
from collections.abc import Iterator
from io import BytesIO
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from pydantic import ValidationError
from test_api import BASE, PASSWORD, login
from test_run_loop import call, harness, reply, wire

from solakit.adapters.vision import INSTRUCTION
from solakit.api import create_app
from solakit.config import EffectiveProfile, OperatorConfig, VisionSettings
from solakit.engine.compaction import Compactor, select
from solakit.records import Artifact, Message, Run, new_id
from solakit.runtime import ApplicationContext
from solakit.storage.uploads import UPLOAD_TTL
from solakit.tools.images import UPLOAD_BYTES, normalize_upload

PIN = "fixture-vision"
OBSERVATION = "Visible text reads ORBIT-8392."
QUESTION = "What can you read?"


def picture(kind: str = "PNG", **options: Any) -> bytes:
    output = BytesIO()
    Image.new("RGB", (80, 64), "purple").save(output, format=kind, **options)
    return output.getvalue()


@pytest.fixture
def vision(
    profile: EffectiveProfile, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, ApplicationContext, dict[str, Any]]]:
    monkeypatch.setenv("SOLA_OPERATOR_PASSWORD", PASSWORD)
    monkeypatch.setenv("FIXTURE_VISION_KEY", "private-vision-key")
    settings = VisionSettings(
        pin=PIN,
        base_url="http://vision.example/v1",
        api_key_env="FIXTURE_VISION_KEY",
        generation_timeout_seconds=1,
    )
    profile = profile.model_copy(update={"vision": settings})
    state: dict[str, Any] = {"vision": [], "text": [], "mode": "normal"}
    started, settled = threading.Event(), threading.Event()
    state.update(started=started, settled=settled)

    async def text(body: dict[str, Any]) -> bytes:
        state["text"].append(body)
        if state["mode"] == "tool" and len(state["text"]) == 1:
            return wire(reply("", calls=[call("runtime.status")]))
        return wire(reply("ORBIT-8392"))

    context, _ = harness(profile, text)
    assert context.runs and context.runs.vision

    async def provider(request: httpx2.Request) -> httpx2.Response:
        assert context.inference.locked()
        assert request.url == httpx2.URL("http://vision.example/v1/chat/completions")
        assert request.headers["Authorization"] == "Bearer private-vision-key"
        assert request.headers["Accept-Encoding"] == "identity"
        state["vision"].append(bytes(request.content))
        mode = state["mode"]
        if mode in {"cancel", "timeout"}:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                settled.set()
        if mode == "unavailable":
            return httpx2.Response(503)
        if mode == "oversize":
            return httpx2.Response(200, content=b"x" * 70000)
        return httpx2.Response(
            200,
            json=reply("x" * 4097 if mode == "long" else OBSERVATION)
            | {"model": "substitute" if mode == "mismatch" else PIN},
        )

    context.runs.vision.transport = httpx2.MockTransport(provider)
    with TestClient(create_app(context), base_url=BASE) as client:
        yield client, context, state


def topic(client: TestClient) -> str:
    response = client.post("/api/v1/sessions", json={"mode": "isolated"})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def upload(
    client: TestClient, session: str, raw: bytes | None = None, *, media_type: str = "image/png"
) -> str:
    response = client.post(
        f"/api/v1/sessions/{session}/image-uploads",
        headers={"Content-Type": media_type},
        content=picture() if raw is None else raw,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def submit(
    client: TestClient,
    session: str,
    image: dict[str, str] | None = None,
    *,
    key: str | None = None,
    question: str = QUESTION,
    wait: int = 5,
) -> httpx2.Response:
    return client.post(
        f"/api/v1/runs?wait={wait}",
        headers={"Idempotency-Key": key or new_id()},
        json={"session_id": session, "content": question, "image": image},
    )


@pytest.mark.parametrize("media_type", ["image/png", "image/webp"])
def test_vision_pixels_evidence_history_compaction_and_reattachment(
    vision: tuple[TestClient, ApplicationContext, dict[str, Any]], media_type: str
) -> None:
    client, context, state = vision
    login(client)
    session = topic(client)
    raw = picture("WEBP", lossless=True) if media_type == "image/webp" else picture()
    identity = upload(client, session, raw, media_type=media_type)
    assert client.portal and context.store and context.model

    # Hold the only permit across admission and give the run a chance to reach the adapter.
    client.portal.call(context.inference.acquire)
    state["mode"] = "tool"
    response = submit(client, session, {"upload_id": identity}, wait=0)
    assert response.status_code == 202, response.text
    run_id = response.json()["run"]["id"]
    client.portal.call(asyncio.sleep, 0.15)
    assert client.get(f"/api/v1/runs/{run_id}").json()["state"] == "running"
    assert state["vision"] == state["text"] == []
    client.portal.call(context.inference.release)
    assert client.get(f"/api/v1/runs/{run_id}?wait=5").json()["state"] == "completed"
    assert len(state["vision"]) == 1 and len(state["text"]) == 2

    request = json.loads(state["vision"][0])
    assert set(request) == {"model", "stream", "temperature", "max_tokens", "messages"}
    assert request["model"] == PIN and not request["stream"] and request["max_tokens"] == 512
    assert request["messages"][0] == {"role": "system", "content": INSTRUCTION}
    question, image = request["messages"][1]["content"]
    assert question == {"type": "text", "text": QUESTION}
    raw = base64.b64decode(image["image_url"]["url"].split(",", 1)[1])
    with Image.open(BytesIO(raw)) as png:
        assert png.size == (80, 64) and png.getpixel((5, 5)) == (128, 0, 128)

    prompts = client.get(f"/api/v1/runs/{run_id}/prompts").json()["items"]
    assert [(p["record"]["purpose"], p["record"]["index"]) for p in prompts] == [
        ("vision", 1),
        ("conversation", 2),
        ("conversation", 3),
    ]
    request_row = prompts[0]["record"]

    def read(path: str) -> httpx2.Response:
        return client.get(f"/api/v1/runs/{run_id}/{path}")

    assert read(request_row["body"]["path"]).content == state["vision"][0]
    manifest = read(request_row["manifest"]["path"]).json()
    assert manifest["image"]["path"] == f"artifacts/{identity}"
    assert read(f"artifacts/{identity}?preview=true").content == raw
    assert read(f"artifacts/{identity}?preview=true").headers["Content-Type"] == "image/png"
    rows = client.get(f"/api/v1/runs/{run_id}/records").json()["items"]
    step = next(
        row["record"]
        for row in rows
        if row["record"].get("purpose") == "vision" and row["kind"] == "model"
    )
    assert step["request_id"] == request_row["id"] and step["content"] == OBSERVATION
    assert read(step["response"]["path"]).json()["model"] == PIN
    for body in state["text"]:
        assert all(isinstance(m["content"], str) for m in body["messages"])
        support = next(m for m in body["messages"] if OBSERVATION in m["content"])
        assert support["role"] == "user"
        assert json.loads(support["content"]) == {"untrusted_visual_observation": OBSERVATION}

    exported = client.get(f"/api/v1/sessions/{session}/export")
    assert exported.status_code == 200, exported.text[:200]
    imported = client.post(
        "/api/v1/sessions/import",
        headers={"Content-Type": "application/zip", "Idempotency-Key": new_id()},
        content=exported.content,
    )
    assert imported.status_code == 201, imported.text
    copy = imported.json()["session_id"]
    messages = client.get(f"/api/v1/sessions/{copy}/messages").json()["items"]
    copied_input = next(row["record"] for row in messages if row["record"]["role"] == "user")
    copied_run = copied_input["run_id"]
    copied_image = copied_input["image"]["path"]
    assert copied_run != run_id and copied_image != f"artifacts/{identity}"
    assert client.get(f"/api/v1/runs/{copied_run}/{copied_image}?preview=true").content == raw
    copied_request = client.get(f"/api/v1/runs/{copied_run}/prompts").json()["items"][0]["record"]
    assert (
        client.get(f"/api/v1/runs/{copied_run}/{copied_request['body']['path']}").content
        == (state["vision"][0])
    )
    assert len(state["vision"]) == 1

    follow = submit(client, session, question="Repeat that.")
    assert follow.json()["run"]["state"] == "completed" and len(state["vision"]) == 1
    assert OBSERVATION in json.dumps(state["text"][-1])
    assert "image_url" not in json.dumps(state["text"][-1])

    def summary_body() -> bytes:
        assert context.store and context.model
        current = Run.model_validate_json(json.dumps(follow.json()["run"]))
        compactor = Compactor(context.store, context.model, lambda admitted: admitted)
        selection = select(context.store, current, 65536)
        prompt, _ = compactor.summary_prompt(
            context.profile, current, selection, [context.store.run(run_id)]
        )
        return prompt.body

    summary = client.portal.call(summary_body)
    assert OBSERVATION.encode() in summary and b"image_url" not in summary
    reattached = submit(
        client,
        session,
        {"run_id": run_id, "artifact_id": identity},
        question="Look at it again. private-vision-key",
    )
    assert reattached.json()["run"]["state"] == "completed"
    second = json.loads(state["vision"][1])
    assert len(second["messages"]) == 2
    assert second["messages"][1]["content"][0]["text"] == "Look at it again. <redacted>"
    assert second["messages"][1]["content"][1] == image


def test_vision_upload_auth_scope_expiry_limits_and_idempotency(
    vision: tuple[TestClient, ApplicationContext, dict[str, Any]],
) -> None:
    client, context, state = vision
    path = f"/api/v1/sessions/{new_id()}/image-uploads"
    png_headers = {"Content-Type": "image/png"}
    assert client.post(path, headers=png_headers, content=picture()).status_code == 401
    login(client)
    session, other = topic(client), topic(client)
    path = f"/api/v1/sessions/{session}/image-uploads"
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post(path, headers=png_headers, content=picture()).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert client.post(path, content=picture()).status_code == 415
    assert client.post(path, content=b"not an image", headers=png_headers).status_code == 422
    oversized = client.post(path, content=iter([b" " * UPLOAD_BYTES, b"x"]), headers=png_headers)
    assert oversized.status_code == 413
    staging = context.profile.data_dir / "image-uploads"
    identity, expired = upload(client, session), upload(client, session)
    stale = time.time() - UPLOAD_TTL - 10
    os.utime(staging / f"{session}-{expired}", (stale, stale))
    assert submit(client, session, {"upload_id": expired}).status_code == 404
    assert submit(client, other, {"upload_id": identity}).status_code == 404
    assert client.get(f"/api/v1/sessions/{other}/image-uploads/{identity}").status_code == 404

    key = new_id()
    first = submit(client, session, {"upload_id": identity}, key=key)
    assert first.json()["run"]["state"] == "completed"
    assert not (staging / f"{session}-{identity}").exists()
    assert submit(client, session, {"upload_id": identity}, key=key).json() == first.json()
    assert submit(client, session, {"upload_id": expired}, key=key).status_code == 409
    artifact = {"run_id": first.json()["run"]["id"], "artifact_id": identity}
    assert submit(client, other, artifact).status_code == 403
    assert len(state["vision"]) == 1
    for _ in range(4):
        upload(client, session)
    assert not (staging / f"{session}-{expired}").exists()
    assert client.post(path, headers=png_headers, content=picture()).status_code == 429


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("timeout", "vision_timeout"),
        ("mismatch", "vision_model_mismatch"),
        ("long", "invalid_vision_response"),
        ("oversize", "invalid_vision_response"),
        ("unavailable", "vision_unavailable"),
    ],
)
def test_vision_fails_image_turn_and_text_remains_available(
    vision: tuple[TestClient, ApplicationContext, dict[str, Any]], mode: str, code: str
) -> None:
    client, _, state = vision
    login(client, bearer=True)
    session = topic(client)
    state["mode"] = mode
    response = submit(client, session, {"upload_id": upload(client, session)})
    run = response.json()["run"]
    assert run["state"] == "failed" and run["reason"]["code"] == code, run
    assert not state["text"]
    if mode == "timeout":
        assert state["settled"].is_set()
    rows = client.get(f"/api/v1/runs/{run['id']}/records").json()["items"]
    step = next(r["record"] for r in rows if r["kind"] == "model")
    assert step["purpose"] == "vision" and not step["content"]
    assert step["response"]["length"] <= 65536
    assert submit(client, session).json()["run"]["state"] == "completed"
    assert len(state["vision"]) == len(state["text"]) == 1
    assert "image_url" not in json.dumps(state["text"][0])


def test_cancel_vision_settles_request_and_run(
    vision: tuple[TestClient, ApplicationContext, dict[str, Any]],
) -> None:
    client, _, state = vision
    login(client, bearer=True)
    session = topic(client)
    state["mode"] = "cancel"
    response = submit(client, session, {"upload_id": upload(client, session)}, wait=0)
    run_id = response.json()["run"]["id"]
    assert state["started"].wait(timeout=3)
    client.post(f"/api/v1/runs/{run_id}/cancel").raise_for_status()
    assert client.get(f"/api/v1/runs/{run_id}?wait=5").json()["state"] == "cancelled"
    assert state["settled"].is_set() and not state["text"]
    rows = client.get(f"/api/v1/runs/{run_id}/records").json()["items"]
    step = next(r["record"] for r in rows if r["kind"] == "model")
    assert step["purpose"] == "vision" and step["failure"]["code"] == "cancelled"
    assert submit(client, session).json()["run"]["state"] == "completed"


def test_missing_vision_credential_leaves_text_available(
    vision: tuple[TestClient, ApplicationContext, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, state = vision
    monkeypatch.delenv("FIXTURE_VISION_KEY")
    login(client, bearer=True)
    session = topic(client)
    run = submit(client, session, {"upload_id": upload(client, session)}).json()["run"]
    assert run["state"] == "failed" and run["reason"]["code"] == "vision_unavailable"
    assert state["vision"] == state["text"] == []
    assert submit(client, session).json()["run"]["state"] == "completed"


def test_vision_strips_metadata_from_existing_artifact(
    vision: tuple[TestClient, ApplicationContext, dict[str, Any]],
) -> None:
    client, context, state = vision
    login(client, bearer=True)
    session = topic(client)
    source = Run.model_validate_json(json.dumps(submit(client, session).json()["run"]))
    metadata = PngInfo()
    metadata.add_text("workflow", "private generation context")
    identity = new_id()
    assert client.portal

    def seed() -> None:
        assert context.store
        artifact = Artifact.model_validate(
            context.store.artifact(identity, picture(pnginfo=metadata))
            | {"media_type": "image/png"}
        )
        context.store.append(
            Message(
                origin=source.origin,
                session_id=source.session_id,
                run_id=source.id,
                role="assistant",
                content="Image artifact",
                image=artifact,
            )
        )

    client.portal.call(seed)
    image = {"run_id": source.id, "artifact_id": identity}
    assert submit(client, session, image).json()["run"]["state"] == "completed"
    request = json.loads(state["vision"][0])
    url = request["messages"][1]["content"][1]["image_url"]["url"]
    with Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1]))) as png:
        assert png.size == (80, 64) and not png.info


def test_normalize_orientation_and_reject_animated_corrupt_or_overlarge() -> None:
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private comment"
    normalized = normalize_upload(picture("JPEG", exif=exif), "image/jpeg")
    with Image.open(BytesIO(normalized)) as image:
        assert image.format == "PNG" and image.size == (64, 80)
        assert not image.info
    with pytest.raises(ValueError):
        normalize_upload(picture()[:-1], "image/png")
    with pytest.raises(ValueError):
        normalize_upload(picture() + picture(), "image/png")
    with pytest.raises(ValueError):
        normalize_upload(picture("JPEG")[:-5], "image/jpeg")
    with pytest.raises(ValueError):
        normalize_upload(picture("JPEG"), "image/png")
    oversize = BytesIO()
    Image.new("L", (8193, 1)).save(oversize, "PNG")
    with pytest.raises(ValueError):
        normalize_upload(oversize.getvalue(), "image/png")
    animated = picture("PNG", save_all=True, append_images=[Image.new("RGB", (80, 64), "red")])
    with pytest.raises(ValueError):
        normalize_upload(animated, "image/png")


def test_webp_orientation_and_invalid_containers() -> None:
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private comment"
    still = picture("WEBP", exif=exif)
    with Image.open(BytesIO(normalize_upload(still, "image/webp"))) as image:
        assert image.format == "PNG" and image.size == (64, 80)
        assert not image.info

    animated = picture("WEBP", save_all=True, append_images=[Image.new("RGB", (80, 64), "red")])
    first_frame = animated[: animated.rindex(b"ANMF")]
    first_frame = first_frame[:4] + (len(first_frame) - 8).to_bytes(4, "little") + first_frame[8:]
    with Image.open(BytesIO(first_frame)) as image:
        assert getattr(image, "n_frames", 0) == 1
    for raw in (still[:-1], still + b"x", picture(), animated, first_frame):
        with pytest.raises(ValueError):
            normalize_upload(raw, "image/webp")


def test_vision_config_requires_pin_and_excludes_native() -> None:
    vision = {"pin": PIN, "base_url": "http://vision.example/v1"}
    with pytest.raises(ValidationError):
        OperatorConfig.model_validate({"vision": vision | {"pin": ""}})
    with pytest.raises(ValidationError):
        OperatorConfig.model_validate({"vision": vision, "model": {"image_inputs": True}})
