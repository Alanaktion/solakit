"""Named ComfyUI workflows; never replay an ambiguous generation request."""

import json
import re
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from pydantic import Field, field_validator, model_validator
from sola_packs.support import IDENTIFIER, Settings, external, resource, secret
from sola_packs.support.evidence import Lease, Operation

from solakit.core.config import SecretRef
from solakit.core.io import durable
from solakit.tools import ToolContext, ToolError, ToolResult, tool

PROMPT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class Image(Settings):
    base_url: str = "http://127.0.0.1:8188"
    workflow: Path
    prompt_node: str
    prompt_input: str = "text"
    seed_node: str
    seed_input: str = "seed"
    size_node: str | None = None
    width_node: str | None = None
    width_input: str = "width"
    height_node: str | None = None
    height_input: str = "height"
    token: SecretRef | None = None
    timeout_s: float = Field(default=15, gt=0, le=60)
    max_response_bytes: int = Field(default=1_000_000, ge=1, le=4_000_000)

    @model_validator(mode="after")
    def dimension_mapping(self):
        if self.size_node is not None:
            self.width_node = self.width_node or self.size_node
            self.height_node = self.height_node or self.size_node
        if (self.width_node is None) != (self.height_node is None):
            raise ValueError("Configure both width_node and height_node")
        mappings = [
            (self.prompt_node, self.prompt_input),
            (self.seed_node, self.seed_input),
        ]
        if self.width_node is not None:
            mappings += [
                (self.width_node, self.width_input),
                (self.height_node, self.height_input),
            ]
        if len(set(mappings)) != len(mappings):
            raise ValueError("Configured workflow inputs must be distinct")
        return self

    @field_validator("workflow", mode="before")
    @classmethod
    def absolute(cls, value):
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("Workflow must be an absolute path")
        return path

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value):
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Use an HTTP(S) endpoint without credentials/query/fragment"
            )
        return value.rstrip("/")


def request(cfg, headers, method, path, body=None):
    # No redirect/retry: credentials stay on the configured endpoint and a POST
    # that loses its acknowledgement remains ambiguous, even on cancellation.
    deadline = time.monotonic() + cfg.timeout_s
    with httpx.Client(timeout=cfg.timeout_s, follow_redirects=False) as client:
        with client.stream(
            method, cfg.base_url + path, headers=headers, json=body
        ) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() >= deadline:
                    raise TimeoutError("ComfyUI response deadline exceeded")
                content.extend(chunk)
                if len(content) > cfg.max_response_bytes:
                    raise ValueError("Oversized ComfyUI response")
            return json.loads(content)


def headers(ctx, cfg):
    return {"Authorization": "Bearer " + secret(ctx, cfg.token)} if cfg.token else {}


def submit(ctx, cfg, auth, workflow, workflow_name, operation_id):
    identity = [workflow_name, operation_id]
    fingerprint = {"endpoint": cfg.base_url, "workflow": workflow}
    with Operation(ctx, "image", identity, fingerprint) as operation:
        previous = operation.current
        if previous:
            if previous["stage"] == "submitted":
                return previous["result"]
            raise ToolError(
                "Generation pending or uncertain; inspect receipt/server first"
            )
        operation.record("prepared", endpoint=cfg.base_url)
        try:
            reply = request(cfg, auth, "POST", "/prompt", {"prompt": workflow})
            prompt_id = reply["prompt_id"]
            if not isinstance(prompt_id, str) or not PROMPT_ID.fullmatch(prompt_id):
                raise ValueError("Invalid prompt identity")
            if reply.get("node_errors"):
                raise ValueError("Workflow node errors")
            result = {
                "status": "submitted",
                "workflow": workflow_name,
                "operation_id": operation_id,
                "prompt_id": prompt_id,
            }
            operation.record("submitted", endpoint=cfg.base_url, result=result)
        except Exception as exc:
            operation.record("uncertain", endpoint=cfg.base_url)
            raise ToolError(
                "Generation uncertain; inspect receipt/server before retrying"
            ) from exc
        return result


