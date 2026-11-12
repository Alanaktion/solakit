"""Conservative detection of short, action-only final replies."""

import re

_ACTION = re.compile(
    r"^(?:(?:I'll|I will|Let me|I'm going to)\s+"
    r"(?:check|read|run|search|look|inspect|verify|load|fetch|use)\b"
    r"|(?:Checking|Reading|Running|Searching|Inspecting|Verifying|Loading|Fetching)\b)",
    re.IGNORECASE,
)


def unfinished_action(text: str) -> bool:
    """Only a single short announcement; never classify a substantive report."""
    text = text.strip()
    return (
        0 < len(text) <= 240
        and "\n" not in text
        and len(re.split(r"[.!?]\s+", text)) == 1
        and bool(_ACTION.match(text))
    )
