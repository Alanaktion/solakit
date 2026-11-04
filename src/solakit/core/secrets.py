"""Central recursive redaction for diagnostic records and logging."""

import logging
import re
from typing import Any

SENSITIVE = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD)$", re.IGNORECASE)


class Redactor:
    def __init__(self, values: list[str] = ()) -> None:
        self.values = tuple(v for v in values if v)

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
        record.msg = self.redactor.redact(record.getMessage())
        record.args = ()
        return True
