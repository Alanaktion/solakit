"""Lossy replaceable previews, independent of durable per-session event cursors."""

from dataclasses import dataclass, field
from typing import TypedDict


class Preview(TypedDict):
    session_id: str
    run_id: str
    index: int
    version: int
    text: str
    truncated: bool


@dataclass
class Previews:
    version: int = 0
    latest: dict[str, Preview] = field(default_factory=dict)

    def update(self, session_id: str, run_id: str, index: int, text: str) -> None:
        self.version += 1
        if run_id not in self.latest and len(self.latest) >= 8:
            del self.latest[next(iter(self.latest))]
        self.latest[run_id] = {
            "session_id": session_id,
            "run_id": run_id,
            "index": index,
            "version": self.version,
            "text": text[-8192:].encode()[-8192:].decode(errors="ignore"),
            "truncated": len(text.encode()) > 8192,
        }
