"""Credentials are registered before use; diagnostics and tool outputs are scrubbed."""

from collections.abc import Iterable, Iterator

from pydantic import JsonValue


def _replace_stream(chunks: Iterable[str], secret: str) -> Iterator[str]:
    pending = ""
    for chunk in chunks:
        pending += chunk
        start = 0
        while (match := pending.find(secret, start)) != -1:
            yield pending[start:match] + "<redacted>"
            start = match + len(secret)
        split = max(start, len(pending) - len(secret) + 1)
        if split > start:
            yield pending[start:split]
        pending = pending[split:]
    if pending:
        yield pending


class Redactor:
    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def register(self, *values: str) -> None:
        self._secrets.update(value for value in values if value)

    def redact(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "<redacted>")
        return text

    def stream(self, chunks: Iterable[str]) -> Iterable[str]:
        for secret in sorted(self._secrets, key=len, reverse=True):
            chunks = _replace_stream(chunks, secret)
        return chunks

    def value(self, value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {self.redact(key): self.value(item) for key, item in value.items()}
        return value
