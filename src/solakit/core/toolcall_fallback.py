"""Recover tool calls a server left in assistant content (fail-closed).

Used only when the server returned no ``tool_calls``, the request offered
tools, and ``[model].tool_call_fallback`` is enabled. Recognized forms:

- Hermes/Qwen JSON: ``<tool_call>{"name": ..., "arguments": {...}}</tool_call>``
- Qwen3-Coder XML: ``<tool_call><function=NAME><parameter=KEY>VAL</parameter>
  </function></tool_call>``

Only blocks that form the entire message, or trail after prose, are accepted;
anything inside a code fence, an unknown tool name, malformed JSON or a
non-object argument rejects the whole message (nothing is executed).
"""

import json
import re

BLOCK = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)
XML_FUNCTION = re.compile(r"\A<function=([^>\s]+)>\s*(.*?)\s*</function>\Z", re.S)
XML_PARAMETER = re.compile(r"<parameter=([^>\s]+)>\n?(.*?)\n?</parameter>", re.S)


def _parse_block(body: str) -> tuple[str, dict] | None:
    match = XML_FUNCTION.match(body)
    if match:
        name, inner = match.groups()
        arguments = {}
        rest = XML_PARAMETER.sub("", inner).strip()
        if rest:
            return None
        for key, value in XML_PARAMETER.findall(inner):
            if key in arguments:
                return None
            try:
                arguments[key] = json.loads(value)
            except ValueError:
                arguments[key] = value
        return name, arguments
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        return None
    arguments = data.get("arguments", data.get("parameters", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            return None
    if not isinstance(arguments, dict):
        return None
    return data["name"], arguments


def parse(content: str, offered: set[str]) -> list[dict] | None:
    """Synthesized OpenAI tool calls, or None when the content is not exactly
    prose followed by one or more well-formed tool-call blocks."""
    if not content or "<tool_call>" not in content:
        return None
    first = content.find("<tool_call>")
    prose, tail = content[:first], content[first:]
    if prose.count("```") % 2:
        return None  # The block starts inside an open code fence.
    if BLOCK.sub("", tail).strip():
        return None  # Text after/between blocks: not a trailing call group.
    calls = []
    for index, body in enumerate(BLOCK.findall(tail)):
        parsed = _parse_block(body)
        if parsed is None or parsed[0] not in offered:
            return None
        name, arguments = parsed
        calls.append(
            {
                "id": f"call_fb_{index}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        )
    return calls or None
