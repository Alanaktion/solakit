"""Helpers for request messages that carry the stable-layout turn block."""

import re

TURN = re.compile(r"\A<turn>\n.*?\n</turn>\n\n", re.S)


def plain(content):
    """The user's verbatim text without the request-time ``<turn>`` preamble."""
    if isinstance(content, str):
        return TURN.sub("", content, count=1)
    if isinstance(content, list):
        return [
            {**part, "text": plain(part["text"])}
            if part.get("type") == "text"
            else part
            for part in content
        ]
    return content