@tool(
    summary="Submit a named image workflow with prompt, seed and dimensions",
    keywords=("image", "generate", "picture", "workflow"),
    name="image.generate",
    profiles=["*"],
)
async def generate(
    ctx: ToolContext,
    workflow: Annotated[
        str,
        Field(
            description=(
                "Configured image workflow alias granted to this profile, not a "
                "graph filename."
            )
        ),
    ],
    prompt: Annotated[
        str,
        Field(
            description=(
                "Exact text prompt for the workflow; graph/model settings "
                "remain operator-owned."
            )
        ),
    ],
    seed: Annotated[
        int,
        Field(
            description=(
                "Explicit integer generation seed for reproducible input; do "
                "not invent a random seed on retry."
            )
        ),
    ],
    operation_id: Annotated[
        str,
        Field(
            description=(
                "Stable unique operation ID; reuse only for the identical "
                "request. Uncertain operations never auto-replay."
            )
        ),
    ],
    width: Annotated[
        int,
        Field(
            ge=64,
            le=4096,
            multiple_of=8,
            description="Output width in pixels; defaults to 768.",
        ),
    ] = 768,
    height: Annotated[
        int,
        Field(
            ge=64,
            le=4096,
            multiple_of=8,
            description="Output height in pixels; defaults to 768.",
        ),
    ] = 768,
) -> ToolResult:
    """Submit a named workflow once; width and height default to 768 pixels.

    Configured dimension inputs are overridden without editing the workflow file.
    Custom sizes require width/height mappings; legacy unmapped workflows retain
    their dimensions when both arguments use the defaults.
    """
    if ctx.config.profiles[ctx.profile].write == "none":
        raise ToolError("Image generation requires filesystem write tier")
    cfg = resource(ctx, "images", workflow, Image, "generate")
    if not IDENTIFIER.fullmatch(operation_id):
        raise ToolError("A stable operation ID is required")
    if not prompt.strip() or len(prompt) > 16000 or not 0 <= seed <= 2**64 - 1:
        raise ToolError("Prompt must contain 1–16000 characters; seed must be uint64")
    if any(
        type(value) is not int or not 64 <= value <= 4096 or value % 8
        for value in (width, height)
    ):
        raise ToolError("Width and height must be multiples of 8 from 64 to 4096")
    if cfg.width_node is None and (width, height) != (768, 768):
        raise ToolError("Custom dimensions require configured width/height mappings")
    text = await ctx.fs.read(cfg.workflow, max_chars=1_000_000)
    if not text.content_complete or len(text.content.encode()) > 1_000_000:
        raise ToolError("Workflow exceeds its byte limit")
    try:
        graph = json.loads(text.content)
        if not isinstance(graph, dict) or not 1 <= len(graph) <= 1000:
            raise ValueError("Invalid workflow graph")
        for node in graph.values():
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                raise ValueError("Invalid workflow node")
        prompt_inputs = graph[cfg.prompt_node]["inputs"]
        seed_inputs = graph[cfg.seed_node]["inputs"]
        if (
            not isinstance(prompt_inputs[cfg.prompt_input], str)
            or type(seed_inputs[cfg.seed_input]) is not int
        ):
            raise ValueError("Configured inputs have incorrect types")
        if cfg.width_node is not None:
            width_inputs = graph[cfg.width_node]["inputs"]
            height_inputs = graph[cfg.height_node]["inputs"]
            if (
                type(width_inputs[cfg.width_input]) is not int
                or type(height_inputs[cfg.height_input]) is not int
            ):
                raise ValueError("Configured dimensions must be integer inputs")
            width_inputs[cfg.width_input] = width
            height_inputs[cfg.height_input] = height
        prompt_inputs[cfg.prompt_input] = prompt
        seed_inputs[cfg.seed_input] = seed
    except (KeyError, ValueError, TypeError) as exc:
        raise ToolError(
            "Invalid API-format workflow or configured input nodes"
        ) from exc
    result = await durable(
        submit, ctx, cfg, headers(ctx, cfg), graph, workflow, operation_id
    )
    return ToolResult(content=json.dumps(result))


def receipt(ctx, workflow, operation_id, endpoint):
    with Lease(ctx, "image", [workflow, operation_id]) as lease:
        events = lease.events()
        if not events:
            raise ToolError("Unknown generation operation")
        last = events[-1]
        if last["stage"] != "submitted":
            raise ToolError(
                "Generation pending or uncertain; inspect receipt/server first"
            )
        if last.get("endpoint") != endpoint:
            raise ToolError("Generation endpoint changed; inspect the original server")
        prompt_id = last.get("result", {}).get("prompt_id", "")
        if not isinstance(prompt_id, str) or not PROMPT_ID.fullmatch(prompt_id):
            raise ToolError("Invalid generation receipt; inspect before retrying")
        return prompt_id


