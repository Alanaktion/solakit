"""Explicit host contracts shared by storage and execution mixins."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from solakit.core.config import Config, Profile
from solakit.core.schemas import MemoryNote, Origin, Run, Session
from solakit.core.secrets import Redactor
from solakit.core.store_base import StorageCorruption

if TYPE_CHECKING:
    from solakit.core.compaction import Compactor
    from solakit.core.loop import Pending
    from solakit.core.memory import MemoryClient
    from solakit.core.model import ModelClient
    from solakit.core.store import Store
    from solakit.tools import Registry


class StoreServices(Protocol):
    root: Path
    lock: threading.RLock
    redactor: Redactor
    _issues: dict[tuple[str, int | None], dict]
    _fts_dirty_sessions: set[str]
    _line_counts: dict[str, tuple[tuple, int]]

    def _append(self, path: Path, record: dict) -> None: ...
    def _corruption(
        self, path: Path, kind: str, offset: int | None = None
    ) -> StorageCorruption: ...
    def _database(self) -> AbstractContextManager[sqlite3.Connection]: ...
    @staticmethod
    def _decode_record(fragment: bytes) -> dict: ...
    def _index(self, kind: str, record_id: str, payload: dict) -> None: ...
    def _issue(self, path: Path, kind: str, offset: int | None = None) -> None: ...
    def _path(self, kind: str, record_id: str, suffix: str) -> Path: ...
    def _read(self, path: Path) -> list[dict]: ...
    def _read_lines(self, path: Path) -> list[tuple[int, dict]]: ...
    def _refresh_offsets(self, db: sqlite3.Connection, session_id: str) -> None: ...
    def archives(self, session_id: str) -> list[str]: ...
    def all_messages(self, session_id: str) -> list[dict]: ...
    def check_compaction(self, session_id: str) -> None: ...
    def check_transcript(self, session_id: str) -> None: ...
    def context_started_at(self, session_id: str) -> str | None: ...
    def delivery_events(self, run_id: str) -> list[dict]: ...
    def save_run(self, run: Run) -> None: ...
    def session(self, session_id: str) -> Session: ...
    def transcript(self, session_id: str) -> list[dict]: ...


class ExecutionServices(Protocol):
    config: Config
    store: Store
    model: ModelClient
    registry: Registry
    memory: MemoryClient | None
    compactor: Compactor
    pending: dict[str, Pending]
    performing: dict[asyncio.Task, Pending]
    semaphore: asyncio.Semaphore
    submit_lock: asyncio.Lock
    closing: bool

    async def submit_nested(
        self,
        session_id: str,
        text: str,
        *,
        profile: str,
        origin: Origin,
        on_started=None,
    ) -> Run: ...
    async def stage_memory_note(self, text: str) -> MemoryNote: ...
    async def _compact_run(
        self, run: Run, session: Session, trigger: str, step: int
    ) -> None: ...
    def _aged(
        self, cfg: Profile, messages: list[dict], run: Run, step: int
    ) -> list[dict]: ...
    def _size_over_budget(
        self, cfg: Profile, size: int, output_tokens: int, measured: dict
    ) -> bool: ...
