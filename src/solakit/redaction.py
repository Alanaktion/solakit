"""Credentials are registered before use; diagnostics and tool outputs are scrubbed."""

from pydantic import JsonValue


class Redactor:
    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def register(self, *values: str) -> None:
        self._secrets.update(value for value in values if value)

    def redact(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "<redacted>")
        return text

    def value(self, value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {self.redact(key): self.value(item) for key, item in value.items()}
        return value
