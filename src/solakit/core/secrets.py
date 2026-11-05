"""Central recursive redaction for diagnostic records and logging."""

import logging
import re
import traceback
from bisect import bisect_left, bisect_right
from typing import Any

SENSITIVE = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD)$", re.IGNORECASE)


class Redactor:
    def __init__(self, values: list[str] = ()) -> None:
        self.values = tuple(v for v in values if v)

    def preview(self, value: str) -> str:
        """Mask full secrets and withhold an unfinished secret at the live edge."""
        value = self.redact(value)
        hold = max(
            (
                size
                for secret in self.values
                for size in range(1, min(len(secret), len(value) + 1))
                if value.endswith(secret[:size])
            ),
            default=0,
        )
        return value[:-hold] if hold else value

    def excerpt(self, content: str, window: str) -> str:
        """Mask even secret fragments clipped at an excerpt's boundaries."""
        if not window:
            return ""
        positions = []
        at = content.find(window)
        while at >= 0:
            positions.append(at)
            at = content.find(window, at + 1)
        if not positions:
            return self.redact(window)
        start, end = positions[0], positions[0] + len(window)
        spans = []
        for secret in self.values:
            at = content.find(secret)
            while at >= 0:
                # Identical excerpts may occur more than once. Mask the union
                # rather than guessing which occurrence SQLite selected.
                first = bisect_right(positions, at - len(window))
                last = bisect_left(positions, at + len(secret))
                for position in positions[first:last]:
                    left = max(position, at)
                    right = min(position + len(window), at + len(secret))
                    if left < right:
                        spans.append((left - position, right - position))
                at = content.find(secret, at + 1)
        merged = []
        for left, right in sorted(spans):
            if merged and left <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], right))
            else:
                merged.append((left, right))
        for left, right in reversed(merged):
            window = window[:left] + "[redacted]" + window[right:]
        return ("…" if start else "") + window + ("…" if end < len(content) else "")

    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: "[redacted]" if SENSITIVE.search(k) else self.redact(v)
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            for secret in self.values:
                value = value.replace(secret, "[redacted]")
        return value


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact structured key/value arguments before they become prose.
        if isinstance(record.args, dict):
            record.args = self.redactor.redact(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(self.redactor.redact(arg) for arg in record.args)
        # Formatters such as Uvicorn's AccessFormatter consume the original
        # argument tuple themselves. Keep its shape and numeric types intact.
        record.msg = self.redactor.redact(record.msg)
        if "color_message" in record.__dict__:
            record.color_message = self.redactor.redact(record.color_message)
        if record.exc_info:
            record.exc_text = self.redactor.redact(
                "".join(traceback.format_exception(*record.exc_info))
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self.redactor.redact(record.exc_text)
        return True
