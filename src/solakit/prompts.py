"""Frozen prompt assembly and offline inspection; no model or server imports."""

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from pydantic import JsonValue

from solakit.config import EffectiveProfile
from solakit.evidence import canonical, checked_json, digest
from solakit.records import Artifact, Message, RecordID, SessionMode, Timestamp, ToolProposal, Value


class PromptTurn(Value):
    source_id: RecordID
    session_id: RecordID
    role: str
    content: str
    image: Artifact | None = None
    tool_calls: tuple[ToolProposal, ...] = ()
    tool_call_id: str | None = None

    def message(self) -> dict[str, JsonValue]:
        if self.role not in ("user", "assistant", "tool", "system"):
            raise ValueError("invalid projected role")
        if (self.role == "tool") != (self.tool_call_id is not None):
            raise ValueError("projected tool result requires a provider call ID")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistants propose tools")
        result: dict[str, JsonValue] = {"role": self.role, "content": self.content}
        if self.tool_call_id is not None:
            result["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            result["tool_calls"] = [
                {
                    "id": call.provider_tool_call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return result


class PromptInput(Value):
    at: Timestamp
    input: Message
    history: tuple[Message, ...] = ()
    summary: Message | None = None
    context_boundary: RecordID | None = None
    recall: str | None = None
    tools: tuple[dict[str, JsonValue], ...] = ()
    stream: bool = True
    temperature: float = 0.0
    top_p: float = 1.0
    enable_thinking: bool = True
    prior_turns: tuple[PromptTurn, ...] = ()
    continuation: tuple[PromptTurn, ...] = ()
    disclosure: str = "eager-v1"
    tool_catalog: tuple[str, ...] = ()
    compaction: dict[str, JsonValue] | None = None


class ContextLimit(ValueError):
    pass


class Accounting(TypedDict):
    kind: str
    components: dict[str, int]
    input_tokens: int
    output_reserved: int
    tool_result_reserved: int
    tool_result_policy: str
    safety_reserved: int
    context_tokens: int
    total_reserved: int


@dataclass(frozen=True)
class PreparedPrompt:
    body: bytes
    manifest: bytes


def assemble(profile: EffectiveProfile, frozen: PromptInput) -> PreparedPrompt:
    """No clock, retrieval or implicit context trimming; all selections are explicit."""
    if frozen.input.role != "user" or frozen.input.origin != profile.origin:
        raise ValueError("input must be a user message with the admitted origin")
    if profile.mode != SessionMode.SHARED and frozen.recall is not None:
        raise ValueError("recall is unavailable in isolated/ephemeral mode")
    if profile.mode == SessionMode.EPHEMERAL and (
        frozen.history or frozen.summary or frozen.prior_turns
    ):
        raise ValueError("ephemeral prompts cannot carry continuing context")
    if (frozen.summary is None) != (frozen.context_boundary is None):
        raise ValueError("summary and context boundary must be supplied together")
    if frozen.summary is not None and frozen.summary.role != "assistant":
        raise ValueError("summary must be an assistant message")
    selected = [*([frozen.summary] if frozen.summary else []), *frozen.history, frozen.input]
    if len({message.id for message in selected}) != len(selected) or any(
        message.session_id != frozen.input.session_id for message in selected
    ):
        raise ValueError("selected messages must be distinct and in the input session")
    # Tool history needs the provider IDs and complete assistant calls (Phase 6).
    if any(message.role == "tool" for message in selected):
        raise ValueError("tool history requires the Phase 6 conversation projection")
    if not 0 <= frozen.temperature <= 2 or not 0 < frozen.top_p <= 1:
        raise ValueError("invalid sampling parameters")
    messages: list[dict[str, JsonValue]] = []
    sections: list[dict[str, object]] = []
    fragments: list[dict[str, object]] = []
    images: list[dict[str, object]] = []

    def attach(image: Artifact | None) -> None:
        if image is None:
            return
        if not profile.model_image_inputs:
            raise ValueError("Image inputs are unsupported for the configured model")
        if any(existing["sha256"] == image.sha256 for existing in images):
            return
        from solakit.images import MAX_IMAGE_BYTES, validate_png

        path = profile.data_dir / image.path
        if image.media_type != "image/png" or image.length > MAX_IMAGE_BYTES or path.is_symlink():
            raise ValueError("Invalid image artifact")
        with path.open("rb") as stream:
            raw = stream.read(MAX_IMAGE_BYTES + 1)
        if len(raw) != image.length or digest(raw) != image.sha256:
            raise ValueError("Image artifact hash mismatch")
        width = int.from_bytes(raw[16:20])
        height = int.from_bytes(raw[20:24])
        if not 64 <= width <= 2048 or not 64 <= height <= 2048:
            raise ValueError("Image dimensions exceed input bounds")
        validate_png(raw, width, height)
        messages[-1]["content"] = [
            {"type": "text", "text": messages[-1]["content"]},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + base64.b64encode(raw).decode()},
            },
        ]
        images.append(image.model_dump(mode="json") | {"width": width, "height": height})

    def add(section: str, role: str, content: str) -> None:
        sections.append({"section": section, "message_index": len(messages)})
        messages.append({"role": role, "content": content})

    def fragment(section: str, path: Path) -> None:
        raw = path.read_bytes()
        fragments.append(
            {"section": section, "path": str(path), "sha256": digest(raw), "text": raw.decode()}
        )
        add(section, "system", raw.decode())

    def project(section: str, turns: tuple[PromptTurn, ...]) -> None:
        pending: set[str] = set()
        for turn in turns:
            if turn.session_id != frozen.input.session_id:
                raise ValueError("projected turn crosses sessions")
            if turn.role == "tool":
                if turn.tool_call_id not in pending:
                    raise ValueError("orphan projected tool result")
                pending.remove(turn.tool_call_id)
            else:
                if pending:
                    raise ValueError("projected tool exchange is incomplete")
                pending = {call.provider_tool_call_id for call in turn.tool_calls}
                if len(pending) != len(turn.tool_calls):
                    raise ValueError("duplicate projected tool ID")
            sections.append({"section": section, "message_index": len(messages)})
            messages.append(turn.message())
            attach(turn.image)
        if pending:
            raise ValueError("projected tool exchange is incomplete")

    if profile.soul_file is not None:
        fragment("soul", profile.soul_file)
    if profile.curated_memory_file is not None:
        fragment("curated_memory", profile.curated_memory_file)
    for path in profile.fragments:
        fragment("profile", path)
    if frozen.summary is not None:
        add("summary", "assistant", frozen.summary.content)
    for message in frozen.history:
        add("history", message.role, message.content)
        attach(message.image)
    project("history", frozen.prior_turns)
    if frozen.recall is not None:
        add("recorded_recall", "user", frozen.recall)
        messages[-1]["name"] = "recorded_recall"
    add(
        "metadata",
        "system",
        canonical(
            {
                "at": frozen.at.isoformat(),
                "origin": profile.origin,
                "mode": profile.mode,
                "profile": profile.name,
            }
        ).decode(),
    )
    if frozen.tool_catalog:
        add(
            "tool_catalog",
            "system",
            "Authorized tool names with deferred schemas: "
            + ", ".join(frozen.tool_catalog)
            + ". Request an identifiable tool to disclose its schema before execution.",
        )
    add("input", "user", frozen.input.content)
    attach(frozen.input.image)
    project("continuation", frozen.continuation)
    named_tools = []
    for tool in frozen.tools:
        function = tool.get("function")
        if (
            tool.get("type") != "function"
            or not isinstance(function, dict)
            or not isinstance(function.get("name"), str)
            or not function["name"]
            or not isinstance(function.get("parameters"), dict)
        ):
            raise ValueError("tools require named function schemas")
        named_tools.append((str(function["name"]), tool))
    if len({name for name, _ in named_tools}) != len(named_tools):
        raise ValueError("duplicate tool schema")
    tools = [tool for _, tool in sorted(named_tools, key=lambda pair: pair[0])]
    request: dict[str, object] = {
        "model": profile.model_pin,
        "messages": messages,
        "stream": frozen.stream,
        "temperature": frozen.temperature,
        "top_p": frozen.top_p,
        "max_tokens": profile.budgets.output_tokens,
        "chat_template_kwargs": {"enable_thinking": frozen.enable_thinking},
    }
    if tools:
        request["tools"] = tools
    if frozen.stream:
        request["stream_options"] = {"include_usage": True}
    accounting = account(
        profile, messages, tools, sections, tool_headroom=bool(tools or frozen.tool_catalog)
    )
    if accounting["total_reserved"] > profile.model_context_tokens:
        raise ContextLimit("estimated context exceeds input/output/tool-result budget")
    body = canonical(request)
    manifest = {
        "version": 1,
        "session_id": frozen.input.session_id,
        "run_id": frozen.input.run_id,
        "origin": frozen.input.origin,
        "endpoint": profile.model_base_url.rstrip("/") + "/chat/completions",
        "timeouts": {
            "connect_seconds": profile.model_connect_timeout_seconds,
            "generation_seconds": profile.model_generation_timeout_seconds,
        },
        "retries": {"sdk": 0, "application": 0},
        "placement": "sola-prompt-v1",
        "body_sha256": digest(body),
        "sections": sections,
        "fragments": fragments,
        "selected_message_ids": [message.id for message in selected],
        "context_boundary": frozen.context_boundary,
        "recall": "recorded" if frozen.recall is not None else "omitted",
        "recall_sha256": digest(frozen.recall.encode()) if frozen.recall is not None else None,
        "disclosure": frozen.disclosure,
        "tools_sha256": digest(canonical(tools)),
        "accounting": accounting,
    }
    if frozen.prior_turns or frozen.continuation:
        manifest["projected_record_ids"] = [
            turn.source_id for turn in (*frozen.prior_turns, *frozen.continuation)
        ]
    if frozen.compaction is not None:
        manifest["compaction"] = frozen.compaction
    if images:
        manifest["images"] = images
    return PreparedPrompt(body=body, manifest=canonical(manifest))


