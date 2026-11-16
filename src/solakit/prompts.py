"""Frozen prompt assembly and offline inspection; no model or server imports."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from solakit.config import EffectiveProfile
from solakit.evidence import canonical, checked_json, digest
from solakit.records import Message, RecordID, SessionMode, Timestamp, Value


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
    if profile.mode == SessionMode.EPHEMERAL and (frozen.history or frozen.summary):
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

    def add(section: str, role: str, content: str) -> None:
        sections.append({"section": section, "message_index": len(messages)})
        messages.append({"role": role, "content": content})

    def fragment(section: str, path: Path) -> None:
        raw = path.read_bytes()
        fragments.append(
            {"section": section, "path": str(path), "sha256": digest(raw), "text": raw.decode()}
        )
        add(section, "system", raw.decode())

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
    add("input", "user", frozen.input.content)
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
    # shortcut: byte estimates cannot enforce exact tokens; replace after tokenizer verification.
    estimated = (len(canonical({"messages": messages, "tools": tools})) + 3) // 4
    estimated += 32 * len(messages)
    headroom = (profile.budgets.tool_result_bytes + 3) // 4
    if estimated + profile.budgets.output_tokens + headroom > profile.model_context_tokens:
        raise ValueError("estimated context exceeds input/output/tool-result budget")
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
        "disclosure": "eager-v1",
        "tools_sha256": digest(canonical(tools)),
        "accounting": {
            "kind": "estimate: UTF-8 bytes/4 plus 32/message; no verified tokenizer",
            "input_tokens": estimated,
            "output_reserved": profile.budgets.output_tokens,
            "tool_result_reserved": headroom,
            "context_tokens": profile.model_context_tokens,
        },
    }
    return PreparedPrompt(body=body, manifest=canonical(manifest))


def inspect_prompt(prompt: PreparedPrompt, *, hashes: bool = False) -> dict[str, object]:
    """Explicit private inspection includes content; ordinary logs never use this."""
    manifest = checked_json(prompt.manifest)
    if not isinstance(manifest, dict) or manifest.get("body_sha256") != digest(prompt.body):
        raise ValueError("request manifest hash mismatch")
    result = {"request": checked_json(prompt.body), "manifest": manifest}
    if hashes:
        result["hashes"] = {"body": digest(prompt.body), "manifest": digest(prompt.manifest)}
    return result
