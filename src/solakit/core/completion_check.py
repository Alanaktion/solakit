"""Conservative detection of short replies that end by announcing an action."""

import re

_VERBS = (
    "check|read|run|search|look|inspect|verify|load|fetch|use|find|see|try|test|"
    "list|open|call|get|pull|query|scan|locate|confirm|explore|retry|figure|grab"
)
_ACTION = re.compile(
    r"^(?:(?:now|first|next|so|ok|okay|then|alright),?\s+)?"
    rf"(?:(?:I'll|I will|Let me|I'm going to|I need to)\s+(?:\w+\s+)?(?:{_VERBS})\b"
    r"|(?:Checking|Reading|Running|Searching|Inspecting|Verifying|Loading|"
    r"Fetching|Looking|Finding|Trying|Testing|Listing|Opening)\b)",
    re.IGNORECASE,
)
# "Let me know…" closes a finished answer; it never announces a tool call.
_CLOSING = re.compile(r"^(?:\w+,?\s+)?let me know\b", re.IGNORECASE)


def unfinished_action(text: str) -> bool:
    """A short reply whose last sentence only announces a next step.

    Covers "Checking." and "That file doesn't exist. Let me see what's there."
    Long replies are substantive reports and are never classified.
    """
    text = text.strip()
    if not 0 < len(text) <= 400 or text.count("\n") > 2:
        return False
    last = re.split(r"(?<=[.!?])\s+", text)[-1].strip()
    return bool(_ACTION.match(last)) and not _CLOSING.match(last)
