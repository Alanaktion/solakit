"""The supported surface for tool packs (sola_packs.*). Everything else in
solakit is internal and may change without notice.

Packs may declare ``SOLA_SDK = ">=1,<2"`` at module level; loading fails with a
clear message when the running SDK_VERSION does not satisfy it.
"""

from solakit.core.config import SecretRef
from solakit.core.io import durable
from solakit.core.search import timestamp
from solakit.core.store import sync_directory
from solakit.services.git import repository_lock
from solakit.services.process import execute
from solakit.tools import ToolContext, ToolError, ToolResult, tool, tool_json
from solakit.tools.ports import (
    ChannelPoster,
    JobManagerPort,
    MemoryStager,
    NestedSubmit,
    RuntimeStatus,
    ToolActivation,
)

SDK_VERSION = "1.0"

__all__ = [
    "SDK_VERSION",
    "ChannelPoster",
    "JobManagerPort",
    "MemoryStager",
    "NestedSubmit",
    "RuntimeStatus",
    "SecretRef",
    "ToolActivation",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "durable",
    "execute",
    "repository_lock",
    "satisfies",
    "sync_directory",
    "timestamp",
    "tool",
    "tool_json",
]


def satisfies(spec: str, version: str = SDK_VERSION) -> bool:
    """Comma-separated >=, >, <=, <, == clauses over dotted integer versions."""
    import re

    def parse(text):
        return tuple(int(part) for part in text.split("."))

    current = parse(version)
    for clause in spec.split(","):
        match = re.fullmatch(r"\s*(>=|<=|==|>|<)\s*([0-9]+(?:\.[0-9]+)*)\s*", clause)
        if not match:
            raise ValueError(f"invalid SOLA_SDK requirement: {spec}")
        operator, wanted = match[1], parse(match[2])
        width = max(len(current), len(wanted))
        left = current + (0,) * (width - len(current))
        right = wanted + (0,) * (width - len(wanted))
        if not {
            ">=": left >= right,
            ">": left > right,
            "<=": left <= right,
            "<": left < right,
            "==": left == right,
        }[operator]:
            return False
    return True
