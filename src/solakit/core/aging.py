"""Request-time aging of old tool results (stored evidence never changes).

Deterministic and monotone: the stub for a message depends only on the message
list, and a result outside the newest ``keep_groups`` tool groups stays stubbed
as the conversation grows, so aging never breaks prefix reuse by flapping.
"""

import json

HEAD_CHARS = 300


def is_error(content: str) -> bool:
    if content.startswith("Error: "):
        return True
    if content.startswith('{"content":'):
        try:
            return bool(json.loads(content).get("error"))
        except ValueError:
            return False
    return False


def stub(name: str, content: str) -> str:
    head = content[:HEAD_CHARS].replace("\n", " ")
    return (
        f"[Earlier result of {name} ({len(content):,} chars) omitted to save "
        f"context. Re-run the tool if you still need it. First {HEAD_CHARS} "
        f"chars: {head}]"
    )


def age_tool_results(
    messages: list[dict], keep_groups: int = 2, min_chars: int = 1500
) -> tuple[list[dict], list[str]]:
    """(request messages, stubbed tool_call_ids). Errors and short results,
    and every result in the newest keep_groups groups, stay verbatim."""
    groups: list[list[int]] = []
    names: dict[str, str] = {}
    for index, message in enumerate(messages):
        if message.get("role") == "assistant" and message.get("tool_calls"):
            groups.append([])
            for call in message["tool_calls"]:
                names[call["id"]] = call["function"]["name"]
        elif message.get("role") == "tool" and groups:
            groups[-1].append(index)
    old = groups[: max(0, len(groups) - keep_groups)]
    result, stubbed = list(messages), []
    for indices in old:
        for index in indices:
            message = messages[index]
            content = message.get("content") or ""
            if len(content) < min_chars or is_error(content):
                continue
            name = names.get(message.get("tool_call_id"), "tool")
            result[index] = {**message, "content": stub(name, content)}
            stubbed.append(message.get("tool_call_id"))
    return result, stubbed