def outcome(cfg, auth, prompt_id):
    try:
        history = request(cfg, auth, "GET", "/history/" + prompt_id)
        if not isinstance(history, dict):
            raise ValueError("Invalid history")
        if prompt_id not in history:
            return {"status": "pending_or_missing", "prompt_id": prompt_id}
        entry = history[prompt_id]
        status = entry["status"]
        if status.get("status_str") == "error":
            return {"status": "failed", "prompt_id": prompt_id}
        if status.get("completed") is not True:
            return {"status": "pending", "prompt_id": prompt_id}
        outputs = []
        for node, output in entry["outputs"].items():
            for item in output.get("images", []):
                if not isinstance(item, dict) or not all(
                    isinstance(item.get(key), str)
                    for key in ("filename", "subfolder", "type")
                ):
                    raise ValueError("Invalid output metadata")
                outputs.append(
                    {
                        "node": node,
                        **{key: item[key] for key in ("filename", "subfolder", "type")},
                    }
                )
                if len(outputs) > 100:
                    raise ValueError("Too many outputs")
        return {"status": "completed", "prompt_id": prompt_id, "images": outputs}
    except (
        httpx.HTTPError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        TimeoutError,
    ) as exc:
        raise ToolError(
            "Cannot read generation result; no submission was retried"
        ) from exc


@tool(
    summary="Inspect an image generation operation without resubmitting",
    keywords=("image", "result", "generation", "status"),
    name="image.result",
    profiles=["*"],
)
async def result(
    ctx: ToolContext,
    workflow: Annotated[
        str,
        Field(
            description=(
                "Configured image workflow alias granted to this profile, not a "
                "graph filename."
            )
        ),
    ],
    operation_id: Annotated[
        str,
        Field(
            description=(
                "Stable unique operation ID; reuse only for the identical "
                "request. Uncertain operations never auto-replay."
            )
        ),
    ],
) -> ToolResult:
    """Inspect a receipted generation and return ComfyUI output image references."""
    cfg = resource(ctx, "images", workflow, Image, "read")
    if not IDENTIFIER.fullmatch(operation_id):
        raise ToolError("Invalid operation ID")
    prompt_id = await durable(receipt, ctx, workflow, operation_id, cfg.base_url)
    data = await durable(outcome, cfg, headers(ctx, cfg), prompt_id)
    return ToolResult(content=external("image:" + workflow, data))


def download_png(cfg, auth, reference):
    """Download a selected server output with fixed bounds; never accept a URL."""
    import io
    import warnings

    from PIL import Image as Raster

    filename, subfolder, kind = (
        reference[key] for key in ("filename", "subfolder", "type")
    )
    if (
        not filename
        or Path(filename).name != filename
        or "\\" in filename
        or "\x00" in filename + subfolder
        or "\\" in subfolder
        or Path(subfolder).is_absolute()
        or ".." in Path(subfolder).parts
        or kind != "output"
    ):
        raise ToolError("Cover must reference a safe ComfyUI output image")
    deadline = time.monotonic() + cfg.timeout_s
    try:
        with httpx.Client(timeout=cfg.timeout_s, follow_redirects=False) as client:
            with client.stream(
                "GET",
                cfg.base_url + "/view",
                headers=auth,
                params={"filename": filename, "subfolder": subfolder, "type": kind},
            ) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Image download deadline exceeded")
                    content.extend(chunk)
                    if len(content) > 20_000_000:
                        raise ValueError("Image download exceeds 20 MB")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Raster.DecompressionBombWarning)
            with Raster.open(io.BytesIO(content)) as picture:
                if picture.width * picture.height > 40_000_000:
                    raise ValueError("Image exceeds 40 megapixels")
                picture.load()
                output = io.BytesIO()
                picture.convert("RGBA" if "A" in picture.getbands() else "RGB").save(
                    output, format="PNG"
                )
                if output.tell() > 20_000_000:
                    raise ValueError("Converted image exceeds 20 MB")
                return output.getvalue()
    except (httpx.HTTPError, OSError, ValueError, Warning, TimeoutError) as exc:
        raise ToolError("Cannot download valid bounded cover image") from exc


async def completed_png(ctx, workflow: str, operation_id: str, image_index: int = 0):
    """Private composition seam: resolve a receipted output and return PNG bytes."""
    cfg = resource(ctx, "images", workflow, Image, "read")
    if not IDENTIFIER.fullmatch(operation_id) or not 0 <= image_index < 100:
        raise ToolError("Invalid image operation or output index")
    auth = headers(ctx, cfg)
    prompt_id = await durable(receipt, ctx, workflow, operation_id, cfg.base_url)
    data = await durable(outcome, cfg, auth, prompt_id)
    if data["status"] != "completed" or image_index >= len(data["images"]):
        raise ToolError("Cover generation is not complete or output is missing")
    return await durable(download_png, cfg, auth, data["images"][image_index])