def account(
    profile: EffectiveProfile,
    messages: list[dict[str, JsonValue]],
    tools: list[dict[str, JsonValue]],
    sections: list[dict[str, object]],
    *,
    tool_headroom: bool = True,
) -> Accounting:
    # shortcut: UTF-8 bytes plus framing is conservative; replace after tokenizer verification.
    components: dict[str, int] = {}
    messages = [dict(message) for message in messages]
    image_reserve = 0
    for message in messages:
        if isinstance(message["content"], list):
            parts = message["content"]
            image_reserve += 16384 * sum(
                isinstance(part, dict) and part.get("type") == "image_url" for part in parts
            )
            message["content"] = [
                part
                for part in parts
                if not isinstance(part, dict) or part.get("type") != "image_url"
            ]
    for message, section in zip(messages, sections, strict=True):
        name = str(section["section"])
        components[name] = components.get(name, 0) + len(canonical(message)) + 32
    estimated = (
        len(canonical({"messages": messages, "tools": tools})) + 32 * len(messages) + image_reserve
    )
    headroom = min(profile.budgets.tool_result_bytes, profile.model_context_tokens // 8)
    headroom = headroom if tool_headroom else 0
    safety = (profile.model_context_tokens + 9) // 10
    return {
        "kind": "estimate: UTF-8 bytes plus 32/message; no verified tokenizer"
        + ("; 16384/image unverified reserve" if image_reserve else ""),
        "components": components
        | {
            "tool_schemas": len(canonical(tools)),
            **({"images": image_reserve} if image_reserve else {}),
        },
        "input_tokens": estimated,
        "output_reserved": profile.budgets.output_tokens,
        "tool_result_reserved": headroom,
        "tool_result_policy": "expected headroom capped at window/8; actual results rechecked",
        "safety_reserved": safety,
        "context_tokens": profile.model_context_tokens,
        "total_reserved": estimated + profile.budgets.output_tokens + headroom + safety,
    }


def inspect_prompt(prompt: PreparedPrompt, *, hashes: bool = False) -> dict[str, object]:
    """Explicit private inspection includes content; ordinary logs never use this."""
    manifest = checked_json(prompt.manifest)
    if not isinstance(manifest, dict) or manifest.get("body_sha256") != digest(prompt.body):
        raise ValueError("request manifest hash mismatch")
    result = {"request": checked_json(prompt.body), "manifest": manifest}
    if hashes:
        result["hashes"] = {"body": digest(prompt.body), "manifest": digest(prompt.manifest)}
    return result
